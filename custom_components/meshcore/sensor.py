"""Sensor platform for MeshCore integration."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict
from meshcore import EventType
from meshcore.events import Event
from custom_components.meshcore import MeshCoreDataUpdateCoordinator
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    ENTITY_DOMAIN_SENSOR,
    CONF_REPEATER_SUBSCRIPTIONS,
    NodeType,
)
from .utils import get_node_type_str
from .utils import (
    sanitize_name,
    format_entity_id,
    calculate_battery_percentage,
)

_LOGGER = logging.getLogger(__name__)


# Define sensors for the main device
SENSORS = [
    SensorEntityDescription(
        key="node_status",
        name="Node Status",
        icon="mdi:radio-tower"
    ),
    SensorEntityDescription(
        key="battery_voltage",
        name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision="2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="battery_percentage",
        name="Battery Percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="node_count",
        name="Node Count",
        icon="mdi:account-group",
        state_class=SensorStateClass.MEASUREMENT
    ),
    SensorEntityDescription(
        key="tx_power",
        name="TX Power",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power"
    ),
    SensorEntityDescription(
        key="latitude",
        name="Latitude",
        icon="mdi:map-marker"
    ),
    SensorEntityDescription(
        key="longitude",
        name="Longitude",
        icon="mdi:map-marker"
    ),
    SensorEntityDescription(
        key="frequency",
        name="Frequency",
        native_unit_of_measurement="MHz",
        suggested_display_precision="3",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio"
    ),
    SensorEntityDescription(
        key="bandwidth",
        name="Bandwidth",
        native_unit_of_measurement="kHz",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio"
    ),
    SensorEntityDescription(
        key="spreading_factor",
        name="Spreading Factor",
        icon="mdi:radio"
    ),
]

# Sensors for remote nodes/contacts
CONTACT_SENSORS = [
    SensorEntityDescription(
        key="status",
        name="Status",
        icon="mdi:radio-tower"
    ),
    SensorEntityDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision="2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="battery_percentage",
        name="Battery Percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="last_rssi",
        name="Last RSSI",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
    SensorEntityDescription(
        key="last_snr",
        name="Last SNR",
        native_unit_of_measurement="dB",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
]

# Additional sensors only for repeaters (type 2)
REPEATER_SENSORS = [
    SensorEntityDescription(
        key="bat",
        name="Battery Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision="2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="battery_percentage",
        name="Battery Percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="uptime",
        name="Uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        suggested_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock",
    ),
    SensorEntityDescription(
        key="airtime",
        name="Airtime",
        native_unit_of_measurement="min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio",
    ),
    SensorEntityDescription(
        key="nb_sent",
        name="Messages Sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right",
    ),
    SensorEntityDescription(
        key="last_rssi",
        name="Last RSSI",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
    SensorEntityDescription(
        key="last_snr",
        name="Last SNR",
        native_unit_of_measurement="dB",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
    SensorEntityDescription(
        key="nb_recv",
        name="Messages Received",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="tx_queue_len",
        name="TX Queue Length",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:playlist-edit",
    ),
    SensorEntityDescription(
        key="noise_floor",
        name="Noise Floor",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:waveform",
    ),
    SensorEntityDescription(
        key="sent_flood",
        name="Sent Flood Messages",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right-outline",
    ),
    SensorEntityDescription(
        key="sent_direct",
        name="Sent Direct Messages",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right",
    ),
    SensorEntityDescription(
        key="recv_flood",
        name="Received Flood Messages",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left-outline",
    ),
    SensorEntityDescription(
        key="recv_direct",
        name="Received Direct Messages",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="full_evts",
        name="Full Events",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-circle",
    ),
    SensorEntityDescription(
        key="direct_dups",
        name="Direct Duplicates",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:content-duplicate",
    ),
    SensorEntityDescription(
        key="flood_dups",
        name="Flood Duplicates",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:content-duplicate",
    ),
    SensorEntityDescription(
        key="airtime_utilization",
        name="Airtime Utilization",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement="%",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    _LOGGER.debug("Setting up MeshCore sensors")
    
    entities = []
    
    # Create sensors for the main device
    for description in SENSORS:
        entities.append(MeshCoreSensor(coordinator, description))
    
    # Store the async_add_entities function for later use
    coordinator.sensor_add_entities = async_add_entities
    
    # First, handle cleanup of removed repeater devices
    # Get registries
    device_registry = async_get_device_registry(hass)
    
    # Add repeater stat sensors if any repeaters are configured
    repeater_subscriptions = entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])

    # Create a set of device IDs for active repeaters - using pubkey_prefix for more stable IDs
    active_repeater_device_ids = set()
    for subscription in repeater_subscriptions:
        device_id = f"{entry.entry_id}_repeater_{subscription.get("pubkey_prefix")}"
        active_repeater_device_ids.add(device_id)
    
    # Find and remove any repeater devices that are no longer in the configuration
    for device in list(device_registry.devices.values()):
        # Check if this is a device from this integration
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN:
                device_id = identifier[1]
                
                # If this device is a repeater but not in our active list, remove it
                if "_repeater_" in device_id and device_id not in active_repeater_device_ids:
                    _LOGGER.info(f"Removing device {device.name} ({device_id}) as it's no longer configured")
                    device_registry.async_remove_device(device.id)
    
    if repeater_subscriptions:
        for repeater in repeater_subscriptions:
            _LOGGER.info(f"Creating sensors for repeater: {repeater.get("name")} ({repeater.get("pubkey_prefix")})")

            # Create repeater sensors for other stats (not status which is now a binary sensor)
            for description in REPEATER_SENSORS:
                try:
                    # Create a sensor for this repeater stat with public key
                    sensor = MeshCoreRepeaterSensor(
                        coordinator,
                        description,
                        repeater
                    )
                    entities.append(sensor)
                except Exception as ex:
                    _LOGGER.error(f"Error creating repeater sensor {description.key}: {ex}")
    
    async_add_entities(entities)


class MeshCoreSensor(CoordinatorEntity, SensorEntity):
    """Representation of a MeshCore sensor."""

    def __init__(
        self,
        coordinator: MeshCoreDataUpdateCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.coordinator = coordinator
        
        # Get raw device name for display purposes
        raw_device_name = coordinator.name or "Unknown"
        # Assume public key is always present, but handle None gracefully
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""

        # Set unique ID using consistent format - filter out any empty parts
        parts = [part for part in [coordinator.config_entry.entry_id, description.key, public_key_short, raw_device_name] if part]
        self._attr_unique_id = "_".join(parts)

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            description.key,
            raw_device_name
        )

        # Set name
        self._attr_name = description.name
        
        # Store cached values
        self._native_value = None
        
    async def async_added_to_hass(self):
        """Register event handlers when entity is added to hass."""
        await super().async_added_to_hass()
        meshcore = self.coordinator.api.mesh_core
        key = self.entity_description.key
        
        if key == "node_status":
            def update_status(event: Event):
                if self.coordinator.api.connected:
                    self._native_value = "online"
                else:
                    self._native_value = "offline"
            meshcore.dispatcher.subscribe(
                None,
                update_status,
            )
        
        elif key == "battery_voltage":
            def update_battery(event: Event):
                print(f"Received status event: {event}")
                self._native_value = event.payload.get("level") / 1000.0  # Convert from mV to V
            meshcore.dispatcher.subscribe(
                EventType.BATTERY,
                update_battery,
            )
            
        elif key == "battery_percentage":
            def update_battery(event: Event):
                voltage_mv = event.payload.get("level")
                self._native_value = calculate_battery_percentage(voltage_mv)
            meshcore.dispatcher.subscribe(
                EventType.BATTERY,
                update_battery,
            )
            
        elif key == "node_count":
            def update_count(event: Event):
                self._native_value = len(event.payload) + 1
            meshcore.dispatcher.subscribe(
                EventType.CONTACTS,
                update_count,
            )
            
        elif key == "tx_power":
            def update_tx(event: Event):
                self._native_value = event.payload.get("max_tx_power")
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_tx,
            )
            
        elif key == "latitude":
            def update_lat(event: Event):
                self._native_value = event.payload.get("adv_lat")
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_lat,
            )
            
        elif key == "longitude":
            def update_lon(event: Event):
                self._native_value = event.payload.get("adv_lon")
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_lon,
            )
            
        elif key == "frequency":
            def update_freq(event: Event):
                self._native_value = event.payload.get("radio_freq")
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_freq,
            )
            
            
        elif key == "bandwidth":
            def update_bw(event: Event):
                self._native_value = event.payload.get("radio_bw")
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_bw,
            )
            
        elif key == "spreading_factor":
            def update_sf(event: Event):
                self._native_value = event.payload.get("radio_sf")
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_sf,
            )
        

    @property
    def device_info(self):
        return DeviceInfo(**self.coordinator.device_info)
    
    @property
    def native_value(self) -> Any:
        return self._native_value


class MeshCoreRepeaterSensor(CoordinatorEntity, SensorEntity):
    """Sensor for repeater statistics with event-based updates."""
    
    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: SensorEntityDescription,
        repeater: dict,
    ) -> None:
        """Initialize the repeater stat sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.repeater_name = repeater.get("name", "Unknown")

        # Use the provided pubkey_prefix
        self.public_key = repeater.get("pubkey_prefix", "")
        self.public_key_short = self.public_key[:6] if self.public_key else ""

        # Generate a unique device_id for this repeater using pubkey_prefix
        self.device_id = f"{coordinator.config_entry.entry_id}_repeater_{self.public_key}"

        # Set friendly name
        self._attr_name = description.name

        # Build device name with pubkey
        device_name = f"MeshCore Repeater: {self.repeater_name} ({self.public_key_short})"
        
        # Set unique ID
        self._attr_unique_id = f"{self.device_id}_{description.key}_{self.public_key_short}_{self.repeater_name}"
        
        # Set entity ID
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            self.public_key[:10],
            description.key,
            self.repeater_name
        )

        # Set device info to create a separate device for this repeater
        device_info = {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": device_name,
            "manufacturer": "MeshCore",
            "model": "Mesh Repeater",
            "sw_version": repeater.get("firmware_version"),
            "via_device": (DOMAIN, coordinator.config_entry.entry_id),  # Link to the main device
        }
            
        self._attr_device_info = DeviceInfo(**device_info)
        
        self._cached_stats = {}
        self._previous_stats = {}
        
    async def async_added_to_hass(self):
        """Register event handlers when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Only set up listener if MeshCore instance is available
        if not self.coordinator.api.mesh_core:
            _LOGGER.warning(f"No MeshCore instance available for repeater stats subscription: {self.repeater_name}")
            return
            
        # Only set up if we have a public key
        if not self.public_key:
            _LOGGER.warning(f"No public key available for repeater {self.repeater_name}, can't subscribe to events")
            return

        try:

            # Use the provided pubkey_prefix for filtering (first 12 chars should match)
            pubkey_prefix_filter = self.public_key[:12]

            # Set up subscription for stats events with filter
            self.coordinator.api.mesh_core.subscribe(
                EventType.STATUS_RESPONSE,  # Status response event type
                self._handle_stats_event,
                {"pubkey_prefix": pubkey_prefix_filter}  # Filter by pubkey_prefix
            )
        except Exception as ex:
            _LOGGER.error(f"Error setting up repeater stats subscription for {self.repeater_name}: {ex}")
            
            
        
    async def _handle_stats_event(self, event):
        """Handle repeater stats events."""
        # Create a deep copy of the payload to avoid modifying the original event
        if event.payload:
            if event.payload.get('uptime', 0) == 0:
                self.logger.error(f"Skipping event with malformed payload: {event.payload}")
                return 
            # Store previous stats before updating
            self._previous_stats = self._cached_stats.copy()
            self._cached_stats = event.payload.copy()
        else:
            self._cached_stats = {}
            
        # Add metadata
        self._cached_stats["repeater_name"] = self.repeater_name
        self._cached_stats["last_updated"] = time.time()
        
        # Update the entity state
        self.async_write_ha_state()
    
    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        key = self.entity_description.key
        value = self._cached_stats.get(key)
        
        # Process the value based on the sensor type
        if key == "bat" and isinstance(value, (int, float)) and value > 0:
            return value / 1000.0  # Convert millivolts to volts
        
        elif key == "battery_percentage" and "bat" in self._cached_stats:
            voltage_mv = self._cached_stats["bat"]
            return calculate_battery_percentage(voltage_mv)  
        
        elif key == "uptime" and isinstance(value, (int, float)) and value > 0:
            return round(value / 60, 1)  # Convert seconds to minutes
            
        elif key == "airtime" and isinstance(value, (int, float)) and value > 0:
            return round(value / 60, 1)  # Convert seconds to minutes
            
        elif key == "airtime_utilization":
            current_uptime = self._cached_stats.get("uptime")
            current_airtime = self._cached_stats.get("airtime")
            
            if not isinstance(current_uptime, (int, float)) or not isinstance(current_airtime, (int, float)):
                return None
                
            if self._previous_stats:
                prev_uptime = self._previous_stats.get("uptime", 0)
                prev_airtime = self._previous_stats.get("airtime", 0)
                
                if isinstance(prev_uptime, (int, float)) and isinstance(prev_airtime, (int, float)):
                    uptime_delta = current_uptime - prev_uptime
                    airtime_delta = current_airtime - prev_airtime
                    
                    if uptime_delta > 0 and airtime_delta >= 0:
                        utilization_rate = (airtime_delta / uptime_delta) * 100
                        return round(utilization_rate, 1)
            return 0  # No previous data or no change
            
        # Return the value directly for other sensors
        return value
                
        
    @property
    def available(self) -> bool:
        """Return if the sensor is available."""
        # First check if we have cached stats from an event
        if self._cached_stats:
            last_updated = self._cached_stats.get("last_updated", 0)
            # Consider stats fresh if they were updated in the last hour
            if time.time() - last_updated < 3600:
                return True
        
        # Otherwise check coordinator data
        if not super().available or not self.coordinator.data:
            return False
            
        # Check if we have stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {})
        return self.repeater_name in repeater_stats
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        # First try to use cached stats from events
        if self._cached_stats:
            attributes = {
                "last_updated": datetime.fromtimestamp(self._cached_stats.get("last_updated", 0)).isoformat()
            }
            
            key = self.entity_description.key
            
            # Add raw values for certain sensors
            if key == "bat" and "bat" in self._cached_stats:
                attributes["raw_millivolts"] = self._cached_stats["bat"]
            elif key in ["uptime", "airtime"] and key in self._cached_stats:
                seconds = self._cached_stats[key]
                if isinstance(seconds, (int, float)) and seconds > 0:
                    # Add human-readable format for uptime
                    if key == "uptime":
                        days = seconds // 86400
                        hours = (seconds % 86400) // 3600
                        minutes = (seconds % 3600) // 60
                        secs = seconds % 60
                        attributes["human_readable"] = f"{days}d {hours}h {minutes}m {secs}s"
            elif key == "airtime_utilization":
                uptime = self._cached_stats.get("uptime")
                airtime = self._cached_stats.get("airtime")
                
                if isinstance(uptime, (int, float)) and isinstance(airtime, (int, float)):
                    attributes["uptime_seconds"] = str(uptime)
                    attributes["airtime_seconds"] = str(airtime)
                    
                    # Calculate airtime since last update if we have previous values
                    if self._previous_stats:
                        prev_airtime = self._previous_stats.get("airtime", 0)
                        if isinstance(prev_airtime, (int, float)) and prev_airtime < airtime:
                            airtime_delta = airtime - prev_airtime
                            attributes["airtime_since_last_update"] = str(round(airtime_delta / 60, 1))  # in minutes
            
            return attributes
            
        # Fall back to coordinator data
        if not self.coordinator.data or "repeater_stats" not in self.coordinator.data:
            return {}
            
        # Get the repeater stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {}).get(self.repeater_name, {})
        if not repeater_stats:
            return {}
            
        attributes = {}
        key = self.entity_description.key
        
        # Add raw values for certain sensors to help with debugging
        if key == "bat":
            bat_value = repeater_stats.get("bat")
            if isinstance(bat_value, (int, float)) and bat_value > 0:
                attributes["raw_millivolts"] = bat_value
        elif key in ["uptime", "airtime"]:
            seconds = repeater_stats.get(key)
            if isinstance(seconds, (int, float)) and seconds > 0:
                attributes["raw_seconds"] = seconds
                
                # Also add a human-readable format for uptime
                if key == "uptime":
                    days = seconds // 86400
                    hours = (seconds % 86400) // 3600
                    minutes = (seconds % 3600) // 60
                    secs = seconds % 60
                    attributes["human_readable"] = f"{days}d {hours}h {minutes}m {secs}s"
                    
        return attributes






