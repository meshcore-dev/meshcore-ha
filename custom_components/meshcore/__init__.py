"""The MeshCore integration."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from datetime import timedelta
from meshcore.events import EventType

from .const import (
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_TRACKED_CLIENTS,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.components.http import StaticPathConfig
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir


from .const import (
    DOMAIN,
    CONF_CONNECTION_TYPE,
    CONF_NAME,
    CONF_USB_PATH,
    CONF_BLE_ADDRESS,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_BAUDRATE,
    CONF_PUBKEY,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_LIMIT_DISCOVERED_CONTACTS,
    CONF_MAX_DISCOVERED_CONTACTS,
    DEFAULT_MAX_DISCOVERED_CONTACTS,
    CONF_MESSAGES_INTERVAL,
    DEFAULT_UPDATE_TICK,
    REPAIR_PUBKEY_CHANGED,
    REPAIR_NAME_CHANGED,
    REPAIR_TRACKED_NODE_NAME_CHANGED,
)
from .coordinator import MeshCoreDataUpdateCoordinator
from .meshcore_api import MeshCoreAPI
from .map_uploader import MeshCoreMapUploader
from .mqtt_uploader import MeshCoreMqttUploader
from .services import async_setup_services, async_unload_services
from .utils import (
    create_message_correlation_key,
    parse_and_decrypt_rx_log,
    parse_rx_log_data,
    sanitize_event_data,
    sanitize_name,
)

_LOGGER = logging.getLogger(__name__)

# List of platforms to set up
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.TEXT, Platform.DEVICE_TRACKER]
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
    if config_entry.version > 2:
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


async def _async_migrate_entity_name(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_name_slug: str,
    new_name_slug: str,
    batch_size: int = 10,
) -> None:
    """Migrate entity IDs when the device name changes.

    Entity IDs are built by format_entity_id() with the sanitized name
    as the final segment: sensor.meshcore_{pubkey}_{key}_{name}.
    This function matches on the trailing _{old_name} suffix and replaces
    it with _{new_name}, avoiding false positives from substring matches
    in pubkey or sensor key segments.

    Does NOT modify unique_ids — those are stabilized separately
    to no longer include the name.

    Processes updates in batches, yielding to the event loop between
    each batch so HA remains responsive during large migrations.
    """
    entity_registry = er.async_get(hass)

    # Phase 1: Collect all entities that need migration
    old_suffix = f"_{old_name_slug}"
    to_migrate: list[tuple[str, str]] = []  # (old_entity_id, new_entity_id)
    for entity in list(entity_registry.entities.values()):
        if entity.config_entry_id != entry.entry_id:
            continue
        if not entity.entity_id.endswith(old_suffix):
            continue
        new_entity_id = entity.entity_id[:-len(old_suffix)] + f"_{new_name_slug}"
        to_migrate.append((entity.entity_id, new_entity_id))

    if not to_migrate:
        return

    _LOGGER.info(
        "Migrating %d entity IDs from name '%s' to '%s'",
        len(to_migrate), old_name_slug, new_name_slug,
    )

    # Phase 2: Apply in batches, yielding to event loop between batches
    migrated = 0
    for i, (old_entity_id, new_entity_id) in enumerate(to_migrate):
        try:
            entity_registry.async_update_entity(
                old_entity_id,
                new_entity_id=new_entity_id,
            )
            _LOGGER.debug("Migrated entity name: %s -> %s", old_entity_id, new_entity_id)
            migrated += 1
        except Exception as ex:
            _LOGGER.error("Failed to migrate entity %s: %s", old_entity_id, ex)

        # Yield to event loop every batch_size entities so HA stays responsive
        if (i + 1) % batch_size == 0:
            await asyncio.sleep(0)

    _LOGGER.info(
        "Migrated %d/%d entity IDs from name '%s' to '%s'",
        migrated, len(to_migrate), old_name_slug, new_name_slug,
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MeshCore from a config entry."""
    # Home Assistant can trigger a duplicate setup during rapid reload/update cycles.
    # Skip duplicate setup for an already initialized entry to avoid double platform setup.
    if entry.entry_id in hass.data.get(DOMAIN, {}):
        _LOGGER.warning(
            "Duplicate setup call detected for entry %s (%s); skipping",
            entry.title,
            entry.entry_id,
        )
        return True

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
    api = MeshCoreAPI(**api_kwargs)

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

    # --- Public key change detection and entity migration ---
    # After connecting, the API caches SELF_INFO (including public_key) via send_appstart().
    # Compare the live key to what's stored in config_entry to detect key changes.
    live_pubkey = api._last_self_info.get("public_key", "") if connected else ""
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
    if connected and api.mesh_core:
        try:
            _LOGGER.info("Loading contacts from device on initialization...")
            contacts_changed = await api.mesh_core.ensure_contacts(follow=False)

            # Index contacts by 12-char prefix
            coordinator._contacts = {}
            for contact in api.mesh_core.contacts.values():
                public_key = contact.get("public_key")
                if public_key:
                    prefix = public_key[:12]
                    coordinator._contacts[prefix] = contact
                    # Mark each contact as dirty so binary sensors update
                    coordinator.mark_contact_dirty(prefix)

            _LOGGER.info(f"Loaded {len(coordinator._contacts)} contacts from device")
        except Exception as ex:
            _LOGGER.error(f"Error loading contacts from device: {ex}")

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
    
    # Set up all platforms for this device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
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

                    rx_log_entry = {
                        "channel_idx": channel_idx,
                        "channel_name": decrypted_data.get("channel_name"),
                        "timestamp": timestamp,
                        "text": text,
                        "snr": event.payload.get("snr"),
                        "rssi": event.payload.get("rssi"),
                        "path_len": decrypted_data.get("path_len"),
                        "path": decrypted_data.get("path"),
                        "channel_hash": decrypted_data.get("channel_hash"),
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
    if coordinator.api.mesh_core:
        _LOGGER.info("Setting up all-events subscriber for MeshCore")
        coordinator.api.mesh_core.subscribe(
            None,
            forward_all_events
        )

        if coordinator.map_uploader:

            def map_self_info_handler(event):
                if isinstance(event.payload, dict):
                    coordinator.map_uploader.update_self_info(event.payload)

            async def map_rx_log_handler(event):
                await coordinator.map_uploader.async_handle_rx_log(
                    str(event.type), event.payload
                )

            coordinator.api.mesh_core.subscribe(EventType.SELF_INFO, map_self_info_handler)
            coordinator.api.mesh_core.subscribe(EventType.RX_LOG_DATA, map_rx_log_handler)

        # Subscribe to SELF_INFO to detect companion name changes
        async def _handle_self_info_name_change(event):
            """Detect companion name changes and propagate to config/entities."""
            if not isinstance(event.payload, dict):
                return
            new_name = str(event.payload.get("name", "") or "").strip()
            if not new_name:
                return
            stored_name = entry.data.get(CONF_NAME, "")
            if new_name == stored_name:
                return

            _LOGGER.info(
                "Companion name changed: '%s' -> '%s'. Updating config and entities.",
                stored_name, new_name,
            )

            # Migrate entity IDs (old sanitized name -> new sanitized name)
            old_suffix = sanitize_name(stored_name)
            new_suffix = sanitize_name(new_name)
            if old_suffix and new_suffix and old_suffix != new_suffix:
                await _async_migrate_entity_name(hass, entry, old_suffix, new_suffix)

            # Update config entry
            new_data = dict(entry.data)
            new_data[CONF_NAME] = new_name
            hass.config_entries.async_update_entry(entry, data=new_data)

            # Update coordinator live values
            coordinator.name = new_name
            pubkey_short = coordinator.pubkey[:6] if coordinator.pubkey else ""
            coordinator.device_info["name"] = f"MeshCore {new_name} ({pubkey_short})"

            # Create persistent repair issue to warn about automation/dashboard references
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"name_changed_{entry.entry_id}_{sanitize_name(stored_name)}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=REPAIR_NAME_CHANGED,
                translation_placeholders={
                    "old_name": stored_name,
                    "new_name": new_name,
                },
            )

        coordinator.api.mesh_core.dispatcher.subscribe(
            EventType.SELF_INFO, _handle_self_info_name_change
        )

        # Register callback for tracked node name changes detected by coordinator
        async def _on_tracked_node_name_change(old_name: str, new_name: str, pubkey_prefix: str) -> None:
            """Handle tracked node name change reported by coordinator.

            Migrates entity IDs (batched to keep HA responsive) and persists
            updated subscription names to config_entry.data.
            """
            old_slug = sanitize_name(old_name)
            new_slug = sanitize_name(new_name)
            if old_slug and new_slug and old_slug != new_slug:
                await _async_migrate_entity_name(hass, entry, old_slug, new_slug)

            # Persist updated subscription names from coordinator to config entry
            new_data = dict(entry.data)
            new_data[CONF_REPEATER_SUBSCRIPTIONS] = [
                dict(s) for s in coordinator._tracked_repeaters
            ]
            new_data[CONF_TRACKED_CLIENTS] = [
                dict(s) for s in coordinator._tracked_clients
            ]
            hass.config_entries.async_update_entry(entry, data=new_data)

            # Create persistent repair issue to warn about automation/dashboard references
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"name_changed_{pubkey_prefix[:12]}_{sanitize_name(old_name)}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=REPAIR_TRACKED_NODE_NAME_CHANGED,
                translation_placeholders={
                    "old_name": old_name,
                    "new_name": new_name,
                    "pubkey": pubkey_prefix[:12],
                },
            )

            _LOGGER.info(
                "Persisted name change for tracked node %s: '%s' -> '%s'",
                pubkey_prefix[:12], old_name, new_name,
            )

        coordinator.on_tracked_node_name_change = _on_tracked_node_name_change

        # Subscribe to NEW_CONTACT events to track discovered contacts
        async def handle_new_contact(event):
            """Handle NEW_CONTACT events for discovered but not-yet-added contacts."""
            if not event or not event.payload:
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

                # Update coordinator data with new contacts list
                updated_data = dict(coordinator.data) if coordinator.data else {}
                updated_data["contacts"] = coordinator.get_all_contacts()
                coordinator.async_set_updated_data(updated_data)

        _LOGGER.info("Setting up NEW_CONTACT event listener")
        coordinator.api.mesh_core.subscribe(
            EventType.NEW_CONTACT,
            handle_new_contact
        )

        # Subscribe to MESSAGES_WAITING for instant message delivery.
        # The companion firmware sends this push notification when messages
        # are queued on the device.  Without this, messages sit in the device
        # queue until the coordinator's periodic poll calls get_msg().
        async def handle_messages_waiting(event):
            """Immediately fetch messages when device signals they are available."""
            _LOGGER.debug("MESSAGES_WAITING received, triggering immediate message fetch")
            asyncio.create_task(coordinator.async_flush_messages())

        coordinator.api.mesh_core.subscribe(
            EventType.MESSAGES_WAITING,
            handle_messages_waiting
        )
        _LOGGER.info("MESSAGES_WAITING auto-fetch subscriber registered")

    # Fetch initial data immediately
    # await coordinator._async_update_data()
    
    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options for a config entry."""
    # Reload the entry to apply the new options
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Remove entry from data
    if unload_ok and entry.entry_id in hass.data[DOMAIN]:
        # Get coordinator and clean up
        coordinator = hass.data[DOMAIN][entry.entry_id]
        
        # Remove any event listeners registered by the coordinator
        if hasattr(coordinator, "_remove_listeners"):
            for remove_listener in coordinator._remove_listeners:
                remove_listener()
                
        # Disconnect from the device
        if getattr(coordinator, "mqtt_uploader", None):
            await coordinator.mqtt_uploader.async_stop()
        await coordinator.api.disconnect()
        
        # Remove entry
        hass.data[DOMAIN].pop(entry.entry_id)
        
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

                
