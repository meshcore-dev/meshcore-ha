"""MeshCore data update coordinator."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from cachetools import TTLCache
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from meshcore.events import Event, EventType

from .config import Settings, get_conf
from .const import (
    AUTO_DISABLE_HOURS,
    CLI_CONSOLE_MAX_LINES,
    CONF_CLIENT_DISABLE_PATH_RESET,
    CONF_CLIENT_UPDATE_INTERVAL,
    CONF_CONSUME_INCOMING_MESSAGES,
    CONF_DEVICE_DISABLED,
    CONF_NAME,
    CONF_PUBKEY,
    CONF_REPEATER_DISABLE_PATH_RESET,
    CONF_REPEATER_NEIGHBORS_ENABLED,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_REPEATER_UPDATE_INTERVAL,
    DEFAULT_CLIENT_UPDATE_INTERVAL,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    DOMAIN,
    MAX_RANDOM_DELAY,
    MODE_FULL,
    MODE_OFF,
    NEIGHBOR_PUBKEY_PREFIX_LENGTH,
    RX_LOG_CACHE_MAX_SIZE,
    RX_LOG_CACHE_TTL_SECONDS,
    SEEN_WINDOW_SECS,
    get_contact_discovery_mode,
)
from .radio import RadioSession
from .traffic import (
    NODE_CLIENT,
    NODE_REPEATER,
    OP_NEIGHBOURS,
    OP_STATUS,
    OP_TELEMETRY,
    POLICY_GOVERNED,
    Lane,
    MeshBudget,
    TrafficPolicy,
    auto_disable_applies,
    backoff_delay,
    classify_lane,
    denial_counts_as_failure,
    iso_timestamp,
    resolve_policy,
    should_login,
    should_reset_path,
)

_LOGGER = logging.getLogger(__name__)

# Seconds of message silence before the safety-net poll fires.
# Normal message delivery is event-driven via MESSAGES_WAITING; this is a fallback.
MSG_SAFETY_NET_INTERVAL: int = 60

# Debounce for the governed node-schedule store.
TRAFFIC_SAVE_DELAY: int = 30

# Key the lane credits are stored under, alongside the per-node schedules.
TRAFFIC_BUDGET_KEY: str = "budget"

# A deferred node is announced at most this often, per node and per lane.
DEFER_LOG_INTERVAL: int = 600

# Stale contact and neighbour sweeps run at most once a day.
DAILY_CLEANUP_INTERVAL: int = 86400


def _log_get_msg_error(action: str, payload: Any) -> None:
    """Log a ``get_msg()`` ERROR result at the appropriate level.

    ``no_event_received`` is a benign startup race -- a flush or poll fired
    before the link produced its first event, and the next cycle recovers --
    so it stays at DEBUG. ``action`` is the gerund used in the message.
    """
    if isinstance(payload, dict) and payload.get("reason") == "no_event_received":
        _LOGGER.debug(
            "Skipped %s messages, radio link still coming up: %s", action, payload
        )
    else:
        _LOGGER.error("Error %s messages: %s", action, payload)


# Companion-protocol error codes that mean the node can never satisfy the
# request, however many times it is repeated -- as opposed to a transient
# fault that a later attempt may clear.
_TELEMETRY_UNSUPPORTED_CODES = frozenset(
    {"ERR_CODE_ILLEGAL_ARG", "ERR_CODE_UNSUPPORTED_CMD"}
)


def _log_self_telemetry_error(payload: Any, already_reported: bool) -> None:
    """Log a failed self-telemetry result at the appropriate level.

    ``get_self_telemetry()`` sends the four-byte "self" form of
    ``CMD_SEND_TELEMETRY_REQ``; an openHop virtual companion up to 1.1.1 wants
    the 36-byte contact form and rejects it. That is a fixed property of the
    peer, so it is named once at WARNING and then demoted to DEBUG; every
    other failure keeps ERROR. The caller clears its flag on success.
    """
    code = payload.get("code_string") if isinstance(payload, dict) else None
    if code in _TELEMETRY_UNSUPPORTED_CODES:
        if already_reported:
            _LOGGER.debug("Self telemetry still unsupported by this node: %s", payload)
        else:
            _LOGGER.warning(
                "This node rejected the self-telemetry request (%s): it does not "
                "implement the 'self' form of CMD_SEND_TELEMETRY_REQ. Turn off "
                "self telemetry for this entry, or update the node's firmware or "
                "companion software. Further occurrences are logged at DEBUG.",
                code,
            )
    else:
        _LOGGER.error("Failed to get self telemetry: %s", payload)


class MeshCoreDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the MeshCore node and trigger event-generating commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta,
        api: RadioSession,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
        )
        self.api = api
        self.config_entry = config_entry
        self.data: dict[str, Any] = {}
        self._contacts = {}  # keyed by 12-char public_key prefix
        self._discovered_contacts = {}  # keyed by public_key
        self._manual_mode_initialized = False

        self._store = Store[dict[str, dict]](hass, 1, f"meshcore.{config_entry.entry_id}.discovered_contacts")
        self._neighbor_store = Store[dict[str, dict]](
            hass, 1, f"meshcore.{config_entry.entry_id}.neighbor_data"
        )
        self._neighbor_data_loaded = False
        # Identity lives in entry data, not options
        self.name = config_entry.data.get(CONF_NAME)
        self.pubkey = config_entry.data.get(CONF_PUBKEY)

        # Every user setting this coordinator reads, options first. Refreshed
        # in place by update_telemetry_settings when the entry changes.
        self.settings = Settings.from_entry(config_entry)

        # Rolling transcript for the CLI console sensor, bounded so the sensor's
        # attribute payload stays small. The sensor registers itself here (when
        # CONF_CLI_CONSOLE_ENABLED) so record_cli_console() can push state.
        self.cli_console_history: deque[dict[str, Any]] = deque(
            maxlen=CLI_CONSOLE_MAX_LINES
        )
        self.cli_console_sensor: Any = None

        self._firmware_version = None
        # Observed repeater firmware, keyed by pubkey prefix. A version is a
        # radio observation, not a setting: it lives here and in the device
        # registry, and never in the config entry.
        self._repeater_firmware: dict[str, str] = {}
        self._hardware_model = None
        self._max_channels = 4  # updated from DEVICE_INFO
        self._channel_info = {}  # keyed by channel_idx

        # Central device info every entity references
        self.device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": f"MeshCore {self.name or 'Node'} ({self.pubkey[:6] if self.pubkey else ''})",
            "manufacturer": "MeshCore",
            "model": "Mesh Radio",
            "sw_version": "Unknown",
        }

        # Tracked nodes: schedules, in-flight tasks and failure counters, all
        # keyed by pubkey_prefix and kept separate for status and telemetry.
        self._tracked_repeaters = self.settings.repeater_records
        self._tracked_clients = self.settings.client_records
        self._repeater_login_times = {}
        self._next_repeater_update_times = {}
        self._active_repeater_tasks = {}
        self._repeater_consecutive_failures = {}
        self._next_telemetry_update_times = {}
        self._active_telemetry_tasks = {}
        self._telemetry_consecutive_failures = {}
        self._last_successful_request = {}
        self._auto_disabled_devices = set()

        # Mesh traffic policy: the lane budget every mesh request crosses, the
        # nodes currently waiting on a lane, and the governed-only store that
        # lets schedules and credits survive a restart.
        self._traffic_policy: TrafficPolicy = resolve_policy(config_entry)
        self._rate_limiter = MeshBudget(self._traffic_policy)
        self._deferred_nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_defer_log: dict[tuple[str, str], float] = {}
        self._path_reset_pending: set[str] = set()
        self._traffic_store: Store[dict[str, Any]] | None = (
            Store(hass, 1, f"{DOMAIN}.traffic_{config_entry.entry_id}")
            if self._traffic_policy == POLICY_GOVERNED
            else None
        )

        self.tracked_contacts = set()
        self.tracked_diagnostic_binary_contacts = set()
        self.channels_added = False
        self.telemetry_manager = None
        self._device_info_initialized = False
        self._coordinator_start_time = time.time()

        self._last_self_telemetry_update = 0
        # Set once a self-telemetry failure has been reported, so a node that
        # cannot answer is named once rather than on every cycle; cleared on
        # the next success.
        self._self_telemetry_error_reported = False
        self._self_telemetry_enabled = self.settings.self_telemetry_enabled
        self._self_telemetry_interval = self.settings.self_telemetry_interval

        # Local get_stats_core/radio/packets -- no mesh traffic
        self._last_self_diagnostics_update = 0
        self._self_diagnostics_enabled = self.settings.self_diagnostics_enabled
        self._self_diagnostics_interval = self.settings.self_diagnostics_interval

        # Daily auto-cleanup of stale discovered contacts and neighbours
        self._auto_cleanup_stale_contacts = self.settings.auto_cleanup_stale_contacts
        self._stale_contact_days = self.settings.stale_contact_days
        self._last_stale_cleanup: float = 0.0
        self._auto_cleanup_stale_neighbors = self.settings.auto_cleanup_stale_neighbors
        self._stale_neighbor_days = self.settings.stale_neighbor_days
        self._last_stale_neighbor_cleanup = 0.0

        # Serializes get_msg() between the MESSAGES_WAITING flush and the poll
        self._message_lock = asyncio.Lock()
        self._last_msg_activity: float = 0.0
        self._initial_drain_done: bool = False

        # {repeater prefix: {neighbour pubkey: {pubkey, snr, secs_ago,
        # last_updated, resolved_name}}}, plus the "repeater:neighbour" keys
        # whose sensor entities already exist.
        self._repeater_neighbors: dict[str, dict[str, dict]] = {}
        self._created_neighbor_sensors: set = set()

        # CHANNEL_INFO listener handle; registered once, kept across reconnects
        self._channel_info_unsub: Callable[[], None] | None = None
        api.add_connect_hook(self._on_radio_connected)

        # RX_LOG correlation hash -> list of RX_LOG payloads, TTL-evicted. The
        # send path reserves its key here so the incoming handler does not
        # pop() a reception the outgoing delivery still needs.
        self._pending_rx_logs = TTLCache(
            maxsize=RX_LOG_CACHE_MAX_SIZE,
            ttl=RX_LOG_CACHE_TTL_SECONDS
        )
        self._outgoing_correlation_keys: TTLCache = TTLCache(maxsize=64, ttl=60)

        if not hasattr(self, "last_update_success_time"):
            self.last_update_success_time = self._current_time()

        self._reliability_stats = {}
        # Pubkey prefixes whose sensors need a refresh
        self._dirty_contacts = set()

    def record_cli_console(
        self, command: str, response: Any, is_error: bool = False
    ) -> None:
        """Append a command/response pair to the CLI console transcript.

        Pushes fresh state to the console sensor immediately when one is
        registered (CONF_CLI_CONSOLE_ENABLED). No-ops gracefully when the
        console is disabled, so the execute_command record_to_console path can
        call this unconditionally.
        """
        self.cli_console_history.append({
            "timestamp": int(time.time()),
            "command": command,
            "response": response,
            "is_error": bool(is_error),
        })
        self._refresh_cli_console()

    def clear_cli_console(self) -> None:
        """Empty the CLI console transcript and refresh the sensor."""
        self.cli_console_history.clear()
        self._refresh_cli_console()

    def _refresh_cli_console(self) -> None:
        """Push the transcript to the console sensor, if one is registered."""
        sensor = self.cli_console_sensor
        if sensor is not None:
            try:
                sensor.async_write_ha_state()
            except Exception as ex:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to update CLI console sensor: %s", ex)

    def mark_contact_dirty(self, pubkey_prefix: str):
        """Flag a contact's sensors for refresh; takes a full key or a prefix."""
        if pubkey_prefix:
            self._dirty_contacts.add(pubkey_prefix[:12])

    def is_contact_dirty(self, pubkey_prefix: str) -> bool:
        """Whether a contact's sensors still need a refresh."""
        return bool(pubkey_prefix) and pubkey_prefix[:12] in self._dirty_contacts

    def clear_contact_dirty(self, pubkey_prefix: str):
        """Clear the refresh flag once a contact's sensors are up to date."""
        if pubkey_prefix:
            self._dirty_contacts.discard(pubkey_prefix[:12])

    def get_all_contacts(self) -> list:
        """Merge added and discovered contacts, keeping the latest lastmod.

        Each entry gains ``pubkey_prefix`` and ``added_to_node``.
        """
        contacts_dict: dict[str, dict] = {}
        added_pubkeys = {
            c.get("public_key") for c in self._contacts.values() if c.get("public_key")
        }

        for contact in list(self._discovered_contacts.values()) + list(self._contacts.values()):
            public_key = contact.get("public_key")
            if not public_key:
                continue

            contact_copy = dict(contact)
            contact_copy["pubkey_prefix"] = public_key[:12]
            contact_copy["added_to_node"] = public_key in added_pubkeys

            existing = contacts_dict.get(public_key)
            if existing is None or contact_copy.get("lastmod", 0) > existing.get("lastmod", 0):
                contacts_dict[public_key] = contact_copy

        return list(contacts_dict.values())

    def _remove_contact_entity(self, entity_registry, pubkey_prefix: str) -> str | None:
        """Remove one contact's diagnostic binary_sensor; return its entity id.

        Contact unique_ids have been scoped by entry_id since PR #236, and
        __init__.py:_migrate_unique_ids_scope_contact_diagnostics guarantees
        every existing entity uses that format. In data-only/off modes there is
        no entity to find and this is a harmless no-op that also clears any
        entity orphaned by an earlier mode switch.
        """
        entity_id = entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{self.config_entry.entry_id}_contact_{pubkey_prefix}"
        )
        if not entity_id:
            return None
        entity_registry.async_remove(entity_id)
        return entity_id

    def _publish_contacts(self) -> None:
        """Broadcast the merged contact list after it has changed."""
        updated_data = dict(self.data) if self.data else {}
        updated_data["contacts"] = self.get_all_contacts()
        self.async_set_updated_data(updated_data)

    def _remove_discovered_contact_entities(self, public_key: str) -> bool:
        """Remove one discovered contact's entities; True if it had a sensor.

        Unless the node has a tracking subscription (whose telemetry/GPS
        entities recreate dynamically), its telemetry and GPS-tracker entities
        go too. The allowlist matches unique_id SHAPE (``<entry_id>_<prefix>_*``
        ending in ``_telemetry`` / ``_gps_tracker``) so a bare substring match
        cannot hit a repeater-neighbor sensor or a subscription-backed entity.

        Mirrors the data-only demote teardown in
        ``services.async_execute_command_service``; keep the two in sync.
        """
        # In-function import: services -> binary_sensor -> ... would cycle.
        from .services import _node_has_tracked_subscription

        prefix = public_key[:12]
        entity_registry = er.async_get(self.hass)

        tracked = getattr(self, "tracked_diagnostic_binary_contacts", None)
        if tracked is not None:
            tracked.discard(public_key)

        removed = self._remove_contact_entity(entity_registry, prefix) is not None

        if not _node_has_tracked_subscription(self, prefix):
            uid_prefix = f"{self.config_entry.entry_id}_{prefix}_"
            to_remove = [
                e.entity_id
                for e in er.async_entries_for_config_entry(
                    entity_registry, self.config_entry.entry_id
                )
                if (e.unique_id or "").startswith(uid_prefix)
                and (
                    e.unique_id.endswith("_telemetry")
                    or e.unique_id.endswith("_gps_tracker")
                )
            ]
            for stale_entity_id in to_remove:
                entity_registry.async_remove(stale_entity_id)

            # Without these the managers keep updating deregistered entities
            # and a same-session re-add will not recreate the sensors.
            tm = getattr(self, "telemetry_manager", None)
            if tm is not None:
                for key in [k for k in tm.discovered_sensors if k.startswith(prefix)]:
                    del tm.discovered_sensors[key]
            dtm = getattr(self, "device_tracker_manager", None)
            if dtm is not None:
                for key in [
                    k for k in dtm.discovered_trackers if k.startswith(prefix)
                ]:
                    del dtm.discovered_trackers[key]

        return removed

    async def async_reconcile_discovered_for_mode(self) -> None:
        """Enforce the configured contact discovery mode on EXISTING contacts.

        ``contact_discovery_mode`` is otherwise a creation-time gate, so this
        pass runs on every setup (a mode change reloads the entry):

        - ``full``      -- create a per-contact entity for any discovered
                           contact still missing one (idempotent safety net).
        - ``data_only`` -- remove those entities, keep the discovered data.
        - ``off``       -- remove them AND clear + persist the discovered set
                           so the next store-load does not repopulate it.

        Added/curated contacts are never touched: membership is tested against
        the added set unioned with the SDK's contact list, so a
        transient-empty ``_contacts`` cannot misclassify one as discovered.
        """
        # A trustworthy contact picture needs a live link; reconcile on the
        # next connected setup instead.
        if not getattr(self.api, "connected", False):
            _LOGGER.debug(
                "Contact-mode reconcile skipped: device not connected"
            )
            return

        mode = get_contact_discovery_mode(self.config_entry)

        sdk_contacts = self.api.contacts
        added_pubkeys = {
            c.get("public_key")
            for c in list(self._contacts.values()) + list(sdk_contacts.values())
            if isinstance(c, dict) and c.get("public_key")
        }

        if mode == MODE_FULL:
            # create_contact_sensor dedups via tracked_diagnostic_binary_contacts
            add_entities = getattr(self, "binary_sensor_async_add_entities", None)
            if add_entities is None:
                return
            from .binary_sensor import create_contact_sensor

            new_entities = []
            for contact in list(self._discovered_contacts.values()):
                if not isinstance(contact, dict):
                    continue
                pubkey = contact.get("public_key")
                if pubkey and pubkey in added_pubkeys:
                    continue  # added contacts use the normal create path
                try:
                    sensor = create_contact_sensor(self, contact)
                except Exception as ex:  # noqa: BLE001
                    _LOGGER.error(
                        "Contact-mode reconcile (full): error creating sensor: %s",
                        ex,
                    )
                    continue
                if sensor:
                    new_entities.append(sensor)
            if new_entities:
                add_entities(new_entities)
                _LOGGER.info(
                    "Contact-mode reconcile (full): created %d missing "
                    "discovered contact entities",
                    len(new_entities),
                )
            return

        # data_only / off: discovered contacts lose their per-contact entities
        removed = 0
        for public_key in list(self._discovered_contacts.keys()):
            if public_key in added_pubkeys:
                continue  # never touch added contacts
            if self._remove_discovered_contact_entities(public_key):
                removed += 1

        if mode == MODE_OFF:
            self._discovered_contacts.clear()
            try:
                await self._store.async_save(self._discovered_contacts)
            except Exception as ex:  # noqa: BLE001
                _LOGGER.error(
                    "Contact-mode reconcile (off): error saving cleared set: %s",
                    ex,
                )

        if removed or mode == MODE_OFF:
            self._publish_contacts()
            _LOGGER.info(
                "Contact-mode reconcile (%s): removed %d discovered contact "
                "entities",
                mode,
                removed,
            )

    async def async_evict_discovered_contacts(self, max_contacts: int) -> bool:
        """Evict oldest discovered contacts using FIFO ordering when over the limit.

        Returns True if any contacts were evicted.
        """
        if len(self._discovered_contacts) <= max_contacts:
            return False

        evict_count = len(self._discovered_contacts) - max_contacts
        keys_to_evict = list(self._discovered_contacts.keys())[:evict_count]

        entity_registry = er.async_get(self.hass)

        for public_key in keys_to_evict:
            del self._discovered_contacts[public_key]
            self.tracked_diagnostic_binary_contacts.discard(public_key)
            entity_id = self._remove_contact_entity(entity_registry, public_key[:12])
            if entity_id:
                _LOGGER.info(f"Evicting binary sensor entity: {entity_id}")

        _LOGGER.info(f"Evicted {evict_count} oldest discovered contacts (limit: {max_contacts})")

        try:
            await self._store.async_save(self._discovered_contacts)
        except Exception as ex:
            _LOGGER.error(f"Error saving discovered contacts after eviction: {ex}")

        self._publish_contacts()
        return True

    async def _cleanup_stale_discovered_contacts(self, days_threshold: int) -> int:
        """Remove discovered contacts whose lastmod exceeds the age threshold.

        Ages on lastmod (the companion's own clock) rather than last_advert,
        which can carry an advertising node's wrong clock. ``added_to_node``
        contacts are always preserved. Removals are batched so a large sweep
        cannot flood the event bus and block the main thread.

        Never early-returns: the orphan sweep below lives in the entity
        registry, not the dict, so it must run even when nothing is stale.
        """
        now = time.time()
        threshold_seconds = days_threshold * 86400
        entity_registry = er.async_get(self.hass)
        skipped_node_contacts = 0
        batch_size = 10

        stale_keys: list[str] = []
        for public_key, contact in self._discovered_contacts.items():
            if contact.get("added_to_node", False):
                skipped_node_contacts += 1
                continue
            lastmod = contact.get("lastmod", 0)
            if not lastmod or (now - lastmod) > threshold_seconds:
                stale_keys.append(public_key)

        # Yields the event loop between batches so WebSocket clients can drain
        removed_count = 0
        for i, public_key in enumerate(stale_keys):
            contact = self._discovered_contacts.get(public_key)
            if contact is None:
                continue

            pubkey_prefix = public_key[:12]
            contact_name = contact.get("adv_name", pubkey_prefix)
            lastmod = contact.get("lastmod", 0)

            del self._discovered_contacts[public_key]
            self.tracked_diagnostic_binary_contacts.discard(public_key)
            self._remove_contact_entity(entity_registry, pubkey_prefix)

            removed_count += 1
            _LOGGER.debug(
                "Removed stale discovered contact: %s (%s) — last updated %.0f days ago",
                contact_name, pubkey_prefix, (now - lastmod) / 86400 if lastmod else 0,
            )

            if (i + 1) % batch_size == 0:
                await asyncio.sleep(0)

        if removed_count > 0:
            try:
                await self._store.async_save(self._discovered_contacts)
            except Exception as ex:
                _LOGGER.error("Error saving discovered contacts: %s", ex)
            self._publish_contacts()

        # Sweep contact entities left orphaned by cleanup calls made between
        # PR #236's migration and its lookup-format fix: the dict deletion ran
        # but the entity stayed in the registry.
        #
        # The 12-hex suffix check is load-bearing: the contact-selector entity
        # has unique_id "<entry_id>_contact_select" which would otherwise match
        # the entry_prefix. Do not loosen this check.
        entry_prefix = f"{self.config_entry.entry_id}_contact_"
        live_pubkey_prefixes = {
            c.get("public_key", "")[:12]
            for c in self.get_all_contacts()
            if c.get("public_key")
        }
        orphan_count = 0
        for entity in list(entity_registry.entities.values()):
            if entity.config_entry_id != self.config_entry.entry_id:
                continue
            if entity.platform != DOMAIN or entity.domain != "binary_sensor":
                continue
            if not entity.unique_id.startswith(entry_prefix):
                continue
            suffix = entity.unique_id[len(entry_prefix):]
            if len(suffix) != 12 or any(c not in "0123456789abcdef" for c in suffix.lower()):
                continue
            if suffix in live_pubkey_prefixes:
                continue
            entity_registry.async_remove(entity.entity_id)
            orphan_count += 1
            if orphan_count % batch_size == 0:
                await asyncio.sleep(0)

        _LOGGER.info(
            "Stale contact cleanup: removed %d stale dict contacts (%d node "
            "contacts skipped) and swept %d orphaned registry entities older "
            "than %d days",
            removed_count, skipped_node_contacts, orphan_count, days_threshold,
        )
        return removed_count

    def get_contact_by_prefix(self, prefix: str) -> dict[str, Any]:
        """Return the added or discovered contact matching a prefix, else {}."""
        if not prefix:
            return {}
        for contact in self.get_all_contacts():
            if contact.get("public_key", "").startswith(prefix):
                return contact
        return {}

    def _increment_success(self, pubkey_prefix: str) -> None:
        """Count a successful request and stamp the node's last success."""
        stats_key = f"{pubkey_prefix}_request_successes"
        self._reliability_stats[stats_key] = self._reliability_stats.get(stats_key, 0) + 1
        self._last_successful_request[pubkey_prefix] = time.time()

    def _increment_failure(self, pubkey_prefix: str) -> None:
        """Count a failed request against a node's reliability stats."""
        stats_key = f"{pubkey_prefix}_request_failures"
        self._reliability_stats[stats_key] = self._reliability_stats.get(stats_key, 0) + 1


    def get_device_update_interval(self, pubkey_prefix: str) -> int:
        """Return a tracked node's update interval, repeater configs first.

        Prefixes are compared both ways round because configs and events do not
        always carry the same prefix length.
        """
        for configs, key, default in (
            (self._tracked_repeaters, CONF_REPEATER_UPDATE_INTERVAL,
             DEFAULT_REPEATER_UPDATE_INTERVAL),
            (self._tracked_clients, CONF_CLIENT_UPDATE_INTERVAL,
             DEFAULT_CLIENT_UPDATE_INTERVAL),
        ):
            for node_config in configs:
                config_prefix = node_config.get("pubkey_prefix", "")
                if config_prefix and (
                    pubkey_prefix.startswith(config_prefix)
                    or config_prefix.startswith(pubkey_prefix)
                ):
                    return node_config.get(key, default)
        return DEFAULT_CLIENT_UPDATE_INTERVAL
    
    @property
    def traffic_policy(self) -> TrafficPolicy:
        """Return the traffic policy this entry runs under."""
        return self._traffic_policy

    def check_interactive_budget(self, lane: Lane) -> float:
        """Charge a user-driven mesh send; seconds to wait when it is refused.

        Frozen under legacy, where service calls have never touched the budget.
        """
        if self._traffic_policy != POLICY_GOVERNED:
            return 0.0
        if self._rate_limiter.try_consume(lane):
            return 0.0
        return self._rate_limiter.next_eligible(lane)

    def require_mesh_budget(self, lane: Lane) -> None:
        """Charge an interactive mesh send, refusing the call when credit is short."""
        wait = self.check_interactive_budget(lane)
        if not wait:
            return
        seconds = max(1, int(wait))
        raise HomeAssistantError(
            f"Mesh traffic {lane} lane is empty; try again in {seconds} seconds",
            translation_domain=DOMAIN,
            translation_key="traffic_deferred",
            translation_placeholders={"lane": lane, "seconds": str(seconds)},
        )

    def deferred_nodes(self) -> list[dict[str, Any]]:
        """Return the nodes whose next poll is still waiting on a lane."""
        now = self._current_time()
        return [
            {"name": entry["name"], "lane": entry["lane"], "until": iso_timestamp(entry["until"])}
            for entry in self._deferred_nodes.values()
            if entry["until"] > now
        ]

    def traffic_attributes(self) -> dict[str, Any] | None:
        """Return the lane rates and deferrals for the rate-limiter sensor."""
        attributes = self._rate_limiter.attributes()
        if attributes is None:
            return None
        attributes["deferred_nodes"] = self.deferred_nodes()
        return attributes

    def _path_reset_disabled(self, node_config: dict) -> bool:
        """Whether this node's path-reset toggle is switched off."""
        return bool(
            node_config.get(
                CONF_REPEATER_DISABLE_PATH_RESET,
                node_config.get(CONF_CLIENT_DISABLE_PATH_RESET, False),
            )
        )

    def _traffic_snapshot(self) -> dict[str, Any]:
        """Per-node schedule state and lane credits worth carrying across a restart."""
        prefixes = (
            set(self._next_repeater_update_times)
            | set(self._next_telemetry_update_times)
            | set(self._auto_disabled_devices)
        )
        snapshot: dict[str, Any] = {
            prefix: {
                "next_due": {
                    "status": self._next_repeater_update_times.get(prefix, 0),
                    "telemetry": self._next_telemetry_update_times.get(prefix, 0),
                },
                "failures": {
                    "status": self._repeater_consecutive_failures.get(prefix, 0),
                    "telemetry": self._telemetry_consecutive_failures.get(prefix, 0),
                },
                "auto_disabled": prefix in self._auto_disabled_devices,
            }
            for prefix in prefixes
        }
        snapshot[TRAFFIC_BUDGET_KEY] = self._rate_limiter.snapshot()
        return snapshot

    def _save_traffic_state(self) -> None:
        """Queue a debounced save of the node schedules; legacy keeps none."""
        if self._traffic_store is not None:
            self._traffic_store.async_delay_save(self._traffic_snapshot, TRAFFIC_SAVE_DELAY)

    async def async_load_traffic_state(self) -> None:
        """Restore the node schedules saved before the last restart."""
        if self._traffic_store is None:
            return
        try:
            stored = await self._traffic_store.async_load()
        except Exception as ex:
            self.logger.warning("Could not load stored node schedules: %s", ex)
            return
        nodes = dict(stored or {})
        self._rate_limiter.restore(nodes.pop(TRAFFIC_BUDGET_KEY, None))
        for prefix, state in nodes.items():
            next_due = state.get("next_due", {})
            failures = state.get("failures", {})
            self._next_repeater_update_times[prefix] = next_due.get("status", 0)
            self._next_telemetry_update_times[prefix] = next_due.get("telemetry", 0)
            self._repeater_consecutive_failures[prefix] = failures.get("status", 0)
            self._telemetry_consecutive_failures[prefix] = failures.get("telemetry", 0)
            if state.get("auto_disabled"):
                self._auto_disabled_devices.add(prefix)

    @property
    def max_channels(self) -> int:
        """Get the maximum number of channels supported by the device."""
        return self._max_channels

    def _on_radio_connected(self) -> None:
        """Re-arm the per-connection work the session's recovery invalidated."""
        self._device_info_initialized = False
        self._manual_mode_initialized = False
        self._initial_drain_done = False

    async def async_shutdown(self) -> None:
        """Stop scheduled refreshes, node tasks and the entry's own listeners."""
        await super().async_shutdown()

        if self._channel_info_unsub is not None:
            self._channel_info_unsub()
            self._channel_info_unsub = None

        tasks = [
            task
            for task in (
                *self._active_repeater_tasks.values(),
                *self._active_telemetry_tasks.values(),
            )
            if not task.done()
        ]
        self._active_repeater_tasks.clear()
        self._active_telemetry_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _setup_channel_info_listener(self) -> None:
        """Capture CHANNEL_INFO payloads; registered once for the entry's life."""
        if self._channel_info_unsub is not None:
            return

        def handle_channel_info(event: Event):
            try:
                channel_idx = event.payload.get("channel_idx")
                if channel_idx is not None:
                    self._channel_info[channel_idx] = event.payload
                    self.logger.debug(f"Saved channel info for channel {channel_idx}: {event.payload}")
            except Exception as ex:
                self.logger.error(f"Error handling CHANNEL_INFO event: {ex}")

        self._channel_info_unsub = self.api.subscribe(
            EventType.CHANNEL_INFO, handle_channel_info
        )
        self.logger.debug("Registered CHANNEL_INFO event listener")

    async def fetch_all_channel_info(self) -> None:
        """Fetch channel info for all channels on startup."""
        self.logger.info(f"Fetching channel info for {self._max_channels} channels...")
        for channel_idx in range(self._max_channels):
            try:
                channel_info_result = await self.api.exchange(
                    "get_channel", channel_idx
                )
                if channel_info_result and channel_info_result.type == EventType.CHANNEL_INFO:
                    self._channel_info[channel_idx] = channel_info_result.payload
                    self.logger.debug(f"Fetched channel info for channel {channel_idx}: {channel_info_result.payload}")
                else:
                    self.logger.warning(f"Failed to get channel info for channel {channel_idx}")
            except Exception as ex:
                self.logger.error(f"Error fetching channel info for channel {channel_idx}: {ex}")
        
        self.logger.info(f"Completed channel info fetch - got info for {len(self._channel_info)} channels")
    
    async def get_channel_info(self, channel_idx: int) -> dict:
        """Get channel info for a specific channel, fetching if not present."""
        if channel_idx not in self._channel_info:
            self.logger.debug(f"Channel {channel_idx} info not cached, attempting to fetch")
            try:
                if self.api.connected:
                    channel_info_result = await self.api.exchange(
                        "get_channel", channel_idx
                    )
                    if channel_info_result and channel_info_result.payload:
                        self._channel_info[channel_idx] = channel_info_result.payload
                        self.logger.debug(f"Successfully fetched channel {channel_idx} info")
                    else:
                        self.logger.warning(f"Failed to get channel info for channel {channel_idx}")
                else:
                    self.logger.warning("No MeshCore instance available for channel info fetch")
            except Exception as ex:
                self.logger.error(f"Error fetching channel info for channel {channel_idx}: {ex}")
        
        return self._channel_info.get(channel_idx, {})
    
    async def _reset_node_path(self, contact, node_config: dict) -> bool:
        """Reset routing path for a node and return success status.

        Callers decide whether a reset is due (see traffic.should_reset_path,
        which owns the failure threshold and the per-node toggle).
        """
        node_name = node_config.get("name", "unknown")

        try:
            result = await self.api.exchange("reset_path", contact)
            if result and result.type != EventType.ERROR:
                self.logger.info(f"Successfully reset path for {node_name}")
                prefix = node_config.get("pubkey_prefix")
                if prefix:
                    self._path_reset_pending.add(prefix)
                return True
            else:
                error_msg = result.payload if result and result.type == EventType.ERROR else "no response or unexpected result"
                self.logger.warning(f"Failed to reset path for {node_name}: {error_msg}")
                return False
        except Exception as ex:
            self.logger.warning(f"Exception resetting path for {node_name}: {ex}")
            return False
    
    def repeater_firmware(self, pubkey_prefix: str) -> str | None:
        """Return the last firmware version observed for a repeater."""
        return self._repeater_firmware.get(pubkey_prefix)

    def set_repeater_firmware(self, pubkey_prefix: str, version: str) -> None:
        """Record a repeater's reported firmware version."""
        self._repeater_firmware[pubkey_prefix] = version

    def seed_repeater_firmware(self) -> None:
        """Seed observed firmware from the device registry after a restart."""
        device_registry = dr.async_get(self.hass)
        identifier_prefix = f"{self.config_entry.entry_id}_repeater_"
        for device in dr.async_entries_for_config_entry(
            device_registry, self.config_entry.entry_id
        ):
            if not device.sw_version:
                continue
            for domain, identifier in device.identifiers:
                if domain == DOMAIN and identifier.startswith(identifier_prefix):
                    prefix = identifier[len(identifier_prefix):]
                    self._repeater_firmware.setdefault(prefix, device.sw_version)

    def apply_traffic_policy(self, policy: TrafficPolicy) -> None:
        """Switch the live traffic policy, keeping the node schedules in place."""
        if policy == self._traffic_policy:
            return
        self._traffic_policy = policy
        self._rate_limiter = MeshBudget(policy)
        self._traffic_store = (
            Store(self.hass, 1, f"{DOMAIN}.traffic_{self.config_entry.entry_id}")
            if policy == POLICY_GOVERNED
            else None
        )
        self._save_traffic_state()

    def update_telemetry_settings(self, config_entry: ConfigEntry) -> None:
        """Re-read the entry's settings into the live coordinator."""
        self.settings = Settings.from_entry(config_entry)
        self._self_telemetry_enabled = self.settings.self_telemetry_enabled
        self._self_telemetry_interval = self.settings.self_telemetry_interval
        self._self_diagnostics_enabled = self.settings.self_diagnostics_enabled
        self._self_diagnostics_interval = self.settings.self_diagnostics_interval
        self._tracked_repeaters = self.settings.repeater_records
        self._tracked_clients = self.settings.client_records
        self._auto_cleanup_stale_contacts = self.settings.auto_cleanup_stale_contacts
        self._stale_contact_days = self.settings.stale_contact_days
        self._auto_cleanup_stale_neighbors = self.settings.auto_cleanup_stale_neighbors
        self._stale_neighbor_days = self.settings.stale_neighbor_days
        _LOGGER.debug(f"Updated telemetry settings - Enabled: {self._self_telemetry_enabled}, Interval: {self._self_telemetry_interval}, Tracked clients: {len(self._tracked_clients)}")

    def seed_tracked_node(self, pubkey_prefix: str) -> None:
        """Arm a node the user just started tracking, as a fresh setup would.

        Its first poll is due immediately, it carries no inherited failures,
        and its inactivity clock starts now rather than at the coordinator's
        start time, which a long-running entry would already have passed.
        """
        if not pubkey_prefix:
            return
        self._next_repeater_update_times[pubkey_prefix] = 0
        self._next_telemetry_update_times[pubkey_prefix] = 0
        self._repeater_consecutive_failures.pop(pubkey_prefix, None)
        self._telemetry_consecutive_failures.pop(pubkey_prefix, None)
        self._auto_disabled_devices.discard(pubkey_prefix)
        self._last_successful_request[pubkey_prefix] = time.time()
        self._save_traffic_state()

    def forget_tracked_node(self, pubkey_prefix: str, node_type: str) -> None:
        """Drop every trace of a node the user stopped tracking.

        Cancels its in-flight polls, clears the schedules, counters and
        reliability stats it owned, and removes its entities and its device.
        The node stays a mesh contact: its discovered record and its contact
        entity are left alone.
        """
        if not pubkey_prefix:
            return

        for tasks in (self._active_repeater_tasks, self._active_telemetry_tasks):
            task = tasks.pop(pubkey_prefix, None)
            if task is not None and not task.done():
                task.cancel()

        for node_state in (
            self._next_repeater_update_times,
            self._next_telemetry_update_times,
            self._repeater_consecutive_failures,
            self._telemetry_consecutive_failures,
            self._last_successful_request,
            self._repeater_login_times,
            self._repeater_firmware,
        ):
            node_state.pop(pubkey_prefix, None)
        self._auto_disabled_devices.discard(pubkey_prefix)
        self._path_reset_pending.discard(pubkey_prefix)
        for stat in ("request_successes", "request_failures"):
            self._reliability_stats.pop(f"{pubkey_prefix}_{stat}", None)
        for key in [k for k in self._deferred_nodes if k[0] == pubkey_prefix]:
            self._deferred_nodes.pop(key, None)
        for key in [k for k in self._last_defer_log if k[0] == pubkey_prefix]:
            self._last_defer_log.pop(key, None)

        if node_type == NODE_REPEATER:
            self.cleanup_neighbor_entities(pubkey_prefix)

        self._remove_node_device(pubkey_prefix, node_type)
        self._save_traffic_state()

    def _remove_node_device(self, pubkey_prefix: str, node_type: str) -> None:
        """Remove a tracked node's device and every entity that sat on it.

        Telemetry sensors and GPS trackers live on the same device, so the
        managers are told to forget the node too: without that they would keep
        pushing to deregistered entities and a re-add in the same session would
        recreate nothing.
        """
        device_id = f"{self.config_entry.entry_id}_{node_type}_{pubkey_prefix}"
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if device is not None:
            entity_registry = er.async_get(self.hass)
            removed = [
                entity.entity_id
                for entity in er.async_entries_for_device(
                    entity_registry, device.id, include_disabled_entities=True
                )
            ]
            for entity_id in removed:
                entity_registry.async_remove(entity_id)
            device_registry.async_remove_device(device.id)
            _LOGGER.info(
                "Removed untracked %s %s: device and %d entities",
                node_type, pubkey_prefix[:6], len(removed),
            )

        for manager, cache_name in (
            (getattr(self, "telemetry_manager", None), "discovered_sensors"),
            (getattr(self, "device_tracker_manager", None), "discovered_trackers"),
        ):
            cache = getattr(manager, cache_name, None)
            if cache is None:
                continue
            for key in [k for k in cache if k.startswith(pubkey_prefix)]:
                del cache[key]

    def _current_time(self) -> int:
        """Return current time as integer seconds since epoch."""
        return int(time.time())

    def resolve_neighbor_name(self, neighbor_pubkey: str) -> str:
        """Resolve a neighbor pubkey prefix to a contact name.

        Searches the merged contacts list (added + discovered) for a
        public_key that starts with the neighbor's prefix.
        Returns the hex prefix (uppercase) if no match is found.
        """
        for contact in (self.data or {}).get("contacts", []):
            pk = contact.get("public_key", "") or contact.get("pubkey_prefix", "")
            if pk and pk.lower().startswith(neighbor_pubkey.lower()):
                return contact.get("adv_name") or contact.get("name") or neighbor_pubkey[:6].upper()
        return neighbor_pubkey[:6].upper()

    async def _fetch_repeater_neighbors(self, contact, repeater_name: str, pubkey_prefix: str):
        """Page a repeater's neighbour table after a successful status request.

        Stores the result in ``_repeater_neighbors``, persists it, creates
        sensors for neighbours not seen before, and tracks sightings as
        timestamps in a rolling 48 h window.
        """
        lane = classify_lane(OP_NEIGHBOURS, contact)

        def charge_page(_page: int) -> bool:
            """Pay for one more page; legacy paid for the whole scan up front."""
            if self._traffic_policy != POLICY_GOVERNED:
                return True
            return self._rate_limiter.try_consume(lane)

        try:
            # Legacy pays one token for the whole scan; governed pays per page.
            if not self._rate_limiter.try_consume(lane):
                self.logger.debug(f"Rate limited: skipping neighbor fetch for {repeater_name}")
                return

            self.logger.debug(f"Fetching neighbors for repeater {repeater_name} ({pubkey_prefix})")
            result = await self.api.fetch_neighbours(
                contact,
                pubkey_prefix_length=NEIGHBOR_PUBKEY_PREFIX_LENGTH,
                page_cb=charge_page,
            )

            if not result or "neighbours" not in result:
                self.logger.debug(f"No neighbor data returned for {repeater_name}")
                return

            neighbours = result["neighbours"]
            now = time.time()
            updated_neighbors = {}

            existing = self._repeater_neighbors.get(pubkey_prefix, {})
            cutoff = now - SEEN_WINDOW_SECS

            for neighbour in neighbours:
                if not neighbour or not isinstance(neighbour, dict):
                    continue
                n_pubkey = neighbour.get("pubkey", "")
                if not n_pubkey:
                    continue
                n_snr = neighbour.get("snr", 0)
                n_secs_ago = neighbour.get("secs_ago", 0)

                # The firmware computes secs_ago from its own RTC, so a
                # secs_ago that shrank since the last poll means the neighbour
                # was heard again. The window check is load-bearing: after a
                # restart the stored secs_ago is inflated by the downtime, and
                # without it every stale neighbour reads as newly heard.
                existing_data = existing.get(n_pubkey, {})
                prev_secs_ago = existing_data.get("secs_ago")
                seen_timestamps = [
                    t for t in existing_data.get("seen_timestamps", []) if t > cutoff
                ]
                if n_secs_ago <= SEEN_WINDOW_SECS and (
                    prev_secs_ago is None or n_secs_ago < prev_secs_ago
                ):
                    seen_timestamps.append(now)

                updated_neighbors[n_pubkey] = {
                    "pubkey": n_pubkey,
                    "snr": n_snr,
                    "secs_ago": n_secs_ago,
                    "last_updated": now,
                    "resolved_name": self.resolve_neighbor_name(n_pubkey),
                    "seen_timestamps": seen_timestamps,
                }

            # Neighbours missing from this response may just be off the page:
            # keep them, but leave last_updated alone (staleness comes from the
            # most recent poll that did include them) and prune their window.
            for n_pubkey, n_data in existing.items():
                if n_pubkey not in updated_neighbors:
                    prev_ts = n_data.get("seen_timestamps", [])
                    n_data["seen_timestamps"] = [t for t in prev_ts if t > cutoff]
                    updated_neighbors[n_pubkey] = n_data

            self._repeater_neighbors[pubkey_prefix] = updated_neighbors
            await self._save_neighbor_data()

            new_neighbors = []
            for n_pubkey in updated_neighbors:
                sensor_key = f"{pubkey_prefix}:{n_pubkey}"
                if sensor_key not in self._created_neighbor_sensors:
                    new_neighbors.append(n_pubkey)
                    self._created_neighbor_sensors.add(sensor_key)

            if new_neighbors and hasattr(self, "sensor_add_entities") and self.sensor_add_entities:
                from .sensor import MeshCoreNeighborSeenSensor, MeshCoreNeighborSensor
                new_entities = []
                for n_pubkey in new_neighbors:
                    try:
                        snr_sensor = MeshCoreNeighborSensor(
                            coordinator=self,
                            repeater_pubkey=pubkey_prefix,
                            repeater_name=repeater_name,
                            neighbor_pubkey=n_pubkey,
                        )
                        seen_sensor = MeshCoreNeighborSeenSensor(
                            coordinator=self,
                            repeater_pubkey=pubkey_prefix,
                            repeater_name=repeater_name,
                            neighbor_pubkey=n_pubkey,
                        )
                        new_entities.extend([snr_sensor, seen_sensor])
                        self.logger.info(
                            "Creating neighbor sensors: %s -> %s (%s)",
                            repeater_name,
                            updated_neighbors[n_pubkey]["resolved_name"],
                            n_pubkey[:6],
                        )
                    except Exception as ex:
                        self.logger.error(f"Error creating neighbor sensors for {n_pubkey}: {ex}")
                if new_entities:
                    self.sensor_add_entities(new_entities)

            self.logger.debug(
                f"Updated {len(neighbours)} neighbors for {repeater_name} "
                f"({len(new_neighbors)} new sensors created)"
            )

        except Exception as ex:
            self.logger.warning(f"Exception fetching neighbors for {repeater_name}: {ex}")

    def _remove_neighbor_entities(self, entity_registry, unique_id_prefix: str) -> list[str]:
        """Remove every neighbour entity under a unique_id prefix; return their ids."""
        removed = [
            entity.entity_id
            for entity in list(entity_registry.entities.values())
            if entity.platform == DOMAIN
            and (entity.unique_id or "").startswith(unique_id_prefix)
        ]
        for entity_id in removed:
            entity_registry.async_remove(entity_id)
        return removed

    def _persistable_neighbors(self) -> dict:
        """Return neighbor data suitable for persistence (no transient fields)."""
        result = {}
        for rptr_prefix, neighbors in self._repeater_neighbors.items():
            result[rptr_prefix] = {}
            for n_pubkey, n_data in neighbors.items():
                result[rptr_prefix][n_pubkey] = {
                    k: v for k, v in n_data.items() if k != "resolved_name"
                }
        return result

    async def _save_neighbor_data(self) -> None:
        """Save current neighbor data to persistent storage."""
        try:
            await self._neighbor_store.async_save(self._persistable_neighbors())
        except Exception as ex:
            _LOGGER.error("Error saving neighbor data: %s", ex)

    async def _cleanup_stale_neighbors(self, days_threshold: int) -> int:
        """Remove neighbors whose last_heard exceeds the age threshold.

        ``last_heard`` is ``last_updated - secs_ago``: when the repeater
        actually heard the neighbour, not when HA polled for it.
        """
        now = time.time()
        threshold_seconds = days_threshold * 86400
        entity_registry = er.async_get(self.hass)

        # (repeater_prefix, neighbor_pubkey, resolved_name)
        stale_entries: list[tuple[str, str, str]] = []
        for rptr_prefix, neighbors in self._repeater_neighbors.items():
            for n_pubkey, n_data in neighbors.items():
                last_updated = n_data.get("last_updated", 0)
                secs_ago = n_data.get("secs_ago", 0)

                # Loaded from storage but never polled: nothing to age yet
                if last_updated == 0 and secs_ago == 0:
                    continue

                last_heard = last_updated - secs_ago
                if last_heard > 0 and (now - last_heard) > threshold_seconds:
                    resolved = n_data.get("resolved_name", n_pubkey[:6])
                    stale_entries.append((rptr_prefix, n_pubkey, resolved))

        if not stale_entries:
            _LOGGER.debug(
                "Stale neighbor cleanup: 0 neighbors older than %d days",
                days_threshold,
            )
            return 0

        # Yields the event loop between batches so WebSocket clients can drain
        batch_size = 10
        removed_count = 0

        for i, (rptr_prefix, n_pubkey, resolved_name) in enumerate(stale_entries):
            self._remove_neighbor_entities(
                entity_registry,
                f"{self.config_entry.entry_id}_repeater_{rptr_prefix}"
                f"_neighbor_{n_pubkey[:12]}",
            )
            self._created_neighbor_sensors.discard(f"{rptr_prefix}:{n_pubkey}")
            self._repeater_neighbors.get(rptr_prefix, {}).pop(n_pubkey, None)

            removed_count += 1
            _LOGGER.debug(
                "Removed stale neighbor: %s (%s) on repeater %s",
                resolved_name, n_pubkey[:6], rptr_prefix[:6],
            )

            if (i + 1) % batch_size == 0:
                await asyncio.sleep(0)

        if removed_count > 0:
            await self._save_neighbor_data()
            self.async_update_listeners()

        _LOGGER.info(
            "Stale neighbor cleanup: removed %d neighbors older than %d days",
            removed_count, days_threshold,
        )
        return removed_count

    async def async_load_neighbor_data(self) -> None:
        """Load persisted neighbor data from storage.

        Must run before sensor platform setup so sensor.py can recreate the
        entities. Leaves ``_created_neighbor_sensors`` alone: sensor.py fills
        it when it instantiates the sensors.
        """
        if self._neighbor_data_loaded:
            return
        try:
            stored = await self._neighbor_store.async_load()
            if stored:
                now = time.time()
                for _rptr_prefix, neighbors in stored.items():
                    for n_pubkey, n_data in neighbors.items():
                        last_updated = n_data.get("last_updated", now)
                        elapsed = now - last_updated
                        n_data["secs_ago"] = n_data.get("secs_ago", 0) + int(elapsed)
                        n_data["resolved_name"] = self.resolve_neighbor_name(n_pubkey)
                        # Migrate seen_count (int) to seen_timestamps (list)
                        if "seen_count" in n_data and "seen_timestamps" not in n_data:
                            n_data["seen_timestamps"] = []
                            del n_data["seen_count"]
                self._repeater_neighbors = stored
                self.logger.info(
                    "Loaded persisted neighbor data for %d repeaters (%d total neighbors)",
                    len(stored),
                    sum(len(n) for n in stored.values()),
                )
        except Exception as ex:
            self.logger.error("Error loading persisted neighbor data: %s", ex)
        self._neighbor_data_loaded = True

    def cleanup_neighbor_entities(self, pubkey_prefix: str) -> int:
        """Drop a repeater's neighbour sensors when its toggle is turned off.

        Removes the SNR and Seen entities, the in-memory tracking and the
        persisted data. Returns the number of entities removed.
        """
        entity_registry = er.async_get(self.hass)
        removed_ids = self._remove_neighbor_entities(
            entity_registry,
            f"{self.config_entry.entry_id}_repeater_{pubkey_prefix}_neighbor_",
        )
        for entity_id in removed_ids:
            _LOGGER.info("Removing neighbor entity: %s", entity_id)

        self._repeater_neighbors.pop(pubkey_prefix, None)
        self._created_neighbor_sensors = {
            k for k in self._created_neighbor_sensors
            if not k.startswith(f"{pubkey_prefix}:")
        }
        self.hass.async_create_task(self._save_neighbor_data())

        if removed_ids:
            _LOGGER.info(
                "Cleaned up %d neighbor entities for repeater %s",
                len(removed_ids), pubkey_prefix[:6]
            )

        return len(removed_ids)

    async def _update_repeater(self, repeater_config):
        """Request a repeater's status, logging in first when it keeps failing.

        Runs as its own background task so the coordinator tick never blocks on
        the mesh.
        """
        await asyncio.sleep(random.uniform(0, MAX_RANDOM_DELAY))

        pubkey_prefix = repeater_config.get("pubkey_prefix")
        repeater_name = repeater_config.get("name")
        if not pubkey_prefix or not repeater_name:
            self.logger.warning(f"Cannot update repeater with missing pubkey_prefix or name: {repeater_config}")
            return

        update_interval = repeater_config.get(
            CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL
        )
        try:
            contact = self.api.contact_by_prefix(pubkey_prefix)
            if not contact:
                # A contact we cannot find was never asked, so it never failed.
                self.logger.warning(f"Could not find repeater contact with pubkey_prefix: {pubkey_prefix}")
                return

            failure_count = self._repeater_consecutive_failures.get(pubkey_prefix, 0)
            has_path = contact.get("out_path_len", -1) > -1
            lane = classify_lane(
                OP_STATUS, contact, path_reset=pubkey_prefix in self._path_reset_pending
            )

            if should_login(
                self._traffic_policy,
                failure_count,
                self._repeater_login_times.get(pubkey_prefix, 0),
                self._current_time(),
            ):
                self.logger.info(f"Attempting login to repeater {repeater_name} after {failure_count} failures")

                if not self._rate_limiter.try_consume(lane):
                    self.logger.debug(f"Rate limited: skipping login to {repeater_name}")
                    if denial_counts_as_failure(self._traffic_policy):
                        # Legacy has never counted this one against the
                        # consecutive counter, only against the reliability stat.
                        self._increment_failure(pubkey_prefix)
                        self._apply_backoff(pubkey_prefix, failure_count + 1, update_interval)
                    else:
                        self._defer_node(pubkey_prefix, repeater_name, lane, OP_STATUS)
                    return

                try:
                    login_result = await self.api.login(
                        contact,
                        repeater_config.get(CONF_REPEATER_PASSWORD, "")
                    )

                    if login_result:
                        self.logger.info(f"Successfully logged in to repeater {repeater_name}")
                        self._increment_success(pubkey_prefix)
                        self._repeater_login_times[pubkey_prefix] = self._current_time()
                        self._repeater_consecutive_failures[pubkey_prefix] = 0
                    else:
                        self.logger.error(f"Login to repeater {repeater_name} failed or timed out")
                        self._increment_failure(pubkey_prefix)
                        self._repeater_login_times[pubkey_prefix] = self._current_time()

                except Exception as ex:
                    self.logger.error(f"Exception during login to repeater {repeater_name}: {ex}")
                    self._increment_failure(pubkey_prefix)
                    # Cooldown applies even when the attempt itself blew up
                    self._repeater_login_times[pubkey_prefix] = self._current_time()
                await asyncio.sleep(1)

            self.logger.debug(f"Sending status request to repeater: {repeater_name} ({pubkey_prefix})")

            if not self._rate_limiter.try_consume(lane):
                self.logger.debug(f"Rate limited: skipping status request to {repeater_name}")
                if denial_counts_as_failure(self._traffic_policy):
                    await self._record_node_failure(
                        pubkey_prefix, failure_count + 1, update_interval, "repeater"
                    )
                else:
                    self._defer_node(pubkey_prefix, repeater_name, lane, OP_STATUS)
                return

            self._begin_attempt(pubkey_prefix, OP_STATUS)
            status_event = await self.api.req_status(contact)
            result = status_event.payload if status_event else None
            _LOGGER.debug(f"Status response received: {result}")

            if not result:
                self.logger.warning(f"Error requesting status from repeater {repeater_name}: no response (timeout or send failure)")
                await self._record_node_failure(
                    pubkey_prefix, failure_count + 1, update_interval, "repeater",
                    node_config=repeater_config, contact=contact, has_path=has_path,
                )
            elif result.get('uptime', 0) == 0:
                self.logger.warning(f"Malformed status response from repeater {repeater_name}: {result}")
                await self._record_node_failure(
                    pubkey_prefix, failure_count + 1, update_interval, "repeater"
                )
            else:
                self.logger.debug(f"Successfully updated repeater {repeater_name}")
                self._repeater_consecutive_failures[pubkey_prefix] = 0
                self._increment_success(pubkey_prefix)

                if repeater_config.get(CONF_REPEATER_NEIGHBORS_ENABLED, False):
                    await self._fetch_repeater_neighbors(contact, repeater_name, pubkey_prefix)

                self.async_update_listeners()
                self._next_repeater_update_times[pubkey_prefix] = (
                    self._current_time() + update_interval
                )

        except Exception as ex:
            self.logger.warning(f"Exception updating repeater {repeater_name}: {ex}")
            await self._record_node_failure(
                pubkey_prefix,
                self._repeater_consecutive_failures.get(pubkey_prefix, 0) + 1,
                update_interval,
                "repeater",
            )
        finally:
            self._active_repeater_tasks.pop(pubkey_prefix, None)
            self._save_traffic_state()
            await asyncio.sleep(1)  # Small delay to avoid tight loops

    async def _record_node_failure(
        self,
        pubkey_prefix: str,
        failures: int,
        update_interval: int,
        update_type: str,
        *,
        node_config: dict | None = None,
        contact: Any = None,
        has_path: bool = False,
    ) -> None:
        """Book one failed poll: counter, reliability stat, path reset, backoff.

        ``node_config`` is passed only where a failure can justify rediscovering
        the node's path; the policy decides whether this failure does.
        """
        counters = (
            self._telemetry_consecutive_failures
            if update_type == "telemetry"
            else self._repeater_consecutive_failures
        )
        counters[pubkey_prefix] = failures
        self._increment_failure(pubkey_prefix)
        if node_config is not None and should_reset_path(
            self._traffic_policy, failures, has_path, self._path_reset_disabled(node_config)
        ):
            await self._reset_node_path(contact, node_config)
        self._apply_backoff(pubkey_prefix, failures, update_interval, update_type)

    def _set_next_due(self, pubkey_prefix: str, update_type: str, when: int) -> None:
        """Record when a node's next status or telemetry attempt falls due."""
        if update_type == "telemetry":
            self._next_telemetry_update_times[pubkey_prefix] = when
        else:
            self._next_repeater_update_times[pubkey_prefix] = when

    def _defer_node(
        self, pubkey_prefix: str, node_name: str, lane: Lane, update_type: str
    ) -> None:
        """Push a node's next attempt to the moment its lane can pay for it.

        The decision is announced at INFO once per node per lane per
        ``DEFER_LOG_INTERVAL``, so a mesh short of credit says so without
        filling the log on every tick.
        """
        now = self._current_time()
        until = now + int(self._rate_limiter.next_eligible(lane))
        self._set_next_due(pubkey_prefix, update_type, until)
        self._deferred_nodes[(pubkey_prefix, update_type)] = {
            "name": node_name,
            "lane": lane,
            "until": until,
        }
        if now - self._last_defer_log.get((pubkey_prefix, lane), 0) >= DEFER_LOG_INTERVAL:
            self._last_defer_log[(pubkey_prefix, lane)] = now
            _LOGGER.info(
                "Deferring %s for %s (%s lane empty, next at %s)",
                update_type, node_name, lane, iso_timestamp(until),
            )

    def _begin_attempt(self, pubkey_prefix: str, update_type: str) -> None:
        """Clear the deferral and the post-reset flag once the lane has paid."""
        self._deferred_nodes.pop((pubkey_prefix, update_type), None)
        self._path_reset_pending.discard(pubkey_prefix)

    def _apply_backoff(
        self,
        pubkey_prefix: str,
        failure_count: int,
        update_interval: int,
        update_type: str = "repeater",
    ) -> None:
        """Delay a failing node's next attempt by the policy's backoff."""
        delay = backoff_delay(self._traffic_policy, failure_count, update_interval)
        self._set_next_due(pubkey_prefix, update_type, self._current_time() + delay)
        self.logger.debug(f"Applied backoff for {update_type} {pubkey_prefix}: "
                         f"failure_count={failure_count}, "
                         f"policy={self._traffic_policy}, "
                         f"delay={delay}s, "
                         f"interval_cap={update_interval}s")

    async def _update_node_telemetry(self, contact, node_config: dict):
        """Request telemetry from a tracked node.

        Repeater login, when one is needed, has already been handled by the
        status loop.
        """
        pubkey_prefix = node_config.get("pubkey_prefix")
        node_name = node_config.get("name")
        if not pubkey_prefix or not node_name:
            self.logger.warning(f"Node config missing required fields - pubkey_prefix: {pubkey_prefix}, name: {node_name}")
            return

        # Repeaters and clients name their interval differently
        update_interval = (node_config.get(CONF_REPEATER_UPDATE_INTERVAL) or
                          node_config.get(CONF_CLIENT_UPDATE_INTERVAL, DEFAULT_CLIENT_UPDATE_INTERVAL))

        failure_count = self._telemetry_consecutive_failures.get(pubkey_prefix, 0)
        has_path = bool(contact) and contact.get("out_path_len", -1) > -1
        lane = classify_lane(
            OP_TELEMETRY, contact, path_reset=pubkey_prefix in self._path_reset_pending
        )

        await asyncio.sleep(random.uniform(0, MAX_RANDOM_DELAY))

        try:
            self.logger.debug(f"Sending telemetry request to node: {node_name} ({pubkey_prefix})")

            if not self._rate_limiter.try_consume(lane):
                self.logger.debug(f"Rate limited: skipping telemetry request to {node_name}")
                if denial_counts_as_failure(self._traffic_policy):
                    await self._record_node_failure(
                        pubkey_prefix, failure_count + 1, update_interval, "telemetry"
                    )
                else:
                    self._defer_node(pubkey_prefix, node_name, lane, OP_TELEMETRY)
                return

            self._begin_attempt(pubkey_prefix, OP_TELEMETRY)
            telemetry_event = await self.api.req_telemetry(contact)
            telemetry_result = telemetry_event.payload.get("lpp") if telemetry_event else None

            if telemetry_result:
                self.logger.debug(f"Telemetry response received from {node_name}: {telemetry_result}")
                self._telemetry_consecutive_failures[pubkey_prefix] = 0
                self._increment_success(pubkey_prefix)
                self._next_telemetry_update_times[pubkey_prefix] = (
                    self._current_time() + update_interval
                )
            else:
                self.logger.debug(f"No telemetry response received from {node_name}")
                await self._record_node_failure(
                    pubkey_prefix, failure_count + 1, update_interval, "telemetry",
                    node_config=node_config, contact=contact, has_path=has_path,
                )

        except Exception as ex:
            self.logger.warning(f"Exception requesting telemetry from node {node_name}: {ex}")
            await self._record_node_failure(
                pubkey_prefix, failure_count + 1, update_interval, "telemetry",
                node_config=node_config, contact=contact, has_path=has_path,
            )
        finally:
            self._active_telemetry_tasks.pop(pubkey_prefix, None)
            self._save_traffic_state()
            await asyncio.sleep(1)  # Small delay to avoid tight loops

    @property
    def consume_incoming_messages(self) -> bool:
        """Whether HA may drain the Companion chat queue."""
        return get_conf(self.config_entry, CONF_CONSUME_INCOMING_MESSAGES, True)

    async def async_flush_messages(self) -> dict[str, Any]:
        """Immediately flush pending messages from the device queue.

        Called by the MESSAGES_WAITING event handler for instant message
        delivery.  Uses _message_lock to prevent overlap with the
        coordinator poll's own get_msg() loop.
        """
        async with self._message_lock:
            try:
                while self.consume_incoming_messages:
                    result = await self.api.exchange("get_msg")
                    if result.type == EventType.NO_MORE_MSGS:
                        break
                    elif result.type == EventType.ERROR:
                        _log_get_msg_error("flushing", result.payload)
                        break
                    else:
                        _LOGGER.debug(
                            "Auto-fetched message: %s", result.type
                        )
                        self._last_msg_activity = time.time()
            except Exception as ex:
                _LOGGER.error("Error in async_flush_messages: %s", ex)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fire the scheduled commands whose events drive the entities.

        Entities update from event subscriptions, not from what this returns;
        the tick only maintains the shared contact list and decides which
        commands are due.
        """
        result_data = dict(self.data) if self.data else {
            "name": "MeshCore Node",
            "contacts": []
        }
        current_time = self._current_time()
        _LOGGER.debug("Starting data update...")
        _LOGGER.debug(f"Timings:"
                      f"Now: {current_time}, "
                      f"Next: {self._next_repeater_update_times}, "
                      f"Failures: {self._repeater_consecutive_failures}")

        # The session owns recovery; a tick during an outage simply fails.
        if not self.api.connected:
            raise UpdateFailed("Device not connected")

        await self.api.exchange("get_bat")

        if not self._manual_mode_initialized:
            try:
                self.logger.info("Setting manual contact mode...")
                result = await self.api.exchange("set_manual_add_contacts", True)
                if result and result.type != EventType.ERROR:
                    self.logger.info("Manual contact mode enabled")
                    self._manual_mode_initialized = True
                    stored_contacts = await self._store.async_load()
                    if stored_contacts:
                        self._discovered_contacts = stored_contacts
                        self.logger.info(f"Loaded {len(stored_contacts)} discovered contacts from storage")
                else:
                    self.logger.error(f"Failed to set manual contact mode: {result}")
            except Exception as ex:
                self.logger.error(f"Error setting manual contact mode: {ex}")

        if not self._device_info_initialized:
            try:
                self.logger.info("Fetching device info...")
                device_query_result = await self.api.exchange("send_device_query")
                if device_query_result.type is EventType.DEVICE_INFO:
                    self._firmware_version = device_query_result.payload.get("ver")
                    self._hardware_model = device_query_result.payload.get("model")
                    self._max_channels = device_query_result.payload.get("max_channels", 4)

                    if self._firmware_version:
                        self.device_info["sw_version"] = self._firmware_version
                    if self._hardware_model:
                        self.device_info["model"] = self._hardware_model

                    self.logger.info(f"Device info updated - Firmware: {self._firmware_version}, Model: {self._hardware_model}, Max Channels: {self._max_channels}")
                    self._device_info_initialized = True
                    self._setup_channel_info_listener()
                    await self.fetch_all_channel_info()
                    self.async_update_listeners()
            except Exception as ex:
                self.logger.error(f"Error fetching device info: {ex}")
        
        # The SDK owns the dirty flag that decides whether this resyncs
        try:
            contacts_changed = await self.api.ensure_contacts(follow=True)
            if contacts_changed:
                self.logger.info("Contacts synced from node")
                self._contacts = {}
                for contact in self.api.contacts.values():
                    public_key = contact.get("public_key")
                    if public_key:
                        prefix = public_key[:12]
                        self._contacts[prefix] = contact
        except Exception as ex:
            self.logger.error(f"Error syncing contacts: {ex}")

        result_data["contacts"] = self.get_all_contacts()

        if self._auto_cleanup_stale_contacts and self._stale_contact_days > 0:
            now_ts = time.time()
            if now_ts - self._last_stale_cleanup >= DAILY_CLEANUP_INTERVAL:
                self._last_stale_cleanup = now_ts
                removed = await self._cleanup_stale_discovered_contacts(
                    self._stale_contact_days
                )
                if removed > 0:
                    _LOGGER.info(
                        "Auto-cleanup removed %d stale discovered contacts "
                        "(older than %d days)",
                        removed, self._stale_contact_days,
                    )
                    result_data["contacts"] = self.get_all_contacts()

        if self._self_telemetry_enabled:
            if current_time - self._last_self_telemetry_update >= self._self_telemetry_interval:
                self.logger.debug(f"Getting self telemetry (interval: {self._self_telemetry_interval}s)")
                # The interval gates *attempts*: advancing it only on success
                # left the gate open against a node that can never answer, and
                # a 300 s setting collapsed to the coordinator tick.
                self._last_self_telemetry_update = current_time
                try:
                    telemetry_result = await self.api.exchange("get_self_telemetry")
                    if telemetry_result.type == EventType.TELEMETRY_RESPONSE:
                        self.logger.debug(f"Self telemetry received: {telemetry_result.payload}")
                        self._self_telemetry_error_reported = False
                    else:
                        _log_self_telemetry_error(
                            telemetry_result.payload, self._self_telemetry_error_reported
                        )
                        self._self_telemetry_error_reported = True
                except Exception as ex:
                    self.logger.error(f"Exception getting self telemetry: {ex}")
            else:
                self.logger.debug(f"Skipping self telemetry (next in {self._self_telemetry_interval - (current_time - self._last_self_telemetry_update):.1f}s)")

        # Local GET_STATS frames: no mesh traffic, no airtime. The SDK
        # dispatches STATS_CORE/RADIO/PACKETS to the diagnostic sensors, so
        # there is nothing to do with the return values here.
        if self._self_diagnostics_enabled:
            if current_time - self._last_self_diagnostics_update >= self._self_diagnostics_interval:
                self.logger.debug(f"Getting self diagnostics (interval: {self._self_diagnostics_interval}s)")
                try:
                    await self.api.exchange("get_stats_core")
                    await self.api.exchange("get_stats_radio")
                    await self.api.exchange("get_stats_packets")
                    self._last_self_diagnostics_update = current_time
                except Exception as ex:
                    self.logger.debug(f"Exception getting self diagnostics: {ex}")
            else:
                self.logger.debug(f"Skipping self diagnostics (next in {self._self_diagnostics_interval - (current_time - self._last_self_diagnostics_update):.1f}s)")

        # Delivery is event-driven (MESSAGES_WAITING -> async_flush_messages).
        # The first cycle drains whatever queued while disconnected; after that
        # this only polls once MSG_SAFETY_NET_INTERVAL passes with no activity.
        current_time_mono = time.time()
        should_poll = (
            not self._initial_drain_done
            or (current_time_mono - self._last_msg_activity) >= MSG_SAFETY_NET_INTERVAL
        )

        if self.consume_incoming_messages and should_poll:
            async with self._message_lock:
                try:
                    while self.consume_incoming_messages:
                        result = await self.api.exchange("get_msg")
                        if result.type == EventType.NO_MORE_MSGS:
                            _LOGGER.debug("No messages in device queue")
                            break
                        elif result.type == EventType.ERROR:
                            _log_get_msg_error("retrieving", result.payload)
                            break
                        else:
                            _LOGGER.debug("Drained queued message: %s", result.type)
                            self._last_msg_activity = current_time_mono
                except Exception as ex:
                    _LOGGER.error("Error draining message queue: %s", ex)

                if not self._initial_drain_done:
                    self._initial_drain_done = True
                    _LOGGER.info("Initial message drain complete")

            # Stamped even on an empty queue, so the next poll waits a full
            # MSG_SAFETY_NET_INTERVAL.
            self._last_msg_activity = current_time_mono

        # One pass per node loop, in the order they have always run:
        # (key, configs, kind, tasks, due times, enabled key, warn on a bad
        # config, skip auto-disabled nodes, may auto-disable, messages) where
        # messages is (task name, busy, failed, starting, contact missing).
        node_loops = (
            ("status", self._tracked_repeaters, NODE_REPEATER,
             self._active_repeater_tasks, self._next_repeater_update_times,
             None, True, True, auto_disable_applies(self._traffic_policy, NODE_REPEATER),
             ("update_repeater_{name}",
              "Update task for repeater %s still running, skipping",
              "Repeater update task for %s failed with exception: %s",
              "Starting repeater update task for %s",
              None)),
            ("repeater_telemetry", self._tracked_repeaters, NODE_REPEATER,
             self._active_telemetry_tasks, self._next_telemetry_update_times,
             CONF_REPEATER_TELEMETRY_ENABLED, False,
             auto_disable_applies(self._traffic_policy, NODE_REPEATER, telemetry=True), False,
             ("telemetry_{name}",
              "Telemetry task for %s still running, skipping",
              "Telemetry update task for %s failed with exception: %s",
              "Starting telemetry update task for %s",
              "Could not find contact for telemetry request: %s")),
            ("client_telemetry", self._tracked_clients, NODE_CLIENT,
             self._active_telemetry_tasks, self._next_telemetry_update_times,
             None, True, True,
             auto_disable_applies(self._traffic_policy, NODE_CLIENT, telemetry=True),
             ("client_telemetry_{name}",
              "Client telemetry task for %s still running, skipping",
              "Client telemetry update task for %s failed with exception: %s",
              "Starting telemetry update task for client %s",
              "Could not find contact for client telemetry request: %s")),
        )

        for (
            key, configs, kind, tasks, due, enabled_key, warn_missing,
            skip_auto_disabled, may_auto_disable, messages,
        ) in node_loops:
            task_name, busy_msg, failed_msg, start_msg, no_contact_msg = messages
            if key == "repeater_telemetry":
                _LOGGER.debug("Checking telemetry for tracked repeaters: %s", due)
            elif key == "client_telemetry":
                _LOGGER.debug("Checking telemetry for tracked clients")

            for node_config in configs:
                pubkey_prefix = node_config.get("pubkey_prefix")
                node_name = node_config.get("name")
                if not node_name or not pubkey_prefix:
                    if warn_missing:
                        _LOGGER.warning(
                            f"{kind.capitalize()} config missing name or "
                            f"pubkey_prefix: {node_config}"
                        )
                    continue
                if node_config.get(CONF_DEVICE_DISABLED, False):
                    continue
                if enabled_key is not None and not node_config.get(enabled_key, False):
                    continue
                if skip_auto_disabled and pubkey_prefix in self._auto_disabled_devices:
                    continue

                if may_auto_disable:
                    last_success = self._last_successful_request.get(
                        pubkey_prefix, self._coordinator_start_time
                    )
                    idle_hours = (current_time - last_success) / 3600
                    if idle_hours >= AUTO_DISABLE_HOURS:
                        _LOGGER.warning(
                            f"{kind.capitalize()} {node_name} has had no successful requests "
                            f"in {idle_hours:.1f} hours. Automatically disabling to reduce "
                            f"network traffic. This will reset on restart."
                        )
                        self._auto_disabled_devices.add(pubkey_prefix)
                        self._save_traffic_state()
                        continue

                task = tasks.get(pubkey_prefix)
                if task is not None:
                    if not task.done():
                        _LOGGER.debug(busy_msg, node_name)
                        continue
                    tasks.pop(pubkey_prefix)
                    if task.exception():
                        _LOGGER.error(failed_msg, node_name, task.exception())

                if current_time < due.get(pubkey_prefix, 0):
                    continue

                contact = None
                if key != "status":
                    contact = self.api.contact_by_prefix(pubkey_prefix)
                    if not contact:
                        _LOGGER.warning(no_contact_msg, pubkey_prefix)
                        continue

                _LOGGER.debug(start_msg, node_name)
                tasks[pubkey_prefix] = self.config_entry.async_create_background_task(
                    self.hass,
                    self._update_repeater(node_config)
                    if contact is None
                    else self._update_node_telemetry(contact, node_config),
                    task_name.format(name=node_name),
                    eager_start=False,
                )

        if self._auto_cleanup_stale_neighbors and self._stale_neighbor_days > 0:
            now_ts = time.time()
            if now_ts - self._last_stale_neighbor_cleanup >= DAILY_CLEANUP_INTERVAL:
                self._last_stale_neighbor_cleanup = now_ts
                removed = await self._cleanup_stale_neighbors(
                    self._stale_neighbor_days
                )
                if removed > 0:
                    _LOGGER.info(
                        "Auto-cleanup removed %d stale neighbors "
                        "(older than %d days)",
                        removed, self._stale_neighbor_days,
                    )

        return result_data