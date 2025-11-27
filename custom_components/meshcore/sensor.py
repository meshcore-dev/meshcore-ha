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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    ENTITY_DOMAIN_SENSOR,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_TRACKED_CLIENTS,
    SENSOR_AVAILABILITY_TIMEOUT_MULTIPLIER,
)
from .utils import (
    format_entity_id,
    calculate_battery_percentage,
)
from .telemetry_sensor import TelemetrySensorManager

_LOGGER = logging.getLogger(__name__)

# Sensor key suffixes
UTILIZATION_SUFFIX = "_utilization"
RATE_SUFFIX = "_rate"

# Path tracking sensors for repeaters and clients
PATH_SENSORS = [
    SensorEntityDescription(
        key="out_path",
        icon="mdi:map-marker-path",
        native_unit_of_measurement=None,
    ),
    SensorEntityDescription(
        key="out_path_len",
        icon="mdi:counter",
        native_unit_of_measurement="hops",
        state_class=SensorStateClass.MEASUREMENT,
    ),
]

# Reliability tracking sensors for repeaters and clients  
RELIABILITY_SENSORS = [
    SensorEntityDescription(
        key="request_successes",
        icon="mdi:check-circle",
        native_unit_of_measurement="requests",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="request_failures", 
        icon="mdi:alert-circle",
        native_unit_of_measurement="requests", 
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
]


# Define sensors for the main device
SENSORS = [
    SensorEntityDescription(
        key="node_status",
        icon="mdi:radio-tower"
    ),
    SensorEntityDescription(
        key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision="3",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="battery_percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        suggested_display_precision="2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="node_count",
        icon="mdi:account-group",
        state_class=SensorStateClass.MEASUREMENT
    ),
    SensorEntityDescription(
        key="tx_power",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power"
    ),
    SensorEntityDescription(
        key="latitude",
        icon="mdi:map-marker"
    ),
    SensorEntityDescription(
        key="longitude",
        icon="mdi:map-marker"
    ),
    SensorEntityDescription(
        key="frequency",
        native_unit_of_measurement="MHz",
        suggested_display_precision="3",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio"
    ),
    SensorEntityDescription(
        key="bandwidth",
        native_unit_of_measurement="kHz",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radio"
    ),
    SensorEntityDescription(
        key="spreading_factor",
        icon="mdi:radio"
    ),
]

# Sensors for remote nodes/contacts
CONTACT_SENSORS = [
    SensorEntityDescription(
        key="status",
        icon="mdi:radio-tower"
    ),
    SensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision="3",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="battery_percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        suggested_display_precision="2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="last_rssi",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
    SensorEntityDescription(
        key="last_snr",
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
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision="3",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="battery_percentage",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        suggested_display_precision="2",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    SensorEntityDescription(
        key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        suggested_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock",
    ),
    SensorEntityDescription(
        key="airtime",
        native_unit_of_measurement="min",
        suggested_display_precision="1",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:radio",
    ),
    SensorEntityDescription(
        key="nb_sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right",
    ),
    SensorEntityDescription(
        key="last_rssi",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
    SensorEntityDescription(
        key="last_snr",
        native_unit_of_measurement="dB",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal"
    ),
    SensorEntityDescription(
        key="nb_recv",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="tx_queue_len",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:playlist-edit",
    ),
    SensorEntityDescription(
        key="noise_floor",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:waveform",
    ),
    SensorEntityDescription(
        key="sent_flood",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right-outline",
    ),
    SensorEntityDescription(
        key="sent_direct",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right",
    ),
    SensorEntityDescription(
        key="recv_flood",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left-outline",
    ),
    SensorEntityDescription(
        key="recv_direct",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="full_evts",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:alert-circle",
    ),
    SensorEntityDescription(
        key="direct_dups",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:content-duplicate",
    ),
    SensorEntityDescription(
        key="flood_dups",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:content-duplicate",
    ),
    SensorEntityDescription(
        key="airtime_utilization",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement="%",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
    SensorEntityDescription(
        key="rx_airtime",
        native_unit_of_measurement="min",
        suggested_display_precision="1",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:radio",
    ),
    SensorEntityDescription(
        key="rx_airtime_utilization",
        device_class=SensorDeviceClass.POWER_FACTOR,
        native_unit_of_measurement="%",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
    SensorEntityDescription(
        key="direct_dups_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:content-duplicate",
    ),
    SensorEntityDescription(
        key="flood_dups_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:content-duplicate",
    ),
    SensorEntityDescription(
        key="nb_recv_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="nb_sent_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-arrow-right",
    ),
    SensorEntityDescription(
        key="recv_direct_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="recv_flood_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-arrow-left-outline",
    ),
    SensorEntityDescription(
        key="sent_direct_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-arrow-right",
    ),
    SensorEntityDescription(
        key="sent_flood_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-arrow-right-outline",
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

    # Add rate limiter monitoring sensor
    entities.append(RateLimiterSensor(coordinator))
    
    # Store the async_add_entities function for later use
    coordinator.sensor_add_entities = async_add_entities
    
    # Initialize telemetry sensor manager for dynamic sensor creation
    coordinator.telemetry_manager = TelemetrySensorManager(coordinator, async_add_entities)
    await coordinator.telemetry_manager.setup_telemetry_listener()
    
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
            
            # Add path tracking sensors for this repeater
            for path_description in PATH_SENSORS:
                try:
                    sensor = MeshCorePathSensor(
                        coordinator,
                        path_description,
                        repeater,
                        "repeater"
                    )
                    entities.append(sensor)
                except Exception as ex:
                    _LOGGER.error(f"Error creating path sensor {path_description.key} for repeater: {ex}")
            
            # Add reliability tracking sensors for this repeater
            for reliability_description in RELIABILITY_SENSORS:
                try:
                    sensor = MeshCoreReliabilitySensor(
                        coordinator,
                        reliability_description,
                        repeater,
                        "repeater"
                    )
                    entities.append(sensor)
                except Exception as ex:
                    _LOGGER.error(f"Error creating reliability sensor {reliability_description.key} for repeater: {ex}")

    # Add path sensors for tracked clients
    client_subscriptions = entry.data.get(CONF_TRACKED_CLIENTS, [])
    if client_subscriptions:
        for client in client_subscriptions:
            _LOGGER.info(f"Creating path sensors for client: {client.get('name')} ({client.get('pubkey_prefix')})")
            
            # Add path tracking sensors for this client
            for path_description in PATH_SENSORS:
                try:
                    sensor = MeshCorePathSensor(
                        coordinator,
                        path_description,
                        client,
                        "client"
                    )
                    entities.append(sensor)
                except Exception as ex:
                    _LOGGER.error(f"Error creating path sensor {path_description.key} for client: {ex}")
            
            # Add reliability tracking sensors for this client
            for reliability_description in RELIABILITY_SENSORS:
                try:
                    sensor = MeshCoreReliabilitySensor(
                        coordinator,
                        reliability_description,
                        client,
                        "client"
                    )
                    entities.append(sensor)
                except Exception as ex:
                    _LOGGER.error(f"Error creating reliability sensor {reliability_description.key} for client: {ex}")
    
    async_add_entities(entities)


class RateLimiterSensor(CoordinatorEntity, SensorEntity):
    """Sensor for monitoring rate limiter token bucket."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeshCoreDataUpdateCoordinator) -> None:
        """Initialize the rate limiter sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator

        raw_device_name = coordinator.name or "Unknown"
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""

        self._attr_unique_id = "_".join([
            coordinator.config_entry.entry_id,
            "rate_limiter_tokens",
            public_key_short,
            raw_device_name
        ])

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            "rate_limiter_tokens",
            raw_device_name
        )

        self._attr_native_unit_of_measurement = "tokens"
        self._attr_suggested_display_precision = 1
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:bucket-outline"
        self._attr_name = "Request Rate Limiter"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return self.coordinator.device_info

    @property
    def translation_key(self) -> str:
        """Return the translation key."""
        return "rate_limiter_tokens"

    @property
    def native_value(self) -> int:
        """Return current token count."""
        tokens = self.coordinator._rate_limiter.get_tokens()
        _LOGGER.debug(f"Rate limiter tokens: {tokens}")
        return tokens

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class MeshCoreSensor(CoordinatorEntity, SensorEntity):
    """Representation of a MeshCore sensor."""
    
    _attr_has_entity_name = True

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

        # Store cached values
        self._native_value = None

    @property
    def translation_key(self) -> str:
        """Return the translation key."""
        return self.entity_description.key

    @property
    def native_value(self) -> Any:
        """Return the native value of the sensor."""
        return self._native_value
        
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
                # Count all added contacts + self
                self._native_value = len([c for c in self.coordinator.get_all_contacts() if c.get("added_to_node", True)]) + 1
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.CONTACTS,
                update_count,
            )
            meshcore.dispatcher.subscribe(
                EventType.NEW_CONTACT,
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

class MeshCoreReliabilitySensor(CoordinatorEntity, SensorEntity):
    """Sensor for tracking request successes/failures for nodes."""
    
    _attr_has_entity_name = True
    
    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: SensorEntityDescription,
        node_config: dict,
        node_type: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.node_name = node_config.get("name", "Unknown")
        self.node_type = node_type
        self.pubkey_prefix = node_config.get("pubkey_prefix", "")
        self.public_key_short = self.pubkey_prefix[:6] if self.pubkey_prefix else ""

        self.device_id = f"{coordinator.config_entry.entry_id}_{node_type}_{self.pubkey_prefix}"
        device_name = f"MeshCore {node_type.title()}: {self.node_name} ({self.public_key_short})"
        self._attr_unique_id = f"{self.device_id}_{description.key}_{self.public_key_short}_{self.node_name}"
        
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            self.pubkey_prefix[:10],
            description.key,
            self.node_name
        )

        device_info = {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": device_name,
            "manufacturer": "MeshCore",
            "model": f"Mesh {node_type.title()}",
            "via_device": (DOMAIN, coordinator.config_entry.entry_id),
        }
        self._attr_device_info = DeviceInfo(**device_info)
        
        if not hasattr(coordinator, '_reliability_stats'):
            coordinator._reliability_stats = {}
        stats_key = f"{self.pubkey_prefix}_{description.key}"
        if stats_key not in coordinator._reliability_stats:
            coordinator._reliability_stats[stats_key] = 0

    @property
    def translation_key(self) -> str:
        """Return the translation key."""
        return self.entity_description.key

    @property
    def native_value(self) -> Any:
        if hasattr(self.coordinator, '_reliability_stats'):
            stats_key = f"{self.pubkey_prefix}_{self.entity_description.key}"
            return self.coordinator._reliability_stats.get(stats_key, 0)
        return 0

class MeshCorePathSensor(CoordinatorEntity, SensorEntity):
    """Sensor for tracking node routing path with CONTACTS event updates."""
    
    _attr_has_entity_name = True
    
    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: SensorEntityDescription,
        node_config: dict,
        node_type: str,
    ) -> None:
        """Initialize the path tracking sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self.node_name = node_config.get("name", "Unknown")
        self.node_type = node_type

        # Use the provided pubkey_prefix
        self.pubkey_prefix = node_config.get("pubkey_prefix", "")
        self.public_key_short = self.pubkey_prefix[:6] if self.pubkey_prefix else ""

        # Generate a unique device_id for this node using pubkey_prefix
        self.device_id = f"{coordinator.config_entry.entry_id}_{node_type}_{self.pubkey_prefix}"

        # Set friendly name

        # Build device name with pubkey
        device_name = f"MeshCore {node_type.title()}: {self.node_name} ({self.public_key_short})"
        
        # Set unique ID
        self._attr_unique_id = f"{self.device_id}_{description.key}_{self.public_key_short}_{self.node_name}"
        
        # Set entity ID
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            self.pubkey_prefix[:10],
            description.key,
            self.node_name
        )

        # Set device info to create a separate device for this node
        device_info = {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": device_name,
            "manufacturer": "MeshCore",
            "model": f"Mesh {node_type.title()}",
            "via_device": (DOMAIN, coordinator.config_entry.entry_id),  # Link to the main device
        }
            
        self._attr_device_info = DeviceInfo(**device_info)
        self._native_value = None
        
    async def async_added_to_hass(self):
        """Register event handlers when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Only set up listener if MeshCore instance is available
        if not self.coordinator.api.mesh_core:
            _LOGGER.warning(f"No MeshCore instance available for path tracking: {self.node_name}")
            return
            
        # Only set up if we have a pubkey_prefix
        if not self.pubkey_prefix:
            _LOGGER.warning(f"No pubkey_prefix available for node {self.node_name}, can't track path")
            return

        meshcore = self.coordinator.api.mesh_core
        
        def handle_contacts_event(event: Event):
            """Handle CONTACTS event to update path information."""
            if event.type != EventType.CONTACTS:
                return
                
            # Find our contact using the helper method
            contact = meshcore.get_contact_by_key_prefix(self.pubkey_prefix)
            if contact:
                # Update the sensor based on the description key
                if self.entity_description.key == "out_path":
                    self._native_value = contact.get("out_path", "")
                elif self.entity_description.key == "out_path_len":
                    path_len = contact.get("out_path_len", -1)
                    self._native_value = path_len if path_len != -1 else None
        
        # Subscribe to CONTACTS events
        meshcore.dispatcher.subscribe(
            EventType.CONTACTS,
            handle_contacts_event
        )
        
        _LOGGER.debug(f"Set up path tracking for {self.node_type} {self.node_name} ({self.pubkey_prefix})")

    @property
    def translation_key(self) -> str:
        """Return the translation key."""
        return self.entity_description.key

    @property
    def native_value(self) -> Any:
        return self._native_value

class MeshCoreRepeaterSensor(CoordinatorEntity, SensorEntity):
    """Sensor for repeater statistics with event-based updates."""
    
    _attr_has_entity_name = True
    
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
                _LOGGER.error(f"Skipping event with malformed payload: {event.payload}")
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
    def translation_key(self) -> str:
        """Return the translation key."""
        return self.entity_description.key
    
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
            
        elif key == "rx_airtime" and isinstance(value, (int, float)) and value > 0:
            return round(value / 60, 1)  # Convert seconds to minutes
            
        # Handle utilization calculations for all _utilization sensors  
        elif key.endswith(UTILIZATION_SUFFIX):
            # Extract the base metric name (remove '_utilization' suffix)
            base_key = key.removesuffix(UTILIZATION_SUFFIX)
            current_uptime = self._cached_stats.get("uptime")
            current_metric = self._cached_stats.get(base_key)
            
            if not isinstance(current_uptime, (int, float)) or not isinstance(current_metric, (int, float)):
                return None
                
            if self._previous_stats:
                prev_uptime = self._previous_stats.get("uptime", 0)
                prev_metric = self._previous_stats.get(base_key, 0)
                
                if isinstance(prev_uptime, (int, float)) and isinstance(prev_metric, (int, float)):
                    uptime_delta = current_uptime - prev_uptime
                    metric_delta = current_metric - prev_metric
                    
                    if uptime_delta > 0 and metric_delta >= 0:
                        utilization_rate = (metric_delta / uptime_delta) * 100
                        return round(utilization_rate, 1)
            return 0  # No previous data or no change
            
        # Handle rate calculations for message counters
        elif key.endswith(RATE_SUFFIX):
            # Extract the base metric name (remove '_rate' suffix)
            base_key = key.removesuffix(RATE_SUFFIX)
            current_uptime = self._cached_stats.get("uptime")
            current_count = self._cached_stats.get(base_key)
            
            if not isinstance(current_uptime, (int, float)) or not isinstance(current_count, (int, float)):
                return None
                
            if self._previous_stats:
                prev_uptime = self._previous_stats.get("uptime", 0)
                prev_count = self._previous_stats.get(base_key, 0)
                
                if isinstance(prev_uptime, (int, float)) and isinstance(prev_count, (int, float)):
                    uptime_delta = current_uptime - prev_uptime
                    count_delta = current_count - prev_count
                    
                    if uptime_delta > 0 and count_delta >= 0:
                        # Calculate messages per second, then convert to messages per minute
                        rate_per_second = count_delta / uptime_delta
                        rate_per_minute = rate_per_second * 60
                        return round(rate_per_minute, 1)
            return 0  # No previous data or no change
            
        # Return the value directly for other sensors
        return value
                
        
    @property
    def available(self) -> bool:
        """Return if the sensor is available."""
        # First check if we have cached stats from an event
        if self._cached_stats:
            last_updated = self._cached_stats.get("last_updated", 0)
            # Use dynamic timeout based on configured update interval
            update_interval = self.coordinator.get_device_update_interval(self.public_key)
            timeout = update_interval * SENSOR_AVAILABILITY_TIMEOUT_MULTIPLIER
            if time.time() - last_updated < timeout:
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
            elif key in ["uptime", "airtime", "rx_airtime"] and key in self._cached_stats:
                seconds = self._cached_stats[key]
                if isinstance(seconds, (int, float)) and seconds > 0:
                    # Add human-readable format for uptime
                    if key == "uptime":
                        days = seconds // 86400
                        hours = (seconds % 86400) // 3600
                        minutes = (seconds % 3600) // 60
                        secs = seconds % 60
                        attributes["human_readable"] = f"{days}d {hours}h {minutes}m {secs}s"
            elif key.endswith(UTILIZATION_SUFFIX):
                # Extract the base metric name (remove '_utilization' suffix)
                base_key = key.removesuffix(UTILIZATION_SUFFIX)
                uptime = self._cached_stats.get("uptime")
                metric_value = self._cached_stats.get(base_key)
                
                if isinstance(uptime, (int, float)) and isinstance(metric_value, (int, float)):
                    attributes["uptime_seconds"] = str(uptime)
                    attributes[f"{base_key}_seconds"] = str(metric_value)
                    
                    # Calculate metric delta since last update if we have previous values
                    if self._previous_stats:
                        prev_metric = self._previous_stats.get(base_key, 0)
                        if isinstance(prev_metric, (int, float)) and prev_metric < metric_value:
                            metric_delta = metric_value - prev_metric
                            attributes[f"{base_key}_since_last_update"] = str(round(metric_delta / 60, 1))  # in minutes
            elif key.endswith(RATE_SUFFIX):
                # Extract the base metric name (remove '_rate' suffix)
                base_key = key.removesuffix(RATE_SUFFIX)
                uptime = self._cached_stats.get("uptime")
                count = self._cached_stats.get(base_key)
                
                if isinstance(uptime, (int, float)) and isinstance(count, (int, float)):
                    attributes["uptime_seconds"] = str(uptime)
                    attributes[f"{base_key}_total"] = str(count)
                    
                    # Calculate count delta since last update if we have previous values
                    if self._previous_stats:
                        prev_count = self._previous_stats.get(base_key, 0)
                        prev_uptime = self._previous_stats.get("uptime", 0)
                        if isinstance(prev_count, (int, float)) and isinstance(prev_uptime, (int, float)):
                            count_delta = count - prev_count
                            uptime_delta = uptime - prev_uptime
                            if count_delta >= 0 and uptime_delta > 0:
                                attributes[f"{base_key}_since_last_update"] = str(count_delta)
                                attributes["uptime_delta_seconds"] = str(uptime_delta)
            
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
        elif key in ["uptime", "airtime", "rx_airtime"]:
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






