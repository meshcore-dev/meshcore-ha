"""MeshCore data update coordinator."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from typing import Any, Dict

from cachetools import TTLCache

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store

from meshcore.events import Event, EventType
from meshcore.packets import BinaryReqType

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
    CONF_DEVICE_DISABLED,
    AUTO_DISABLE_HOURS,
    RATE_LIMITER_CAPACITY,
    RATE_LIMITER_REFILL_RATE_SECONDS,
    RX_LOG_CACHE_MAX_SIZE,
    RX_LOG_CACHE_TTL_SECONDS,
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
        self._discovered_contacts = {}  # Dict keyed by public_key
        self._manual_mode_initialized = False

        # Storage for discovered contacts
        self._store = Store[dict[str, dict]](hass, 1, f"meshcore.{config_entry.entry_id}.discovered_contacts")
        # Get name and pubkey from config_entry.data (not options)
        self.name = config_entry.data.get(CONF_NAME)
        self.pubkey = config_entry.data.get(CONF_PUBKEY)
        
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
        self._device_info_initialized = False

        # Telemetry sensor manager - will be initialized when sensors are set up
        self.telemetry_manager = None

        # Track coordinator start time for auto-disable logic
        self._coordinator_start_time = time.time()

        # RX_LOG correlation cache: auto-evicts after TTL expires
        # Key: correlation hash, Value: list of RX_LOG data (multiple receptions possible)
        self._pending_rx_logs = TTLCache(
            maxsize=RX_LOG_CACHE_MAX_SIZE,
            ttl=RX_LOG_CACHE_TTL_SECONDS
        )

        if not hasattr(self, "last_update_success_time"):
            self.last_update_success_time = self._current_time()

        # Initialize reliability stats tracking
        self._reliability_stats = {}

        # Dirty contacts tracking for performance optimization
        # Set of pubkey prefixes that have been updated and need sensor refresh
        self._dirty_contacts = set()

    def mark_contact_dirty(self, pubkey_prefix: str):
        """Mark a contact as needing update (for performance optimization)."""
        if pubkey_prefix:
            self._dirty_contacts.add(pubkey_prefix)

    def is_contact_dirty(self, pubkey_prefix: str) -> bool:
        """Check if a contact needs update."""
        return pubkey_prefix in self._dirty_contacts

    def clear_contact_dirty(self, pubkey_prefix: str):
        """Clear dirty flag after updating contact sensor."""
        self._dirty_contacts.discard(pubkey_prefix)

    def get_all_contacts(self) -> list:
        """Get deduplicated list of all contacts (added + discovered).

        For each public_key, uses the contact with the latest lastmod.
        Marks as added_to_node=True if contact exists in added list.
        """
        contacts_dict = {}

        # Build set of public keys that are in added contacts
        added_pubkeys = set(c.get("public_key") for c in self._contacts if c.get("public_key"))

        # Process all contacts (discovered + added)
        all_contacts = list(self._discovered_contacts.values()) + self._contacts

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
    
    async def _fetch_all_channel_info(self) -> None:
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
                    login_result = await self.api.mesh_core.commands.send_login(
                        contact,
                        repeater_config.get(CONF_REPEATER_PASSWORD, "")
                    )
                    
                    if login_result and login_result.type == EventType.LOGIN_SUCCESS:
                        self.logger.info(f"Successfully logged in to repeater {repeater_name}")
                        self._increment_success(pubkey_prefix)
                        # Track login time and reset failure count on success
                        self._repeater_login_times[pubkey_prefix] = self._current_time()
                        self._repeater_consecutive_failures[pubkey_prefix] = 0
                    else:
                        error_msg = login_result.payload if login_result and login_result.type == EventType.ERROR else "timeout or no response"
                        self.logger.error(f"Login to repeater {repeater_name} failed: {error_msg}")
                        self._increment_failure(pubkey_prefix)
                        # Update login time to enforce cooldown even on failure
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

            await self.api.mesh_core.commands.send_binary_req(contact, BinaryReqType.STATUS)
            result = await self.api.mesh_core.wait_for_event(
                EventType.STATUS_RESPONSE,
                attribute_filters={"pubkey_prefix": pubkey_prefix},
            )
            _LOGGER.debug(f"Status response received: {result}")
                
            # Handle response
            if not result or result.type == EventType.ERROR:
                self.logger.warn(f"Error requesting status from repeater {repeater_name}: {result}")
                # Increment failure count and apply backoff
                new_failure_count = failure_count + 1
                self._repeater_consecutive_failures[pubkey_prefix] = new_failure_count
                self._increment_failure(pubkey_prefix)

                # Reset path after configured failures if there's an established path
                if new_failure_count >= MAX_FAILURES_BEFORE_PATH_RESET and contact and contact.get("out_path_len", -1) > -1:
                    await self._reset_node_path(contact, repeater_config)

                update_interval = repeater_config.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
                self._apply_repeater_backoff(pubkey_prefix, new_failure_count, update_interval)
            elif result.payload.get('uptime', 0) == 0:
                self.logger.warn(f"Malformed status response from repeater {repeater_name}: {result.payload}")
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

            await self.api.mesh_core.commands.send_binary_req(contact, BinaryReqType.TELEMETRY)
            telemetry_result = await self.api.mesh_core.wait_for_event(
                EventType.TELEMETRY_RESPONSE,
                attribute_filters={"pubkey_prefix": pubkey_prefix},
            )
            
            if telemetry_result and telemetry_result.type != EventType.ERROR:
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
            self.logger.warn(f"Exception requesting telemetry from node {node_name}: {ex}")
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
                    await self._fetch_all_channel_info()

                    self.async_update_listeners()
            except Exception as ex:
                self.logger.error(f"Error fetching device info: {ex}")
        
        # Sync contacts if dirty (uses SDK's internal dirty flag)
        try:
            contacts_changed = await self.api.mesh_core.ensure_contacts(follow=True)
            if contacts_changed:
                self.logger.info("Contacts synced from node")
            # Always read from meshcore's in-memory list
            self._contacts = list(self.api.mesh_core.contacts.values())
        except Exception as ex:
            self.logger.error(f"Error syncing contacts: {ex}")

        # Store combined contacts (added + discovered) in result data
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