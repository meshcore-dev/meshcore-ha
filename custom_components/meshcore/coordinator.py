"""MeshCore data update coordinator."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from datetime import timedelta
from typing import Any, Dict

from cachetools import TTLCache

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers import entity_registry as er

from meshcore.events import Event, EventType

from .rate_limiter import TokenBucket
from .const import (
    CONF_NAME,
    CONF_PUBKEY,
    DOMAIN,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_UPDATE_INTERVAL,
    CONF_REPEATER_DISABLE_PATH_RESET,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    CONF_TRACKED_CLIENTS,
    CONF_CLIENT_UPDATE_INTERVAL,
    CONF_CLIENT_DISABLE_PATH_RESET,
    DEFAULT_CLIENT_UPDATE_INTERVAL,
    MAX_REPEATER_FAILURES_BEFORE_LOGIN,
    REPEATER_BACKOFF_BASE,
    MAX_FAILURES_BEFORE_PATH_RESET,
    MAX_RANDOM_DELAY,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_INTERVAL,
    DEFAULT_SELF_TELEMETRY_INTERVAL,
    CLI_CONSOLE_MAX_LINES,
    CONF_SELF_DIAGNOSTICS_ENABLED,
    CONF_SELF_DIAGNOSTICS_INTERVAL,
    DEFAULT_SELF_DIAGNOSTICS_INTERVAL,
    CONF_AUTO_CLEANUP_STALE_CONTACTS,
    CONF_STALE_CONTACT_DAYS,
    DEFAULT_STALE_CONTACT_DAYS,
    CONF_DEVICE_DISABLED,
    AUTO_DISABLE_HOURS,
    RATE_LIMITER_CAPACITY,
    RATE_LIMITER_REFILL_RATE_SECONDS,
    RX_LOG_CACHE_MAX_SIZE,
    RX_LOG_CACHE_TTL_SECONDS,
    NEIGHBOR_PUBKEY_PREFIX_LENGTH,
    NEIGHBOR_STALE_THRESHOLD,
    SEEN_WINDOW_SECS,
    CONF_REPEATER_NEIGHBORS_ENABLED,
    CONF_AUTO_CLEANUP_STALE_NEIGHBORS,
    CONF_STALE_NEIGHBOR_DAYS,
    DEFAULT_STALE_NEIGHBOR_DAYS,
    MODE_FULL,
    MODE_DATA_ONLY,
    MODE_OFF,
    get_contact_discovery_mode,
)
from .meshcore_api import MeshCoreAPI

_LOGGER = logging.getLogger(__name__)

# Seconds of message silence before the safety-net poll fires.
# Normal message delivery is event-driven via MESSAGES_WAITING; this is a fallback.
MSG_SAFETY_NET_INTERVAL: int = 60


def _log_get_msg_error(action: str, payload: Any) -> None:
    """Log a ``get_msg()`` ERROR result at the appropriate level.

    The ``no_event_received`` reason is a benign startup race: a flush or poll
    fired before the radio link produced its first event, and the next cycle
    recovers. It is logged at DEBUG to avoid ERROR-tier noise (e.g. on a
    config-entry re-add). Every other failure is logged at ERROR. ``action``
    is the gerund used in the message ("flushing" / "retrieving").
    """
    if isinstance(payload, dict) and payload.get("reason") == "no_event_received":
        _LOGGER.debug(
            "Skipped %s messages, radio link still coming up: %s", action, payload
        )
    else:
        _LOGGER.error("Error %s messages: %s", action, payload)


class MeshCoreDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the MeshCore node and trigger event-generating commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta,
        api: MeshCoreAPI,
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
        self.data: Dict[str, Any] = {}
        self._current_node_info = {}
        self._contacts = {}  # Dict keyed by 12-char public_key prefix
        self._discovered_contacts = {}  # Dict keyed by public_key
        self._manual_mode_initialized = False

        # Storage for discovered contacts
        self._store = Store[dict[str, dict]](hass, 1, f"meshcore.{config_entry.entry_id}.discovered_contacts")
        # Storage for neighbor data (persists SNR, seen_timestamps, etc. across restarts)
        self._neighbor_store = Store[dict[str, dict]](
            hass, 1, f"meshcore.{config_entry.entry_id}.neighbor_data"
        )
        self._neighbor_data_loaded = False
        # Get name and pubkey from config_entry.data (not options)
        self.name = config_entry.data.get(CONF_NAME)
        self.pubkey = config_entry.data.get(CONF_PUBKEY)

        # Rolling transcript for the CLI console sensor: each entry is a dict of
        # {timestamp, command, response, is_error}. Bounded so the sensor's
        # attribute payload stays small regardless of how much the console is
        # used. The sensor (when CONF_CLI_CONSOLE_ENABLED) registers itself here
        # so record_cli_console() can push fresh state immediately.
        self.cli_console_history: deque[dict[str, Any]] = deque(
            maxlen=CLI_CONSOLE_MAX_LINES
        )
        self.cli_console_sensor: Any = None
        
        # Set up device info that entities can reference
        self._firmware_version = None
        self._hardware_model = None
        self._max_channels = 4  # Default to 4 channels, updated from DEVICE_INFO
        self._channel_info = {}  # Dict keyed by channel_idx to store channel info
        
        # Create a central device_info dict that all entities can reference
        self.device_info = {
            "identifiers": {(DOMAIN, config_entry.entry_id)},
            "name": f"MeshCore {self.name or 'Node'} ({self.pubkey[:6] if self.pubkey else ''})",
            "manufacturer": "MeshCore",
            "model": "Mesh Radio",
            "sw_version": "Unknown",
        }

        # Single map to track all message timestamps (key -> timestamp)
        # Keys can be channel indices (int) or public key prefixes (str)
        self.message_timestamps = {}

        # Rate limiter for mesh requests
        self._rate_limiter = TokenBucket(
            capacity=RATE_LIMITER_CAPACITY,
            refill_rate_seconds=RATE_LIMITER_REFILL_RATE_SECONDS
        )

        # Repeater subscription tracking
        self._tracked_repeaters = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        self._repeater_stats = {}
        self._repeater_login_times = {}
        self._next_repeater_update_times = {}  # Track when each repeater should next be updated
        self._active_repeater_tasks = {}  # Track active update tasks by pubkey_prefix
        self._repeater_consecutive_failures = {}  # Track consecutive failed updates by pubkey_prefix
        self._last_successful_request = {}  # Track last successful request timestamp by pubkey_prefix
        self._auto_disabled_devices = set()  # Track devices auto-disabled due to inactivity (resets on restart)
        
        # Tracked clients tracking (no login needed, uses ACLs)
        self._tracked_clients = self.config_entry.data.get(CONF_TRACKED_CLIENTS, [])
        
        
        # Initialize tracking sets for entities
        self.tracked_contacts = set()
        self.tracked_diagnostic_binary_contacts = set()
        self.channels_added = False
        
        # Track last update times for different data types
        self._last_repeater_updates = {}  # Dictionary to track per-repeater updates
        self._last_contact_refresh = 0  # Track when contacts were last refreshed
        
        # Self telemetry tracking
        self._last_self_telemetry_update = 0
        self._self_telemetry_enabled = config_entry.data.get(CONF_SELF_TELEMETRY_ENABLED, False)
        self._self_telemetry_interval = config_entry.data.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL)

        # Self diagnostics tracking (local get_stats_core/radio/packets — no mesh traffic)
        self._last_self_diagnostics_update = 0
        self._self_diagnostics_enabled = config_entry.data.get(CONF_SELF_DIAGNOSTICS_ENABLED, False)
        self._self_diagnostics_interval = config_entry.data.get(CONF_SELF_DIAGNOSTICS_INTERVAL, DEFAULT_SELF_DIAGNOSTICS_INTERVAL)

        # Auto-cleanup of stale discovered contacts (daily)
        self._auto_cleanup_stale_contacts = config_entry.data.get(
            CONF_AUTO_CLEANUP_STALE_CONTACTS, False
        )
        self._stale_contact_days = config_entry.data.get(
            CONF_STALE_CONTACT_DAYS, DEFAULT_STALE_CONTACT_DAYS
        )
        self._last_stale_cleanup: float = 0.0

        # Telemetry tracking - separate from repeater/client specific logic
        self._next_telemetry_update_times = {}  # Track when each node should have telemetry updated
        self._active_telemetry_tasks = {}  # Track active telemetry tasks by pubkey_prefix
        self._telemetry_consecutive_failures = {}  # Track consecutive failed telemetry updates by pubkey_prefix
        
        # Initialization tracking flags
        self._device_info_initialized = False

        # Telemetry sensor manager - will be initialized when sensors are set up
        self.telemetry_manager = None

        # Track coordinator start time for auto-disable logic
        self._coordinator_start_time = time.time()

        # Lock to serialize get_msg() calls between MESSAGES_WAITING
        # auto-fetch and the coordinator's periodic poll
        self._message_lock = asyncio.Lock()

        # Conditional message polling: track last message activity so the
        # safety-net poll only fires after MSG_SAFETY_NET_INTERVAL of silence.
        self._last_msg_activity: float = 0.0
        self._initial_drain_done: bool = False

        # Repeater neighbor tracking
        # Key: repeater pubkey_prefix, Value: dict of neighbor data keyed by neighbor pubkey
        # Each neighbor entry: {pubkey, snr, secs_ago, last_updated, resolved_name}
        self._repeater_neighbors: Dict[str, Dict[str, dict]] = {}
        # Track which neighbor sensor entities have been created: set of "repeater_pubkey:neighbor_pubkey"
        self._created_neighbor_sensors: set = set()

        # Auto-cleanup of stale neighbors (daily)
        self._auto_cleanup_stale_neighbors = config_entry.data.get(
            CONF_AUTO_CLEANUP_STALE_NEIGHBORS, False
        )
        self._stale_neighbor_days = config_entry.data.get(
            CONF_STALE_NEIGHBOR_DAYS, DEFAULT_STALE_NEIGHBOR_DAYS
        )
        self._last_stale_neighbor_cleanup = 0.0

        # RX_LOG correlation cache: auto-evicts after TTL expires
        # Key: correlation hash, Value: list of RX_LOG data (multiple receptions possible)
        self._pending_rx_logs = TTLCache(
            maxsize=RX_LOG_CACHE_MAX_SIZE,
            ttl=RX_LOG_CACHE_TTL_SECONDS
        )

        # Track correlation keys reserved for outgoing message delivery.
        # When we send a channel message, the outgoing handler registers its key here
        # so the incoming handler knows not to pop() it from _pending_rx_logs.
        self._outgoing_correlation_keys: TTLCache = TTLCache(maxsize=64, ttl=60)

        if not hasattr(self, "last_update_success_time"):
            self.last_update_success_time = self._current_time()

        # Initialize reliability stats tracking
        self._reliability_stats = {}

        # Dirty contacts tracking for performance optimization
        # Set of pubkey prefixes that have been updated and need sensor refresh
        self._dirty_contacts = set()

    def record_cli_console(
        self, command: str, response: Any, is_error: bool = False
    ) -> None:
        """Append a command/response pair to the CLI console transcript.

        Pushes fresh state to the console sensor immediately when one is
        registered (CONF_CLI_CONSOLE_ENABLED). No-ops gracefully when the
        console is disabled, so the cli_command service can call this
        unconditionally.
        """
        self.cli_console_history.append({
            "timestamp": int(time.time()),
            "command": command,
            "response": response,
            "is_error": bool(is_error),
        })
        sensor = self.cli_console_sensor
        if sensor is not None:
            try:
                sensor.async_write_ha_state()
            except Exception as ex:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to update CLI console sensor: %s", ex)

    def clear_cli_console(self) -> None:
        """Empty the CLI console transcript and refresh the sensor."""
        self.cli_console_history.clear()
        sensor = self.cli_console_sensor
        if sensor is not None:
            try:
                sensor.async_write_ha_state()
            except Exception as ex:  # pragma: no cover - defensive
                _LOGGER.debug("Failed to update CLI console sensor: %s", ex)

    def mark_contact_dirty(self, pubkey_prefix: str):
        """Mark a contact as needing update (for performance optimization).

        Accepts either full public key or 12-char prefix, normalizes to 12-char prefix.
        """
        if pubkey_prefix:
            normalized = pubkey_prefix[:12]
            self._dirty_contacts.add(normalized)

    def is_contact_dirty(self, pubkey_prefix: str) -> bool:
        """Check if a contact needs update.

        Accepts either full public key or 12-char prefix, normalizes to 12-char prefix.
        """
        if not pubkey_prefix:
            return False
        normalized = pubkey_prefix[:12]
        return normalized in self._dirty_contacts

    def clear_contact_dirty(self, pubkey_prefix: str):
        """Clear dirty flag after updating contact sensor.

        Accepts either full public key or 12-char prefix, normalizes to 12-char prefix.
        """
        if pubkey_prefix:
            normalized = pubkey_prefix[:12]
            self._dirty_contacts.discard(normalized)

    def get_all_contacts(self) -> list:
        """Get deduplicated list of all contacts (added + discovered).

        For each public_key, uses the contact with the latest lastmod.
        Marks as added_to_node=True if contact exists in added list.
        """
        contacts_dict = {}

        # Build set of public keys that are in added contacts
        added_pubkeys = set(c.get("public_key") for c in self._contacts.values() if c.get("public_key"))

        # Process all contacts (discovered + added)
        all_contacts = list(self._discovered_contacts.values()) + list(self._contacts.values())

        for contact in all_contacts:
            public_key = contact.get("public_key")
            if not public_key:
                continue

            contact_copy = dict(contact)
            contact_copy["pubkey_prefix"] = public_key[:12]
            contact_copy["added_to_node"] = public_key in added_pubkeys

            # If we already have this contact, keep the one with latest lastmod
            if public_key in contacts_dict:
                existing = contacts_dict[public_key]
                existing_lastmod = existing.get("lastmod", 0)
                new_lastmod = contact_copy.get("lastmod", 0)

                if new_lastmod > existing_lastmod:
                    contacts_dict[public_key] = contact_copy
            else:
                contacts_dict[public_key] = contact_copy

        return list(contacts_dict.values())

    def _remove_discovered_contact_entities(self, public_key: str) -> bool:
        """Remove the per-contact entities for one discovered (un-added) contact.

        Removes the contact-diagnostic ``binary_sensor`` and, unless the node
        has a repeater/client tracking subscription (whose telemetry/GPS
        entities recreate dynamically), its telemetry and GPS-tracker entities.
        Allowlists by unique_id SHAPE (``<entry_id>_<prefix>_*`` ending in
        ``_telemetry`` / ``_gps_tracker``) so a bare substring match cannot hit
        a repeater-neighbor sensor (which embeds another node's pubkey) or a
        subscription-backed entity. Returns True if a contact binary_sensor was
        removed.

        This mirrors the data-only demote teardown in
        ``services.async_execute_command_service`` (keep the two in sync); the
        mode reconciler reuses it to bring the existing discovered population
        into the configured mode.
        """
        # Imported in-function to avoid a module-level import cycle
        # (services -> binary_sensor -> ... ; coordinator stays a leaf).
        from .services import _node_has_tracked_subscription

        prefix = public_key[:12]
        entity_registry = er.async_get(self.hass)

        tracked = getattr(self, "tracked_diagnostic_binary_contacts", None)
        if tracked is not None:
            tracked.discard(public_key)

        removed = False
        unique_id = f"{self.config_entry.entry_id}_contact_{prefix}"
        entity_id = entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, unique_id
        )
        if entity_id:
            entity_registry.async_remove(entity_id)
            removed = True

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

            # In-memory dedup maps: without these discards the managers keep
            # updating deregistered entities and a same-session re-add will not
            # recreate the sensors.
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

        ``contact_discovery_mode`` is otherwise a creation-time gate: switching
        modes governs new contacts but leaves the existing discovered
        population as-is. This pass reconciles what already exists so each mode
        is the source of truth on every setup (start / reload — and because a
        mode change reloads the entry, on every mode switch too):

        - ``full``      -- ensure every discovered contact has a per-contact
                           entity (idempotent safety net; the platform setup
                           create-pass and the NEW_CONTACT event path already
                           cover the common cases).
        - ``data_only`` -- remove the per-contact entities for discovered
                           contacts; keep the discovered data (summary sensor /
                           dropdown / get_discovered_contact).
        - ``off``       -- remove the per-contact entities AND clear + persist
                           the discovered set so it does not repopulate on the
                           next store-load. The advert handler is gated
                           separately (NEW_CONTACT off early-return).

        Added/curated contacts are never touched: membership is tested against
        the added set (the same source create_contact_sensor uses), unioned
        with the SDK's authoritative contact list as a safety net so a
        transient-empty ``_contacts`` cannot misclassify an added contact as
        discovered. Entirely HA-side -- the companion is in manual mode and
        never stored discovered contacts. Runs in the coordinator at
        post-reload setup with the correct (new) coordinator reference, so it
        does not reintroduce the pre-reload race.
        """
        # Reconciliation needs a trustworthy contact picture; skip when the
        # device is not connected and reconcile on the next connected setup.
        mesh_core = getattr(self.api, "mesh_core", None)
        if not getattr(self.api, "connected", False) or mesh_core is None:
            _LOGGER.debug(
                "Contact-mode reconcile skipped: device not connected"
            )
            return

        mode = get_contact_discovery_mode(self.config_entry)

        sdk_contacts = getattr(mesh_core, "contacts", None) or {}
        added_pubkeys = {
            c.get("public_key")
            for c in list(self._contacts.values()) + list(sdk_contacts.values())
            if isinstance(c, dict) and c.get("public_key")
        }

        if mode == MODE_FULL:
            # Create an entity for any discovered contact still lacking one.
            # create_contact_sensor dedups via tracked_diagnostic_binary_contacts
            # so already-created contacts are skipped (no double-create).
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

        # data_only / off: remove per-contact entities for discovered contacts.
        removed = 0
        for public_key in list(self._discovered_contacts.keys()):
            if public_key in added_pubkeys:
                continue  # never touch added contacts
            if self._remove_discovered_contact_entities(public_key):
                removed += 1

        if mode == MODE_OFF:
            # Disabled: do not keep/track discovered contacts. Clear and persist
            # the empty set so the next store-load does not repopulate it.
            self._discovered_contacts.clear()
            try:
                await self._store.async_save(self._discovered_contacts)
            except Exception as ex:  # noqa: BLE001
                _LOGGER.error(
                    "Contact-mode reconcile (off): error saving cleared set: %s",
                    ex,
                )

        if removed or mode == MODE_OFF:
            updated_data = dict(self.data) if self.data else {}
            updated_data["contacts"] = self.get_all_contacts()
            self.async_set_updated_data(updated_data)
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

        from homeassistant.helpers import entity_registry as er

        evict_count = len(self._discovered_contacts) - max_contacts
        keys_to_evict = list(self._discovered_contacts.keys())[:evict_count]

        entity_registry = er.async_get(self.hass)

        # Removal runs unconditionally. In data-only/off modes discovered
        # contacts have no per-contact entity, so async_get_entity_id below
        # returns None and the removal is a harmless no-op; the dict trim still
        # bounds the discovered set by max_contacts. Running it in every mode
        # also clears any entity orphaned by a prior mode switch.
        for public_key in keys_to_evict:
            pubkey_prefix = public_key[:12]
            del self._discovered_contacts[public_key]
            self.tracked_diagnostic_binary_contacts.discard(public_key)

            # Post-PR-#236 contact unique_ids are scoped by entry_id; the
            # migration at __init__.py:_migrate_unique_ids_scope_contact_diagnostics
            # guarantees every existing entity uses this format.
            unique_id = f"{self.config_entry.entry_id}_contact_{pubkey_prefix}"
            entity_id = entity_registry.async_get_entity_id(
                "binary_sensor", DOMAIN, unique_id
            )
            if entity_id:
                _LOGGER.info(f"Evicting binary sensor entity: {entity_id}")
                entity_registry.async_remove(entity_id)

        _LOGGER.info(f"Evicted {evict_count} oldest discovered contacts (limit: {max_contacts})")

        try:
            await self._store.async_save(self._discovered_contacts)
        except Exception as ex:
            _LOGGER.error(f"Error saving discovered contacts after eviction: {ex}")

        updated_data = dict(self.data) if self.data else {}
        updated_data["contacts"] = self.get_all_contacts()
        self.async_set_updated_data(updated_data)

        return True

    async def _cleanup_stale_discovered_contacts(self, days_threshold: int) -> int:
        """Remove discovered contacts whose lastmod exceeds the age threshold.

        Uses lastmod (companion device's local clock, synced by HA) instead of
        last_advert (which may contain timestamps from advertising nodes with
        incorrect clocks).

        Contacts with added_to_node=True are always preserved.

        Removals are batched to avoid flooding the event bus with state_changed
        events, which can overwhelm WebSocket clients and block the main thread.

        Returns the number of contacts removed.

        Note: this function ALWAYS runs the Phase 4 orphan sweep, even when
        the dict is empty or no stale contacts are found. Orphans live in the
        entity registry, not the dict — early-returning on empty-stale would
        skip the sweep on every typical call once the dict is stable.
        """
        now = time.time()
        threshold_seconds = days_threshold * 86400
        entity_registry = er.async_get(self.hass)
        skipped_node_contacts = 0
        batch_size = 10

        # Phase 1: Collect stale contacts (no side effects).
        stale_keys: list[str] = []
        for public_key, contact in self._discovered_contacts.items():
            if contact.get("added_to_node", False):
                skipped_node_contacts += 1
                continue
            lastmod = contact.get("lastmod", 0)
            if not lastmod or (now - lastmod) > threshold_seconds:
                stale_keys.append(public_key)

        # Phase 2: Remove in batches, yielding the event loop between each
        # batch so WebSocket clients can drain their message queues. No-op
        # when stale_keys is empty; Phase 4 still runs.
        removed_count = 0

        # Removal runs unconditionally. In data-only/off modes discovered
        # contacts have no per-contact entity, so the registry lookup returns
        # None and removal is a no-op; the dict trim still removes stale
        # contacts. Running it in every mode also clears any entity orphaned by
        # a prior mode switch.
        for i, public_key in enumerate(stale_keys):
            contact = self._discovered_contacts.get(public_key)
            if contact is None:
                continue

            pubkey_prefix = public_key[:12]
            contact_name = contact.get("adv_name", pubkey_prefix)
            lastmod = contact.get("lastmod", 0)

            del self._discovered_contacts[public_key]
            self.tracked_diagnostic_binary_contacts.discard(public_key)

            # Post-PR-#236 contact unique_ids are scoped by entry_id; the
            # migration at __init__.py:_migrate_unique_ids_scope_contact_diagnostics
            # guarantees every existing entity uses this format.
            unique_id = f"{self.config_entry.entry_id}_contact_{pubkey_prefix}"
            entity_id = entity_registry.async_get_entity_id(
                "binary_sensor", DOMAIN, unique_id
            )
            if entity_id:
                entity_registry.async_remove(entity_id)

            removed_count += 1
            _LOGGER.debug(
                "Removed stale discovered contact: %s (%s) — last updated %.0f days ago",
                contact_name, pubkey_prefix, (now - lastmod) / 86400 if lastmod else 0,
            )

            # Yield the event loop every batch_size removals
            if (i + 1) % batch_size == 0:
                await asyncio.sleep(0)

        # Phase 3: Save and refresh once after all removals
        if removed_count > 0:
            try:
                await self._store.async_save(self._discovered_contacts)
            except Exception as ex:
                _LOGGER.error("Error saving discovered contacts: %s", ex)

            updated_data = dict(self.data) if self.data else {}
            updated_data["contacts"] = self.get_all_contacts()
            self.async_set_updated_data(updated_data)

        # Phase 4: Sweep pre-existing orphaned contact entities.
        #
        # Catches entities that were "removed" by buggy cleanup calls between
        # PR #236's migration (2026-05-10) and the lookup-format fix in this
        # change set — the dict deletion ran but the entity-lookup format was
        # wrong, so the entity stayed in the registry. Walks entities tied to
        # this config entry whose unique_id matches
        # "<entry_id>_contact_<hex12>" and removes any whose pubkey is not in
        # the current contact set (added + discovered).
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
            # Yield the event loop every batch_size removals (consistent with Phase 2).
            if orphan_count % batch_size == 0:
                await asyncio.sleep(0)

        _LOGGER.info(
            "Stale contact cleanup: removed %d stale dict contacts (%d node "
            "contacts skipped) and swept %d orphaned registry entities older "
            "than %d days",
            removed_count, skipped_node_contacts, orphan_count, days_threshold,
        )
        return removed_count

    def get_contact_by_prefix(self, prefix: str) -> Dict[str, Any]:
        """Get a contact by its public key prefix.

        Searches all contacts (both added and discovered).
        Returns the contact dict if found, otherwise returns an empty dict.
        """
        if not prefix:
            return {}

        all_contacts = self.get_all_contacts()

        for contact in all_contacts:
            pubkey = contact.get("public_key", "")
            if pubkey.startswith(prefix):
                return contact

        return {}

    def _increment_success(self, pubkey_prefix: str) -> None:
        """Increment success counter for a node."""
        stats_key = f"{pubkey_prefix}_request_successes"
        self._reliability_stats[stats_key] = self._reliability_stats.get(stats_key, 0) + 1
        # Track last successful request time
        self._last_successful_request[pubkey_prefix] = time.time()
        
    def _increment_failure(self, pubkey_prefix: str) -> None:
        """Increment failure counter for a node."""
        stats_key = f"{pubkey_prefix}_request_failures"
        self._reliability_stats[stats_key] = self._reliability_stats.get(stats_key, 0) + 1
    
    def get_device_update_interval(self, pubkey_prefix: str) -> int:
        """Get the configured update interval for a device by its pubkey prefix."""
        # Check repeaters - use startswith to handle varying prefix lengths
        for repeater_config in self._tracked_repeaters:
            config_prefix = repeater_config.get("pubkey_prefix", "")
            if config_prefix and (pubkey_prefix.startswith(config_prefix) or config_prefix.startswith(pubkey_prefix)):
                return repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
        
        # Check clients - use startswith to handle varying prefix lengths
        for client_config in self._tracked_clients:
            config_prefix = client_config.get("pubkey_prefix", "")
            if config_prefix and (pubkey_prefix.startswith(config_prefix) or config_prefix.startswith(pubkey_prefix)):
                return client_config.get(CONF_CLIENT_UPDATE_INTERVAL, DEFAULT_CLIENT_UPDATE_INTERVAL)
        
        # Default fallback - use a reasonable timeout
        return DEFAULT_CLIENT_UPDATE_INTERVAL
    
    @property
    def max_channels(self) -> int:
        """Get the maximum number of channels supported by the device."""
        return self._max_channels
    
    def _setup_channel_info_listener(self) -> None:
        """Set up CHANNEL_INFO event listener to capture channel information."""
        def handle_channel_info(event: Event):
            try:
                channel_idx = event.payload.get("channel_idx")
                if channel_idx is not None:
                    self._channel_info[channel_idx] = event.payload
                    self.logger.debug(f"Saved channel info for channel {channel_idx}: {event.payload}")
            except Exception as ex:
                self.logger.error(f"Error handling CHANNEL_INFO event: {ex}")
        
        # Subscribe to CHANNEL_INFO events
        self.api.mesh_core.dispatcher.subscribe(
            EventType.CHANNEL_INFO,
            handle_channel_info,
        )
        self.logger.debug("Registered CHANNEL_INFO event listener")
    
    async def fetch_all_channel_info(self) -> None:
        """Fetch channel info for all channels on startup."""
        self.logger.info(f"Fetching channel info for {self._max_channels} channels...")
        for channel_idx in range(self._max_channels):
            try:
                # Use get_channel command
                channel_info_result = await self.api.mesh_core.commands.get_channel(channel_idx)
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
                if self.api.mesh_core:
                    channel_info_result = await self.api.mesh_core.commands.get_channel(channel_idx)
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
        """Reset routing path for a node and return success status."""
        node_name = node_config.get("name", "unknown")
        
        # Check disable_path_reset flag
        disable_path_reset = node_config.get(CONF_REPEATER_DISABLE_PATH_RESET, node_config.get(CONF_CLIENT_DISABLE_PATH_RESET, False))
        if disable_path_reset:
            self.logger.debug(f"Path reset disabled for {node_name}, skipping")
            return False
            
        try:
            result = await self.api.mesh_core.commands.reset_path(contact)
            if result and result.type != EventType.ERROR:
                self.logger.info(f"Successfully reset path for {node_name}")
                return True
            else:
                error_msg = result.payload if result and result.type == EventType.ERROR else "no response or unexpected result"
                self.logger.warning(f"Failed to reset path for {node_name}: {error_msg}")
                return False
        except Exception as ex:
            self.logger.warning(f"Exception resetting path for {node_name}: {ex}")
            return False
    
    def update_telemetry_settings(self, config_entry: ConfigEntry) -> None:
        """Update telemetry settings from config entry."""
        self._self_telemetry_enabled = config_entry.data.get(CONF_SELF_TELEMETRY_ENABLED, False)
        self._self_telemetry_interval = config_entry.data.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL)
        self._self_diagnostics_enabled = config_entry.data.get(CONF_SELF_DIAGNOSTICS_ENABLED, False)
        self._self_diagnostics_interval = config_entry.data.get(CONF_SELF_DIAGNOSTICS_INTERVAL, DEFAULT_SELF_DIAGNOSTICS_INTERVAL)
        self._tracked_repeaters = config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        self._tracked_clients = config_entry.data.get(CONF_TRACKED_CLIENTS, [])
        self._auto_cleanup_stale_contacts = config_entry.data.get(
            CONF_AUTO_CLEANUP_STALE_CONTACTS, False
        )
        self._stale_contact_days = config_entry.data.get(
            CONF_STALE_CONTACT_DAYS, DEFAULT_STALE_CONTACT_DAYS
        )
        self._auto_cleanup_stale_neighbors = config_entry.data.get(
            CONF_AUTO_CLEANUP_STALE_NEIGHBORS, False
        )
        self._stale_neighbor_days = config_entry.data.get(
            CONF_STALE_NEIGHBOR_DAYS, DEFAULT_STALE_NEIGHBOR_DAYS
        )
        _LOGGER.debug(f"Updated telemetry settings - Enabled: {self._self_telemetry_enabled}, Interval: {self._self_telemetry_interval}, Tracked clients: {len(self._tracked_clients)}")

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
        """Fetch neighbor data for a repeater after a successful status request.

        Calls the SDK's fetch_all_neighbours() binary command and stores the
        result in self._repeater_neighbors. Creates new sensor entities for
        any neighbors not previously seen. Tracks sightings as timestamps in
        a rolling 48h window (seen_timestamps). Persists neighbor data to
        storage for survival across restarts.
        """
        try:
            # Check rate limiter before making mesh request
            if not self._rate_limiter.try_consume(1):
                self.logger.debug(f"Rate limited: skipping neighbor fetch for {repeater_name}")
                return

            self.logger.debug(f"Fetching neighbors for repeater {repeater_name} ({pubkey_prefix})")
            result = await self.api.mesh_core.commands.fetch_all_neighbours(
                contact, pubkey_prefix_length=NEIGHBOR_PUBKEY_PREFIX_LENGTH
            )

            if not result or "neighbours" not in result:
                self.logger.debug(f"No neighbor data returned for {repeater_name}")
                return

            neighbours = result["neighbours"]
            now = time.time()
            updated_neighbors = {}

            # Get existing data before iteration (for seen_timestamps carry-over)
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

                # Track sightings as timestamps in a rolling 48h window.
                # The firmware computes secs_ago from its own RTC, so if secs_ago
                # decreased since last poll, heard_timestamp was refreshed — the
                # neighbor was heard again. If secs_ago grew or stayed the same,
                # nothing new happened.
                existing_data = existing.get(n_pubkey, {})
                prev_secs_ago = existing_data.get("secs_ago")

                # Carry forward existing timestamps, pruning entries older than 48h
                prev_timestamps = existing_data.get("seen_timestamps", [])
                seen_timestamps = [t for t in prev_timestamps if t > cutoff]

                # Only record a sighting if the neighbor was actually heard
                # within the 48h window.  After an HA restart the stored
                # secs_ago is inflated by the elapsed downtime, so the
                # "n_secs_ago < prev_secs_ago" comparison would incorrectly
                # treat every stale neighbor as newly heard.
                if n_secs_ago <= SEEN_WINDOW_SECS:
                    if prev_secs_ago is None:
                        # First sighting — always record it
                        seen_timestamps.append(now)
                    elif n_secs_ago < prev_secs_ago:
                        # secs_ago decreased — firmware heard this neighbor again
                        seen_timestamps.append(now)
                # else: stale (>48h) or secs_ago grew/stayed same — don't record

                updated_neighbors[n_pubkey] = {
                    "pubkey": n_pubkey,
                    "snr": n_snr,
                    "secs_ago": n_secs_ago,
                    "last_updated": now,
                    "resolved_name": self.resolve_neighbor_name(n_pubkey),
                    "seen_timestamps": seen_timestamps,
                }

            # Preserve neighbors from previous polls that aren't in this response
            # (they may still be valid, just not in the current page)
            for n_pubkey, n_data in existing.items():
                if n_pubkey not in updated_neighbors:
                    # Keep old data but don't update last_updated — staleness
                    # is tracked via secs_ago from the most recent poll that included it.
                    # Prune seen_timestamps so stale entries don't inflate the count.
                    prev_ts = n_data.get("seen_timestamps", [])
                    n_data["seen_timestamps"] = [t for t in prev_ts if t > cutoff]
                    updated_neighbors[n_pubkey] = n_data

            self._repeater_neighbors[pubkey_prefix] = updated_neighbors

            # Persist neighbor data (strip resolved_name — resolved live from contacts)
            await self._save_neighbor_data()

            # Create sensor entities for any new neighbors
            new_neighbors = []
            for n_pubkey in updated_neighbors:
                sensor_key = f"{pubkey_prefix}:{n_pubkey}"
                if sensor_key not in self._created_neighbor_sensors:
                    new_neighbors.append(n_pubkey)
                    self._created_neighbor_sensors.add(sensor_key)

            if new_neighbors and hasattr(self, "sensor_add_entities") and self.sensor_add_entities:
                from .sensor import MeshCoreNeighborSensor, MeshCoreNeighborSeenSensor
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

        Uses last_heard = last_updated - secs_ago (the actual time the repeater
        heard the neighbor, not the poll time).

        Three-phase approach mirroring stale contacts cleanup:
        1. Collect stale neighbors (no side effects)
        2. Remove entities + in-memory data in batches
        3. Persist and refresh state

        Returns the number of neighbors removed.
        """
        from homeassistant.helpers import entity_registry as er

        now = time.time()
        threshold_seconds = days_threshold * 86400
        entity_registry = er.async_get(self.hass)

        # Phase 1: Collect stale neighbors across all repeaters
        stale_entries: list[tuple[str, str, str]] = []  # (repeater_prefix, neighbor_pubkey, resolved_name)
        for rptr_prefix, neighbors in self._repeater_neighbors.items():
            for n_pubkey, n_data in neighbors.items():
                last_updated = n_data.get("last_updated", 0)
                secs_ago = n_data.get("secs_ago", 0)

                # Skip neighbors with no data yet (just loaded from storage
                # without a poll)
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

        # Phase 2: Remove in batches, yielding the event loop between each
        # batch so WebSocket clients can drain their message queues.
        batch_size = 10
        removed_count = 0

        for i, (rptr_prefix, n_pubkey, resolved_name) in enumerate(stale_entries):
            # Remove both sensor entities (SNR + Seen) from entity registry
            unique_id_prefix = (
                f"{self.config_entry.entry_id}_repeater_{rptr_prefix}"
                f"_neighbor_{n_pubkey[:12]}"
            )
            for entity in list(entity_registry.entities.values()):
                if entity.platform == DOMAIN and (entity.unique_id or "").startswith(unique_id_prefix):
                    entity_registry.async_remove(entity.entity_id)

            # Remove from created sensors tracking
            sensor_key = f"{rptr_prefix}:{n_pubkey}"
            self._created_neighbor_sensors.discard(sensor_key)

            # Remove from in-memory neighbor data
            repeater_neighbors = self._repeater_neighbors.get(rptr_prefix, {})
            repeater_neighbors.pop(n_pubkey, None)

            removed_count += 1
            _LOGGER.debug(
                "Removed stale neighbor: %s (%s) on repeater %s",
                resolved_name, n_pubkey[:6], rptr_prefix[:6],
            )

            # Yield the event loop every batch_size removals
            if (i + 1) % batch_size == 0:
                await asyncio.sleep(0)

        # Phase 3: Persist and refresh once after all removals
        if removed_count > 0:
            await self._save_neighbor_data()

            updated_data = dict(self.data) if self.data else {}
            self.async_set_updated_data(updated_data)

        _LOGGER.info(
            "Stale neighbor cleanup: removed %d neighbors older than %d days",
            removed_count, days_threshold,
        )
        return removed_count

    async def async_load_neighbor_data(self) -> None:
        """Load persisted neighbor data from storage.

        Must be called before sensor platform setup so that sensor.py can
        recreate neighbor sensor entities from the persisted data. Does NOT
        populate _created_neighbor_sensors — that is done by sensor.py when
        it actually instantiates the sensor objects.
        """
        if self._neighbor_data_loaded:
            return
        try:
            stored = await self._neighbor_store.async_load()
            if stored:
                now = time.time()
                for rptr_prefix, neighbors in stored.items():
                    for n_pubkey, n_data in neighbors.items():
                        last_updated = n_data.get("last_updated", now)
                        elapsed = now - last_updated
                        n_data["secs_ago"] = n_data.get("secs_ago", 0) + int(elapsed)
                        n_data["resolved_name"] = self.resolve_neighbor_name(n_pubkey)
                        # Migrate from seen_count (integer) to seen_timestamps (list)
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
        """Remove all neighbor sensor entities for a repeater from the entity registry.

        Called when the neighbors_enabled toggle is turned off for a repeater.
        Removes both SNR and Seen sensor entities, clears in-memory tracking,
        and removes persisted data for this repeater.
        Returns the number of entities removed.
        """
        from homeassistant.helpers import entity_registry as er

        entity_registry = er.async_get(self.hass)
        unique_id_prefix = f"{self.config_entry.entry_id}_repeater_{pubkey_prefix}_neighbor_"
        removed = 0

        for entity in list(entity_registry.entities.values()):
            if entity.platform == DOMAIN and (entity.unique_id or "").startswith(unique_id_prefix):
                _LOGGER.info("Removing neighbor entity: %s", entity.entity_id)
                entity_registry.async_remove(entity.entity_id)
                removed += 1

        # Clear in-memory tracking for this repeater's neighbors
        self._repeater_neighbors.pop(pubkey_prefix, None)
        self._created_neighbor_sensors = {
            k for k in self._created_neighbor_sensors
            if not k.startswith(f"{pubkey_prefix}:")
        }

        # Remove persisted data for this repeater and save
        self.hass.async_create_task(self._save_neighbor_data())

        if removed:
            _LOGGER.info(
                "Cleaned up %d neighbor entities for repeater %s",
                removed, pubkey_prefix[:6]
            )

        return removed

    async def _update_repeater(self, repeater_config):
        """Update a repeater and schedule the next update.

        
        This runs as a separate task so it doesn't block the main update loop.
        If we fail to get stats multiple times, we'll try to login.
        """
        # add a random delay to avoid all repeaters updating at the same time
        # 0-30 seconds random delay
        random_delay = random.uniform(0, MAX_RANDOM_DELAY)
        await asyncio.sleep(random_delay)

        
        pubkey_prefix = repeater_config.get("pubkey_prefix")
        repeater_name = repeater_config.get("name")
        
        if not pubkey_prefix or not repeater_name:
            self.logger.warning(f"Cannot update repeater with missing pubkey_prefix or name: {repeater_config}")
            return
            
        try:
            # Find the contact by public key prefix
            contact = self.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
            if not contact:
                self.logger.warning(f"Could not find repeater contact with pubkey_prefix: {pubkey_prefix}")
                # Don't count this as a failure since the contact isn't found
                return
                
            # Get the current failure count
            failure_count = self._repeater_consecutive_failures.get(pubkey_prefix, 0)

            # Check if we need to login (only after failures and not too recently)
            needs_failure_recovery = failure_count >= MAX_REPEATER_FAILURES_BEFORE_LOGIN
            last_login_time = self._repeater_login_times.get(pubkey_prefix, 0)
            time_since_login = self._current_time() - last_login_time
            login_cooldown = 3600  # 1 hour in seconds

            if needs_failure_recovery and time_since_login >= login_cooldown:
                self.logger.info(f"Attempting login to repeater {repeater_name} after {failure_count} failures")

                # Check rate limiter before making mesh request
                if not self._rate_limiter.try_consume(1):
                    self.logger.debug(f"Rate limited: skipping login to {repeater_name}")
                    self._increment_failure(pubkey_prefix)
                    update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                    self._apply_repeater_backoff(pubkey_prefix, failure_count + 1, update_interval)
                    return

                try:
                    login_result = await self.api.mesh_core.commands.send_login_sync(
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
                    # Update login time to enforce cooldown even on exception
                    self._repeater_login_times[pubkey_prefix] = self._current_time()
                await asyncio.sleep(1)
            
            # Request status from the repeater
            self.logger.debug(f"Sending status request to repeater: {repeater_name} ({pubkey_prefix})")

            # Check rate limiter before making mesh request
            if not self._rate_limiter.try_consume(1):
                self.logger.debug(f"Rate limited: skipping status request to {repeater_name}")
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                self._increment_failure(pubkey_prefix)
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
                return

            result = await self.api.mesh_core.commands.req_status_sync(contact)
            _LOGGER.debug(f"Status response received: {result}")


            # Handle response -- req_status_sync returns a payload dict or None
            if not result:
                self.logger.warning(f"Error requesting status from repeater {repeater_name}: no response (timeout or send failure)")
                # Increment failure count and apply backoff
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                self._increment_failure(pubkey_prefix)

                # Reset path after configured failures if there's an established path
                if new_failure_count >= MAX_FAILURES_BEFORE_PATH_RESET and contact and contact.get("out_path_len", -1) > -1:
                    await self._reset_node_path(contact, repeater_config)

                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
            elif result.get('uptime', 0) == 0:
                self.logger.warning(f"Malformed status response from repeater {repeater_name}: {result}")
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                self._increment_failure(pubkey_prefix)
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
            else:
                self.logger.debug(f"Successfully updated repeater {repeater_name}")
                # Reset failure count on success
                self._repeater_consecutive_failures[pubkey_prefix] = 0
                self._increment_success(pubkey_prefix)

                # Fetch neighbor data while we have a good connection (if enabled)
                if repeater_config.get(CONF_REPEATER_NEIGHBORS_ENABLED, False):
                    await self._fetch_repeater_neighbors(contact, repeater_name, pubkey_prefix)

                # Trigger state updates for any entities listening for this repeater
                self.async_set_updated_data(self.data)

                # Schedule next update based on configured interval
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                next_update_time = self._current_time() + update_interval
                self._next_repeater_update_times[pubkey_prefix] = next_update_time

        except Exception as ex:
            self.logger.warning(f"Exception updating repeater {repeater_name}: {ex}")
            # Increment failure count and apply backoff
            new_failure_count = self._repeater_consecutive_failures.get(pubkey_prefix, 0) + 1
            self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
            self._increment_failure(pubkey_prefix)
            update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
            self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
        finally:
            # Remove this task from active tasks
            if pubkey_prefix in self._active_repeater_tasks:
                self._active_repeater_tasks.pop(pubkey_prefix)
            await asyncio.sleep(1)  # Small delay to avoid tight loops

    def _apply_backoff(self, pubkey_prefix: str, failure_count: int, update_interval: int, update_type: str = "repeater") -> None:
        """Apply exponential backoff delay for failed updates.
        
        Uses dynamic base interval to ensure max 5 retries within the refresh window.
        Resets failure count after MAX_RETRY_ATTEMPTS to start fresh.
        
        Args:
            pubkey_prefix: The node's public key prefix
            failure_count: Number of consecutive failures
            update_interval: The configured update interval to cap the backoff at
            update_type: Type of update ("repeater" or "telemetry")
        """
        # Calculate base interval to fit 5 retries within refresh window
        # Sum of geometric series: base * (2^5 - 1) / (2 - 1) = base * 31
        # We want this to be roughly half the refresh interval for safety
        base_interval = max(1, update_interval // (31 * 2))
        
        backoff_delay = min(base_interval * (REPEATER_BACKOFF_BASE ** failure_count), update_interval)
        next_update_time = self._current_time() + backoff_delay
        
        if update_type == "telemetry":
            self._next_telemetry_update_times[pubkey_prefix] = next_update_time
        else:
            self._next_repeater_update_times[pubkey_prefix] = next_update_time
        
        self.logger.debug(f"Applied backoff for {update_type} {pubkey_prefix}: "
                         f"failure_count={failure_count}, "
                         f"base_interval={base_interval}s, "
                         f"delay={backoff_delay}s, "
                         f"interval_cap={update_interval}s")

    def _apply_repeater_backoff(self, pubkey_prefix: str, failure_count: int, update_interval: int) -> None:
        """Apply exponential backoff delay for failed repeater updates."""
        self._apply_backoff(pubkey_prefix, failure_count, update_interval, "repeater")

    async def _update_node_telemetry(self, contact, node_config: dict):
        """Update telemetry for a node (repeater or client).
        
        This is a separate method that can be used by both repeater and client update logic.
        Assumes repeater login has already been handled by status update logic.
        """
        # Extract values from node_config
        pubkey_prefix = node_config.get("pubkey_prefix")
        node_name = node_config.get("name")
        
        # Validate required fields
        if not pubkey_prefix or not node_name:
            self.logger.warning(f"Node config missing required fields - pubkey_prefix: {pubkey_prefix}, name: {node_name}")
            return
            
        # Handle different field names for update_interval between repeaters and clients
        update_interval = (node_config.get(CONF_REPEATER_UPDATE_INTERVAL) or 
                          node_config.get(CONF_CLIENT_UPDATE_INTERVAL, DEFAULT_CLIENT_UPDATE_INTERVAL))
        
        # Get current failure count
        failure_count = self._telemetry_consecutive_failures.get(pubkey_prefix, 0)

        # add a random delay to avoid all updating at the same time
        # 0-30 seconds random delay
        random_delay = random.uniform(0, MAX_RANDOM_DELAY)
        await asyncio.sleep(random_delay)
        
        try:
            self.logger.debug(f"Sending telemetry request to node: {node_name} ({pubkey_prefix})")

            # Check rate limiter before making mesh request
            if not self._rate_limiter.try_consume(1):
                self.logger.debug(f"Rate limited: skipping telemetry request to {node_name}")
                new_failure_count = failure_count + 1
                self._telemetry_consecutive_failures[pubkey_prefix] = new_failure_count
                self._increment_failure(pubkey_prefix)
                self._apply_backoff(pubkey_prefix, new_failure_count, update_interval, "telemetry")
                return

            telemetry_result = await self.api.mesh_core.commands.req_telemetry_sync(contact)
            
            if telemetry_result:
                self.logger.debug(f"Telemetry response received from {node_name}: {telemetry_result}")
                # Reset failure count on success
                self._telemetry_consecutive_failures[pubkey_prefix] = 0
                self._increment_success(pubkey_prefix)
                # Schedule next telemetry update
                next_telemetry_time = self._current_time() + update_interval
                self._next_telemetry_update_times[pubkey_prefix] = next_telemetry_time
            else:
                self.logger.debug(f"No telemetry response received from {node_name}")
                # Increment failure count and apply backoff
                new_failure_count = failure_count + 1
                self._telemetry_consecutive_failures[pubkey_prefix] = new_failure_count
                self._increment_failure(pubkey_prefix)
                
                # Reset path after configured failures if there's an established path
                if new_failure_count >= MAX_FAILURES_BEFORE_PATH_RESET and contact and contact.get("out_path_len", -1) > -1:
                    await self._reset_node_path(contact, node_config)
                
                self._apply_backoff(pubkey_prefix, new_failure_count, update_interval, "telemetry")
                
        except Exception as ex:
            self.logger.warning(f"Exception requesting telemetry from node {node_name}: {ex}")
            # Increment failure count and apply backoff
            new_failure_count = failure_count + 1
            self._telemetry_consecutive_failures[pubkey_prefix] = new_failure_count
            self._increment_failure(pubkey_prefix)
            
            # Reset path after configured failures if there's an established path
            if new_failure_count >= MAX_FAILURES_BEFORE_PATH_RESET and contact and contact.get("out_path_len", -1) > -1:
                await self._reset_node_path(contact, node_config)
            
            self._apply_backoff(pubkey_prefix, new_failure_count, update_interval, "telemetry")
        finally:
            # Remove this task from active telemetry tasks
            if pubkey_prefix in self._active_telemetry_tasks:
                self._active_telemetry_tasks.pop(pubkey_prefix)
            await asyncio.sleep(1)  # Small delay to avoid tight loops

    async def async_flush_messages(self) -> Dict[str, Any]:
        """Immediately flush pending messages from the device queue.

        Called by the MESSAGES_WAITING event handler for instant message
        delivery.  Uses _message_lock to prevent overlap with the
        coordinator poll's own get_msg() loop.
        """
        async with self._message_lock:
            try:
                while True:
                    result = await self.api.mesh_core.commands.get_msg()
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

    async def _async_update_data(self) -> Dict[str, Any]:
        """Trigger commands that will generate events on schedule.
        
        In the event-driven architecture, this method:
        1. Ensures we're connected to the device
        2. Triggers commands based on scheduled intervals
        3. Maintains shared data like contacts list
        
        The actual state updates happen through event subscriptions in the entities.
        """
        # Initialize result with previous data
        result_data = dict(self.data) if self.data else {
            "name": "MeshCore Node", 
            "contacts": []
        }
        # Check and update repeaters that need updating
        current_time = self._current_time()
        _LOGGER.debug("Starting data update...")
        
        _LOGGER.debug(f"Timings:"
                      f"Now: {current_time}, "
                      f"Next: {self._next_repeater_update_times}, "
                      f"Failures: {self._repeater_consecutive_failures}")
        
    
        # Reconnect if needed
        if not self.api.connected:
            self.logger.info("Connecting to device... (init)")
            await self.api.disconnect()
            # Reset initialization flags
            self._device_info_initialized = False
            connection_success = await self.api.connect()
            if not connection_success:
                self.logger.error("Failed to connect to MeshCore device")
                raise UpdateFailed("Failed to connect to MeshCore device")
        
        # Always get battery status
        await self.api.mesh_core.commands.get_bat()
        
        # Initialize manual contact mode on first run
        if not self._manual_mode_initialized:
            try:
                self.logger.info("Setting manual contact mode...")
                result = await self.api.mesh_core.commands.set_manual_add_contacts(True)
                if result and result.type != EventType.ERROR:
                    self.logger.info("Manual contact mode enabled")
                    self._manual_mode_initialized = True

                    # Load discovered contacts from storage
                    stored_contacts = await self._store.async_load()
                    if stored_contacts:
                        self._discovered_contacts = stored_contacts
                        self.logger.info(f"Loaded {len(stored_contacts)} discovered contacts from storage")
                else:
                    self.logger.error(f"Failed to set manual contact mode: {result}")
            except Exception as ex:
                self.logger.error(f"Error setting manual contact mode: {ex}")

        # Fetch device info if we don't have it yet or don't have complete info
        if not self._device_info_initialized:
            try:
                self.logger.info("Fetching device info...")
                device_query_result = await self.api.mesh_core.commands.send_device_query()
                if device_query_result.type is EventType.DEVICE_INFO:
                    self._firmware_version = device_query_result.payload.get("ver")
                    self._hardware_model = device_query_result.payload.get("model")
                    self._max_channels = device_query_result.payload.get("max_channels", 4)  # Default to 4 if not provided

                    if self._firmware_version:
                        self.device_info["sw_version"] = self._firmware_version
                    if self._hardware_model:
                        self.device_info["model"] = self._hardware_model

                    self.logger.info(f"Device info updated - Firmware: {self._firmware_version}, Model: {self._hardware_model}, Max Channels: {self._max_channels}")
                    self._device_info_initialized = True

                    # Set up CHANNEL_INFO event listener
                    self._setup_channel_info_listener()

                    # Fetch channel info for all channels
                    await self.fetch_all_channel_info()

                    self.async_update_listeners()
            except Exception as ex:
                self.logger.error(f"Error fetching device info: {ex}")
        
        # Sync contacts if dirty (uses SDK's internal dirty flag)
        try:
            contacts_changed = await self.api.mesh_core.ensure_contacts(follow=True)
            if contacts_changed:
                self.logger.info("Contacts synced from node")
                self._contacts = {}
                for contact in self.api.mesh_core.contacts.values():
                    public_key = contact.get("public_key")
                    if public_key:
                        prefix = public_key[:12]
                        self._contacts[prefix] = contact
        except Exception as ex:
            self.logger.error(f"Error syncing contacts: {ex}")

        # Store combined contacts (added + discovered) in result data
        result_data["contacts"] = self.get_all_contacts()

        # Auto-cleanup stale discovered contacts (once per day)
        if self._auto_cleanup_stale_contacts and self._stale_contact_days > 0:
            now_ts = time.time()
            if now_ts - self._last_stale_cleanup >= 86400:  # 24 hours
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
                    # Refresh contacts in result_data after cleanup
                    result_data["contacts"] = self.get_all_contacts()

        # Check for self telemetry updates if enabled
        if self._self_telemetry_enabled:
            if current_time - self._last_self_telemetry_update >= self._self_telemetry_interval:
                self.logger.debug(f"Getting self telemetry (interval: {self._self_telemetry_interval}s)")
                try:
                    telemetry_result = await self.api.mesh_core.commands.get_self_telemetry()
                    if telemetry_result.type == EventType.TELEMETRY_RESPONSE:
                        self.logger.debug(f"Self telemetry received: {telemetry_result.payload}")
                        self._last_self_telemetry_update = current_time
                    else:
                        self.logger.error(f"Failed to get self telemetry: {telemetry_result.payload}")
                except Exception as ex:
                    self.logger.error(f"Exception getting self telemetry: {ex}")
            else:
                self.logger.debug(f"Skipping self telemetry (next in {self._self_telemetry_interval - (current_time - self._last_self_telemetry_update):.1f}s)")

        # Check for self diagnostics updates if enabled.
        # These are LOCAL-transport queries to the attached radio (a 2-byte
        # GET_STATS opcode frame, no destination contact) — they add no mesh
        # traffic and consume no airtime/duty-cycle. The SDK dispatches
        # STATS_CORE/RADIO/PACKETS events that the diagnostic sensor entities
        # subscribe to, so no return-value handling is needed here.
        if self._self_diagnostics_enabled:
            if current_time - self._last_self_diagnostics_update >= self._self_diagnostics_interval:
                self.logger.debug(f"Getting self diagnostics (interval: {self._self_diagnostics_interval}s)")
                try:
                    await self.api.mesh_core.commands.get_stats_core()
                    await self.api.mesh_core.commands.get_stats_radio()
                    await self.api.mesh_core.commands.get_stats_packets()
                    self._last_self_diagnostics_update = current_time
                except Exception as ex:
                    self.logger.debug(f"Exception getting self diagnostics: {ex}")
            else:
                self.logger.debug(f"Skipping self diagnostics (next in {self._self_diagnostics_interval - (current_time - self._last_self_diagnostics_update):.1f}s)")

        # --- Message handling ---
        # On first cycle: drain any messages queued while disconnected.
        # After that: only poll if no message activity in MSG_SAFETY_NET_INTERVAL.
        # Normal message delivery is event-driven via MESSAGES_WAITING -> async_flush_messages().
        current_time_mono = time.time()
        should_poll = (
            not self._initial_drain_done
            or (current_time_mono - self._last_msg_activity) >= MSG_SAFETY_NET_INTERVAL
        )

        if should_poll:
            async with self._message_lock:
                try:
                    while True:
                        result = await self.api.mesh_core.commands.get_msg()
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

            # Update activity timestamp even if queue was empty,
            # so we don't re-poll until another MSG_SAFETY_NET_INTERVAL elapses.
            self._last_msg_activity = current_time_mono

        for repeater_config in self._tracked_repeaters:
            if not repeater_config.get('name') or not repeater_config.get('pubkey_prefix'):
                _LOGGER.warning(f"Repeater config missing name or pubkey_prefix: {repeater_config}")
                continue

            pubkey_prefix = repeater_config.get("pubkey_prefix")
            repeater_name = repeater_config.get("name")

            # Check if device is disabled (either in config or auto-disabled)
            if repeater_config.get(CONF_DEVICE_DISABLED, False) or pubkey_prefix in self._auto_disabled_devices:
                continue

            # Check if repeater has had no successful requests in AUTO_DISABLE_HOURS
            # Use last success time, or coordinator start time if never succeeded
            last_success_time = self._last_successful_request.get(pubkey_prefix, self._coordinator_start_time)
            hours_since_success = (current_time - last_success_time) / 3600  # Convert to hours

            if hours_since_success >= AUTO_DISABLE_HOURS:
                _LOGGER.warning(
                    f"Repeater {repeater_name} has had no successful requests in {hours_since_success:.1f} hours. "
                    f"Automatically disabling to reduce network traffic. This will reset on restart."
                )
                # Add to auto-disabled set (will reset on restart)
                self._auto_disabled_devices.add(pubkey_prefix)
                continue

            # Clean c completed or failed tasks
            if pubkey_prefix in self._active_repeater_tasks:
                task = self._active_repeater_tasks[pubkey_prefix]
                if task.done():
                    # Remove completed task
                    self._active_repeater_tasks.pop(pubkey_prefix)
                    # Handle exceptions
                    if task.exception():
                        _LOGGER.error(f"Repeater update task for {repeater_name} failed with exception: {task.exception()}")
                else:
                    # Task is still running, skip this repeater
                    _LOGGER.debug(f"Update task for repeater {repeater_name} still running, skipping")
                    continue
                
            # Check if it's time to update this repeater
            next_update_time = self._next_repeater_update_times.get(pubkey_prefix, 0)
            if current_time >= next_update_time:
                _LOGGER.debug(f"Starting repeater update task for {repeater_name}")
                
                # Create and start a new task for this repeater
                update_task = asyncio.create_task(self._update_repeater(repeater_config))
                self._active_repeater_tasks[pubkey_prefix] = update_task
                
                # Set a name for the task for better debugging
                update_task.set_name(f"update_repeater_{repeater_name}")

        # Check and update telemetry for nodes that have it enabled
        _LOGGER.debug("Checking telemetry for tracked repeaters: %s", self._next_telemetry_update_times)
        for repeater_config in self._tracked_repeaters:
            if not repeater_config.get('name') or not repeater_config.get('pubkey_prefix'):
                continue

            if repeater_config.get(CONF_DEVICE_DISABLED, False):
                continue

            telemetry_enabled = repeater_config.get(CONF_REPEATER_TELEMETRY_ENABLED, False)
            if not telemetry_enabled:
                continue

            pubkey_prefix = repeater_config.get("pubkey_prefix")
            repeater_name = repeater_config.get("name")
            
            # Clean up completed telemetry tasks
            if pubkey_prefix in self._active_telemetry_tasks:
                task = self._active_telemetry_tasks[pubkey_prefix]
                if task.done():
                    self._active_telemetry_tasks.pop(pubkey_prefix)
                    if task.exception():
                        _LOGGER.error(f"Telemetry update task for {repeater_name} failed with exception: {task.exception()}")
                else:
                    # Task is still running, skip this node
                    _LOGGER.debug(f"Telemetry task for {repeater_name} still running, skipping")
                    continue
            
            # Check if it's time to update telemetry for this node
            next_telemetry_time = self._next_telemetry_update_times.get(pubkey_prefix, 0)
            if current_time >= next_telemetry_time:
                # Find the contact for this node
                contact = self.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
                if contact:
                    _LOGGER.debug(f"Starting telemetry update task for {repeater_name}")
                    
                    # Use same interval as repeater update for telemetry
                    update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                    
                    # Create and start telemetry task
                    telemetry_task = asyncio.create_task(
                        self._update_node_telemetry(contact, repeater_config)
                    )
                    self._active_telemetry_tasks[pubkey_prefix] = telemetry_task
                    telemetry_task.set_name(f"telemetry_{repeater_name}")
                else:
                    _LOGGER.warning(f"Could not find contact for telemetry request: {pubkey_prefix}")
        
        _LOGGER.debug("Checking telemetry for tracked clients")
        for client_config in self._tracked_clients:
            if not client_config.get('name') or not client_config.get('pubkey_prefix'):
                _LOGGER.warning(f"Client config missing name or pubkey_prefix: {client_config}")
                continue

            pubkey_prefix = client_config.get("pubkey_prefix")
            client_name = client_config.get("name")

            # Check if device is disabled (either in config or auto-disabled)
            if client_config.get(CONF_DEVICE_DISABLED, False) or pubkey_prefix in self._auto_disabled_devices:
                continue
            
            if pubkey_prefix in self._active_telemetry_tasks:
                task = self._active_telemetry_tasks[pubkey_prefix]
                if task.done():
                    self._active_telemetry_tasks.pop(pubkey_prefix)
                    if task.exception():
                        _LOGGER.error(f"Client telemetry update task for {client_name} failed with exception: {task.exception()}")
                else:
                    _LOGGER.debug(f"Client telemetry task for {client_name} still running, skipping")
                    continue
            
            next_telemetry_time = self._next_telemetry_update_times.get(pubkey_prefix, 0)
            if current_time >= next_telemetry_time:
                contact = self.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
                if contact:
                    _LOGGER.debug(f"Starting telemetry update task for client {client_name}")
                    
                    update_interval = client_config.get(CONF_CLIENT_UPDATE_INTERVAL, DEFAULT_CLIENT_UPDATE_INTERVAL)
                    
                    telemetry_task = asyncio.create_task(
                        self._update_node_telemetry(contact, client_config)
                    )
                    self._active_telemetry_tasks[pubkey_prefix] = telemetry_task
                    telemetry_task.set_name(f"client_telemetry_{client_name}")
                else:
                    _LOGGER.warning(f"Could not find contact for client telemetry request: {pubkey_prefix}")

        # Auto-cleanup stale neighbors (once per day)
        if self._auto_cleanup_stale_neighbors and self._stale_neighbor_days > 0:
            now_ts = time.time()
            if now_ts - self._last_stale_neighbor_cleanup >= 86400:  # 24 hours
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