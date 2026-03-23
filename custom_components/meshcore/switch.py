"""Switch platform for MeshCore integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .utils import format_entity_id, sanitize_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore switch entities from config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Initialize rx_log_enabled on the coordinator (defaults to OFF)
    coordinator.rx_log_enabled = False

    async_add_entities([RxMessageLogSwitch(coordinator)])


class RxMessageLogSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable RX Message Log."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator) -> None:
        """Initialize the RX Message Log switch."""
        super().__init__(coordinator)

        device_key = coordinator.pubkey or ""
        pubkey6 = device_key[:6]

        # Unique ID
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_rx_message_log_{pubkey6}"
        )

        # Entity ID: switch.meshcore_{pubkey6}_rx_message_log
        self.entity_id = format_entity_id(
            "switch", pubkey6, "rx_message_log"
        )

        self._attr_name = "RX Message Log"
        self._attr_icon = "mdi:radio-tower"

    @property
    def device_info(self):
        """Return device info to link this entity to the MeshCore device."""
        return DeviceInfo(**self.coordinator.device_info)

    @property
    def is_on(self) -> bool:
        """Return true if the RX message log is enabled."""
        return getattr(self.coordinator, "rx_log_enabled", False)

    @property
    def icon(self) -> str:
        """Return icon based on state."""
        return "mdi:radio-tower" if self.is_on else "mdi:radio-tower-off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the RX message log."""
        self.coordinator.rx_log_enabled = True
        _LOGGER.info("RX Message Log enabled")
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the RX message log."""
        self.coordinator.rx_log_enabled = False
        _LOGGER.info("RX Message Log disabled")
        self.async_write_ha_state()
