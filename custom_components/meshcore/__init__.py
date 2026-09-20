"""The MeshCore integration."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event as HassEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntry

from meshcore.events import EventType

from .const import (
    CONF_BAUDRATE,
    CONF_BLE_ADDRESS,
    CONF_CONNECTION_TYPE,
    CONF_CONTACT_DISCOVERY_MODE,
    CONF_FLOOD_SCOPES,
    CONF_LIMIT_DISCOVERED_CONTACTS,
    CONF_MAX_DISCOVERED_CONTACTS,
    CONF_MESSAGES_INTERVAL,
    CONF_NAME,
    CONF_PUBKEY,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_TRACKED_CLIENTS,
    CONF_USB_PATH,
    DEFAULT_MAX_DISCOVERED_CONTACTS,
    DEFAULT_UPDATE_TICK,
    DOMAIN,
    MODE_DATA_ONLY,
    MODE_FULL,
    MODE_OFF,
    REPAIR_PUBKEY_CHANGED,
    get_contact_discovery_mode,
)
from .coordinator import MeshCoreDataUpdateCoordinator
from .map_uploader import MeshCoreMapUploader
from .mqtt_uploader import MeshCoreMqttUploader
from .radio import RadioSession
from .services import async_setup_services, async_unload_services
from .utils import (
    create_message_correlation_key,
    load_flood_scope_keys,
    match_flood_scope,
    parse_and_decrypt_rx_log,
    parse_rx_log_data,
    sanitize_event_data,
)

_LOGGER = logging.getLogger(__name__)

# List of platforms to set up
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.TEXT, Platform.DEVICE_TRACKER, Platform.BUTTON]
STATIC_PATH_REGISTERED_KEY = f"{DOMAIN}_static_path_registered"


def _read_integration_version() -> str:
    """Read integration version from manifest."""
    try:
        manifest_path = Path(__file__).with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = str(manifest.get("version", "")).strip()
        return version or "unknown"
    except Exception:
        return "unknown"

async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating configuration from version %s", config_entry.version)
    
    # Don't allow downgrading from future versions
    if config_entry.version > 3:
        _LOGGER.error("Cannot downgrade from version %s", config_entry.version)
        return False
    
    # Migrate from version 1 to version 2
    if config_entry.version == 1:
        new_data = dict(config_entry.data)
        
        # Add new fields if they don't exist
        if CONF_TRACKED_CLIENTS not in new_data:
            new_data[CONF_TRACKED_CLIENTS] = []
        
        if CONF_REPEATER_SUBSCRIPTIONS not in new_data:
            new_data[CONF_REPEATER_SUBSCRIPTIONS] = []
        else:
            # Update existing repeater subscriptions to include telemetry_enabled
            for repeater in new_data[CONF_REPEATER_SUBSCRIPTIONS]:
                if CONF_REPEATER_TELEMETRY_ENABLED not in repeater:
                    repeater[CONF_REPEATER_TELEMETRY_ENABLED] = False
        
        # Update the config entry
        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            version=2
        )
        
        _LOGGER.info("Migrated configuration from version %s to version 2", config_entry.version)
    
    # Migrate from version 2 to version 3: collapse the two overlapping
    # discovery booleans into the single contact_discovery_mode tri-state.
    # Standalone `if` (not `elif`): a v1 entry's v1->v2 block above sets
    # version=2, so it falls into this block and chains v1->v3 in one pass.
    if config_entry.version == 2:
        new_data = dict(config_entry.data)
        # Read the legacy keys as string literals so the migration is
        # independent of the const definitions.
        disable = new_data.pop("disable_contact_discovery", False)
        large_mesh = new_data.pop("large_mesh_mode", False)
        if disable:
            mode = MODE_OFF  # "off" wins the tie-break: no discovery, nothing to be data-only
        elif large_mesh:
            mode = MODE_DATA_ONLY
        else:
            mode = MODE_FULL
        new_data[CONF_CONTACT_DISCOVERY_MODE] = mode
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)
        _LOGGER.info("Migrated contact-discovery settings to %s (version 3)", mode)

    _LOGGER.debug("Migration to configuration version %s successful", config_entry.version)
    return True

def _migrate_entity_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_prefix: str,
    new_prefix: str,
) -> None:
    """Rename entity IDs and unique_ids that contain the old pubkey prefix.

    Called when the device's public key changes (e.g. after private key import).
    Updates both entity_id and unique_id in the entity registry so that
    entities created with the new prefix match existing registry entries,
    preventing orphaned duplicates.
    """
    entity_registry = er.async_get(hass)
    migrated = 0
    old_pattern = f"_{old_prefix}_"
    new_pattern = f"_{new_prefix}_"

    for entity in list(entity_registry.entities.values()):
        # Only migrate entities belonging to this config entry
        if entity.config_entry_id != entry.entry_id:
            continue

        needs_update = False
        new_entity_id = entity.entity_id
        new_unique_id = entity.unique_id

        # Migrate entity_id if it contains the old pubkey prefix
        if old_pattern in entity.entity_id:
            new_entity_id = entity.entity_id.replace(old_pattern, new_pattern, 1)
            needs_update = True

        # Migrate unique_id if it contains the old pubkey prefix
        if old_prefix in entity.unique_id:
            new_unique_id = entity.unique_id.replace(old_prefix, new_prefix)
            needs_update = True

        if not needs_update:
            continue

        try:
            entity_registry.async_update_entity(
                entity.entity_id,
                new_entity_id=new_entity_id,
                new_unique_id=new_unique_id,
            )
            _LOGGER.info("Migrated entity: %s -> %s", entity.entity_id, new_entity_id)
            migrated += 1
        except Exception as ex:
            _LOGGER.error("Failed to migrate entity %s: %s", entity.entity_id, ex)

    _LOGGER.info(
        "Migrated %d entities from prefix %s to %s", migrated, old_prefix, new_prefix
    )


def _migrate_unique_ids_remove_name(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """One-time migration: remove device name from unique_ids.

    Entity unique_ids previously included the device name, which made them
    unstable across name changes. This migration strips the name suffix
    so unique_ids use only stable identifiers (entry_id, key, pubkey).
    """
    entity_registry = er.async_get(hass)
    migrated = 0

    # Get the current companion name from config entry
    companion_name = entry.data.get(CONF_NAME, "")

    # Build list of all names that might appear in unique_ids
    # (companion + all tracked repeaters + all tracked clients).
    # Sorted longest-first so that if one name is a suffix of another
    # (e.g., "mytest" vs "test"), the longer name matches first via
    # endswith(), preventing partial stripping.
    names_raw: set[str] = set()
    if companion_name:
        names_raw.add(companion_name)
    for sub in entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, []):
        name = sub.get("name", "")
        if name:
            names_raw.add(name)
    for sub in entry.data.get(CONF_TRACKED_CLIENTS, []):
        name = sub.get("name", "")
        if name:
            names_raw.add(name)

    names_to_strip = sorted(names_raw, key=len, reverse=True)

    if not names_to_strip:
        return

    for entity in list(entity_registry.entities.values()):
        if entity.config_entry_id != entry.entry_id:
            continue

        new_unique_id = entity.unique_id
        for name in names_to_strip:
            # unique_ids have the name appended with underscore separator
            suffix = f"_{name}"
            if new_unique_id.endswith(suffix):
                new_unique_id = new_unique_id[: -len(suffix)]
                break

        if new_unique_id == entity.unique_id:
            continue

        # Verify the new unique_id doesn't already exist
        existing = entity_registry.async_get_entity_id(
            entity.domain, DOMAIN, new_unique_id
        )
        if existing and existing != entity.entity_id:
            _LOGGER.warning(
                "Cannot migrate unique_id for %s: target %s already exists (%s)",
                entity.entity_id, new_unique_id, existing,
            )
            continue

        try:
            entity_registry.async_update_entity(
                entity.entity_id,
                new_unique_id=new_unique_id,
            )
            _LOGGER.info(
                "Stabilized unique_id: %s -> %s",
                entity.unique_id, new_unique_id,
            )
            migrated += 1
        except Exception as ex:
            _LOGGER.error(
                "Failed to stabilize unique_id for %s: %s",
                entity.entity_id, ex,
            )

    if migrated:
        _LOGGER.info("Stabilized %d entity unique_ids (removed name)", migrated)


def _migrate_unique_ids_scope_contact_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """One-time: prefix contact-diagnostic unique_ids with entry_id.

    Pre-fix unique_ids were the raw 12-char pubkey prefix, which
    collides on multi-entry installs where the same mesh contact is
    observed by both entries. This migration scopes them to
    "{entry_id}_contact_{pubkey[:12]}" so each entry has its own
    diagnostic entity per contact. Single-entry installs see no
    behavior change beyond the unique_id format itself.

    Idempotent: a second call finds no 12-char-only unique_ids
    (they've all been migrated to the new format).
    """
    entity_registry = er.async_get(hass)
    migrated = 0
    for entity in list(entity_registry.entities.values()):
        if entity.config_entry_id != entry.entry_id:
            continue
        if entity.platform != DOMAIN:
            continue
        # Old unique_ids are exactly 12 hex chars (pubkey[:12]).
        old_uid = entity.unique_id
        if not (len(old_uid) == 12 and all(c in "0123456789abcdef" for c in old_uid.lower())):
            continue
        new_uid = f"{entry.entry_id}_contact_{old_uid}"
        try:
            entity_registry.async_update_entity(
                entity.entity_id, new_unique_id=new_uid,
            )
            migrated += 1
        except Exception as ex:
            _LOGGER.error(
                "Failed to migrate contact unique_id %s: %s",
                old_uid, ex,
            )
    if migrated:
        _LOGGER.info(
            "Migrated %d contact-diagnostic unique_ids for entry %s",
            migrated, entry.entry_id,
        )


async def _async_release_runtime(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MeshCoreDataUpdateCoordinator
) -> None:
    """Drop an entry's runtime: coordinator jobs, uploaders, then the radio."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    await coordinator.async_shutdown()
    if getattr(coordinator, "mqtt_uploader", None):
        await coordinator.mqtt_uploader.async_stop()
    await coordinator.api.disconnect()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MeshCore from a config entry."""
    # Home Assistant can trigger a duplicate setup during rapid reload/update cycles.
    # A runtime whose radio is still up owns the device and is left alone; a stale
    # one is torn down first so the entry never ends up with two radio owners.
    existing = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if existing is not None:
        if existing.api.connected:
            _LOGGER.warning(
                "Duplicate setup call detected for entry %s (%s); skipping",
                entry.title,
                entry.entry_id,
            )
            return True
        _LOGGER.warning(
            "Replacing stale runtime for entry %s (%s)", entry.title, entry.entry_id
        )
        await _async_release_runtime(hass, entry, existing)

    # Get configuration from entry
    connection_type = entry.data[CONF_CONNECTION_TYPE]
    
    _LOGGER.debug("Entry data: %s", entry.data)
    
    # Create API instance based on connection type
    api_kwargs = {
        "hass": hass,
        "connection_type": connection_type
    }
    
    if CONF_USB_PATH in entry.data:
        api_kwargs["usb_path"] = entry.data[CONF_USB_PATH]
    if CONF_BAUDRATE in entry.data:
        api_kwargs["baudrate"] = entry.data[CONF_BAUDRATE]
    if CONF_BLE_ADDRESS in entry.data:
        api_kwargs["ble_address"] = entry.data[CONF_BLE_ADDRESS]
    if CONF_TCP_HOST in entry.data:
        api_kwargs["tcp_host"] = entry.data[CONF_TCP_HOST]
    if CONF_TCP_PORT in entry.data:
        api_kwargs["tcp_port"] = entry.data[CONF_TCP_PORT]
    
    # Initialize API
    api = RadioSession(**api_kwargs)

    # Try to connect with retries for initial setup
    max_retries = 3
    retry_delay = 5  # seconds
    connected = False

    for attempt in range(max_retries):
        _LOGGER.info(f"Connection attempt {attempt + 1}/{max_retries}...")
        connected = await api.connect()

        if connected:
            _LOGGER.info("Successfully connected to MeshCore device")
            break

        if attempt < max_retries - 1:
            _LOGGER.warning(f"Connection attempt {attempt + 1} failed, retrying in {retry_delay} seconds...")
            await asyncio.sleep(retry_delay)
        else:
            _LOGGER.error(f"Failed to connect after {max_retries} attempts")

    if not connected:
        raise ConfigEntryNotReady(
            f"Failed to connect to MeshCore device at "
            f"{entry.data.get(CONF_TCP_HOST, 'unknown')}:{entry.data.get(CONF_TCP_PORT, 5000)} "
            f"after {max_retries} attempts"
        )

    try:
        # --- Public key change detection and entity migration ---
        # After connecting, the API caches SELF_INFO (including public_key) via send_appstart().
        # Compare the live key to what's stored in config_entry to detect key changes.
        live_pubkey = api.self_info.get("public_key", "") if connected else ""
        stored_pubkey = entry.data.get(CONF_PUBKEY, "")

        if live_pubkey and stored_pubkey and live_pubkey != stored_pubkey:
            _LOGGER.warning(
                "Public key changed! Old: %s... New: %s... Migrating entities.",
                stored_pubkey[:12], live_pubkey[:12],
            )

            # Migrate entity IDs and unique_ids before platforms are set up
            old_prefix = stored_pubkey[:6]
            new_prefix = live_pubkey[:6]
            _migrate_entity_ids(hass, entry, old_prefix, new_prefix)

            # Update config entry with new pubkey so coordinator picks it up
            new_data = dict(entry.data)
            new_data[CONF_PUBKEY] = live_pubkey
            hass.config_entries.async_update_entry(entry, data=new_data)

            # Create persistent repair issue to warn about automation/dashboard references
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"pubkey_changed_{entry.entry_id}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=REPAIR_PUBKEY_CHANGED,
                translation_placeholders={
                    "old_key": stored_pubkey[:12],
                    "new_key": live_pubkey[:12],
                },
            )
        elif live_pubkey and not stored_pubkey:
            # First time getting pubkey (shouldn't normally happen, but handle gracefully)
            new_data = dict(entry.data)
            new_data[CONF_PUBKEY] = live_pubkey
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info("Stored initial public key: %s...", live_pubkey[:12])

        # One-time migration: remove device name from unique_ids
        _migrate_unique_ids_remove_name(hass, entry)
        # One-time migration: scope contact-diagnostic unique_ids by
        # entry_id so multi-entry installs don't collide on shared mesh
        # contacts (F-U1). Idempotent.
        _migrate_unique_ids_scope_contact_diagnostics(hass, entry)

        # TODO: remove this with contact refresh interval migration?
        # Get the messages interval for base update frequency
        # Check options first, then data, then use default
        messages_interval = entry.options.get(
            CONF_MESSAGES_INTERVAL,
            entry.data.get(CONF_MESSAGES_INTERVAL, DEFAULT_UPDATE_TICK)
        )
    
        coordinator = MeshCoreDataUpdateCoordinator(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=messages_interval),
            api=api,
            config_entry=entry,
        )
    
        # Initialize all repeater next update times to 0 so they get updated immediately
        for repeater in coordinator._tracked_repeaters:
            if repeater.get("pubkey_prefix"):
                coordinator._next_repeater_update_times[repeater.get("pubkey_prefix")] = 0
    
        # Load discovered contacts from storage before platforms set up
        try:
            stored_contacts = await coordinator._store.async_load()
            if stored_contacts:
                coordinator._discovered_contacts = stored_contacts
                _LOGGER.info(f"Loaded {len(stored_contacts)} discovered contacts from storage")
        except Exception as ex:
            _LOGGER.error(f"Error loading discovered contacts: {ex}")

        # Enforce discovered contacts limit on startup (trim dict + save only, no entity cleanup)
        if entry.data.get(CONF_LIMIT_DISCOVERED_CONTACTS, False):
            max_contacts = entry.data.get(CONF_MAX_DISCOVERED_CONTACTS, DEFAULT_MAX_DISCOVERED_CONTACTS)
            if len(coordinator._discovered_contacts) > max_contacts:
                evict_count = len(coordinator._discovered_contacts) - max_contacts
                keys_to_evict = list(coordinator._discovered_contacts.keys())[:evict_count]
                for key in keys_to_evict:
                    del coordinator._discovered_contacts[key]
                try:
                    await coordinator._store.async_save(coordinator._discovered_contacts)
                except Exception as ex:
                    _LOGGER.error(f"Error saving discovered contacts after startup eviction: {ex}")
                _LOGGER.info(f"Evicted {evict_count} discovered contacts on startup (limit: {max_contacts})")

        # Load contacts from device on initialization
        if connected:
            try:
                _LOGGER.info("Loading contacts from device on initialization...")
                await api.ensure_contacts(follow=False)

                # Index contacts by 12-char prefix
                coordinator._contacts = {}
                for contact in api.contacts.values():
                    public_key = contact.get("public_key")
                    if public_key:
                        prefix = public_key[:12]
                        coordinator._contacts[prefix] = contact
                        # Mark each contact as dirty so binary sensors update
                        coordinator.mark_contact_dirty(prefix)

                _LOGGER.info(f"Loaded {len(coordinator._contacts)} contacts from device")
            except Exception as ex:
                _LOGGER.error(f"Error loading contacts from device: {ex}")

        # Load channel info eagerly so RX_LOG decryption works before the first
        # coordinator update fires (avoids empty _channel_info causing pending_cache_keys=[]).
        if connected:
            try:
                _LOGGER.info("Loading channel info on startup for RX_LOG correlation...")
                await coordinator.fetch_all_channel_info()
                _LOGGER.info(f"Startup channel info loaded: {len(coordinator._channel_info)} channels")
            except Exception as ex:
                _LOGGER.error(f"Error loading channel info on startup: {ex}")

        # Store coordinator for this entry
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = coordinator

        integration_version = await hass.async_add_executor_job(_read_integration_version)
        try:
            mqtt_uploader = MeshCoreMqttUploader(
                hass,
                _LOGGER,
                entry,
                api=coordinator.api,
                integration_version=integration_version,
            )
            await mqtt_uploader.async_start()
            coordinator.mqtt_uploader = mqtt_uploader
        except Exception as ex:
            _LOGGER.warning("MQTT uploader failed to start: %s - continuing without it", ex)
            coordinator.mqtt_uploader = None

        try:
            map_uploader = MeshCoreMapUploader(hass, _LOGGER, entry, api=coordinator.api)
            if coordinator.api.self_info:
                map_uploader.update_self_info(coordinator.api.self_info)
            coordinator.map_uploader = map_uploader
        except Exception as ex:
            _LOGGER.warning("Map Auto Uploader failed to initialize: %s - continuing without it", ex)
            coordinator.map_uploader = None
    
        # Load persisted neighbor data before sensor platform setup so that
        # sensor.py can recreate neighbor sensor entities from the stored data.
        await coordinator.async_load_neighbor_data()

        # Restore node schedules so a restart does not re-poll the whole mesh
        # (governed policy only; legacy has never persisted schedule state).
        await coordinator.async_load_traffic_state()

        # Set up all platforms for this device
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Bring the EXISTING discovered-contact population into line with the
        # configured contact discovery mode now that platforms are up (entities,
        # the binary_sensor add-callback, and the tracking sets all exist). Runs
        # once per setup with the correct post-reload coordinator, so it covers
        # start, reload, and the reload a mode switch triggers -- without the
        # pre-reload race. full is a near no-op (the setup create-pass already
        # built entities); data_only/off remove the discovered per-contact
        # entities, and off also clears the discovered set.
        try:
            await coordinator.async_reconcile_discovered_for_mode()
        except Exception as ex:
            _LOGGER.error("Error reconciling discovered contacts to mode: %s", ex)

        # Register static paths for icons
        should_cache = False
        icons_path = Path(__file__).parent / "www" / "icons"
    
        if not hass.data.get(STATIC_PATH_REGISTERED_KEY):
            await hass.http.async_register_static_paths([
                StaticPathConfig("/api/meshcore/static", str(icons_path), should_cache)
            ])
            hass.data[STATIC_PATH_REGISTERED_KEY] = True
    
        # Set up services
        await async_setup_services(hass)
    
        # Register update listener for config entry updates
        entry.async_on_unload(entry.add_update_listener(async_update_options))

        async def close_session_on_stop(event: HassEvent) -> None:
            """Release the radio when Home Assistant shuts down."""
            await api.disconnect()

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_session_on_stop)
        )

        # Subscribe to all MeshCore events and forward them to the HA event bus
        async def forward_all_events(event):
            """Forward all MeshCore events to Home Assistant event bus."""
            if not event:
                return

            # Convert event type to string if possible
            event_type_str = str(event.type) if hasattr(event, "type") else "UNKNOWN"

            try:
                sanitized_payload = sanitize_event_data(event.payload)

                # Special handling for RX_LOG events
                if hasattr(event, "type") and event.type == EventType.RX_LOG_DATA:
                    # Parse basic packet structure
                    parsed_rx_log = parse_rx_log_data(event.payload)

                    # Also attempt to decrypt GroupText payload
                    decrypted_data = parse_and_decrypt_rx_log(event.payload, coordinator._channel_info)

                    if isinstance(sanitized_payload, dict):
                        if parsed_rx_log:
                            sanitized_payload["parsed"] = parsed_rx_log
                        if decrypted_data:
                            sanitized_payload["decrypted"] = decrypted_data

                    # Store for correlation if decryption succeeded
                    if decrypted_data.get("decrypted") and decrypted_data.get("timestamp"):
                        channel_idx = decrypted_data["channel_idx"]
                        timestamp = decrypted_data["timestamp"]
                        text = decrypted_data.get("text")

                        hash_key = create_message_correlation_key(channel_idx, timestamp)

                        route_type = event.payload.get("route_type")
                        flood_scope = None
                        if route_type == 0:
                            # 'payload' hex starts at the header byte;
                            # 'raw_hex' has 2 framing bytes before the header.
                            pkt_hex = event.payload.get("payload", "")
                            pkt_payload = event.payload.get("pkt_payload", b"")
                            payload_type_int = event.payload.get("payload_type", 0)
                            scope_keys = load_flood_scope_keys(
                                coordinator.config_entry.data.get(CONF_FLOOD_SCOPES, "")
                            )
                            if scope_keys and pkt_hex and pkt_payload:
                                try:
                                    pkt_bytes = bytes.fromhex(pkt_hex) if isinstance(pkt_hex, str) else pkt_hex
                                    # header(1) + transport_codes[0](2 LE) + transport_codes[1](2)
                                    if len(pkt_bytes) >= 3:
                                        transport_code = int.from_bytes(pkt_bytes[1:3], "little")
                                        flood_scope = match_flood_scope(
                                            transport_code, payload_type_int, pkt_payload, scope_keys
                                        )
                                except Exception:
                                    pass

                        rx_log_entry = {
                            "channel_idx": channel_idx,
                            "channel_name": decrypted_data.get("channel_name"),
                            "timestamp": timestamp,
                            "text": text,
                            "snr": event.payload.get("snr"),
                            "rssi": event.payload.get("rssi"),
                            "path_len": decrypted_data.get("path_len"),
                            "path": decrypted_data.get("path"),
                            "path_hash_size": decrypted_data.get("path_hash_size"),
                            "channel_hash": decrypted_data.get("channel_hash"),
                            "route_type": route_type,
                            "route_typename": event.payload.get("route_typename"),
                            "region_scope": route_type == 0,
                            "flood_scope": flood_scope,
                        }

                        if hash_key in coordinator._pending_rx_logs:
                            coordinator._pending_rx_logs[hash_key].append(rx_log_entry)
                        else:
                            coordinator._pending_rx_logs[hash_key] = [rx_log_entry]

                        _LOGGER.debug(f"Stored RX_LOG for correlation: ch={channel_idx}, hash={hash_key[:8]}")

                # Fire event to HA event bus with sanitized payload
                _LOGGER.debug(f"Firing event to HA event bus: {event}")
                hass.bus.async_fire(f"{DOMAIN}_raw_event", {
                    "event_type": event_type_str,
                    "payload": sanitized_payload,
                    "timestamp": time.time()
                })
                if getattr(coordinator, "mqtt_uploader", None):
                    hass.async_create_task(
                        coordinator.mqtt_uploader.async_publish_raw_event(event_type_str, sanitized_payload)
                    )
            except Exception as ex:
                _LOGGER.error(f"Error serializing event payload: {ex}")
                # Fire event without payload to ensure delivery
                hass.bus.async_fire(f"{DOMAIN}_raw_event", {
                    "event_type": event_type_str,
                    "payload": None,
                    "timestamp": time.time(),
                    "serialization_error": str(ex)
                })
        
        # Add the all-events listener
        session = coordinator.api
        _LOGGER.info("Setting up all-events subscriber for MeshCore")
        entry.async_on_unload(session.subscribe(None, forward_all_events))

        if coordinator.map_uploader:

            def map_self_info_handler(event):
                if isinstance(event.payload, dict):
                    coordinator.map_uploader.update_self_info(event.payload)

            async def map_rx_log_handler(event):
                await coordinator.map_uploader.async_handle_rx_log(
                    str(event.type), event.payload
                )

            entry.async_on_unload(
                session.subscribe(EventType.SELF_INFO, map_self_info_handler)
            )
            entry.async_on_unload(
                session.subscribe(EventType.RX_LOG_DATA, map_rx_log_handler)
            )

        # Subscribe to NEW_CONTACT events to track discovered contacts
        async def handle_new_contact(event):
            """Handle NEW_CONTACT events for discovered but not-yet-added contacts."""
            if not event or not event.payload:
                return

            # Discovery disabled (mode off): drop incoming adverts so they do
            # not repopulate the discovered set (mirrors the off early-return in
            # binary_sensor.handle_contacts_update). HA-side only -- the radio
            # still hears and forwards the advert; HA just stops keeping it.
            if get_contact_discovery_mode(entry) == MODE_OFF:
                return

            contact = event.payload
            public_key = contact.get("public_key")

            if public_key:
                _LOGGER.info(f"Discovered new contact: {contact.get('adv_name', 'Unknown')} ({public_key[:12]})")

                # Refresh insertion order: delete + re-insert moves active contacts to back of FIFO
                if public_key in coordinator._discovered_contacts:
                    del coordinator._discovered_contacts[public_key]
                coordinator._discovered_contacts[public_key] = contact

                # Mark contact as dirty for binary sensor updates
                coordinator.mark_contact_dirty(public_key[:12])

                # Evict oldest contacts if limit is enabled
                limit_enabled = entry.data.get(CONF_LIMIT_DISCOVERED_CONTACTS, False)
                if limit_enabled:
                    max_contacts = entry.data.get(CONF_MAX_DISCOVERED_CONTACTS, DEFAULT_MAX_DISCOVERED_CONTACTS)
                    evicted = await coordinator.async_evict_discovered_contacts(max_contacts)
                    if evicted:
                        return  # eviction already saves and triggers async_set_updated_data

                # Save to storage
                try:
                    await coordinator._store.async_save(coordinator._discovered_contacts)
                except Exception as ex:
                    _LOGGER.error(f"Error saving discovered contacts: {ex}")

                coordinator._publish_contacts()

        _LOGGER.info("Setting up NEW_CONTACT event listener")
        entry.async_on_unload(session.subscribe(EventType.NEW_CONTACT, handle_new_contact))

        # Subscribe to MESSAGES_WAITING for instant message delivery.
        # The companion firmware sends this push notification when messages
        # are queued on the device.  Without this, messages sit in the device
        # queue until the coordinator's periodic poll calls get_msg().
        async def handle_messages_waiting(event):
            """Immediately fetch messages when device signals they are available."""
            if not coordinator.consume_incoming_messages:
                return
            _LOGGER.debug("MESSAGES_WAITING received, triggering immediate message fetch")
            asyncio.create_task(coordinator.async_flush_messages())

        entry.async_on_unload(
            session.subscribe(EventType.MESSAGES_WAITING, handle_messages_waiting)
        )
        _LOGGER.info("MESSAGES_WAITING auto-fetch subscriber registered")
    except Exception:
        _LOGGER.exception("Setup failed for entry %s; rolling back", entry.entry_id)
        runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if runtime is not None:
            await _async_release_runtime(hass, entry, runtime)
        else:
            await api.disconnect()
        raise

    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options for a config entry."""
    # Reload the entry to apply the new options
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, silencing the radio before the platforms go away."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    # Entities stop receiving radio pushes before teardown, but a platform may
    # still refuse the unload, so nothing is destroyed until that result is known.
    if coordinator is not None:
        coordinator.api.pause_forwarding()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if not unload_ok:
        if coordinator is not None:
            coordinator.api.resume_forwarding()
        return False

    if coordinator is not None:
        await _async_release_runtime(hass, entry, coordinator)

        # Unsubscribe from the message_sent event listener for this entry
        event_key = f"{DOMAIN}_message_sent_listener_{entry.entry_id}"
        if event_key in hass.data[DOMAIN]:
            unsubscribe_func = hass.data[DOMAIN].pop(event_key)
            if callable(unsubscribe_func):
                unsubscribe_func()
                _LOGGER.debug("Unsubscribed message_sent event listener")

        # If no more entries, unload services
        if not hass.data[DOMAIN]:
            await async_unload_services(hass)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Decide whether a device may be removed from the HA UI.

    Refuses removal of the main hub device and of repeater/client devices
    that are still present in the config entry. Allows removal of orphans
    (devices whose pubkey_prefix is no longer in the configured lists).
    """
    entry_id = config_entry.entry_id

    # Allow removing a device that has no entities left. An empty device
    # shell is no longer being populated, so deletion is safe -- this covers
    # a device whose entities the user has already removed (e.g. a discovered
    # contact's telemetry sensor). The hub device (identifier == entry_id) is
    # excluded: it is never user-removable even when empty.
    meshcore_identifiers = {i for d, i in device_entry.identifiers if d == DOMAIN}
    if entry_id not in meshcore_identifiers:
        entity_registry = er.async_get(hass)
        if not er.async_entries_for_device(
            entity_registry, device_entry.id, include_disabled_entities=True
        ):
            return True

    repeater_prefixes = {
        r.get("pubkey_prefix")
        for r in config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        if r.get("pubkey_prefix")
    }
    client_prefixes = {
        c.get("pubkey_prefix")
        for c in config_entry.data.get(CONF_TRACKED_CLIENTS, [])
        if c.get("pubkey_prefix")
    }

    # Build the set of live contact prefixes from the coordinator. Config
    # prefixes are always 12 chars; normalize contact prefixes the same way.
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    live_contact_prefixes = set()
    if coordinator is not None and getattr(coordinator, "data", None):
        for contact in coordinator.data.get("contacts", []):
            prefix = contact.get("pubkey_prefix") or (contact.get("public_key") or "")[:12]
            if prefix:
                live_contact_prefixes.add(prefix[:12])

    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue

        if identifier == entry_id:
            return False

        prefix = f"{entry_id}_"
        if not identifier.startswith(prefix):
            continue
        remainder = identifier[len(prefix):]
        node_type, _, pubkey_prefix = remainder.partition("_")
        # Device identifiers may carry the full 64-char public_key (contact
        # fallback in _get_node_info). Config prefixes are always 12 chars,
        # so normalize before comparing.
        pubkey_prefix = pubkey_prefix[:12]

        if node_type == "repeater" and pubkey_prefix in repeater_prefixes:
            return False
        if node_type == "client" and pubkey_prefix in client_prefixes:
            return False
        if node_type in ("contact", "unknown") and pubkey_prefix in live_contact_prefixes:
            return False

    return True
