"""Dynamic telemetry sensor platform for MeshCore integration."""
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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_REPEATER_SUBSCRIPTIONS, CONF_TRACKED_CLIENTS, BATTERY_CURVE
from .utils import (
    sanitize_name, 
    format_entity_id, 
    build_device_name, 
    get_device_model, 
    build_device_id
)

_LOGGER = logging.getLogger(__name__)

# Cayenne LPP Data Type mappings (IPSO Object IDs - 3200)
# Maps both numeric LPP codes and string type names to sensor configurations
LPP_TYPE_MAPPINGS = {
    0: {
        "name": "Digital Input", 
        "icon": "mdi:toggle-switch",
        "create_multi": False
    },
    1: {
        "name": "Digital Output", 
        "icon": "mdi:toggle-switch",
        "create_multi": False
    },
    2: {
        "name": "Analog Input", 
        "icon": "mdi:sine-wave", 
        "state_class": SensorStateClass.MEASUREMENT,
        "native_unit_of_measurement": "V",
        "suggested_display_precision": 2,
        "create_multi": False
    },
    3: {
        "name": "Analog Output", 
        "icon": "mdi:sine-wave", 
        "state_class": SensorStateClass.MEASUREMENT,
        "native_unit_of_measurement": "V", 
        "suggested_display_precision": 2,
        "create_multi": False
    },
    100: {
        "name": "Generic Sensor", 
        "icon": "mdi:gauge", 
        "state_class": SensorStateClass.MEASUREMENT,
        "create_multi": False
    },
    101: {
        "name": "Illuminance", 
        "icon": "mdi:brightness-6", 
        "device_class": SensorDeviceClass.ILLUMINANCE, 
        "native_unit_of_measurement": "lx", 
        "state_class": SensorStateClass.MEASUREMENT,
        "create_multi": False
    },
    102: {
        "name": "Presence", 
        "icon": "mdi:motion-sensor",
        "create_multi": False
    },
    103: {
        "name": "Temperature", 
        "icon": "mdi:thermometer", 
        "device_class": SensorDeviceClass.TEMPERATURE, 
        "native_unit_of_measurement": "°C", 
        "state_class": SensorStateClass.MEASUREMENT, 
        "suggested_display_precision": 1,
        "create_multi": False
    },
    104: {
        "name": "Humidity", 
        "icon": "mdi:water-percent", 
        "device_class": SensorDeviceClass.HUMIDITY, 
        "native_unit_of_measurement": "%", 
        "state_class": SensorStateClass.MEASUREMENT, 
        "suggested_display_precision": 1,
        "create_multi": False
    },
    113: {
        "name": "Accelerometer", 
        "icon": "mdi:axis-arrow", 
        "state_class": SensorStateClass.MEASUREMENT,
        "native_unit_of_measurement": "G",
        "suggested_display_precision": 3,
        "create_multi": True,  # Create separate sensors for X, Y, Z
        "multi_fields": ["x", "y", "z"]
    },
    116: {
        "name": "Voltage", 
        "icon": "mdi:flash", 
        "device_class": SensorDeviceClass.VOLTAGE, 
        "native_unit_of_measurement": "V", 
        "state_class": SensorStateClass.MEASUREMENT, 
        "suggested_display_precision": 2,
        "create_multi": False
    },
    117: {
        "name": "Current", 
        "icon": "mdi:current-ac", 
        "device_class": SensorDeviceClass.CURRENT, 
        "native_unit_of_measurement": "A", 
        "state_class": SensorStateClass.MEASUREMENT, 
        "suggested_display_precision": 2,
        "create_multi": False
    },
    135: {
        "name": "Color", 
        "icon": "mdi:palette", 
        "state_class": SensorStateClass.MEASUREMENT,
        "create_multi": True,  # Create separate sensors for R, G, B
        "multi_fields": ["r", "g", "b"]
    },
    136: {
        "name": "GPS", 
        "icon": "mdi:crosshairs-gps", 
        "state_class": SensorStateClass.MEASUREMENT,
        "create_multi": True,  # Create separate sensors for lat, lon, alt
        "multi_fields": [
            {"field": "latitude", "name": "Latitude", "icon": "mdi:latitude", "unit": "°", "precision": 4},
            {"field": "longitude", "name": "Longitude", "icon": "mdi:longitude", "unit": "°", "precision": 4},
            {"field": "altitude", "name": "Altitude", "icon": "mdi:elevation-rise", "unit": "m", "precision": 1}
        ]
    },
}


