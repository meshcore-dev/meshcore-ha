"""The MeshCore integration."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.components.http import StaticPathConfig

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_CONNECTION_TYPE,
    CONF_USB_PATH,
    CONF_BLE_ADDRESS,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_BAUDRATE,

    CONF_REPEATER_SUBSCRIPTIONS,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    CONF_INFO_INTERVAL,
    CONF_MESSAGES_INTERVAL,
    DEFAULT_INFO_INTERVAL,
    DEFAULT_MESSAGES_INTERVAL,
    NodeType,
)
from .meshcore_api import MeshCoreAPI
from .services import async_setup_services, async_unload_services
from .logbook import handle_log_message

_LOGGER = logging.getLogger(__name__)

# List of platforms to set up
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT, Platform.TEXT]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MeshCore from a config entry."""
    # Get configuration from entry
    connection_type = entry.data[CONF_CONNECTION_TYPE]
    
    # Create API instance based on connection type
    api_kwargs = {"connection_type": connection_type}
    
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
    
    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()
    
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
    
    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options for a config entry."""
    # Check if repeater subscriptions have changed and handle device removal
    old_subscriptions = entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
    
    # Reload the entry to apply the new options
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Remove entry from data
    if unload_ok and entry.entry_id in hass.data[DOMAIN]:
        # Get coordinator and disconnect
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.api.disconnect()
        
        # Remove entry
        hass.data[DOMAIN].pop(entry.entry_id)
        
        # If no more entries, unload services
        if not hass.data[DOMAIN]:
            await async_unload_services(hass)
    
    return unload_ok


class MeshCoreDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the MeshCore node."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta,
        api: MeshCoreAPI,
        config_entry: ConfigEntry = None,
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
        
        # Single map to track all message timestamps (key -> timestamp)
        # Keys can be channel indices (int) or public key prefixes (str)
        self.message_timestamps = {}
        
        # Repeater subscription tracking
        self._repeater_stats = {}
        self._repeater_login_times = {}
        
        # Room server ping tracking - hourly interval
        self._roomserver_ping_times = {}
        self._roomserver_ping_interval = 3600  # 1 hour in seconds
        
        # Helper method to create entities for new contacts
        async def _create_new_contact_entities(self, latest_contacts=None):
            """Create entities for newly discovered contacts."""
            self.logger.info(f"Create New Contact Entities with {len(latest_contacts) if latest_contacts else 0} contacts")
            
            # Binary sensor entities
            if hasattr(self, "create_binary_sensor_entities"):
                self.logger.info("Creating new binary sensor entities for contacts")
                self.create_binary_sensor_entities(latest_contacts)
                
            # Diagnostic sensor entities
            if hasattr(self, "create_contact_diagnostic_sensors"):
                self.logger.info("Creating new diagnostic sensor entities for contacts")
                self.create_contact_diagnostic_sensors(latest_contacts)
                
            # Contact diagnostic binary sensors
            if hasattr(self, "create_contact_diagnostic_binary_sensors"):
                self.logger.info("Creating new diagnostic binary sensors for contacts")
                self.create_contact_diagnostic_binary_sensors(latest_contacts)
                
        # Add the method to the class
        self._create_new_contact_entities = _create_new_contact_entities.__get__(self)
        
        # Track last update times for different data types
        self._last_info_update = 0  # Combined time for node info and contacts
        self._last_messages_update = 0
        self._last_repeater_updates = {}  # Dictionary to track per-repeater updates
        
        # Get interval settings from config (or use defaults)
        if config_entry:
            self._info_interval = config_entry.options.get(
                CONF_INFO_INTERVAL, DEFAULT_INFO_INTERVAL
            )
            self._messages_interval = config_entry.options.get(
                CONF_MESSAGES_INTERVAL, DEFAULT_MESSAGES_INTERVAL
            )
        else:
            # Use defaults if no config entry
            self._info_interval = DEFAULT_INFO_INTERVAL
            self._messages_interval = DEFAULT_MESSAGES_INTERVAL
        
        # For compatibility with older HA versions
        if not hasattr(self, "last_update_success_time"):
            import time
            self.last_update_success_time = time.time()
    
    async def _fetch_node_info(self, result_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch basic node information and battery status from the device."""
        self.logger.info("Fetching basic node info...")
        try:
            # Fetch basic node info
            node_info = await self.api.get_node_info()
            
            if node_info and isinstance(node_info, dict):
                # Update our data with node info
                for key, value in node_info.items():
                    result_data[key] = value
                
                # Log key node parameters
                node_name = node_info.get("name", "Unknown")
                self.logger.info(f"Node info updated: {node_name}")
            else:
                self.logger.warning("Could not get node info or empty response")
                
            # Fetch battery status
            try:
                self.logger.info("Fetching battery status...")
                battery_value = await self.api.get_battery()
                
                if battery_value is not None and battery_value > 0:
                    # Store battery value in raw form (divided by 10 in sensor.py)
                    result_data["bat"] = battery_value
                    self.logger.info(f"Battery status updated: {battery_value}")
                else:
                    self.logger.warning(f"Could not get battery status or invalid value: {battery_value}")
            except Exception as bat_ex:
                self.logger.error(f"Error getting battery status: {bat_ex}")
                # Continue without battery info
                
        except Exception as ex:
            self.logger.error(f"Error getting node info: {ex}")
            # Continue rather than failing completely
        
        return result_data
        
    async def _fetch_repeater_stats(self, result_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch stats from configured repeaters based on their individual update intervals."""
        if not self.config_entry:
            self.logger.debug("No config entry available, skipping repeater stats")
            return result_data
            
        # Get repeater subscriptions from config entry
        repeater_subscriptions = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        if not repeater_subscriptions:
            self.logger.debug("No repeater subscriptions configured, skipping repeater stats")
            return result_data
            
        self.logger.debug(f"Found {len(repeater_subscriptions)} repeater subscriptions to check")
            
        # Create a dictionary to store all repeater stats (including cached ones)
        all_repeater_stats = {}
        
        # Initialize repeater versions if needed
        if not hasattr(self, "_repeater_versions"):
            self._repeater_versions = {}
            # Also initialize version check timestamps
            self._last_version_checks = {}
            
        # Start with any existing stats we have
        if hasattr(self, "_repeater_stats") and self._repeater_stats:
            all_repeater_stats.update(self._repeater_stats)
        
        # Current time for interval calculations
        current_time = time.time()
        
        # Process each repeater subscription
        for repeater in repeater_subscriptions:
            # Skip disabled repeaters
            if not repeater.get("enabled", True):
                self.logger.debug(f"Skipping disabled repeater: {repeater.get('name')}")
                continue
                
            repeater_name = repeater.get("name")
            password = repeater.get("password", "")
            update_interval = repeater.get("update_interval", DEFAULT_REPEATER_UPDATE_INTERVAL)
            
            if not repeater_name:
                self.logger.warning(f"Skipping repeater with missing name: {repeater}")
                continue
                
            # Check if it's time to update this repeater based on its interval
            last_update = self._last_repeater_updates.get(repeater_name, 0)
            time_since_update = current_time - last_update
            
            # Skip update if not enough time has passed
            if time_since_update < update_interval and last_update > 0:
                self.logger.debug(
                    f"Skipping repeater {repeater_name} update - " +
                    f"last update was {time_since_update:.1f}s ago (interval: {update_interval}s)"
                )
                continue
                
            self.logger.info(
                f"Updating repeater {repeater_name} after {time_since_update:.1f}s " +
                f"(interval: {update_interval}s)"
            )
                
            # Check if we need to re-login (login times tracked per repeater)
            last_login_time = self._repeater_login_times.get(repeater_name, 0)
            # Re-login every hour (3600 seconds)
            login_interval = 3600
            time_since_login = current_time - last_login_time
            need_login = time_since_login > login_interval
            
            if need_login:
                self.logger.info(f"Login needed for {repeater_name} - last login was {time_since_login:.1f}s ago (limit: {login_interval}s)")
                # Password can be empty for guest login
                login_success = await self.api.login_to_repeater(repeater_name, password)
                
                if login_success:
                    self._repeater_login_times[repeater_name] = current_time
                    self.logger.info(f"Successfully logged in to repeater: {repeater_name}")
                    
                    # Only check version if a password was provided (admin permissions)
                    if password:
                        # Check if we need to fetch repeater version (on first login or daily)
                        # Daily version check (86400 seconds = 24 hours)
                        version_check_interval = 86400
                        last_version_check = self._last_version_checks.get(repeater_name, 0)
                        time_since_version_check = current_time - last_version_check
                        
                        # Get version on first login or if it's been more than a day
                        if repeater_name not in self._repeater_versions or time_since_version_check >= version_check_interval:
                            self.logger.info(f"Fetching version for repeater {repeater_name} (admin login)")
                            version = await self.api.get_repeater_version(repeater_name)
                            
                            if version:
                                self._repeater_versions[repeater_name] = version
                                self.logger.info(f"Updated version for repeater {repeater_name}: {version}")
                            else:
                                self.logger.warning(f"Could not get version for repeater {repeater_name}")
                                
                            # Update timestamp even on failure to avoid constant retries
                            self._last_version_checks[repeater_name] = current_time
                    else:
                        self.logger.debug(f"Skipping version check for repeater {repeater_name} (no admin password provided)")
                
                    # Check if this repeater is also a room server
                    is_roomserver = False
                    for contact_name, contact in self.api._cached_contacts.items():
                        if contact_name == repeater_name:
                            # Check if node type indicates it's a room server
                            if contact.get("type") == NodeType.ROOM_SERVER:
                                is_roomserver = True
                                break
                    
                    # If this is a room server, check if we need to send a ping
                    if is_roomserver:
                        last_ping_time = self._roomserver_ping_times.get(repeater_name, 0)
                        time_since_ping = current_time - last_ping_time
                        
                        # Only ping if the room server ping interval has passed
                        if time_since_ping >= self._roomserver_ping_interval:
                            self.logger.info(f"Sending room server ping to {repeater_name} (after {time_since_ping:.1f}s)")
                            await self.api.roomserver_ping(repeater_name)
                            self._roomserver_ping_times[repeater_name] = current_time
                        else:
                            self.logger.debug(f"Skipping room server ping to {repeater_name} - last ping was {time_since_ping:.1f}s ago")
                else:
                    self.logger.error(f"Failed to login to repeater: {repeater_name} - using password: {'yes' if password else 'no (guest)'}")
            else:
                self.logger.debug(f"No login needed for {repeater_name} - last login was {time_since_login:.1f}s ago")
            
            try:
                self.logger.info(f"Fetching stats from repeater: {repeater_name}")
                stats = await self.api.get_repeater_stats(repeater_name)
                
                if stats:
                    # Always add repeater_name and public_key to stats
                    stats["repeater_name"] = repeater_name
                    
                    # Find and add public key
                    for contact_name, contact in self.api._cached_contacts.items():
                        if contact_name == repeater_name:
                            public_key = contact.get("public_key", "")
                            stats["public_key"] = public_key
                            # Also add a shortened version for display
                            stats["public_key_short"] = public_key[:10] if public_key else ""
                            break
                    
                    # Add version info if available
                    if repeater_name in self._repeater_versions:
                        stats["version"] = self._repeater_versions[repeater_name]
                    
                    # Add the stats to our results
                    self._repeater_stats[repeater_name] = stats
                    all_repeater_stats[repeater_name] = stats
                    self.logger.info(f"Successfully updated stats for repeater: {repeater_name}")
                else:
                    self.logger.warning(f"No stats received for repeater: {repeater_name}")
            except Exception as ex:
                self.logger.error(f"Error fetching stats for repeater {repeater_name}: {ex}")
            
            self._last_repeater_updates[repeater_name] = current_time
        
        # Add all repeater stats to the result data
        if all_repeater_stats:
            result_data["repeater_stats"] = all_repeater_stats
            self.logger.debug(f"Added stats for {len(all_repeater_stats)} repeaters to result data")
            
        return result_data
    
    async def _fetch_contacts(self, result_data: Dict[str, Any], force_update: bool = False) -> Dict[str, Any]:
        """Fetch contacts list from the device.
        
        Args:
            result_data: The data dictionary to update
            force_update: Whether to force a contacts update regardless of schedule
        """
        self.logger.info("Fetching contacts list...")
        try:
            contacts = await self.api.get_contacts()
            
            if contacts and isinstance(contacts, dict):
                # Convert contacts dict to list
                contacts_list = []
                for name, data in contacts.items():
                    if isinstance(data, dict):
                        if "adv_name" not in data:
                            data["adv_name"] = name
                        contacts_list.append(data)
                
                # Check for new contacts
                if self._contacts:
                    new_contacts_found = False
                    for contact in contacts_list:
                        contact_name = contact.get("adv_name", "")
                        public_key = contact.get("public_key", "")
                        
                        # Skip contacts without identification
                        if not contact_name and not public_key:
                            continue
                            
                        # Check if this is a new contact
                        exists = False
                        for existing in self._contacts:
                            existing_name = existing.get("adv_name", "")
                            existing_key = existing.get("public_key", "")
                            
                            if (public_key and public_key == existing_key) or \
                                (contact_name and contact_name == existing_name):
                                exists = True
                                break
                                
                        if not exists:
                            self.logger.info(f"New contact discovered: {contact_name or public_key[:12]}")
                            new_contacts_found = True
                    
                    # If new contacts were found, trigger entity creation
                    if new_contacts_found:
                        # Schedule entity creation on the event loop
                        self.logger.info("Scheduling creation of entity sensors for new contacts")
                        self.hass.async_create_task(self._create_new_contact_entities())
                
                # Update our contact cache
                self._contacts = contacts_list
                result_data["contacts"] = contacts_list
                
                self.logger.info(f"Retrieved {len(contacts_list)} contacts")
            else:
                self.logger.info("No contacts found or empty response")
                result_data["contacts"] = []
        except Exception as ex:
            self.logger.error(f"Error getting contacts: {ex}")
            # Use previously cached contacts if any
            result_data["contacts"] = self._contacts

        
        return result_data
    
    async def _fetch_messages(self, result_data: Dict[str, Any]) -> None:
        """Fetch and process new messages from the device, logging them to the logbook."""
        self.logger.info("Checking for new messages...")
        try:
            new_messages = await self.api.get_new_messages()
            
            if new_messages and isinstance(new_messages, list) and len(new_messages) > 0:
                # Log message count
                self.logger.info(f"Found {len(new_messages)} new message(s)")
                
                # Process each message and add to our history
                for msg in new_messages:
                    # Extract key message details for logging
                    message_text = msg.get("text", "")
                    message_type = msg.get("type", "PRIV")  # Default to private message
                    
                    if message_type == "CHAN":
                        channel_idx = msg.get('channel_idx', 'Unknown')
                        message_source = f"Channel {channel_idx}"
                    else:
                        pubkey_prefix = msg.get('pubkey_prefix', 'Unknown')
                        message_source = f"Contact {pubkey_prefix}"
                    
                    # Log with more detail
                    self.logger.info(f"Message from {message_source}: {message_text}")
                    self.logger.debug(f"Full message: {msg}")
                    
                    # Add timestamp if not present
                    if "timestamp" not in msg:
                        msg["timestamp"] = int(time.time())
                    
                    # Log message to Home Assistant logbook
                    try:
                        handle_log_message(self.hass, msg)
                    except Exception as log_ex:
                        self.logger.error(f"Error logging message to logbook: {log_ex}")
                
            else:
                # No new messages found
                if new_messages is None or not isinstance(new_messages, list):
                    self.logger.warning("Invalid response from message check")
                else:
                    self.logger.debug("No new messages found")
                
                # No new messages to process
        except Exception as ex:
            self.logger.error(f"Error checking messages: {ex}")
            # Error fetching messages, but continue
    
    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data from the MeshCore node.
        
        Implementation with different polling intervals:
        1. Messages: Every update (DEFAULT_MESSAGES_INTERVAL - 10 seconds)
        2. Node info: Every _node_info_interval (60 seconds by default)
        3. Contacts: Every _contacts_interval (60 seconds by default)
        4. Repeater stats: Per-repeater based on their update_interval (300 seconds by default)
        """
        # Initialize result with previous data
        result_data = dict(self.data) if self.data else {
            "name": "MeshCore Node", 
            "contacts": []
        }
        
        # Track update count for debugging
        if not hasattr(self, "_update_count"):
            self._update_count = 0
        self._update_count += 1
        
        # Flag to track if this is the first update
        first_update = self._update_count == 1
        
        # Current time for interval calculations
        current_time = time.time()
        
        # Initialize contacts list if needed
        if not hasattr(self, "_contacts"):
            self._contacts = []
        
        try:
            # Reconnect if needed
            if not self.api._connected:
                self.logger.info("Connecting to device...")
                await self.api.disconnect()
                connection_success = await self.api.connect()
                if not connection_success:
                    self.logger.error("Failed to connect to MeshCore device")
                    raise UpdateFailed("Failed to connect to MeshCore device")
            
            # Always fetch on first update
            if first_update:
                self.logger.info("First update - fetching all data types")
                result_data = await self._fetch_node_info(result_data)
                result_data = await self._fetch_contacts(result_data, force_update=True)
                await self._fetch_messages(result_data)
                result_data = await self._fetch_repeater_stats(result_data)
                
                # Set initial update times
                self._last_info_update = current_time
                self._last_messages_update = current_time
                
                # Initialize repeater update tracking
                if self.config_entry:
                    repeaters = self.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
                    for repeater in repeaters:
                        repeater_name = repeater.get("name")
                        if repeater_name:
                            self._last_repeater_updates[repeater_name] = current_time
            else:
                # Conditional updates based on intervals
                
                # 1. Always check for messages (base update interval)
                time_since_messages = current_time - self._last_messages_update
                self.logger.debug(f"Time since last messages update: {time_since_messages:.1f}s (interval: {self._messages_interval}s)")
                await self._fetch_messages(result_data)
                self._last_messages_update = current_time
                
                # 2. Check node info and contacts if interval has passed
                time_since_info = current_time - self._last_info_update
                if time_since_info >= self._info_interval:
                    self.logger.debug(f"Fetching node info and contacts after {time_since_info:.1f}s (interval: {self._info_interval}s)")
                    # Fetch node info first
                    result_data = await self._fetch_node_info(result_data)
                    # Then fetch contacts
                    result_data = await self._fetch_contacts(result_data)
                    self._last_info_update = current_time
                else:
                    self.logger.debug(f"Skipping node info and contacts update - last update was {time_since_info:.1f}s ago")
                    # Use cached contacts
                    result_data["contacts"] = self._contacts
                
                # 4. Check repeater stats only for repeaters whose interval has passed
                await self._fetch_repeater_stats(result_data)
            
            # Always update last_update_success_time 
            self.last_update_success_time = current_time
            
            # Schedule entity creation for any new contacts
            if result_data.get("contacts"):
                # Check for new contacts since the last update
                if getattr(self, "_last_contacts_processed", None) != result_data["contacts"]:
                    self.logger.info("Contacts have changed, scheduling entity creation")
                    # Pass the current contacts list directly to the entity creation function
                    latest_contacts = result_data["contacts"]
                    self.hass.async_create_task(self._create_new_contact_entities(latest_contacts))
                    # Store the processed contacts to avoid duplicate processing
                    self._last_contacts_processed = result_data["contacts"].copy()
            
            return result_data
            
        except Exception as err:
            self.logger.error(f"Error during update: {err}")
            
            # If we have previous data, return that instead of failing
            if self.data:
                return self.data
                
            # Minimal fallback data
            return {
                "name": "MeshCore Node",
                "contacts": self._contacts
            }