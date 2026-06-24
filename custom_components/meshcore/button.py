"""Button platform for MeshCore integration.

Provides the CLI Console controls as button entities so they appear on the
device page automatically and render compactly (unlike a full button *card*).
Created only when CONF_CLI_CONSOLE_ENABLED is set.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLI_CONSOLE_ENABLED,
    DOMAIN,
    ENTITY_DOMAIN_BUTTON,
    SERVICE_CLI_COMMAND_UI,
)
from .utils import format_entity_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore button entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[ButtonEntity] = []
    if entry.data.get(CONF_CLI_CONSOLE_ENABLED, False):
        entities.append(MeshCoreCLIRunButton(coordinator))
        entities.append(MeshCoreCLIClearButton(coordinator))

    if entities:
        async_add_entities(entities)


class _MeshCoreCLIButton(CoordinatorEntity, ButtonEntity):
    """Shared base for CLI Console buttons (attached to the companion device)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the main companion device so it shows on the device page."""
        return DeviceInfo(**self.coordinator.device_info)


class MeshCoreCLIRunButton(_MeshCoreCLIButton):
    """Runs the command in text.meshcore_command through the CLI Console."""

    _attr_name = "CLI Run Command"
    _attr_icon = "mdi:play"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_cli_run"
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BUTTON, public_key_short, "cli_run"
        )

    async def async_press(self) -> None:
        """Execute the command in the input helper and record its output."""
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_CLI_COMMAND_UI,
            {"entry_id": self.coordinator.config_entry.entry_id},
            blocking=True,
        )


class MeshCoreCLIClearButton(_MeshCoreCLIButton):
    """Clears the CLI Console transcript."""

    _attr_name = "CLI Clear Console"
    _attr_icon = "mdi:notification-clear-all"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_cli_clear"
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BUTTON, public_key_short, "cli_clear"
        )

    async def async_press(self) -> None:
        """Empty the console transcript."""
        self.coordinator.clear_cli_console()