class TelemetrySensorManager:
    """Manages dynamic creation and updates of telemetry sensors."""
    
    def __init__(self, coordinator: MeshCoreDataUpdateCoordinator, async_add_entities: AddEntitiesCallback):
        self.coordinator = coordinator
        self.async_add_entities = async_add_entities
        self.discovered_sensors = {}  # Track discovered sensors by unique key
        
    async def setup_telemetry_listener(self):
        """Set up the telemetry event listener."""
        if not self.coordinator.api.mesh_core:
            _LOGGER.warning("No MeshCore instance available for telemetry sensor setup")
            return
            
        # Subscribe to telemetry response events
        self.coordinator.api.mesh_core.subscribe(
            EventType.TELEMETRY_RESPONSE,
            self._handle_telemetry_event
        )
        _LOGGER.debug("Telemetry sensor manager initialized")
        
    async def _handle_telemetry_event(self, event: Event):
        """Handle incoming telemetry events and discover new sensors."""
        _LOGGER.debug(f"Received telemetry event: {event}")
        
        if not event.payload or "lpp" not in event.payload:
            _LOGGER.debug("No LPP data in telemetry event")
            return
            
        pubkey_prefix = event.payload.get("pubkey_pre", "")
        lpp_data = event.payload.get("lpp", [])
        
        # If no pubkey_prefix, this might be self telemetry
        if not pubkey_prefix:
            # Check if this is a self telemetry response (no pubkey_pre field)
            # Use the coordinator's own pubkey for self telemetry
            if "lpp" in event.payload and self.coordinator.pubkey:
                pubkey_prefix = self.coordinator.pubkey[:12]  # Use first 12 chars as prefix
                _LOGGER.debug(f"Self telemetry detected, using coordinator pubkey: {pubkey_prefix}")
            else:
                _LOGGER.warning("Telemetry event missing pubkey_prefix and not self telemetry")
                return
            
        # Find the node info for smart naming
        node_info = self._get_node_info(pubkey_prefix)
        
        # Process each channel in the LPP data
        new_sensors = []
        for channel_data in lpp_data:
            channel = channel_data.get("channel")
            lpp_type = channel_data.get("type")
            value = channel_data.get("value")
            
            if channel is None or lpp_type is None:
                continue
                
            # Create sensors based on the LPP type
            sensors = self._create_sensors_for_channel(
                pubkey_prefix, channel, lpp_type, value, node_info
            )
            
            _LOGGER.debug(f"Created {len(sensors)} sensors for channel {channel}")
            
            for sensor in sensors:
                sensor_key = sensor.get_unique_key()
                _LOGGER.debug(f"Sensor: name={sensor.name}, key={sensor_key}, entity_id={sensor.entity_id}")
                if sensor_key not in self.discovered_sensors:
                    self.discovered_sensors[sensor_key] = sensor
                    new_sensors.append(sensor)
                    _LOGGER.info(f"Discovered new telemetry sensor: {sensor.name} ({sensor_key})")
                else:
                    _LOGGER.debug(f"Sensor already discovered: {sensor_key}")
        
        # Add any new sensors to Home Assistant
        if new_sensors:
            self.async_add_entities(new_sensors)
            
        # Update all existing sensors for this node (but skip newly discovered ones)
        for sensor_key, sensor in self.discovered_sensors.items():
            if sensor_key.startswith(pubkey_prefix) and sensor not in new_sensors:
                sensor.update_from_telemetry(lpp_data)
                
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
        
    def _create_sensors_for_channel(
        self, pubkey_prefix: str, channel: int, lpp_type: int, value: Any, node_info: Dict[str, Any]
    ) -> list[MeshCoreTelemetrySensor]:
        """Create sensors for a channel, handling multi-value sensors."""
        # Special handling for client battery on channel 1
        if node_info.get("type") == "client" and channel == 1 and lpp_type == "voltage":
            sensors = []
            
            # Create voltage sensor
            voltage_description = SensorEntityDescription(
                key=f"telemetry_{pubkey_prefix}_{channel}_voltage",
                name=f"Ch{channel} Battery Voltage",
                icon="mdi:sine-wave",
                device_class=SensorDeviceClass.VOLTAGE,
                native_unit_of_measurement="V",
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2
            )
            voltage_sensor = MeshCoreTelemetrySensor(
                self.coordinator, voltage_description, pubkey_prefix, channel, lpp_type, node_info
            )
            sensors.append(voltage_sensor)
            
            # Create battery percentage sensor
            battery_description = SensorEntityDescription(
                key=f"telemetry_{pubkey_prefix}_{channel}_battery",
                name=f"Ch{channel} Battery",
                icon="mdi:battery",
                device_class=SensorDeviceClass.BATTERY,
                native_unit_of_measurement="%",
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0
            )
            battery_sensor = MeshCoreBatteryPercentageSensor(
                self.coordinator, battery_description, pubkey_prefix, channel, lpp_type, node_info
            )
            sensors.append(battery_sensor)
            
            return sensors
        
        if lpp_type not in LPP_TYPE_MAPPINGS:
            if isinstance(lpp_type, str):
                # Handle string types by creating a generic sensor with the string name
                type_config = {
                    "name": lpp_type.replace("_", " ").title(), 
                    "icon": "mdi:gauge", 
                    "create_multi": False,
                    "state_class": SensorStateClass.MEASUREMENT
                }
                _LOGGER.debug(f"Creating generic sensor for string LPP type: {lpp_type}")
            else:
                _LOGGER.debug(f"Unknown LPP type {lpp_type}, creating generic sensor")
                type_config = {"name": f"Sensor Type {lpp_type}", "icon": "mdi:gauge", "create_multi": False}
        else:
            type_config = LPP_TYPE_MAPPINGS[lpp_type]
            
        sensors = []
        
        if type_config.get("create_multi", False) and isinstance(value, dict):
            # Create separate sensors for multi-value types
            multi_fields = type_config.get("multi_fields", [])
            
            for field_info in multi_fields:
                if isinstance(field_info, dict):
                    # Complex field definition (like GPS)
                    field = field_info["field"]
                    field_name = field_info["name"]
                    field_icon = field_info.get("icon", type_config["icon"])
                    field_unit = field_info.get("unit")
                    field_precision = field_info.get("precision")
                else:
                    # Simple field name (like accelerometer x/y/z)
                    field = field_info
                    field_name = f"{type_config['name']} {field.upper()}"
                    field_icon = type_config["icon"]
                    field_unit = type_config.get("native_unit_of_measurement")
                    field_precision = type_config.get("suggested_display_precision")
                
                if field in value:
                    description = SensorEntityDescription(
                        key=f"telemetry_{pubkey_prefix}_{channel}_{lpp_type}_{field}",
                        name=f"Ch{channel} {field_name}",
                        icon=field_icon,
                        device_class=type_config.get("device_class"),
                        native_unit_of_measurement=field_unit,
                        state_class=type_config.get("state_class"),
                        suggested_display_precision=field_precision,
                    )
                    
                    sensor = MeshCoreTelemetrySensor(
                        self.coordinator, description, pubkey_prefix, channel, lpp_type, node_info, field
                    )
                    sensors.append(sensor)
        else:
            # Single sensor for simple types
            description = SensorEntityDescription(
                key=f"telemetry_{pubkey_prefix}_{channel}_{lpp_type}",
                name=f"Ch{channel} {type_config['name']}",
                icon=type_config.get("icon", "mdi:gauge"),
                device_class=type_config.get("device_class"),
                native_unit_of_measurement=type_config.get("native_unit_of_measurement"),
                state_class=type_config.get("state_class"),
                suggested_display_precision=type_config.get("suggested_display_precision"),
            )
            
            sensor = MeshCoreTelemetrySensor(
                self.coordinator, description, pubkey_prefix, channel, lpp_type, node_info
            )
            sensors.append(sensor)
            
        return sensors


