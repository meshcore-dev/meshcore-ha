"""Sensor platform for MeshCore integration."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict

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
)

_LOGGER = logging.getLogger(__name__)

# Battery voltage constants
MIN_BATTERY_VOLTAGE = 3.2  # Minimum LiPo voltage
MAX_BATTERY_VOLTAGE = 4.2  # Maximum LiPo voltage

# Define sensors for the main device
SENSORS = [
    SensorEntityDescription(
        key="node_status",
        name="Node Status",
        icon="mdi:radio-tower",
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
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="tx_power",
        name="TX Power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power",
    ),
    SensorEntityDescription(
        key="latitude",
        name="Latitude",
        icon="mdi:map-marker",
    ),
    SensorEntityDescription(
        key="longitude",
        name="Longitude",
        icon="mdi:map-marker",
    ),
    SensorEntityDescription(
        key="frequency",
        name="Frequency",
        native_unit_of_measurement="MHz",
        suggested_display_precision="3",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio",
    ),
    SensorEntityDescription(
        key="bandwidth",
        name="Bandwidth",
        native_unit_of_measurement="kHz",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio",
    ),
    SensorEntityDescription(
        key="spreading_factor",
        name="Spreading Factor",
        icon="mdi:radio",
    ),
]

# Sensors for remote nodes/contacts
CONTACT_SENSORS = [
    SensorEntityDescription(
        key="status",
        name="Status",
        icon="mdi:radio-tower",
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
        icon="mdi:signal",
    ),
    SensorEntityDescription(
        key="last_snr",
        name="Last SNR",
        native_unit_of_measurement="dB",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
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
        key="free_queue_len",
        name="Free Queue Length",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:playlist-plus",
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
        _LOGGER.debug("Adding sensor: %s", description.key)
        entities.append(MeshCoreSensor(coordinator, description))
    
    # Add a contact list sensor to track all contacts
    entities.append(MeshCoreContactListSensor(coordinator))
    
    # Store the async_add_entities function for later use
    coordinator.sensor_add_entities = async_add_entities
    
    # First, handle cleanup of removed repeater devices
    # Get registries
    device_registry = async_get_device_registry(hass)
    
    # Add repeater stat sensors if any repeaters are configured
    repeater_subscriptions = entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
    repeater_names = {r.get("name") for r in repeater_subscriptions if r.get("name") and r.get("enabled", True)}
    
    # Create a set of device IDs for active repeaters
    active_repeater_device_ids = set()
    for repeater_name in repeater_names:
        safe_name = sanitize_name(repeater_name)
        device_id = f"{entry.entry_id}_repeater_{safe_name}"
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
        _LOGGER.debug("Creating sensors for %d repeater subscriptions", len(repeater_subscriptions))
        
        for repeater in repeater_subscriptions:
            if not repeater.get("enabled", True):
                continue
                
            repeater_name = repeater.get("name")
            if not repeater_name:
                continue
                
            _LOGGER.info(f"Creating sensors for repeater: {repeater_name}")
            
            # Create repeater sensors for other stats (not status which is now a binary sensor)
            for description in REPEATER_SENSORS:
                try:
                    # Create a sensor for this repeater stat
                    sensor = MeshCoreRepeaterSensor(
                        coordinator,
                        description,
                        repeater_name
                    )
                    entities.append(sensor)
                except Exception as ex:
                    _LOGGER.error(f"Error creating repeater sensor {description.key}: {ex}")
    
    async_add_entities(entities)


class MeshCoreSensor(CoordinatorEntity, SensorEntity):
    """Representation of a MeshCore sensor."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        
        # Get raw device name for display purposes
        raw_device_name = coordinator.data.get('name', 'Node') if coordinator.data else 'Node'
        device_name = f"MeshCore {raw_device_name}"

        public_key_short = ""
        if coordinator.data and "public_key" in coordinator.data:
            public_key_short = coordinator.data["public_key"][:6]
            device_name = f"MeshCore {raw_device_name} ({public_key_short})"

        # Set unique ID using consistent format - filter out any empty parts
        parts = [part for part in [coordinator.config_entry.entry_id,  description.key, public_key_short, raw_device_name] if part]
        self._attr_unique_id = "_".join(parts)

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            description.key,
            raw_device_name
        )

        # Set name
        self._attr_name = description.name
        
        # Set device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=device_name,
            manufacturer=coordinator.data.get("manufacturer_name", "MeshCore") if coordinator.data else "MeshCore",
            model="Mesh Radio",
            sw_version=coordinator.data.get("firmware_version", coordinator.data.get("version", "Unknown")) if coordinator.data else "Unknown",
            hw_version=coordinator.data.get("firmware_build_date", "Unknown") if coordinator.data else "Unknown",
        )

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        key = self.entity_description.key
        
        if key == "node_status":
            if getattr(self.coordinator, "last_update_success", False):
                return "online"
            return "offline"
            
        elif key == "battery_voltage":
            bat_value = self.coordinator.data.get("bat", 0)
            if isinstance(bat_value, (int, float)) and bat_value > 0:
                return bat_value / 1000.0  # Convert millivolts to volts
            return None
            
        elif key == "battery_percentage":
            bat_value = self.coordinator.data.get("bat", 0)
            if isinstance(bat_value, (int, float)) and bat_value > 0:
                voltage = bat_value / 1000.0  # Convert millivolts to volts
                # Calculate percentage based on min/max voltage range
                percentage = ((voltage - MIN_BATTERY_VOLTAGE) / 
                             (MAX_BATTERY_VOLTAGE - MIN_BATTERY_VOLTAGE)) * 100
                
                # Ensure percentage is within 0-100 range
                percentage = max(0, min(100, percentage))
                return round(percentage, 1)  # Round to 1 decimal place
            return None
            
        elif key == "node_count":
            contacts = self.coordinator.data.get("contacts", [])
            return len(contacts) + 1
            
        elif key == "tx_power":
            return self.coordinator.data.get("tx_power")
            
        elif key == "latitude":
            return self.coordinator.data.get("lat")
            
        elif key == "longitude":
            return self.coordinator.data.get("long")
            
        elif key == "frequency":
            freq = self.coordinator.data.get("radio_freq")
            if freq is not None:
                return freq / 1000
            return None
            
        elif key == "bandwidth":
            bw = self.coordinator.data.get("radio_bw") 
            if bw is not None:
                # Check if already in kHz
                if bw < 1000:
                    return bw  # Already in kHz
                else:
                    return bw / 1_000  # Convert Hz to kHz
            return None
            
        elif key == "spreading_factor":
            return self.coordinator.data.get("radio_sf")
        
        return None


