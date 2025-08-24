"""Select platform for MeshCore integration."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    NodeType,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore select entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = []
    
    # Create helper entities
    entities.extend([
        MeshCoreChannelSelect(coordinator),
        MeshCoreContactSelect(coordinator),
        MeshCoreRecipientTypeSelect(coordinator)
    ])
    
    # Add entities
    async_add_entities(entities)


class MeshCoreChannelSelect(CoordinatorEntity, SelectEntity):
    """Helper entity for selecting MeshCore channels."""
    
    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the channel select entity."""
        super().__init__(coordinator)
        
        # Set unique ID and name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_channel_select"
        self._attr_name = "MeshCore Channel"
        
        # Available options - channels 0-3
        self._attr_options = ["Channel 0", "Channel 1", "Channel 2", "Channel 3"]
        self._attr_current_option = self._attr_options[0]
        
        # Don't associate with device to keep it off device page
        # self._attr_device_info = DeviceInfo(
        #     identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        # )
        
        # Set icon
        self._attr_icon = "mdi:tune-vertical"
        
        # Hide from device page
        self._attr_entity_registry_visible_default = False
    
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}
        
        # Add the channel index as an integer attribute for easier use in automations
        if self._attr_current_option.startswith("Channel "):
            try:
                channel_idx = int(self._attr_current_option.replace("Channel ", ""))
                attributes["channel_idx"] = channel_idx
            except (ValueError, TypeError):
                pass
            
        return attributes


class MeshCoreContactSelect(CoordinatorEntity, SelectEntity):
    """Helper entity for selecting MeshCore contacts."""
    
    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the contact select entity."""
        super().__init__(coordinator)
        
        # Set unique ID and name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_contact_select"
        self._attr_name = "MeshCore Contact"
        
        # Initial options
        self._attr_options = self._get_contact_options()
        self._attr_current_option = self._attr_options[0] if self._attr_options else "No contacts"
        
        # Don't associate with device to keep it off device page
        # self._attr_device_info = DeviceInfo(
        #     identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        # )
        
        # Set icon
        self._attr_icon = "mdi:account-multiple"
        
        # Hide from device page
        self._attr_entity_registry_visible_default = False
    
    def _get_contact_options(self) -> List[str]:
        """Get the list of contact options directly from the API."""
        # Check if we have access to the API and mesh_core
        if not hasattr(self.coordinator, "api") or not self.coordinator.api or not self.coordinator.api.mesh_core:
            return ["No contacts"]
            
        try:
            # Access contacts directly from the mesh_core API
            contacts = self.coordinator.api.mesh_core.contacts.values()
            if not contacts:
                return ["No contacts"]
                
            # Include only client type contacts, not repeaters
            contact_options = []
            
            for contact in contacts:
                if not isinstance(contact, dict):
                    continue
                    
                # Skip repeaters, only include clients
                if contact.get("type") == NodeType.REPEATER:
                    continue
                    
                # Get contact name
                name = contact.get("adv_name", "Unknown")
                public_key = contact.get("public_key", "")
                
                if not public_key:
                    continue
                    
                # Format as "Name (pubkey12345)"
                option = f"{name} ({public_key[:12]})"
                contact_options.append(option)
            
            # Add a default option if no contacts found
            if not contact_options:
                return ["No contacts"]
                
            return contact_options
        except Exception as ex:
            _LOGGER.error(f"Error getting contacts from API: {ex}")
            return ["No contacts"]
    
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Update the available options
        self._attr_options = self._get_contact_options()
        
        # If current option is not in the new options, reset to the first option
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]
            
        # Update the entity state
        self.async_write_ha_state()
    
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}
        
        # Add the selected contact's public key as an attribute
        if self._attr_current_option and self._attr_current_option != "No contacts":
            try:
                # Extract the public key from the selection format "Name (pubkey12345)"
                if "(" in self._attr_current_option and ")" in self._attr_current_option:
                    pubkey_part = self._attr_current_option.split("(")[1].split(")")[0]
                    attributes["public_key_prefix"] = pubkey_part
                    
                    # Find the full contact details from the API
                    if hasattr(self.coordinator, "api") and self.coordinator.api and self.coordinator.api.mesh_core:
                        contact = self.coordinator.api.mesh_core.get_contact_by_key_prefix(pubkey_part)
                        if contact:
                            attributes["public_key"] = contact.get("public_key")
                            attributes["contact_name"] = contact.get("adv_name")
            except (IndexError, AttributeError, Exception) as ex:
                _LOGGER.error(f"Error retrieving contact details: {ex}")
                
        return attributes


class MeshCoreRecipientTypeSelect(CoordinatorEntity, SelectEntity):
    """Select entity for choosing between channel or contact recipient."""
    
    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the recipient type select entity."""
        super().__init__(coordinator)
        
        # Set unique ID and entity ID
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_recipient_type"
        self.entity_id = "select.meshcore_recipient_type"
        
        # Set name and icon
        self._attr_name = "MeshCore Recipient Type"
        self._attr_icon = "mdi:account-switch"
        
        # Hide from device page
        self._attr_entity_registry_visible_default = False
        
        # Available options
        self._attr_options = ["Channel", "Contact"]
        self._attr_current_option = "Channel"
        
        # Don't associate with device to keep it off device page
        # self._attr_device_info = DeviceInfo(
        #     identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
        # )
    
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()