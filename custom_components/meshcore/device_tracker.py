"""Device tracker platform for MeshCore integration."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict
from meshcore import EventType
from meshcore.events import Event

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_REPEATER_SUBSCRIPTIONS, CONF_TRACKED_CLIENTS
from .utils import (
    sanitize_name, 
    format_entity_id, 
    build_device_name, 
    get_device_model, 
    build_device_id
)
from . import MeshCoreDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


class DeviceTrackerManager:
    """Manages dynamic creation and updates of device trackers for GPS telemetry."""
    
    def __init__(self, coordinator: MeshCoreDataUpdateCoordinator, async_add_entities: AddEntitiesCallback):
        self.coordinator = coordinator
        self.async_add_entities = async_add_entities
        self.discovered_trackers = {}  # Track discovered device trackers by unique key
        
    async def setup_gps_listener(self):
        """Set up the GPS telemetry event listener."""
        if not self.coordinator.api.mesh_core:
            _LOGGER.warning("No MeshCore instance available for GPS device tracker setup")
            return
            
        self.coordinator.api.mesh_core.subscribe(
            EventType.TELEMETRY_RESPONSE,
            self._handle_gps_telemetry_event
        )
        _LOGGER.debug("GPS device tracker manager initialized")
        
    async def _handle_gps_telemetry_event(self, event: Event):
        """Handle incoming telemetry events and discover new GPS trackers."""
        _LOGGER.debug(f"Received telemetry event for GPS tracking: {event}")
        
        if not event.payload or "lpp" not in event.payload:
            _LOGGER.debug("No LPP data in telemetry event")
            return

        pubkey_prefix = event.payload.get("pubkey_prefix", "")
        lpp_data = event.payload.get("lpp", [])
        
        # If no pubkey_prefix, this might be self telemetry
        if not pubkey_prefix:
            if "lpp" in event.payload and self.coordinator.pubkey:
                pubkey_prefix = self.coordinator.pubkey[:12]
                _LOGGER.debug(f"Self GPS telemetry detected, using coordinator pubkey: {pubkey_prefix}")
            else:
                _LOGGER.warning("GPS telemetry event missing pubkey_prefix and not self telemetry")
                return
            
        node_info = self._get_node_info(pubkey_prefix)
        
        gps_data = None
        for channel_data in lpp_data:
            if channel_data.get("type") == 'gps':
                gps_data = channel_data
                break
                
        if not gps_data:
            return
            
        gps_value = gps_data.get("value")
        
        if not isinstance(gps_value, dict) or "latitude" not in gps_value or "longitude" not in gps_value:
            _LOGGER.warning(f"Invalid GPS data format: {gps_value}")
            return
            
        tracker_key = f"{pubkey_prefix}_gps"
        
        if tracker_key not in self.discovered_trackers:
            tracker = MeshCoreGPSTracker(
                self.coordinator, pubkey_prefix, node_info
            )
            self.discovered_trackers[tracker_key] = tracker
            self.async_add_entities([tracker])
            _LOGGER.info(f"Discovered new GPS tracker: {tracker.name} ({tracker_key})")
        else:
            tracker = self.discovered_trackers[tracker_key]
            
        tracker.update_gps_location(gps_value)
                
    def _get_node_info(self, pubkey_prefix: str) -> Dict[str, Any]:
        """Get node information for smart naming."""
        # Check if this is a tracked repeater
        repeater_subscriptions = self.coordinator.config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
        for repeater in repeater_subscriptions:
            if repeater.get("pubkey_prefix", "").startswith(pubkey_prefix):
                return {
                    "name": repeater.get("name"),
                    "type": "repeater",
                    "pubkey_prefix": repeater.get("pubkey_prefix")
                }
                
        # Check if this is a tracked client
        tracked_clients = self.coordinator.config_entry.data.get(CONF_TRACKED_CLIENTS, [])
        for client in tracked_clients:
            if client.get("pubkey_prefix", "").startswith(pubkey_prefix):
                return {
                    "name": client.get("name"),
                    "type": "client", 
                    "pubkey_prefix": client.get("pubkey_prefix")
                }
                
        # Check if this is the root node
        coordinator_pubkey = self.coordinator.pubkey or ""
        if coordinator_pubkey.startswith(pubkey_prefix):
            return {
                "name": self.coordinator.name or "Root Node",
                "type": "root",
                "pubkey_prefix": coordinator_pubkey
            }
            
        # Default to unknown contact
        contacts = self.coordinator.data.get("contacts", [])
        for contact in contacts:
            contact_pubkey = contact.get("public_key", {}).get("hex", "")
            if contact_pubkey.startswith(pubkey_prefix):
                return {
                    "name": contact.get("name", f"Node {pubkey_prefix[:6]}"),
                    "type": "contact",
                    "pubkey_prefix": contact_pubkey
                }
                
        return {
            "name": f"Unknown Node {pubkey_prefix[:6]}",
            "type": "unknown",
            "pubkey_prefix": pubkey_prefix
        }


class MeshCoreGPSTracker(CoordinatorEntity, TrackerEntity):
    """GPS device tracker for mesh nodes."""
    
    def __init__(
        self,
        coordinator: MeshCoreDataUpdateCoordinator,
        pubkey_prefix: str,
        node_info: Dict[str, Any],
    ) -> None:
        """Initialize the GPS tracker."""
        super().__init__(coordinator)
        self.pubkey_prefix = pubkey_prefix
        self.node_info = node_info
        
        # Set up naming based on node type
        node_name = node_info.get("name", f"Node {pubkey_prefix[:6]}")
        node_type = node_info.get("type", "unknown")
        full_pubkey = node_info.get("pubkey_prefix", pubkey_prefix)
        
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{pubkey_prefix}_gps_tracker"
        
        if node_type == "root":
            device_name = "meshcore"
            entity_key = "gps"
            self.entity_id = format_entity_id("device_tracker", device_name, entity_key)
        else:
            device_name = pubkey_prefix[:10]
            entity_key = "gps"
            suffix = sanitize_name(node_name)
            self.entity_id = format_entity_id("device_tracker", device_name, entity_key, suffix)
        
        self._attr_name = f"{node_name} GPS"
        
        device_id = build_device_id(coordinator.config_entry.entry_id, full_pubkey, node_type)
        device_name = build_device_name(node_name, full_pubkey, node_type)
        device_model = get_device_model(node_type)
        
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="MeshCore",
            model=device_model,
            via_device=(DOMAIN, coordinator.config_entry.entry_id) if node_type != "root" else None,
        )
        
        self._latitude = None
        self._longitude = None
        self._altitude = None
        self._last_updated = None
        self._gps_accuracy = None
        
    def update_gps_location(self, gps_data: Dict[str, Any]):
        """Update GPS location from telemetry data."""
        self._latitude = gps_data.get("latitude")
        self._longitude = gps_data.get("longitude")
        self._altitude = gps_data.get("altitude")
        self._last_updated = time.time()
        
        self._gps_accuracy = gps_data.get("accuracy", 10)
        
        self.async_write_ha_state()
        _LOGGER.debug(f"Updated GPS location for {self.name}: {self._latitude}, {self._longitude}")
        
    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        return self._latitude
        
    @property 
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        return self._longitude
        
    @property
    def source_type(self) -> SourceType:
        """Return the source type, eg gps or router."""
        return SourceType.GPS
        
    @property
    def location_accuracy(self) -> int:
        """Return the location accuracy of the device."""
        return self._gps_accuracy or 10
        
    @property
    def available(self) -> bool:
        """Return if the tracker is available."""
        if self._last_updated is None:
            return False
        return time.time() - self._last_updated < 3600
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {
            "pubkey_prefix": self.pubkey_prefix,
            "node_type": self.node_info.get("type"),
            "node_name": self.node_info.get("name"),
        }
        
        if self._altitude is not None:
            attributes["altitude"] = round(self._altitude, 1)
            
        if self._last_updated:
            attributes["last_updated"] = datetime.fromtimestamp(self._last_updated).isoformat()
            
        return attributes


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up MeshCore device tracker from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    tracker_manager = DeviceTrackerManager(coordinator, async_add_entities)
    await tracker_manager.setup_gps_listener()
    coordinator.device_tracker_manager = tracker_manager