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
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    ENTITY_DOMAIN_SENSOR,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_NEIGHBORS_ENABLED,
    CONF_TRACKED_CLIENTS,
    CONF_SELF_DIAGNOSTICS_ENABLED,
    CONF_LIMIT_DISCOVERED_CONTACTS,
    CONF_MAX_DISCOVERED_CONTACTS,
    DEFAULT_MAX_DISCOVERED_CONTACTS,
    SENSOR_AVAILABILITY_TIMEOUT_MULTIPLIER,
    NEIGHBOR_STALE_THRESHOLD,
    SEEN_WINDOW_SECS,
    NodeType,
)
from .utils import (
    format_entity_id,
    calculate_battery_percentage,
    sanitize_name,
)
from .telemetry_sensor import TelemetrySensorManager

_LOGGER = logging.getLogger(__name__)

# Sensor key suffixes
UTILIZATION_SUFFIX = "_utilization"
RATE_SUFFIX = "_rate"

# A discovered contact is "fresh" if its last advert was heard within this
# window. Mirrors the per-contact binary_sensor freshness threshold
# (binary_sensor.py: 12 hours) so the summary's fresh/stale split matches what
# users already see on the individual contact entities.
DISCOVERED_FRESH_WINDOW_SECS = 3600 * 12

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