class MeshCoreContactListSensor(CoordinatorEntity, SensorEntity):
    """A sensor to track all MeshCore contacts in a single entity."""

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the contact list sensor."""
        super().__init__(coordinator)
        
        # Set unique ID and name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_contacts"
        self._attr_name = "MeshCore Contacts"
        
        # Set device info to link to the main device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        )
        
        # Set entity category to diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        
        # Use a custom icon
        self._attr_icon = "mdi:account-group"

    @property
    def native_value(self) -> str:
        """Return the current number of contacts as the state."""
        if not self.coordinator.data:
            return "0"
            
        contacts = self.coordinator.data.get("contacts", [])
        return str(len(contacts))
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return details about all contacts as attributes."""
        if not self.coordinator.data:
            return {}
            
        contacts = self.coordinator.data.get("contacts", [])
        if not contacts:
            return {"contacts": []}
            
        contact_list = []
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
                
            # Extract the key info we want to display
            contact_info = {
                "name": contact.get("adv_name", "Unknown"),
                "type": get_node_type_str(contact.get("type")),
                "public_key": contact.get("public_key", "")[:16] + "...",  # Truncate for display
                "last_seen": contact.get("last_advert", 0),
            }
            
            # Add location if available
            if "latitude" in contact and "longitude" in contact:
                contact_info["location"] = f"{contact.get('latitude')}, {contact.get('longitude')}"
                
            contact_list.append(contact_info)
            
        # Sort by name
        contact_list.sort(key=lambda x: x.get("name", ""))
        
        return {
            "contacts": contact_list,
            "last_updated": datetime.now().isoformat(),
        }
        

