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
    ENTITY_DOMAIN_BINARY_SENSOR,
    MESSAGES_SUFFIX,
    NodeType,
    SELECT_NO_CONTACTS,
    SELECT_NO_DISCOVERED,
    SELECT_NO_ADDED,
)
from .utils import extract_pubkey_from_selection, format_entity_id

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
        MeshCoreRecipientTypeSelect(coordinator),
        MeshCoreDiscoveredContactSelect(coordinator),
        MeshCoreAddedContactSelect(coordinator)
    ])

    # Add entities
    async_add_entities(entities)


class MeshCoreChannelSelect(CoordinatorEntity, SelectEntity):
    """Helper entity for selecting MeshCore channels with actual channel names."""

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the channel select entity."""
        super().__init__(coordinator)

        # Set unique ID and name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_channel_select"
        self._attr_name = "MeshCore Channel"

        # Get initial channel options
        self._attr_options = self._get_channel_options()
        self._attr_current_option = self._attr_options[0] if self._attr_options else "No channels"

        # Set icon
        self._attr_icon = "mdi:tune-vertical"

        # Hide from device page
        self._attr_entity_registry_visible_default = False

    def _get_channel_options(self) -> List[str]:
        """Get list of configured channels with their names.

        Slots with no name / (unused) in firmware memory are omitted so the UI
        only lists channels that are actually assigned (except index 0, which
        defaults to Public when unnamed).
        """
        options: List[str] = []

        max_channels = getattr(self.coordinator, "_max_channels", 4)

        for idx in range(max_channels):
            channel_info = self.coordinator._channel_info.get(idx, {})
            raw_name = channel_info.get("channel_name")
            if raw_name is None:
                raw_name = "(unused)"
            name = raw_name.strip() if isinstance(raw_name, str) else str(raw_name).strip()

            if idx == 0:
                if not name or name == "(unused)":
                    name = "Public"
                options.append(f"{name} ({idx})")
                continue

            if not name or name == "(unused)":
                continue

            options.append(f"{name} ({idx})")

        return options if options else ["No channels"]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Update channel options when coordinator data changes
        self._attr_options = self._get_channel_options()

        # If current option is not in the new options, reset to first option
        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]

        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}

        # Extract channel_idx from format "Name (idx)"
        if self._attr_current_option and self._attr_current_option != "No channels":
            import re
            match = re.search(r'\((\d+)\)$', self._attr_current_option)
            if match:
                attributes["channel_idx"] = int(match.group(1))

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
        """Get the list of contact options from the coordinator."""
        try:
            # Use coordinator's cached and managed contacts for consistency
            all_contacts = self.coordinator.get_all_contacts()
            if not all_contacts:
                return ["No contacts"]

            contact_options = []

            for contact in all_contacts:
                if not isinstance(contact, dict):
                    continue

                # Only show contacts that have been added to the node
                if not contact.get("added_to_node", False):
                    continue

                # Skip repeaters, only include clients
                if contact.get("type") == NodeType.REPEATER:
                    continue

                # Get contact name and pubkey_prefix
                name = contact.get("adv_name", "Unknown")
                pubkey_prefix = contact.get("pubkey_prefix", "")

                if not pubkey_prefix:
                    continue

                # Format as "Name (pubkey_prefix)"
                option = f"{name} ({pubkey_prefix})"
                contact_options.append(option)

            # Add a default option if no contacts found
            if not contact_options:
                return ["No contacts"]

            # Sort alphabetically (case-insensitive)
            contact_options.sort(key=str.lower)

            return contact_options
        except Exception as ex:
            _LOGGER.error(f"Error getting contacts from coordinator: {ex}")
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
            pubkey_part = extract_pubkey_from_selection(self._attr_current_option)
            if pubkey_part:
                attributes["public_key_prefix"] = pubkey_part

                # Find the full contact details from the coordinator
                contact = self.coordinator.get_contact_by_prefix(pubkey_part)
                if contact:
                    attributes["public_key"] = contact.get("public_key")
                    attributes["contact_name"] = contact.get("adv_name")

                # Matches binary_sensor message entity id (entity_key[:6] in MeshCoreMessageEntity)
                device_key = (self.coordinator.pubkey or "")[:6]
                msg_key = pubkey_part[:6]
                attributes["message_log_entity_id"] = format_entity_id(
                    ENTITY_DOMAIN_BINARY_SENSOR,
                    device_key,
                    msg_key,
                    MESSAGES_SUFFIX,
                )

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


class MeshCoreDiscoveredContactSelect(CoordinatorEntity, SelectEntity):
    """Select entity for discovered contacts not yet added to node."""

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the discovered contact select entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_discovered_contact_select"
        self._attr_name = "MeshCore Discovered Contact"
        self._attr_icon = "mdi:account-question"
        self._attr_entity_registry_visible_default = False

        self._attr_options = self._get_discovered_contact_options()
        self._attr_current_option = SELECT_NO_CONTACTS

    def _get_discovered_contact_options(self) -> List[str]:
        """Get list of discovered contacts not yet added."""
        all_contacts = self.coordinator.get_all_contacts()

        discovered_options = []

        for contact in all_contacts:
            if not isinstance(contact, dict):
                continue

            if not contact.get("added_to_node", True):
                name = contact.get("adv_name", "Unknown")
                pubkey_prefix = contact.get("pubkey_prefix", "")
                if pubkey_prefix:
                    option = f"{name} ({pubkey_prefix})"
                    discovered_options.append(option)

        # Sort alphabetically (case-insensitive)
        discovered_options.sort(key=str.lower)

        # Add placeholder at the beginning
        return [SELECT_NO_CONTACTS] + discovered_options

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_options = self._get_discovered_contact_options()

        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]

        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}

        if self._attr_current_option and self._attr_current_option not in [SELECT_NO_CONTACTS, SELECT_NO_DISCOVERED]:
            pubkey_prefix = extract_pubkey_from_selection(self._attr_current_option)
            if pubkey_prefix:
                attributes["pubkey_prefix"] = pubkey_prefix

                contact = self.coordinator.get_contact_by_prefix(pubkey_prefix)
                if contact:
                    attributes["public_key"] = contact.get("public_key")
                    attributes["contact_name"] = contact.get("adv_name")

        return attributes


class MeshCoreAddedContactSelect(CoordinatorEntity, SelectEntity):
    """Select entity for contacts already added to node."""

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the added contact select entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_added_contact_select"
        self._attr_name = "MeshCore Added Contact"
        self._attr_icon = "mdi:account-check"
        self._attr_entity_registry_visible_default = False

        self._attr_options = self._get_added_contact_options()
        self._attr_current_option = SELECT_NO_CONTACTS

    def _get_added_contact_options(self) -> List[str]:
        """Get list of contacts already added to node."""
        all_contacts = self.coordinator.get_all_contacts()

        added_options = []
        for contact in all_contacts:
            if not isinstance(contact, dict):
                continue

            if contact.get("added_to_node", False):
                name = contact.get("adv_name", "Unknown")
                pubkey_prefix = contact.get("pubkey_prefix", "")
                if pubkey_prefix:
                    option = f"{name} ({pubkey_prefix})"
                    added_options.append(option)

        # Sort alphabetically (case-insensitive)
        added_options.sort(key=str.lower)

        # Add placeholder at the beginning
        return [SELECT_NO_CONTACTS] + added_options

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_options = self._get_added_contact_options()

        if self._attr_current_option not in self._attr_options:
            self._attr_current_option = self._attr_options[0]

        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}

        if self._attr_current_option and self._attr_current_option not in [SELECT_NO_CONTACTS, SELECT_NO_ADDED]:
            pubkey_prefix = extract_pubkey_from_selection(self._attr_current_option)
            if pubkey_prefix:
                attributes["pubkey_prefix"] = pubkey_prefix

                contact = self.coordinator.get_contact_by_prefix(pubkey_prefix)
                if contact:
                    attributes["public_key"] = contact.get("public_key")
                    attributes["contact_name"] = contact.get("adv_name")

        return attributes