# Self-diagnostic sensors for the local companion node.
# Created only when CONF_SELF_DIAGNOSTICS_ENABLED is set (default off). Sourced
# from local get_stats_core/radio/packets polling (no mesh traffic). Definitions
# mirror the matching REPEATER_SENSORS keys/device-classes/units/icons so units,
# precision, and icons stay consistent across node types. Battery is intentionally
# omitted (the companion already exposes battery_voltage/battery_percentage).
# Unit conversions (seconds->minutes for uptime/airtime) are applied in the
# async_added_to_hass handlers below.
SELF_DIAGNOSTIC_SENSORS = [
    # STATS_CORE
    SensorEntityDescription(
        key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        suggested_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock",
    ),
    SensorEntityDescription(
        key="tx_queue_len",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:playlist-edit",
    ),
    # NOTE: STATS_CORE `errors` is intentionally NOT a sensor here — it is a
    # latching bitmask of radio fault events, not a count, so it is decoded
    # into individual `problem` binary sensors (see binary_sensor.py).
    # STATS_RADIO
    SensorEntityDescription(
        key="noise_floor",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:waveform",
    ),
    SensorEntityDescription(
        key="last_rssi",
        native_unit_of_measurement="dBm",
        suggested_display_precision="0",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
    ),
    SensorEntityDescription(
        key="last_snr",
        native_unit_of_measurement="dB",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:signal",
    ),
    SensorEntityDescription(
        key="tx_airtime",
        native_unit_of_measurement="min",
        suggested_display_precision="1",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:radio",
    ),
    SensorEntityDescription(
        key="rx_airtime",
        native_unit_of_measurement="min",
        suggested_display_precision="1",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:radio",
    ),
    # STATS_PACKETS
    SensorEntityDescription(
        key="nb_recv",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-left",
    ),
    SensorEntityDescription(
        key="nb_sent",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-arrow-right",
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
        key="recv_errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-alert",
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
        key="recv_errors",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:message-alert",
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
    SensorEntityDescription(
        key="recv_errors_rate",
        native_unit_of_measurement="msg/min",
        suggested_display_precision="1",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:message-alert",
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

    # Create self-diagnostic sensors only when opted in (default off). These
    # subscribe to the STATS_CORE/RADIO/PACKETS events emitted by the local
    # get_stats_* polling added to the coordinator.
    if entry.data.get(CONF_SELF_DIAGNOSTICS_ENABLED, False):
        for description in SELF_DIAGNOSTIC_SENSORS:
            entities.append(MeshCoreSensor(coordinator, description))

    # Add rate limiter monitoring sensor
    entities.append(RateLimiterSensor(coordinator))

    # Add companion prefix sensor (first byte of public key, used in routing paths)
    entities.append(MeshCoreCompanionPrefixSensor(coordinator))

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

                # Only consider devices belonging to this config entry
                if not device_id.startswith(entry.entry_id):
                    continue

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

            # Add neighbor count sensor when neighbor tracking is enabled
            if repeater.get(CONF_REPEATER_NEIGHBORS_ENABLED, False):
                try:
                    entities.append(
                        MeshCoreNeighborCountSensor(
                            coordinator,
                            repeater.get("pubkey_prefix", ""),
                            repeater.get("name", "Unknown"),
                        )
                    )
                except Exception as ex:
                    _LOGGER.error(f"Error creating neighbor count sensor for repeater: {ex}")

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

    # Add message delivery status sensor (tracks repeater count for channel msgs, ACK for direct msgs)
    delivery_sensor = LastMessageDeliverySensor(coordinator)
    entities.append(delivery_sensor)

    # Add the aggregate discovered-contact summary sensor. Created in both
    # modes (useful to default-mode users deciding whether to enable large
    # mesh mode), but registered disabled-by-default so it imposes no recorder
    # cost until a user opts to chart it. See MeshCoreDiscoveredSummarySensor.
    entities.append(MeshCoreDiscoveredSummarySensor(coordinator))

    async_add_entities(entities)

    # Set up listeners for outgoing message events to update the delivery sensor.
    # - meshcore_message_sent: fires immediately when a message is sent (from services.py)
    # - meshcore_delivery_update: fires on each intermediate collection pass (sensor only)
    # - meshcore_message: fires once on the final pass (logbook + sensor)
    from .logbook import EVENT_MESHCORE_MESSAGE, EVENT_MESHCORE_DELIVERY_UPDATE

    @callback
    def _handle_message_sent(event):
        """Immediately set sensor to 'waiting' when a message is sent."""
        data = event.data
        if data.get("message_type"):
            delivery_sensor.set_waiting(data)

    @callback
    def _handle_delivery_update(event):
        """Update delivery sensor on each progressive collection pass."""
        data = event.data
        if data.get("outgoing") and data.get("message_type"):
            delivery_sensor.update_from_event(data)

    @callback
    def _handle_message_event(event):
        """Update delivery sensor on the final logbook event."""
        data = event.data
        if data.get("outgoing") and data.get("message_type"):
            delivery_sensor.update_from_event(data)

    unsub_sent = hass.bus.async_listen(f"{DOMAIN}_message_sent", _handle_message_sent)
    unsub_delivery = hass.bus.async_listen(EVENT_MESHCORE_DELIVERY_UPDATE, _handle_delivery_update)
    unsub_logbook = hass.bus.async_listen(EVENT_MESHCORE_MESSAGE, _handle_message_event)
    entry.async_on_unload(unsub_sent)
    entry.async_on_unload(unsub_delivery)
    entry.async_on_unload(unsub_logbook)

    # Recreate sensor entities for any persisted neighbors (survives restarts)
    if coordinator._repeater_neighbors:
        persisted_entities = []
        for rptr_prefix, neighbors in coordinator._repeater_neighbors.items():
            # Look up repeater name and check if neighbors are enabled
            rptr_name = "Unknown"
            neighbors_enabled = False
            for sub in repeater_subscriptions:
                if sub.get("pubkey_prefix") == rptr_prefix:
                    rptr_name = sub.get("name", "Unknown")
                    neighbors_enabled = sub.get(CONF_REPEATER_NEIGHBORS_ENABLED, False)
                    break

            if not neighbors_enabled:
                continue

            for n_pubkey in neighbors:
                try:
                    snr_sensor = MeshCoreNeighborSensor(
                        coordinator=coordinator,
                        repeater_pubkey=rptr_prefix,
                        repeater_name=rptr_name,
                        neighbor_pubkey=n_pubkey,
                    )
                    seen_sensor = MeshCoreNeighborSeenSensor(
                        coordinator=coordinator,
                        repeater_pubkey=rptr_prefix,
                        repeater_name=rptr_name,
                        neighbor_pubkey=n_pubkey,
                    )
                    persisted_entities.extend([snr_sensor, seen_sensor])
                    # Mark as created so _fetch_repeater_neighbors doesn't
                    # try to duplicate them on the next poll cycle
                    coordinator._created_neighbor_sensors.add(
                        f"{rptr_prefix}:{n_pubkey}"
                    )
                except Exception as ex:
                    _LOGGER.error("Error recreating neighbor sensor for %s: %s", n_pubkey, ex)

        if persisted_entities:
            async_add_entities(persisted_entities)
            _LOGGER.info(
                "Recreated %d neighbor sensor entities from persisted data",
                len(persisted_entities),
            )


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
        ])

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            "rate_limiter_tokens",
            sanitize_name(raw_device_name)
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


class LastMessageDeliverySensor(CoordinatorEntity, SensorEntity):
    """Sensor showing delivery status of the last sent message.

    For channel messages: shows how many repeaters relayed the message by
    counting RX_LOG re-broadcasts (similar to MeshCore iOS app feedback).

    For direct messages: shows whether the recipient acknowledged (ACK'd)
    the message, confirming delivery.

    The sensor state is a human-readable delivery summary. Detailed data
    (per-repeater RSSI/SNR, ACK status, etc.) is available as attributes.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: MeshCoreDataUpdateCoordinator) -> None:
        """Initialize the last message delivery sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator

        raw_device_name = coordinator.name or "Unknown"
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""

        self._attr_unique_id = "_".join([
            coordinator.config_entry.entry_id,
            "last_message_delivery",
            public_key_short,
        ])

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            "last_message_delivery",
            sanitize_name(raw_device_name)
        )

        self._attr_icon = "mdi:radio-tower"
        self._attr_name = "Last Message Delivery"

        # Internal state – default to "Idle" so the entity is never "unavailable"
        self._state: str = "Idle"
        self._message_type: str | None = None
        self._repeater_count: int | None = None
        self._ack_received: bool | None = None
        self._rx_log_data: list[dict] = []
        self._last_message: str | None = None
        self._current_send_id: str | None = None
        self._last_send_time: str | None = None
        self._receiver: str | None = None
        self._channel: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return self.coordinator.device_info

    @property
    def native_value(self) -> str | None:
        """Return a human-readable delivery status string."""
        return self._state

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return detailed delivery data as attributes."""
        attrs: Dict[str, Any] = {}
        if self._message_type is not None:
            attrs["message_type"] = self._message_type
        if self._last_message is not None:
            attrs["last_message"] = self._last_message
        if self._last_send_time is not None:
            attrs["last_send_time"] = self._last_send_time

        # Channel-specific attributes
        if self._message_type == "channel":
            attrs["repeater_count"] = self._repeater_count
            if self._channel is not None:
                attrs["channel"] = self._channel
            if self._rx_log_data:
                attrs["rx_log_data"] = self._rx_log_data
                attrs["repeater_details"] = [
                    {
                        "snr": entry.get("snr"),
                        "rssi": entry.get("rssi"),
                        "path_len": entry.get("path_len"),
                        "path": entry.get("path"),
                    }
                    for entry in self._rx_log_data
                ]

        # Direct message-specific attributes
        if self._message_type == "direct":
            if self._ack_received is not None:
                attrs["ack_received"] = self._ack_received
            if self._receiver is not None:
                attrs["receiver"] = self._receiver

        return attrs

    def set_waiting(self, event_data: dict) -> None:
        """Set sensor to 'waiting' state immediately when a message is sent."""
        self._current_send_id = event_data.get("send_id")
        self._message_type = event_data.get("message_type")
        self._last_message = event_data.get("message")
        self._last_send_time = event_data.get("timestamp")
        self._channel = event_data.get("channel") or (
            f"channel_{event_data.get('channel_idx', 0)}"
            if self._message_type == "channel" else None
        )
        self._receiver = event_data.get("receiver")
        self._repeater_count = None
        self._rx_log_data = []
        self._ack_received = None
        self._state = "Waiting"
        self.async_write_ha_state()

    def update_from_event(self, event_data: dict) -> None:
        """Update sensor state from a meshcore_message logbook event.

        For channel messages with progressive=True, this merges new RX_LOG
        entries with any already collected, giving rolling repeater counts.

        Events from a previous send (stale send_id) are silently ignored
        so that a rapid follow-up message isn't overwritten by late arrivals
        from the earlier send's collection loop.
        """
        # Ignore updates from a previous (superseded) send
        event_send_id = event_data.get("send_id")
        if event_send_id and self._current_send_id and event_send_id != self._current_send_id:
            return

        self._message_type = event_data.get("message_type")
        self._last_message = event_data.get("message")
        self._last_send_time = event_data.get("timestamp")

        if self._message_type == "channel":
            new_rx_logs = event_data.get("rx_log_data", [])
            is_progressive = event_data.get("progressive", False)

            if is_progressive and self._rx_log_data:
                # Merge: append only entries we haven't already seen.
                # Each RX_LOG entry is identified by its LoRa reception
                # characteristics — keys that actually exist in the entry schema.
                existing_ids = set()
                for entry in self._rx_log_data:
                    eid = (
                        entry.get("snr"),
                        entry.get("rssi"),
                        entry.get("path", ""),
                        entry.get("channel_hash", ""),
                        entry.get("timestamp"),
                    )
                    existing_ids.add(eid)
                for entry in new_rx_logs:
                    eid = (
                        entry.get("snr"),
                        entry.get("rssi"),
                        entry.get("path", ""),
                        entry.get("channel_hash", ""),
                        entry.get("timestamp"),
                    )
                    if eid not in existing_ids:
                        self._rx_log_data.append(entry)
                        existing_ids.add(eid)
            else:
                self._rx_log_data = new_rx_logs

            self._repeater_count = len(self._rx_log_data)
            self._channel = event_data.get("channel")
            self._ack_received = None
            self._receiver = None
            count = self._repeater_count or 0
            if count == 0 and is_progressive:
                self._state = "Waiting"
            else:
                self._state = f"{count} Repeater{'s' if count != 1 else ''}"

        elif self._message_type == "direct":
            self._ack_received = event_data.get("ack_received")
            self._receiver = event_data.get("receiver_name")
            self._repeater_count = None
            self._rx_log_data = []
            self._channel = None
            if self._ack_received is True:
                self._state = "Delivered"
            elif self._ack_received is False:
                self._state = "Unconfirmed"
            else:
                self._state = "Sent"

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
        parts = [part for part in [coordinator.config_entry.entry_id, description.key, public_key_short] if part]
        self._attr_unique_id = "_".join(parts)

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            description.key,
            sanitize_name(raw_device_name)
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
                self._native_value = "online" if self.coordinator.api.connected else "offline"
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                None,
                update_status,
            )
            # Seed initial state from the current connection: the API may
            # already be connected before the first event arrives post-restart.
            self._native_value = "online" if self.coordinator.api.connected else "offline"

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
                self._native_value = event.payload.get("tx_power")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_tx,
            )

        elif key == "latitude":
            def update_lat(event: Event):
                self._native_value = event.payload.get("adv_lat")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_lat,
            )

        elif key == "longitude":
            def update_lon(event: Event):
                self._native_value = event.payload.get("adv_lon")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_lon,
            )

        elif key == "frequency":
            def update_freq(event: Event):
                self._native_value = event.payload.get("radio_freq")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_freq,
            )

        elif key == "bandwidth":
            def update_bw(event: Event):
                self._native_value = event.payload.get("radio_bw")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_bw,
            )

        elif key == "spreading_factor":
            def update_sf(event: Event):
                self._native_value = event.payload.get("radio_sf")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO,
                update_sf,
            )

        # --- Self-diagnostic sensors (local get_stats_* polling) ---
        # STATS_CORE
        elif key == "uptime":
            def update_uptime(event: Event):
                value = event.payload.get("uptime_secs")
                self._native_value = round(value / 60, 1) if isinstance(value, (int, float)) else None
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_CORE, update_uptime)

        elif key == "tx_queue_len":
            def update_queue_len(event: Event):
                self._native_value = event.payload.get("queue_len")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_CORE, update_queue_len)

        # STATS_CORE `errors` is decoded into `problem` binary sensors
        # (see binary_sensor.py MeshCoreSelfDiagnosticBinarySensor) rather
        # than a numeric sensor — it is a latching fault-flag bitmask.

        # STATS_RADIO
        elif key == "noise_floor":
            def update_noise_floor(event: Event):
                self._native_value = event.payload.get("noise_floor")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_RADIO, update_noise_floor)

        elif key == "last_rssi":
            def update_last_rssi(event: Event):
                self._native_value = event.payload.get("last_rssi")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_RADIO, update_last_rssi)

        elif key == "last_snr":
            def update_last_snr(event: Event):
                # SDK already unscales SNR (raw value was multiplied by 4)
                self._native_value = event.payload.get("last_snr")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_RADIO, update_last_snr)

        elif key == "tx_airtime":
            def update_tx_airtime(event: Event):
                value = event.payload.get("tx_air_secs")
                self._native_value = round(value / 60, 1) if isinstance(value, (int, float)) else None
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_RADIO, update_tx_airtime)

        elif key == "rx_airtime":
            def update_rx_airtime(event: Event):
                value = event.payload.get("rx_air_secs")
                self._native_value = round(value / 60, 1) if isinstance(value, (int, float)) else None
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_RADIO, update_rx_airtime)

        # STATS_PACKETS
        elif key == "nb_recv":
            def update_nb_recv(event: Event):
                self._native_value = event.payload.get("recv")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_nb_recv)

        elif key == "nb_sent":
            def update_nb_sent(event: Event):
                self._native_value = event.payload.get("sent")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_nb_sent)

        elif key == "sent_flood":
            def update_sent_flood(event: Event):
                self._native_value = event.payload.get("flood_tx")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_sent_flood)

        elif key == "sent_direct":
            def update_sent_direct(event: Event):
                self._native_value = event.payload.get("direct_tx")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_sent_direct)

        elif key == "recv_flood":
            def update_recv_flood(event: Event):
                self._native_value = event.payload.get("flood_rx")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_recv_flood)

        elif key == "recv_direct":
            def update_recv_direct(event: Event):
                self._native_value = event.payload.get("direct_rx")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_recv_direct)

        elif key == "recv_errors":
            def update_recv_errors(event: Event):
                # May be None on a legacy 26-byte STATS_PACKETS frame
                self._native_value = event.payload.get("recv_errors")
                self.async_write_ha_state()
            meshcore.dispatcher.subscribe(EventType.STATS_PACKETS, update_recv_errors)


    @property
    def device_info(self):
        return DeviceInfo(**self.coordinator.device_info)

    @property
    def native_value(self) -> Any:
        return self._native_value

class MeshCoreCompanionPrefixSensor(CoordinatorEntity, SensorEntity):
    """Sensor displaying the device's companion prefix from its public key.

    In MeshCore, the first N bytes of a node's public key are used as its
    routing prefix — the short identifier shown in message paths to indicate
    which repeaters a packet traversed.

    The prefix length is determined by the device's path_hash_mode setting
    (available in firmware v1.14.0+ / protocol v10+):
      - mode 0: 1 byte  (2 hex chars)  — default
      - mode 1: 2 bytes (4 hex chars)
      - mode 2: 3 bytes (6 hex chars)
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: MeshCoreDataUpdateCoordinator) -> None:
        """Initialize the companion prefix sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""

        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_companion_prefix_{public_key_short}"
        )
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR, public_key_short, "companion_prefix"
        )
        self._attr_name = "Companion Prefix"
        self._attr_icon = "mdi:routes"
        self._full_key = coordinator.pubkey or ""
        # path_hash_mode: 0 = 1 byte, 1 = 2 bytes, 2 = 3 bytes
        # Default to 0 (1 byte) for firmware versions that don't report it
        self._path_hash_mode = 0

        # Try to read initial path_hash_mode from cached SELF_INFO
        if coordinator.api._last_self_info:
            self._path_hash_mode = coordinator.api._last_self_info.get(
                "path_hash_mode", 0
            )

        # Subscribe to SELF_INFO for live updates
        if coordinator.api.mesh_core:
            meshcore = coordinator.api.mesh_core

            def update_from_self_info(event: Event):
                changed = False
                new_key = event.payload.get("public_key")
                if new_key and new_key != self._full_key:
                    self._full_key = new_key
                    changed = True
                new_mode = event.payload.get("path_hash_mode")
                if new_mode is not None and new_mode != self._path_hash_mode:
                    self._path_hash_mode = new_mode
                    changed = True
                if changed:
                    self.async_write_ha_state()

            meshcore.dispatcher.subscribe(
                EventType.SELF_INFO, update_from_self_info
            )

    @property
    def _prefix_byte_len(self) -> int:
        """Return the prefix length in bytes based on path_hash_mode."""
        return self._path_hash_mode + 1

    @property
    def device_info(self):
        """Return device info."""
        return DeviceInfo(**self.coordinator.device_info)

    @property
    def native_value(self) -> str:
        """Return the companion prefix (N bytes of public key as hex chars)."""
        hex_chars = self._prefix_byte_len * 2
        if self._full_key and len(self._full_key) >= hex_chars:
            return self._full_key[:hex_chars].upper()
        return "Unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full public key and path hash mode as attributes."""
        mode_labels = {0: "1 byte", 1: "2 bytes", 2: "3 bytes"}
        return {
            "public_key": self._full_key,
            "path_hash_mode": self._path_hash_mode,
            "prefix_length": mode_labels.get(self._path_hash_mode, "unknown"),
        }


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
        self._attr_unique_id = f"{self.device_id}_{description.key}_{self.public_key_short}"

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            self.pubkey_prefix[:10],
            description.key,
            sanitize_name(self.node_name)
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
        self._attr_unique_id = f"{self.device_id}_{description.key}_{self.public_key_short}"

        # Set entity ID
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            self.pubkey_prefix[:10],
            description.key,
            sanitize_name(self.node_name)
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
        self._attr_unique_id = f"{self.device_id}_{description.key}_{self.public_key_short}"

        # Set entity ID
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            self.public_key[:10],
            description.key,
            sanitize_name(self.repeater_name)
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


