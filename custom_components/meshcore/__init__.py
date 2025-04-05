"""The MeshCore integration."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict
import time
import asyncio
from webbrowser import get
from meshcore.events import EventType
from .const import (
    CONF_REPEATER_PASSWORD, 
    CONF_REPEATER_UPDATE_INTERVAL,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    MAX_REPEATER_FAILURES_BEFORE_LOGIN,
    NodeType
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.components.http import StaticPathConfig

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_NAME,
    CONF_PUBKEY,
    DOMAIN,
    CONF_CONNECTION_TYPE,
    CONF_USB_PATH,
    CONF_BLE_ADDRESS,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_BAUDRATE,

    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_MESSAGES_INTERVAL,
    DEFAULT_MESSAGES_INTERVAL,
    NodeType,
)
from .meshcore_api import MeshCoreAPI
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

# List of platforms to set up
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.TEXT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MeshCore from a config entry."""
    # Get configuration from entry
    connection_type = entry.data[CONF_CONNECTION_TYPE]
    
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
    await api.connect()
    
    # Get the messages interval for base update frequency
    # Check options first, then data, then use default
    messages_interval = entry.options.get(
        CONF_MESSAGES_INTERVAL, 
        entry.data.get(CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL)
    )
    
    # Create update coordinator with the messages interval (fastest polling rate)
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
    def forward_all_events(event):
        """Forward all MeshCore events to Home Assistant event bus."""
        if not event:
            return
            
        # Convert event type to string if possible
        event_type_str = str(event.type) if hasattr(event, "type") else "UNKNOWN"
        
        # Import the sanitize function for JSON serialization
        from .utils import sanitize_event_data
            
        # Fire event to HA event bus with sanitized payload
        hass.bus.async_fire(f"{DOMAIN}_raw_event", {
            "event_type": event_type_str,
            "payload": sanitize_event_data(event.payload),
            "timestamp": time.time()
        })
        
    # Add the all-events listener
    if coordinator.api.mesh_core:
        _LOGGER.info("Setting up all-events subscriber for MeshCore")
        coordinator.api.mesh_core.subscribe(
            None,
            forward_all_events
        )
    
    # Fetch initial data immediately
    await coordinator._async_update_data()
    
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
        
        # If no more entries, unload services
        if not hass.data[DOMAIN]:
            await async_unload_services(hass)
    
    return unload_ok


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
        self._contacts = []
        # Get name and pubkey from config_entry.data (not options)
        self.name = config_entry.data.get(CONF_NAME)
        self.pubkey = config_entry.data.get(CONF_PUBKEY)
        
        # Set up device info that entities can reference
        self._firmware_version = None
        self._hardware_model = None
        
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
        
        # Repeater subscription tracking
        self._tracked_repeaters = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        self._repeater_stats = {}
        self._repeater_login_times = {}
        self._next_repeater_update_times = {}  # Track when each repeater should next be updated
        self._active_repeater_tasks = {}  # Track active update tasks by pubkey_prefix
        self._repeater_consecutive_failures = {}  # Track consecutive failed updates by pubkey_prefix
        
        # Track connected state
        self._is_connected = False
        
        # Register listener for connection state changes
        if hass:
            self._remove_listeners = [
                hass.bus.async_listen(f"{DOMAIN}_connected", self._handle_connected),
                hass.bus.async_listen(f"{DOMAIN}_disconnected", self._handle_disconnected)
            ]
            
        # Initialize tracking sets for entities
        self.tracked_contacts = set()
        self.tracked_diagnostic_binary_contacts = set()
        self.channels_added = False
        
        # Track last update times for different data types
        self._last_repeater_updates = {}  # Dictionary to track per-repeater updates
        self._contacts = []
        
        # Initialization tracking flags
        self._appstart_initialized = False
        self._device_info_initialized = False
        
        if not hasattr(self, "last_update_success_time"):
            self.last_update_success_time = time.time()
        
    async def _handle_connected(self, event):
        """Handle connected event."""
        self._is_connected = True
        self.logger.info("MeshCore device connected")
        
    async def _handle_disconnected(self, event):
        """Handle disconnected event."""
        self._is_connected = False
        # Reset initialization flags so we'll re-initialize on reconnection
        self._appstart_initialized = False
        self._device_info_initialized = False
        
        # Cancel any active repeater update tasks
        for task in self._active_repeater_tasks.values():
            if not task.done():
                task.cancel()
        self._active_repeater_tasks.clear()
        
        self.logger.info("MeshCore device disconnected")
        
    async def _update_repeater(self, repeater_config):
        """Update a repeater and schedule the next update.
        
        This runs as a separate task so it doesn't block the main update loop.
        If we fail to get stats multiple times, we'll try to login.
        """
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
            
            # If we've failed multiple times, try a login first
            if failure_count >= MAX_REPEATER_FAILURES_BEFORE_LOGIN:
                self.logger.info(f"Attempting login to repeater {repeater_name} after {failure_count} failures")
                
                try:
                    login_result = await self.api.mesh_core.commands.send_login(
                        contact, 
                        repeater_config.get(CONF_REPEATER_PASSWORD, "")
                    )
                    
                    if login_result.type == EventType.ERROR:
                        self.logger.error(f"Login to repeater {repeater_name} failed: {login_result.payload}")
                    else:
                        self.logger.info(f"Successfully logged in to repeater {repeater_name}")
                
                except Exception as ex:
                    self.logger.error(f"Exception during login to repeater {repeater_name}: {ex}")
                
                # Reset failures after login attempt regardless of outcome
                # This prevents repeated login attempts if they keep failing
                self._repeater_consecutive_failures[pubkey_prefix] = 0
            
            # Request status from the repeater
            self.logger.debug(f"Sending status request to repeater: {repeater_name} ({pubkey_prefix})")
            await self.api.mesh_core.commands.send_statusreq(contact)
            result = await self.api.mesh_core.wait_for_event(
                EventType.STATUS_RESPONSE,
                attribute_filters={"pubkey_prefix": pubkey_prefix},
            )
            _LOGGER.debug(f"Status response received: {result}")
                
            # Handle response
            if not result:
                self.logger.error(f"Error requesting status from repeater {repeater_name}: {result}")
                # Increment failure count
                self._repeater_consecutive_failures[pubkey_prefix] = failure_count + 1
            elif result.payload.get('uptime', 0) == 0:
                self.logger.error(f"Malformed status response from repeater {repeater_name}: {result.payload}")
                self._repeater_consecutive_failures[pubkey_prefix] = failure_count + 1
            else:
                self.logger.debug(f"Successfully updated repeater {repeater_name}")
                # Reset failure count on success
                self._repeater_consecutive_failures[pubkey_prefix] = 0
                
                # Trigger state updates for any entities listening for this repeater
                self.async_set_updated_data(self.data)
                
                # Only schedule next update on success
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                next_update_time = time.time() + update_interval
                self._next_repeater_update_times[pubkey_prefix] = next_update_time
            
        except Exception as ex:
            self.logger.error(f"Exception updating repeater {repeater_name}: {ex}")
            # Increment failure count
            failure_count = self._repeater_consecutive_failures.get(pubkey_prefix, 0)
            self._repeater_consecutive_failures[pubkey_prefix] = failure_count + 1
        finally:
            # Remove this task from active tasks
            if pubkey_prefix in self._active_repeater_tasks:
                self._active_repeater_tasks.pop(pubkey_prefix)

                
    # function just looks at update intervals and triggers commands
    async def _async_update_data(self) -> None:
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
        current_time = time.time()
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
            self._appstart_initialized = False
            self._device_info_initialized = False
            connection_success = await self.api.connect()
            if not connection_success:
                self.logger.error("Failed to connect to MeshCore device")
                raise UpdateFailed("Failed to connect to MeshCore device")

        # Only send appstart if not initialized or after reconnection
        if not self._appstart_initialized:
            self.logger.info("Initializing app start...")
            await self.api.mesh_core.commands.send_appstart()
            self._appstart_initialized = True
        
        # Always get battery status
        await self.api.mesh_core.commands.get_bat()
        
        # Fetch device info if we don't have it yet or don't have complete info
        if not self._device_info_initialized:
            try:
                self.logger.info("Fetching device info...")
                device_query_result = await self.api.mesh_core.commands.send_device_query()
                if device_query_result.type is EventType.DEVICE_INFO:
                    self._firmware_version = device_query_result.payload.get("ver")
                    self._hardware_model = device_query_result.payload.get("model")
                    
                    if self._firmware_version:
                        self.device_info["sw_version"] = self._firmware_version
                    if self._hardware_model:
                        self.device_info["model"] = self._hardware_model
                        
                    self.logger.info(f"Device info updated - Firmware: {self._firmware_version}, Model: {self._hardware_model}")
                    self._device_info_initialized = True
                    
                    self.async_update_listeners()
            except Exception as ex:
                self.logger.error(f"Error fetching device info: {ex}")
        
        # Get contacts - no need to process, just store in data
        contacts_result = await self.api.mesh_core.commands.get_contacts()
        
        # Convert contacts to list and store
        if contacts_result and hasattr(contacts_result, "payload"):
            self._contacts = list(contacts_result.payload.values())
            
        # Store contacts in result data
        result_data["contacts"] = self._contacts

        # Check for messages
        _LOGGER.info("Clearing message queue...")
        try:
            res = True
            while res:
                result = await self.api.mesh_core.commands.get_msg()
                if result.type == EventType.NO_MORE_MSGS:
                    res = False
                    _LOGGER.debug("No more messages in queue")
                elif result.type == EventType.ERROR:
                    res = False
                    _LOGGER.error(f"Error retrieving messages: {result.payload}")
                else:
                    _LOGGER.debug(f"Cleared message: {result}")
        except Exception as ex:
            _LOGGER.error(f"Error clearing message queue: {ex}")
        
        for repeater_config in self._tracked_repeaters:
            if not repeater_config.get('name') or not repeater_config.get('pubkey_prefix'):
                _LOGGER.warning(f"Repeater config missing name or pubkey_prefix: {repeater_config}")
                continue
                
            pubkey_prefix = repeater_config.get("pubkey_prefix")
            repeater_name = repeater_config.get("name")
            
            # Clean up completed or failed tasks
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
                