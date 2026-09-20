"""Button platform for MeshCore integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CLI_CONSOLE_ENABLED,
    CONF_REPEATER_SUBSCRIPTIONS,
    DOMAIN,
    ENTITY_DOMAIN_BUTTON,
    SERVICE_EXECUTE_COMMAND_UI,
)
from .repeater_firmware import (
    RepeaterFirmwareRefreshError,
    async_refresh_repeater_firmware,
)
from .traffic import COST_LOGIN_STATUS
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

    entities.extend(
        MeshCoreRepeaterFirmwareRefreshButton(coordinator, repeater)
        for repeater in entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, [])
    )

    if entities:
        async_add_entities(entities)


class _MeshCoreCLIButton(CoordinatorEntity, ButtonEntity):
    """Shared base for CLI Console buttons.

    Deliberately NOT attached to the companion device and hidden by default:
    the console only works as a dashboard card (the device page can't render the
    transcript), so surfacing these on the device page would be misleading.
    They're used by referencing their entity_id in the CLI Console card.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_registry_visible_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator


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
            SERVICE_EXECUTE_COMMAND_UI,
            {
                "entry_id": self.coordinator.config_entry.entry_id,
                "record_to_console": True,
            },
            blocking=True,
            context=self._context,
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


class MeshCoreRepeaterFirmwareRefreshButton(CoordinatorEntity, ButtonEntity):
    """Refresh a configured repeater's firmware version on demand."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Refresh firmware version"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator, repeater: dict) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.pubkey_prefix = repeater.get("pubkey_prefix", "")
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_repeater_{self.pubkey_prefix}_refresh_firmware"
        )
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BUTTON, self.pubkey_prefix[:10], "refresh_firmware"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{coordinator.config_entry.entry_id}_repeater_{self.pubkey_prefix}",
                )
            }
        )

    async def async_press(self) -> None:
        """Query the repeater and persist its reported firmware version."""
        if not self.coordinator.api.connected:
            raise HomeAssistantError("MeshCore device is not connected")

        self.coordinator.require_mesh_budget(COST_LOGIN_STATUS)

        try:
            await async_refresh_repeater_firmware(
                self.hass,
                self.coordinator.config_entry,
                self.coordinator.api,
                self.pubkey_prefix,
            )
        except RepeaterFirmwareRefreshError as ex:
            raise HomeAssistantError(f"Failed to refresh repeater firmware version: {ex}") from ex