class MeshCoreRepeaterSensor(CoordinatorEntity, SensorEntity):
    """Sensor for repeater statistics."""
    
    def __init__(
        self, 
        coordinator: DataUpdateCoordinator,
        description: SensorEntityDescription,
        repeater_name: str,
    ) -> None:
        """Initialize the repeater stat sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.repeater_name = repeater_name
        
        # Create sanitized names
        safe_name = sanitize_name(repeater_name)
        
        # Generate a unique device_id for this repeater
        self.device_id = f"{coordinator.config_entry.entry_id}_repeater_{safe_name}"
        
        # Set friendly name
        self._attr_name = description.name
        
        # Get repeater stats if available
        repeater_stats = coordinator.data.get("repeater_stats", {}).get(repeater_name, {})

        # Default device name, include public key if available
        device_name = f"MeshCore Repeater: {repeater_name}"
        public_key_short = ""
        if repeater_stats and "public_key" in repeater_stats:
            public_key_short = repeater_stats.get("public_key_short", repeater_stats["public_key"][:6])
            device_name = f"MeshCore Repeater: {repeater_name} ({public_key_short})"
        
        # Set unique ID
        self._attr_unique_id = f"{self.device_id}_{description.key}_{public_key_short}_{repeater_name}"
        
        # Set entity ID
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            description.key,
            repeater_name
        )

        print(f"entity ID: {self.entity_id}")


        # Set device info to create a separate device for this repeater
        device_info = {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": device_name,
            "manufacturer": repeater_stats.get("manufacturer_name", "MeshCore") if repeater_stats else "MeshCore",
            "model": "Mesh Repeater",
            "via_device": (DOMAIN, coordinator.config_entry.entry_id),  # Link to the main device
        }
        
        # Add version information if available
        if repeater_stats:
            # Prefer firmware_version if available, fall back to version
            if "firmware_version" in repeater_stats:
                device_info["sw_version"] = repeater_stats["firmware_version"]
            elif "version" in repeater_stats:
                device_info["sw_version"] = repeater_stats["version"]
                
            # Add build date as hardware version if available
            if "firmware_build_date" in repeater_stats:
                device_info["hw_version"] = repeater_stats["firmware_build_date"]
            
        self._attr_device_info = DeviceInfo(**device_info)
    
    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if not self.coordinator.data or "repeater_stats" not in self.coordinator.data:
            return None
            
        # Get the repeater stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {}).get(self.repeater_name, {})
        if not repeater_stats:
            return None
        
        key = self.entity_description.key
        
        # Special handling for battery voltage - convert from mV to V
        if key == "bat":
            bat_value = repeater_stats.get("bat")
            if isinstance(bat_value, (int, float)) and bat_value > 0:
                # Convert from millivolts to volts
                return bat_value / 1000.0
            return None
        elif key == "battery_percentage":
            bat_value = repeater_stats.get("bat")
            if isinstance(bat_value, (int, float)) and bat_value > 0:
                voltage = bat_value / 1000.0  # Convert mV to V
                # Calculate percentage based on min/max voltage range
                percentage = ((voltage - MIN_BATTERY_VOLTAGE) / 
                             (MAX_BATTERY_VOLTAGE - MIN_BATTERY_VOLTAGE)) * 100
                
                # Ensure percentage is within 0-100 range
                percentage = max(0, min(100, percentage))
                return round(percentage, 1)  # Round to 1 decimal place
            return None
        elif key == "uptime":
            # Convert from seconds to minutes
            seconds = repeater_stats.get("uptime")
            if isinstance(seconds, (int, float)) and seconds > 0:
                return round(seconds / 60, 1)  # Convert to minutes and round to 1 decimal
            return None
        elif key == "airtime":
            # Convert from seconds to minutes
            seconds = repeater_stats.get("airtime")
            if isinstance(seconds, (int, float)) and seconds > 0:
                return round(seconds / 60, 1)  # Convert to minutes and round to 1 decimal
            return None
            
        # Get the value from the repeater stats for other sensors
        return repeater_stats.get(key)
        
    @property
    def available(self) -> bool:
        """Return if the sensor is available."""
        # Check if coordinator is available and we have data for this repeater
        if not super().available or not self.coordinator.data:
            return False
            
        # Check if we have stats for this repeater
        repeater_stats = self.coordinator.data.get("repeater_stats", {})
        return self.repeater_name in repeater_stats
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
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






