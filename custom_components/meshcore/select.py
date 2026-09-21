"""Select platform for MeshCore integration."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

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
    SELECT_NO_ADDED,
    SELECT_NO_CONTACTS,
    SELECT_NO_DISCOVERED,
    NodeType,
)
from .utils import extract_pubkey_from_selection

_LOGGER = logging.getLogger(__name__)


class MeshCoreHelperSelect(CoordinatorEntity, SelectEntity):
    """Base for the local UI pickers, which hold the user's own choice.

    A picker's value is local state, so it stays readable and editable while
    the radio is down: a failed coordinator tick used to make every helper
    unavailable and ``send_ui_message`` refuse.
    """

    _attr_available = True

    @property
    def available(self) -> bool:
        """Report always available; only transmission needs a live link."""
        return True

    def _retarget(self, options: list[str], identity: Callable[[str], Any]) -> None:
        """Adopt a new option list, keeping the selection on the same target.

        Options are display labels, so a rename or a new contact reshuffles
        them. The selection follows its identity -- channel index or pubkey
        prefix -- and falls back to the list's first entry only when that
        identity is gone, which is why every picker that can fall back puts a
        placeholder first.
        """
        self._attr_options = options
        current = self._attr_current_option
        if current in options:
            return
        target = identity(current) if current else None
        if target is not None:
            for option in options:
                if identity(option) == target:
                    self._attr_current_option = option
                    return
        self._attr_current_option = options[0]


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


def _channel_index(option: str) -> int | None:
    """Return the channel index an option label ends with, else None."""
    match = re.search(r"\((\d+)\)$", option or "")
    return int(match.group(1)) if match else None


def _contact_prefix(option: str) -> str | None:
    """Return the pubkey prefix an option label ends with, else None."""
    return extract_pubkey_from_selection(option or "")


class MeshCoreChannelSelect(MeshCoreHelperSelect):
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

    def _get_channel_options(self) -> list[str]:
        """Get list of channels with their names."""
        options = []

        # Get max channels from coordinator (default 4)
        max_channels = getattr(self.coordinator, "_max_channels", 4)

        for idx in range(max_channels):
            # Get channel info from coordinator
            channel_info = self.coordinator._channel_info.get(idx, {})
            channel_name = channel_info.get("channel_name", "(unused)")

            # Format as "Name (idx)"
            option = f"{channel_name} ({idx})"
            options.append(option)

        return options if options else ["No channels"]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # A channel rename rewrites its label; the selection follows the index.
        self._retarget(self._get_channel_options(), _channel_index)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}

        # Extract channel_idx from format "Name (idx)"
        if self._attr_current_option and self._attr_current_option != "No channels":
            channel_idx = _channel_index(self._attr_current_option)
            if channel_idx is not None:
                attributes["channel_idx"] = channel_idx

        return attributes


class MeshCoreContactSelect(MeshCoreHelperSelect):
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
    
    def _get_contact_options(self) -> list[str]:
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

            # Placeholder first so a contact that disappears falls back to
            # "pick someone", never silently to whoever now sorts first.
            return [SELECT_NO_CONTACTS] + contact_options
        except Exception as ex:
            _LOGGER.error(f"Error getting contacts from coordinator: {ex}")
            return ["No contacts"]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # A rename rewrites the label; the selection follows the pubkey prefix.
        self._retarget(self._get_contact_options(), _contact_prefix)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()
        
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        attributes = {}

        # Add the selected contact's public key as an attribute
        if self._attr_current_option and self._attr_current_option not in (
            "No contacts",
            SELECT_NO_CONTACTS,
        ):
            pubkey_part = extract_pubkey_from_selection(self._attr_current_option)
            if pubkey_part:
                attributes["public_key_prefix"] = pubkey_part

                # Find the full contact details from the coordinator
                contact = self.coordinator.get_contact_by_prefix(pubkey_part)
                if contact:
                    attributes["public_key"] = contact.get("public_key")
                    attributes["contact_name"] = contact.get("adv_name")

        return attributes


class MeshCoreRecipientTypeSelect(MeshCoreHelperSelect):
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


class MeshCoreDiscoveredContactSelect(MeshCoreHelperSelect):
    """Select entity for discovered contacts not yet added to node."""

    # The options list grows with the discovered-contact set and can exceed
    # the recorder's 16 KiB per-state attribute cap on dense meshes, which
    # makes the recorder drop the state's attributes and log warnings. A
    # picker's options history has no value, so exclude it from recording.
    _unrecorded_attributes = frozenset({"options"})

    def __init__(self, coordinator: DataUpdateCoordinator) -> None:
        """Initialize the discovered contact select entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_discovered_contact_select"
        self._attr_name = "MeshCore Discovered Contact"
        self._attr_icon = "mdi:account-question"
        self._attr_entity_registry_visible_default = False

        self._attr_options = self._get_discovered_contact_options()
        self._attr_current_option = SELECT_NO_CONTACTS

    def _get_discovered_contact_options(self) -> list[str]:
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
        self._retarget(self._get_discovered_contact_options(), _contact_prefix)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
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


class MeshCoreAddedContactSelect(MeshCoreHelperSelect):
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

    def _get_added_contact_options(self) -> list[str]:
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
        self._retarget(self._get_added_contact_options(), _contact_prefix)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
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