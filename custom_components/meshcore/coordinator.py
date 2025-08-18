"""MeshCore data update coordinator."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from meshcore.events import EventType

from .const import (
    CONF_NAME,
    CONF_PUBKEY,
    DOMAIN,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_UPDATE_INTERVAL,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    DEFAULT_UPDATE_TICK,
    MAX_REPEATER_FAILURES_BEFORE_LOGIN,
    REPEATER_BACKOFF_BASE,
    REPEATER_BACKOFF_MAX_MULTIPLIER,
)
from .meshcore_api import MeshCoreAPI

_LOGGER = logging.getLogger(__name__)


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
    
        
    async def _update_repeater(self, repeater_config):
        """Update a repeater and schedule the next update.

        
        This runs as a separate task so it doesn't block the main update loop.
        If we fail to get stats multiple times, we'll try to login.
        """
        # add a random delay to avoid all repeaters updating at the same time
        # 0-5 seconds random delay
        random_delay = random.uniform(0, 5)
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
                self.logger.warn(f"Error requesting status from repeater {repeater_name}: {result}")
                # Increment failure count and apply backoff
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count)
            elif result.payload.get('uptime', 0) == 0:
                self.logger.warn(f"Malformed status response from repeater {repeater_name}: {result.payload}")
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count)
            else:
                self.logger.debug(f"Successfully updated repeater {repeater_name}")
                # Reset failure count on success
                self._repeater_consecutive_failures[pubkey_prefix] = 0
                
                # Trigger state updates for any entities listening for this repeater
                self.async_set_updated_data(self.data)
                
                # Schedule next update based on configured interval
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                next_update_time = time.time() + update_interval
                self._next_repeater_update_times[pubkey_prefix] = next_update_time
            
        except Exception as ex:
            self.logger.warn(f"Exception updating repeater {repeater_name}: {ex}")
            # Increment failure count and apply backoff
            new_failure_count = self._repeater_consecutive_failures.get(pubkey_prefix, 0) + 1
            self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
            self._apply_repeater_backoff(pubkey_prefix, new_failure_count)
        finally:
            # Remove this task from active tasks
            if pubkey_prefix in self._active_repeater_tasks:
                self._active_repeater_tasks.pop(pubkey_prefix)

    def _apply_repeater_backoff(self, pubkey_prefix: str, failure_count: int) -> None:
        """Apply exponential backoff delay for failed repeater updates.
        
        Uses DEFAULT_UPDATE_TICK as base since that's how often we check for updates.
        """
        backoff_multiplier = min(REPEATER_BACKOFF_BASE ** failure_count, REPEATER_BACKOFF_MAX_MULTIPLIER)
        backoff_delay = DEFAULT_UPDATE_TICK * backoff_multiplier
        next_update_time = time.time() + backoff_delay
        
        self._next_repeater_update_times[pubkey_prefix] = next_update_time
        
        self.logger.debug(f"Applied backoff for repeater {pubkey_prefix}: "
                         f"failure_count={failure_count}, "
                         f"multiplier={backoff_multiplier}, "
                         f"delay={backoff_delay}s")
                
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
        if contacts_result.type == EventType.CONTACTS:
            self._contacts = list(contacts_result.payload.values())
        else:
            self.logger.error(f"Failed to get contacts: {contacts_result.payload}")
            
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