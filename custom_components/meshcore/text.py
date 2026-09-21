"""Text platform for MeshCore integration."""
from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class MeshCoreHelperText(CoordinatorEntity, TextEntity):
    """Base for the local UI text inputs, which hold the user's own draft.

    The value is local state, so it stays editable while the radio is down: a
    failed coordinator tick used to make every helper unavailable and
    ``send_ui_message`` refuse, with the draft still on screen.
    """

    _attr_available = True

    @property
    def available(self) -> bool:
        """Report always available; only transmission needs a live link."""
        return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore text entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = [MeshCoreMessageInput(coordinator), MeshCoreCommandInput(coordinator)]
    async_add_entities(entities)


class MeshCoreMessageInput(MeshCoreHelperText):
    """Text input entity for composing MeshCore messages."""
    
    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the message input entity."""
        super().__init__(coordinator)
        
        # Set unique ID and entity ID
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_message_input"
        self.entity_id = "text.meshcore_message"
        
        # Set name and icon
        self._attr_name = "MeshCore Message"
        self._attr_icon = "mdi:message-text"
        
        # Hide from device page
        self._attr_entity_registry_visible_default = False
        
        # Set validation properties
        self._attr_native_max = 200
        self._attr_mode = "text"
        self._attr_native_value = ""
        
        # Don't associate with device to keep it off device page
        # self._attr_device_info = DeviceInfo(
        #     identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        # )
    
    async def async_set_value(self, value: str) -> None:
        """Set the value of the text entity."""
        self._attr_native_value = value
        self.async_write_ha_state()


class MeshCoreCommandInput(MeshCoreHelperText):
    """Text input entity for MeshCore commands."""
    
    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the command input entity."""
        super().__init__(coordinator)
        
        # Set unique ID and entity ID
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_command_input"
        self.entity_id = "text.meshcore_command"
        
        # Set name and icon
        self._attr_name = "MeshCore Command"
        self._attr_icon = "mdi:console"
        
        # Hide from device page
        self._attr_entity_registry_visible_default = False
        
        # Set validation properties
        self._attr_native_max = 255
        self._attr_mode = "text"
        self._attr_native_value = ""
        
        # Don't associate with device to keep it off device page
        # self._attr_device_info = DeviceInfo(
        #     identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        # )
    
    async def async_set_value(self, value: str) -> None:
        """Set the value of the text entity."""
        self._attr_native_value = value
        self.async_write_ha_state()