class MeshCoreTelemetrySensor(CoordinatorEntity, SensorEntity):
    """Sensor for telemetry data from mesh nodes."""
    
    def __init__(
        self,
        coordinator: MeshCoreDataUpdateCoordinator,
        description: SensorEntityDescription,
        pubkey_prefix: str,
        channel: int,
        lpp_type: int,
        node_info: Dict[str, Any],
        field: str = None,
    ) -> None:
        """Initialize the telemetry sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.pubkey_prefix = pubkey_prefix
        self.channel = channel
        self.lpp_type = lpp_type
        self.node_info = node_info
        self.field = field  # For multi-value sensors like GPS lat/lon or accelerometer x/y/z
        
        # Set up naming based on node type
        node_name = node_info.get("name", f"Node {pubkey_prefix[:6]}")
        node_type = node_info.get("type", "unknown")
        full_pubkey = node_info.get("pubkey_prefix", pubkey_prefix)
        
        # Build unique ID and entity ID using consistent format
        field_suffix = f"_{field}" if field else ""
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{pubkey_prefix}_{channel}_{lpp_type}{field_suffix}_telemetry"
        
        # Smart entity naming - consistent with existing sensors
        # Remove channel prefix from the name (case-insensitive)
        sensor_name_lower = description.name.lower().replace(" ", "_")
        channel_prefix = f"ch{channel}_"
        if sensor_name_lower.startswith(channel_prefix):
            sensor_type_name = sensor_name_lower[len(channel_prefix):]
        else:
            sensor_type_name = sensor_name_lower
        
        if node_type == "root":
            # For root node, use cleaner entity IDs
            device_name = "meshcore"
            entity_key = f"{sensor_type_name}_ch{channel}"
            self.entity_id = format_entity_id("sensor", device_name, entity_key)
        else:
            # For other nodes, include more details
            device_name = pubkey_prefix[:10]
            entity_key = f"ch{channel}_{sensor_type_name}"
            suffix = sanitize_name(node_name)
            self.entity_id = format_entity_id("sensor", device_name, entity_key, suffix)
        
        # Set display name
        self._attr_name = description.name
        
        # Set device info using common utilities
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
        
        # Store sensor value
        self._native_value = None
        self._last_updated = None
        self._raw_value = None
        
    def get_unique_key(self) -> str:
        """Get unique key for this sensor."""
        field_suffix = f"_{self.field}" if self.field else ""
        return f"{self.pubkey_prefix}_{self.channel}_{self.lpp_type}{field_suffix}"
            
    def update_from_telemetry(self, lpp_data: list):
        """Update sensor value from telemetry data."""
        for channel_data in lpp_data:
            if (channel_data.get("channel") == self.channel and 
                channel_data.get("type") == self.lpp_type):
                
                value = channel_data.get("value")
                self._raw_value = value
                self._last_updated = time.time()
                
                # Extract the specific field value if this is a multi-value sensor
                if self.field and isinstance(value, dict):
                    self._native_value = value.get(self.field)
                else:
                    self._native_value = value
                
                # Update Home Assistant state
                self.async_write_ha_state()
                break
        
    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self._native_value
        
    @property
    def available(self) -> bool:
        """Return if the sensor is available."""
        if self._last_updated is None:
            return False
        # Consider data stale after 1 hour
        return time.time() - self._last_updated < 3600
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {
            "channel": self.channel,
            "lpp_type": self.lpp_type,
            "pubkey_prefix": self.pubkey_prefix,
            "node_type": self.node_info.get("type"),
            "node_name": self.node_info.get("name"),
        }
        
        if self.field:
            attributes["field"] = self.field
        
        if self._last_updated:
            attributes["last_updated"] = datetime.fromtimestamp(self._last_updated).isoformat()
            
        if self._raw_value is not None:
            attributes["raw_value"] = self._raw_value
            
        return attributes


class MeshCoreBatteryPercentageSensor(MeshCoreTelemetrySensor):
    """Battery percentage sensor that converts voltage to percentage."""
    
    def __init__(self, *args, **kwargs):
        """Initialize the battery percentage sensor."""
        super().__init__(*args, **kwargs)
        # Override the unique_id to use "battery" instead of "voltage"
        self._attr_unique_id = f"{self.coordinator.config_entry.entry_id}_{self.pubkey_prefix}_{self.channel}_battery_telemetry"
    
    def get_unique_key(self) -> str:
        """Get unique key for battery percentage sensor."""
        # Use a different suffix to distinguish from voltage sensor
        return f"{self.pubkey_prefix}_{self.channel}_battery"
    
    def update_from_telemetry(self, lpp_data: list) -> None:
        """Update sensor value from telemetry data, converting voltage to percentage."""
        for channel_data in lpp_data:
            if (channel_data.get("channel") == self.channel and 
                channel_data.get("type") == self.lpp_type):
                
                voltage = channel_data.get("value")
                if voltage is not None:
                    self._raw_value = voltage
                    self._last_updated = time.time()
                    
                    # Convert voltage to battery percentage
                    self._native_value = self._voltage_to_percentage(voltage)
                    
                    # Update Home Assistant state
                    self.async_write_ha_state()
                break
    
    def _voltage_to_percentage(self, voltage: float) -> int:
        """Convert battery voltage to percentage using the battery curve."""
        if voltage is None:
            return None
            
        # Find the appropriate percentage from the curve
        for v, p in BATTERY_CURVE:
            if voltage >= v:
                return p
        
        # If voltage is below the lowest curve value
        return 0
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes including the raw voltage."""
        attributes = super().extra_state_attributes
        if self._raw_value is not None:
            attributes["voltage"] = round(self._raw_value, 2)
        return attributes