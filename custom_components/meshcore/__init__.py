"""The MeshCore integration."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from datetime import timedelta
from .const import (
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_TRACKED_CLIENTS,
    CONF_CONTACT_REFRESH_INTERVAL,
    DEFAULT_CONTACT_REFRESH_INTERVAL,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.components.http import StaticPathConfig


from .const import (
    DOMAIN,
    CONF_CONNECTION_TYPE,
    CONF_USB_PATH,
    CONF_BLE_ADDRESS,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_BAUDRATE,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_MESSAGES_INTERVAL,
    DEFAULT_UPDATE_TICK,
)
from .coordinator import MeshCoreDataUpdateCoordinator
from .meshcore_api import MeshCoreAPI
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

# List of platforms to set up
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.TEXT, Platform.DEVICE_TRACKER]

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
        if CONF_CONTACT_REFRESH_INTERVAL not in new_data:
            new_data[CONF_CONTACT_REFRESH_INTERVAL] = DEFAULT_CONTACT_REFRESH_INTERVAL
        
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

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MeshCore from a config entry."""
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

    # Continue setup even if connection failed - coordinator will retry
    if not connected:
        _LOGGER.warning("Starting integration with no initial connection - coordinator will retry")

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
    
    # Store coordinator for this entry
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Set up all platforms for this device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register static paths for icons
    should_cache = False
    icons_path = Path(__file__).parent / "www" / "icons"
    
    await hass.http.async_register_static_paths([
        StaticPathConfig("/api/meshcore/static", str(icons_path), should_cache)
    ])
    
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
            # Import the sanitize function for JSON serialization
            from .utils import sanitize_event_data
                
            # Fire event to HA event bus with sanitized payload
            _LOGGER.debug(f"Firing event to HA event bus: {event}")
            hass.bus.async_fire(f"{DOMAIN}_raw_event", {
                "event_type": event_type_str,
                "payload": sanitize_event_data(event.payload),
                "timestamp": time.time()
            })
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

                