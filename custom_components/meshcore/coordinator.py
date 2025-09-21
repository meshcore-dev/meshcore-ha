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
from meshcore.packets import BinaryReqType

from .const import (
    CONF_NAME,
    CONF_PUBKEY,
    DOMAIN,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_UPDATE_INTERVAL,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    CONF_TRACKED_CLIENTS,
    CONF_CLIENT_UPDATE_INTERVAL,
    DEFAULT_CLIENT_UPDATE_INTERVAL,
    DEFAULT_UPDATE_TICK,
    MAX_REPEATER_FAILURES_BEFORE_LOGIN,
    REPEATER_BACKOFF_BASE,
    REPEATER_BACKOFF_MAX_MULTIPLIER,
    CONF_CONTACT_REFRESH_INTERVAL,
    DEFAULT_CONTACT_REFRESH_INTERVAL,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_INTERVAL,
    DEFAULT_SELF_TELEMETRY_INTERVAL,
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
        
        # Tracked clients tracking (no login needed, uses ACLs)
        self._tracked_clients = self.config_entry.data.get(CONF_TRACKED_CLIENTS, [])
        
        
        # Initialize tracking sets for entities
        self.tracked_contacts = set()
        self.tracked_diagnostic_binary_contacts = set()
        self.channels_added = False
        
        # Track last update times for different data types
        self._last_repeater_updates = {}  # Dictionary to track per-repeater updates
        self._contacts = []
        self._last_contact_refresh = 0  # Track when contacts were last refreshed
        
        # Self telemetry tracking
        self._last_self_telemetry_update = 0
        self._self_telemetry_enabled = config_entry.data.get(CONF_SELF_TELEMETRY_ENABLED, False)
        self._self_telemetry_interval = config_entry.data.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL)
        
        # Telemetry tracking - separate from repeater/client specific logic
        self._next_telemetry_update_times = {}  # Track when each node should have telemetry updated
        self._active_telemetry_tasks = {}  # Track active telemetry tasks by pubkey_prefix
        self._telemetry_consecutive_failures = {}  # Track consecutive failed telemetry updates by pubkey_prefix
        
        # Initialization tracking flags
        self._appstart_initialized = False
        self._device_info_initialized = False
        
        # Telemetry sensor manager - will be initialized when sensors are set up
        self.telemetry_manager = None
        
        if not hasattr(self, "last_update_success_time"):
            self.last_update_success_time = self._current_time()
    
    def update_telemetry_settings(self, config_entry: ConfigEntry) -> None:
        """Update telemetry settings from config entry."""
        self._self_telemetry_enabled = config_entry.data.get(CONF_SELF_TELEMETRY_ENABLED, False)
        self._self_telemetry_interval = config_entry.data.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL)
        self._tracked_repeaters = config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        self._tracked_clients = config_entry.data.get(CONF_TRACKED_CLIENTS, [])
        _LOGGER.debug(f"Updated telemetry settings - Enabled: {self._self_telemetry_enabled}, Interval: {self._self_telemetry_interval}, Tracked clients: {len(self._tracked_clients)}")

    def _current_time(self) -> int:
        """Return current time as integer seconds since epoch."""
        return int(time.time())
    
        
    async def _update_repeater(self, repeater_config):
        """Update a repeater and schedule the next update.

        
        This runs as a separate task so it doesn't block the main update loop.
        If we fail to get stats multiple times, we'll try to login.
        """
        # add a random delay to avoid all repeaters updating at the same time
        # 0-5 seconds random delay
        random_delay = random.uniform(0, 5000)
        await asyncio.sleep(random_delay / 1000)

        
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
            
            # Check if we need to login (initial login or after failures)
            last_login_time = self._repeater_login_times.get(pubkey_prefix)
            needs_initial_login = last_login_time is None
            needs_failure_recovery = failure_count >= MAX_REPEATER_FAILURES_BEFORE_LOGIN
            
            if needs_initial_login or needs_failure_recovery:
                if needs_initial_login:
                    self.logger.info(f"Attempting initial login to repeater {repeater_name}")
                else:
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
                        # Track login time for telemetry refresh
                        self._repeater_login_times[pubkey_prefix] = self._current_time()
                
                except Exception as ex:
                    self.logger.error(f"Exception during login to repeater {repeater_name}: {ex}")
                
                # Reset failures after login attempt regardless of outcome
                # This prevents repeated login attempts if they keep failing
                self._repeater_consecutive_failures[pubkey_prefix] = 0
            
            # Request status from the repeater
            self.logger.debug(f"Sending status request to repeater: {repeater_name} ({pubkey_prefix})")
            await self.api.mesh_core.commands.send_binary_req(contact, BinaryReqType.STATUS)
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
                
                # Reset path after 5 failures if there's an established path
                if new_failure_count == 5 and contact and contact.get("out_path_len", 0) != -1:
                    try:
                        await self.api.mesh_core.commands.reset_path(pubkey_prefix)
                        self.logger.info(f"Reset path for repeater {repeater_name} after 5 failures")
                    except Exception as ex:
                        self.logger.warning(f"Failed to reset path for repeater {repeater_name}: {ex}")
                
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
            elif result.payload.get('uptime', 0) == 0:
                self.logger.warn(f"Malformed status response from repeater {repeater_name}: {result.payload}")
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
            else:
                self.logger.debug(f"Successfully updated repeater {repeater_name}")
                # Reset failure count on success
                self._repeater_consecutive_failures[pubkey_prefix] = 0
                
                # Trigger state updates for any entities listening for this repeater
                self.async_set_updated_data(self.data)
                
                # Schedule next update based on configured interval
                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                next_update_time = self._current_time() + update_interval
                self._next_repeater_update_times[pubkey_prefix] = next_update_time
            
        except Exception as ex:
            self.logger.warn(f"Exception updating repeater {repeater_name}: {ex}")
            # Increment failure count and apply backoff
            new_failure_count = self._repeater_consecutive_failures.get(pubkey_prefix, 0) + 1
            self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
            update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
            self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
        finally:
            # Remove this task from active tasks
            if pubkey_prefix in self._active_repeater_tasks:
                self._active_repeater_tasks.pop(pubkey_prefix)

    def _apply_backoff(self, pubkey_prefix: str, failure_count: int, update_interval: int, update_type: str = "repeater") -> None:
        """Apply exponential backoff delay for failed updates.
        
        Args:
            pubkey_prefix: The node's public key prefix
            failure_count: Number of consecutive failures
            update_interval: The configured update interval to cap the backoff at
            update_type: Type of update ("repeater" or "telemetry")
        """
        backoff_delay = min(REPEATER_BACKOFF_BASE ** failure_count, update_interval)
        next_update_time = self._current_time() + backoff_delay
        
        if update_type == "telemetry":
            self._next_telemetry_update_times[pubkey_prefix] = next_update_time
        else:
            self._next_repeater_update_times[pubkey_prefix] = next_update_time
        
        self.logger.debug(f"Applied backoff for {update_type} {pubkey_prefix}: "
                         f"failure_count={failure_count}, "
                         f"delay={backoff_delay}s, "
                         f"interval_cap={update_interval}s")

    def _apply_repeater_backoff(self, pubkey_prefix: str, failure_count: int, update_interval: int) -> None:
        """Apply exponential backoff delay for failed repeater updates."""
        self._apply_backoff(pubkey_prefix, failure_count, update_interval, "repeater")

    async def _update_node_telemetry(self, contact, pubkey_prefix: str, node_name: str, update_interval: int):
        """Update telemetry for a node (repeater or client).
        
        This is a separate method that can be used by both repeater and client update logic.
        Assumes repeater login has already been handled by status update logic.
        """
        # Get current failure count
        failure_count = self._telemetry_consecutive_failures.get(pubkey_prefix, 0)

        # add a random delay to avoid all updating at the same time
        # 0-5 seconds random delay
        random_delay = random.uniform(0, 5000)
        await asyncio.sleep(random_delay / 1000)
        
        try:
            self.logger.debug(f"Sending telemetry request to node: {node_name} ({pubkey_prefix})")
            await self.api.mesh_core.commands.send_binary_req(contact, BinaryReqType.TELEMETRY)
            telemetry_result = await self.api.mesh_core.wait_for_event(
                EventType.TELEMETRY_RESPONSE,
                attribute_filters={"pubkey_prefix": pubkey_prefix},
            )
            
            if telemetry_result:
                self.logger.debug(f"Telemetry response received from {node_name}: {telemetry_result}")
                # Reset failure count on success
                self._telemetry_consecutive_failures[pubkey_prefix] = 0
                # Schedule next telemetry update
                next_telemetry_time = self._current_time() + update_interval
                self._next_telemetry_update_times[pubkey_prefix] = next_telemetry_time
            else:
                self.logger.debug(f"No telemetry response received from {node_name}")
                # Increment failure count and apply backoff
                new_failure_count = failure_count + 1
                self._telemetry_consecutive_failures[pubkey_prefix] = new_failure_count
                
                # Reset path after 5 failures if there's an established path
                if new_failure_count == 5 and contact and contact.get("out_path_len", 0) != -1:
                    try:
                        await self.api.mesh_core.commands.reset_path(pubkey_prefix)
                        self.logger.info(f"Reset path for node {node_name} after 5 telemetry failures")
                    except Exception as ex:
                        self.logger.warning(f"Failed to reset path for node {node_name}: {ex}")
                
                self._apply_backoff(pubkey_prefix, new_failure_count, update_interval, "telemetry")
                
        except Exception as ex:
            self.logger.warn(f"Exception requesting telemetry from node {node_name}: {ex}")
            # Increment failure count and apply backoff
            new_failure_count = failure_count + 1
            self._telemetry_consecutive_failures[pubkey_prefix] = new_failure_count
            
            # Reset path after 5 failures if there's an established path
            if new_failure_count == 5 and contact and contact.get("out_path_len", 0) != -1:
                try:
                    await self.api.mesh_core.commands.reset_path(pubkey_prefix)
                    self.logger.info(f"Reset path for node {node_name} after 5 telemetry failures")
                except Exception as reset_ex:
                    self.logger.warning(f"Failed to reset path for node {node_name}: {reset_ex}")
            
            self._apply_backoff(pubkey_prefix, new_failure_count, update_interval, "telemetry")
        finally:
            # Remove this task from active telemetry tasks
            if pubkey_prefix in self._active_telemetry_tasks:
                self._active_telemetry_tasks.pop(pubkey_prefix)
                
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
        
        # Get contacts based on refresh interval
        contact_refresh_interval = self.config_entry.options.get(CONF_CONTACT_REFRESH_INTERVAL, DEFAULT_CONTACT_REFRESH_INTERVAL)
        
        if current_time - self._last_contact_refresh >= contact_refresh_interval:
            self.logger.debug(f"Refreshing contacts (interval: {contact_refresh_interval}s)")
            contacts_result = await self.api.mesh_core.commands.get_contacts()
            
            # Convert contacts to list and store
            if contacts_result.type == EventType.CONTACTS:
                self._contacts = list(contacts_result.payload.values())
                self._last_contact_refresh = current_time
            else:
                self.logger.error(f"Failed to get contacts: {contacts_result.payload}")
        else:
            self.logger.debug(f"Skipping contact refresh (next in {contact_refresh_interval - (current_time - self._last_contact_refresh):.1f}s)")
            
        # Store contacts in result data
        result_data["contacts"] = self._contacts

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

        # Check and update telemetry for nodes that have it enabled
        _LOGGER.debug("Checking telemetry for tracked repeaters: %s", self._next_telemetry_update_times)
        for repeater_config in self._tracked_repeaters:
            if not repeater_config.get('name') or not repeater_config.get('pubkey_prefix'):
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
                        self._update_node_telemetry(contact, pubkey_prefix, repeater_name, update_interval)
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
                    
                    update_interval = client_config.get("update_interval", DEFAULT_CLIENT_UPDATE_INTERVAL)
                    
                    telemetry_task = asyncio.create_task(
                        self._update_node_telemetry(contact, pubkey_prefix, client_name, update_interval)
                    )
                    self._active_telemetry_tasks[pubkey_prefix] = telemetry_task
                    telemetry_task.set_name(f"client_telemetry_{client_name}")
                else:
                    _LOGGER.warning(f"Could not find contact for client telemetry request: {pubkey_prefix}")