class MeshCoreNeighborSensor(CoordinatorEntity, SensorEntity):
    """Sensor tracking SNR from a repeater to one of its neighbors.

    Created dynamically by the coordinator when a new neighbor is discovered
    during a repeater update cycle. The sensor reads its state from
    coordinator._repeater_neighbors[repeater_pubkey][neighbor_pubkey].
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "dB"
    _attr_icon = "mdi:signal-variant"

    def __init__(
        self,
        coordinator: MeshCoreDataUpdateCoordinator,
        repeater_pubkey: str,
        repeater_name: str,
        neighbor_pubkey: str,
    ) -> None:
        """Initialize the neighbor SNR sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._repeater_pubkey = repeater_pubkey
        self._repeater_name = repeater_name
        self._neighbor_pubkey = neighbor_pubkey
        self._repeater_pubkey_short = repeater_pubkey[:6] if repeater_pubkey else ""
        self._neighbor_pubkey_short = neighbor_pubkey[:6].lower() if neighbor_pubkey else ""

        # Build device_id matching the repeater's existing device
        self._device_id = f"{coordinator.config_entry.entry_id}_repeater_{repeater_pubkey}"

        # Unique ID uses 12-char neighbor prefix for stability
        self._attr_unique_id = (
            f"{self._device_id}_neighbor_{neighbor_pubkey[:12]}_snr"
        )

        # Entity ID: sensor.meshcore_{repeater_pubkey[:10]}_neighbor_{neighbor_pubkey[:6]}
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            repeater_pubkey[:10],
            "neighbor",
            self._neighbor_pubkey_short,
        )

        # Resolve initial name
        self._resolved_name = coordinator.resolve_neighbor_name(neighbor_pubkey)

        # Friendly name: "{Repeater} Neighbor {Name} SNR"
        self._update_friendly_name()

        # Device info — attach to the repeater's existing device
        device_name = f"MeshCore Repeater: {repeater_name} ({self._repeater_pubkey_short})"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device_name,
            manufacturer="MeshCore",
            model="Mesh Repeater",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _update_friendly_name(self):
        """Update the friendly name based on current resolved name."""
        self._attr_name = f"Neighbor {self._resolved_name} SNR"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates — re-resolve friendly name before state write.

        Picks up name changes when a neighbor later shows up in the contact list,
        without depending on the frontend reading extra_state_attributes.
        """
        new_name = self.coordinator.resolve_neighbor_name(self._neighbor_pubkey)
        if new_name != self._resolved_name:
            self._resolved_name = new_name
            self._update_friendly_name()
        super()._handle_coordinator_update()

    @property
    def _neighbor_data(self) -> dict | None:
        """Get current neighbor data from coordinator."""
        repeater_neighbors = self.coordinator._repeater_neighbors.get(self._repeater_pubkey, {})
        return repeater_neighbors.get(self._neighbor_pubkey)

    @property
    def available(self) -> bool:
        """Return True if the neighbor was heard within the stale threshold."""
        data = self._neighbor_data
        if data is None:
            return False
        secs_ago = data.get("secs_ago", 0)
        return secs_ago < NEIGHBOR_STALE_THRESHOLD

    @property
    def native_value(self) -> float | None:
        """Return the SNR value."""
        data = self._neighbor_data
        if data is None:
            return None
        return data.get("snr")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes for the neighbor."""
        data = self._neighbor_data
        if data is None:
            return {}

        secs_ago = data.get("secs_ago", 0)

        # Human-readable last seen
        if secs_ago < 60:
            last_seen = f"{secs_ago}s ago"
        elif secs_ago < 3600:
            last_seen = f"{secs_ago // 60}m {secs_ago % 60}s ago"
        elif secs_ago < 86400:
            hours = secs_ago // 3600
            mins = (secs_ago % 3600) // 60
            last_seen = f"{hours}h {mins}m ago"
        else:
            days = secs_ago // 86400
            hours = (secs_ago % 86400) // 3600
            last_seen = f"{days}d {hours}h ago"

        last_updated = data.get("last_updated")
        last_updated_str = (
            datetime.fromtimestamp(last_updated).isoformat()
            if last_updated
            else "Unknown"
        )

        return {
            "secs_ago": secs_ago,
            "last_seen": last_seen,
            "pubkey_prefix": self._neighbor_pubkey,
            "resolved_name": self._resolved_name,
            "last_updated": last_updated_str,
            "seen_48h": len([t for t in data.get("seen_timestamps", []) if t > time.time() - SEEN_WINDOW_SECS]),
        }


class MeshCoreNeighborSeenSensor(CoordinatorEntity, SensorEntity):
    """Sensor tracking how many times a neighbor has been seen in the last 48 hours.

    Uses state_class MEASUREMENT since the 48h rolling window can decrease
    as old sightings age out.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:counter"

    def __init__(
        self,
        coordinator: MeshCoreDataUpdateCoordinator,
        repeater_pubkey: str,
        repeater_name: str,
        neighbor_pubkey: str,
    ) -> None:
        """Initialize the neighbor seen counter sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._repeater_pubkey = repeater_pubkey
        self._repeater_name = repeater_name
        self._neighbor_pubkey = neighbor_pubkey
        self._repeater_pubkey_short = repeater_pubkey[:6] if repeater_pubkey else ""
        self._neighbor_pubkey_short = neighbor_pubkey[:6].lower() if neighbor_pubkey else ""

        # Build device_id matching the repeater's existing device
        self._device_id = f"{coordinator.config_entry.entry_id}_repeater_{repeater_pubkey}"

        # Unique ID: distinct from the SNR sensor (_seen vs _snr suffix)
        self._attr_unique_id = (
            f"{self._device_id}_neighbor_{neighbor_pubkey[:12]}_seen"
        )

        # Entity ID: sensor.meshcore_{repeater_short}_neighbor_{neighbor_short}_seen
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            repeater_pubkey[:10],
            "neighbor",
            f"{self._neighbor_pubkey_short}_seen",
        )

        # Resolve initial name
        self._resolved_name = coordinator.resolve_neighbor_name(neighbor_pubkey)
        self._update_friendly_name()

        # Device info — attach to the repeater's existing device
        device_name = f"MeshCore Repeater: {repeater_name} ({self._repeater_pubkey_short})"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device_name,
            manufacturer="MeshCore",
            model="Mesh Repeater",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _update_friendly_name(self):
        """Update the friendly name based on current resolved name."""
        self._attr_name = f"Neighbor {self._resolved_name} Seen"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates — re-resolve friendly name before state write.

        Picks up name changes when a neighbor later shows up in the contact list,
        without depending on the frontend reading extra_state_attributes.
        """
        new_name = self.coordinator.resolve_neighbor_name(self._neighbor_pubkey)
        if new_name != self._resolved_name:
            self._resolved_name = new_name
            self._update_friendly_name()
        super()._handle_coordinator_update()

    @property
    def _neighbor_data(self) -> dict | None:
        """Get current neighbor data from coordinator."""
        repeater_neighbors = self.coordinator._repeater_neighbors.get(self._repeater_pubkey, {})
        return repeater_neighbors.get(self._neighbor_pubkey)

    @property
    def available(self) -> bool:
        """Return True if the neighbor was heard within the stale threshold."""
        data = self._neighbor_data
        if data is None:
            return False
        secs_ago = data.get("secs_ago", 0)
        return secs_ago < NEIGHBOR_STALE_THRESHOLD

    @property
    def native_value(self) -> int | None:
        """Return the number of sightings in the last 48 hours."""
        data = self._neighbor_data
        if data is None:
            return None
        cutoff = time.time() - SEEN_WINDOW_SECS
        return len([t for t in data.get("seen_timestamps", []) if t > cutoff])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "pubkey_prefix": self._neighbor_pubkey,
            "resolved_name": self._resolved_name,
        }


class MeshCoreNeighborCountSensor(CoordinatorEntity, SensorEntity):
    """Sensor tracking the number of neighbors known for a repeater."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-group"
    _attr_name = "Neighbor Count"

    def __init__(
        self,
        coordinator: MeshCoreDataUpdateCoordinator,
        repeater_pubkey: str,
        repeater_name: str,
    ) -> None:
        """Initialize the neighbor count sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._repeater_pubkey = repeater_pubkey
        self._repeater_name = repeater_name
        self._repeater_pubkey_short = repeater_pubkey[:6] if repeater_pubkey else ""

        self._device_id = f"{coordinator.config_entry.entry_id}_repeater_{repeater_pubkey}"
        self._attr_unique_id = f"{self._device_id}_neighbor_count"

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            repeater_pubkey[:10],
            "neighbor_count",
        )

        device_name = f"MeshCore Repeater: {repeater_name} ({self._repeater_pubkey_short})"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device_name,
            manufacturer="MeshCore",
            model="Mesh Repeater",
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> int:  # type: ignore[override]
        """Return the total number of neighbors known for this repeater."""
        return len(self.coordinator._repeater_neighbors.get(self._repeater_pubkey, {}))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # type: ignore[override]
        """Split the total into active (within stale threshold) and stale."""
        neighbors = self.coordinator._repeater_neighbors.get(self._repeater_pubkey, {})
        active = sum(
            1 for n in neighbors.values()
            if n.get("secs_ago", 0) < NEIGHBOR_STALE_THRESHOLD
        )
        return {"active": active, "stale": len(neighbors) - active}


class MeshCoreDiscoveredSummarySensor(CoordinatorEntity, SensorEntity):
    """Aggregate summary of discovered (un-added) contacts on the main device.

    State is the total discovered-contact count. Attributes carry a small,
    bounded rollup (fresh/stale split, per-type counts, the single most-recent
    advert, and capacity headroom) so the discovered contacts in data-only mode
    remain inspectable at a glance without one entity per contact.

    Registered disabled-by-default and under the diagnostic category. The state
    is a count that changes on every advert, so leaving it enabled would write
    a recorder time-series on every install -- including dense-mesh users who
    never opted in -- which is the exact recorder churn data-only mode exists
    to reduce. Disabled-by-default keeps it free until a user chooses to chart
    it. The attribute payload is counts + one sample, so its size is constant
    regardless of how many contacts are discovered.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-search"
    _attr_name = "Discovered Contacts"

    def __init__(self, coordinator: MeshCoreDataUpdateCoordinator) -> None:
        """Initialize the discovered-contact summary sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator

        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""

        self._attr_unique_id = "_".join([
            coordinator.config_entry.entry_id,
            "discovered_summary",
        ])

        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_SENSOR,
            public_key_short,
            "discovered_summary",
            sanitize_name(coordinator.name or "Unknown"),
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info (attach to the main companion device)."""
        return self.coordinator.device_info

    @property
    def translation_key(self) -> str:
        """Return the translation key."""
        return "discovered_summary"

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> int:  # type: ignore[override]
        """Return the total number of discovered (un-added) contacts."""
        return len(self.coordinator._discovered_contacts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:  # type: ignore[override]
        """Return a bounded rollup of the discovered-contact set.

        Shape is constant regardless of contact count: scalar counts, a
        fixed-key per-type breakdown, one newest-advert sample, and capacity
        headroom. No per-contact detail is emitted.
        """
        discovered = self.coordinator._discovered_contacts
        now = time.time()

        # Fixed-key per-type breakdown so the attribute shape never changes.
        by_type: Dict[str, int] = {
            "chat": 0,           # NodeType.CLIENT
            "repeater": 0,       # NodeType.REPEATER
            "room_server": 0,    # NodeType.ROOM_SERVER
            "sensor": 0,         # NodeType.SENSOR
            "unknown": 0,        # missing / unrecognized type
        }
        type_key = {
            NodeType.CLIENT: "chat",
            NodeType.REPEATER: "repeater",
            NodeType.ROOM_SERVER: "room_server",
            NodeType.SENSOR: "sensor",
        }

        fresh_count = 0
        newest_contact = None
        newest_advert = -1.0
        for contact in discovered.values():
            last_advert = contact.get("last_advert", 0) or 0
            if last_advert and (now - last_advert) < DISCOVERED_FRESH_WINDOW_SECS:
                fresh_count += 1
            by_type[type_key.get(contact.get("type"), "unknown")] += 1
            if last_advert > newest_advert:
                newest_advert = last_advert
                newest_contact = contact

        total = len(discovered)

        if newest_contact is not None:
            newest_pubkey = newest_contact.get("public_key", "") or ""
            newest = {
                "adv_name": newest_contact.get("adv_name", "Unknown"),
                "pubkey_short": newest_pubkey[:12],
                "last_advert": newest_contact.get("last_advert", 0) or 0,
            }
        else:
            newest = None

        # Capacity headroom: only meaningful when the discovered-contact limit
        # is enabled; otherwise the set is unbounded by count.
        if self.coordinator.config_entry.data.get(CONF_LIMIT_DISCOVERED_CONTACTS, False):
            max_contacts = self.coordinator.config_entry.data.get(
                CONF_MAX_DISCOVERED_CONTACTS, DEFAULT_MAX_DISCOVERED_CONTACTS
            )
            capacity: Any = max_contacts
            capacity_used_pct: Any = (
                round(100.0 * total / max_contacts, 1) if max_contacts else None
            )
        else:
            capacity = "unlimited"
            capacity_used_pct = None

        return {
            "fresh_count": fresh_count,
            "stale_count": total - fresh_count,
            "by_type": by_type,
            "newest": newest,
            "capacity": capacity,
            "capacity_used_pct": capacity_used_pct,
        }
