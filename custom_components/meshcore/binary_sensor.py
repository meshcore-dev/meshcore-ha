"""Binary sensor platform for MeshCore integration."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict

from meshcore.events import EventType

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    CONF_DISABLE_CONTACT_DISCOVERY,
    DOMAIN,
    ENTITY_DOMAIN_BINARY_SENSOR,
    MESSAGES_SUFFIX,
    CHANNEL_PREFIX,
    CONTACT_SUFFIX,
    NodeType,
)
from .utils import (
    format_entity_id,
    extract_channel_idx,
)
from .logbook import (
    handle_channel_message as log_channel_message, 
    handle_contact_message as log_contact_message,
    handle_outgoing_message
)

_LOGGER = logging.getLogger(__name__)

def create_contact_sensor(coordinator, contact: dict):
    """Create a contact diagnostic sensor if not already tracked."""
    if not isinstance(contact, dict):
        return None

    contact_name = contact.get("adv_name", "Unknown")
    public_key = contact.get("public_key", "")

    if public_key and public_key not in coordinator.tracked_diagnostic_binary_contacts:
        coordinator.tracked_diagnostic_binary_contacts.add(public_key)
        return MeshCoreContactDiagnosticBinarySensor(
            coordinator,
            contact_name,
            public_key,
            public_key[:12]
        )
    return None

@callback
def handle_contacts_update(event, coordinator, async_add_entities):
    """Process contacts update from mesh_core."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return

    # Skip contact discovery if disabled in settings
    if coordinator.config_entry.data.get(CONF_DISABLE_CONTACT_DISCOVERY, False):
        return

    # Initialize tracking sets if needed
    if not hasattr(coordinator, "tracked_contacts"):
        coordinator.tracked_contacts = set()
    if not hasattr(coordinator, "tracked_diagnostic_binary_contacts"):
        coordinator.tracked_diagnostic_binary_contacts = set()

    contact_entities = []

    # Handle both CONTACTS (dict) and NEW_CONTACT (single contact) events
    if event.type == EventType.NEW_CONTACT:
        # NEW_CONTACT: payload is a single contact dict
        contacts_to_process = [event.payload]
    else:
        # CONTACTS: payload is a dict of contacts
        contacts_to_process = list(event.payload.values())

    # Process each contact
    for contact in contacts_to_process:
        try:
            sensor = create_contact_sensor(coordinator, contact)
            if sensor:
                contact_entities.append(sensor)
        except Exception as ex:
            _LOGGER.error(f"Error setting up contact diagnostic binary sensor: {ex}")

    # Add new entities
    if contact_entities:
        async_add_entities(contact_entities)

@callback
def handle_contact_message(event, coordinator, async_add_entities):
    """Create message entity on first message received from a contact."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return
        
    _LOGGER.debug(f"Received contact message event: {event}")
    
    # Skip if we don't have meshcore
    if not coordinator.api.mesh_core:
        return
    
    # Extract pubkey_prefix from the event payload
    payload = event.payload
    pubkey_prefix = payload.get("pubkey_prefix")
    
    # Add contact to logbook
    if pubkey_prefix not in coordinator.tracked_contacts:
        # Get contact information from MeshCore
        contact = coordinator.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
        if not contact:
            return
            
        contact_name = contact.get("adv_name", "Unknown")
        
        # Create message entity for this contact
        message_entity = MeshCoreMessageEntity(
            coordinator, pubkey_prefix, f"{contact_name} Messages", 
            public_key=pubkey_prefix
        )
        
        # Track this contact
        coordinator.tracked_contacts.add(pubkey_prefix)
        
        # Add the entity
        _LOGGER.info(f"Adding message entity for {contact_name} after receiving message")
        async_add_entities([message_entity])
        
    # Log message to the logbook
    log_contact_message(event, coordinator)

@callback
async def handle_channel_message(event, coordinator, async_add_entities):
    """Create channel message entity on first message in a channel."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return

    _LOGGER.debug(f"Received channel message event: {event}")

    # Skip if we don't have meshcore
    if not coordinator.api.mesh_core:
        return

    # Extract channel_idx from the event payload
    payload = event.payload
    channel_idx = payload.get("channel_idx")

    # Skip if no channel_idx or if channels are already added
    if channel_idx is None or hasattr(coordinator, "channels_added") and coordinator.channels_added:
        return
    
    # Initialize channels list if needed
    if not hasattr(coordinator, "tracked_channels"):
        coordinator.tracked_channels = set()
    
        
    # Add channel if it doesnt exist
    if channel_idx not in coordinator.tracked_channels:
        # Get actual channel name from stored channel info
        channel_info = await coordinator.get_channel_info(channel_idx)
        channel_name = channel_info.get("channel_name", f"Channel {channel_idx}")
        
        safe_channel = f"{CHANNEL_PREFIX}{channel_idx}"
        channel_entity = MeshCoreMessageEntity(
            coordinator, safe_channel, f"{channel_name} Messages"
        )
        coordinator.tracked_channels.add(channel_idx)
        _LOGGER.info(f"Adding message entity for channel {channel_idx} ({channel_name}) after receiving message")
        async_add_entities([channel_entity])
    
     # Log message to the logbook
    await log_channel_message(event, coordinator)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore message entities from config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    # Initialize tracking sets
    coordinator.tracked_contacts = set()
    coordinator.tracked_channels = set()
    coordinator.tracked_diagnostic_binary_contacts = set()
    
    # Set up event listeners
    listeners = []
    
    # Create event handlers
    @callback
    def contacts_event_handler(event):
        handle_contacts_update(event, coordinator, async_add_entities)
    
    @callback
    def contact_message_handler(event):
        handle_contact_message(event, coordinator, async_add_entities)
        
    @callback
    async def channel_message_handler(event):
        await handle_channel_message(event, coordinator, async_add_entities)
    
    # Subscribe to events directly from mesh_core
    if coordinator.api.mesh_core:
        # Contact discovery for diagnostic entities (both CONTACTS and NEW_CONTACT events)
        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.CONTACTS,
            contacts_event_handler
        ))

        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.NEW_CONTACT,
            contacts_event_handler
        ))

        # Message events to create entities on first message
        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.CONTACT_MSG_RECV,
            contact_message_handler
        ))

        # Channel message events
        listeners.append(coordinator.api.mesh_core.subscribe(
            EventType.CHANNEL_MSG_RECV,
            channel_message_handler
        ))
        
    # Create sensors for any existing contacts (including discovered ones loaded from storage)
    existing_contacts = coordinator.get_all_contacts()
    if existing_contacts:
        contact_entities = []
        for contact in existing_contacts:
            try:
                sensor = create_contact_sensor(coordinator, contact)
                if sensor:
                    contact_entities.append(sensor)
            except Exception as ex:
                _LOGGER.error(f"Error creating sensor for existing contact: {ex}")

        if contact_entities:
            async_add_entities(contact_entities)

    # Subscribe to our internal message sent event for outgoing messages
    @callback
    async def message_sent_handler(event):
        """Handle outgoing message events."""
        if event.data.get("message_type") == "direct":
            # Handle outgoing direct message
            _LOGGER.debug(f"Handling outgoing direct message: {event.data}")
            if "contact_public_key" in event.data:
                # Create message entity if needed
                pubkey_prefix = event.data["contact_public_key"][:12]
                if pubkey_prefix not in coordinator.tracked_contacts:
                    contact_name = event.data.get("receiver", "Unknown")
                    message_entity = MeshCoreMessageEntity(
                        coordinator, pubkey_prefix, f"{contact_name} Messages", 
                        public_key=pubkey_prefix
                    )
                    coordinator.tracked_contacts.add(pubkey_prefix)
                    _LOGGER.info(f"Adding message entity for {contact_name} after sending message")
                    async_add_entities([message_entity])
        
        elif event.data.get("message_type") == "channel":
            # Handle outgoing channel message
            _LOGGER.debug(f"Handling outgoing channel message: {event.data}")
            if "channel_idx" in event.data:
                channel_idx = event.data["channel_idx"]
                if not hasattr(coordinator, "tracked_channels"):
                    coordinator.tracked_channels = set()
                if channel_idx not in coordinator.tracked_channels:
                    # Get actual channel name from stored channel info
                    channel_info = await coordinator.get_channel_info(channel_idx)
                    channel_name = channel_info.get("channel_name", f"Channel {channel_idx}")
                    
                    # Create channel entity
                    safe_channel = f"{CHANNEL_PREFIX}{channel_idx}"
                    channel_entity = MeshCoreMessageEntity(
                        coordinator, safe_channel, f"{channel_name} Messages"
                    )
                    coordinator.tracked_channels.add(channel_idx)
                    _LOGGER.info(f"Adding message entity for channel {channel_idx} after sending message")
                    async_add_entities([channel_entity])
        
        # Log the message to the logbook using our dedicated handler
        await handle_outgoing_message(event.data, coordinator)
    
    # Register for the message_sent event - use a global check to prevent duplicates
    event_key = f"{DOMAIN}_message_sent_listener_{entry.entry_id}"
    if not hasattr(hass.data[DOMAIN], event_key):
        _LOGGER.debug("Registering message_sent event listener")
        unsubscribe_func = hass.bus.async_listen(f"{DOMAIN}_message_sent", message_sent_handler)
        hass.data[DOMAIN][event_key] = unsubscribe_func
    else:
        _LOGGER.debug("Message_sent event listener already registered, skipping")
    

class MeshCoreMessageEntity(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor entity that tracks mesh network messages using event subscription."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    
    @property
    def state(self) -> str:
        """Return the state of the entity."""
        return "Active" if self.is_on else "Inactive"
    
    def __init__(
        self, 
        coordinator: DataUpdateCoordinator, 
        entity_key: str,
        name: str,
        public_key: str = ""
    ) -> None:
        """Initialize the message entity."""
        super().__init__(coordinator)
        
        # Store entity type and public key if applicable
        self.entity_key = entity_key
        self.public_key = public_key
        
        # Get device name for unique ID and entity_id
        device_key = coordinator.pubkey
        
        # Set unique ID with device key included - ensure consistent format with no empty parts
        parts = [part for part in [coordinator.config_entry.entry_id, device_key[:6], entity_key[:6], MESSAGES_SUFFIX] if part]
        self._attr_unique_id = "_".join(parts)
        
        # Manually set entity_id to match logbook entity_id format
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR, 
            device_key[:6], 
            entity_key[:6], 
            MESSAGES_SUFFIX
        )
        
        # Debug: Log the entity ID for troubleshooting
        _LOGGER.debug(f"Created entity with ID: {self.entity_id}")
        
        self._attr_name = name
        
        # Set icon based on entity type
        if self.entity_key.startswith(CHANNEL_PREFIX):
            self._attr_icon = "mdi:message-bulleted"
        else:
            self._attr_icon = "mdi:message-text-outline"
             
    
    @property
    def device_info(self):
        return DeviceInfo(**self.coordinator.device_info)
        
    @property
    def is_on(self) -> bool:
        """Return true if there are recent messages in the activity window."""
        return True
    
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return message details as attributes."""
        attributes = {}
        
        # Add appropriate attributes based on entity type
        if self.entity_key.startswith(CHANNEL_PREFIX):
            # For channel-specific message entities
            try:
                channel_idx = extract_channel_idx(self.entity_key)
                attributes["channel_index"] = f"{channel_idx}"
            except (ValueError, TypeError):
                _LOGGER.warning(f"Could not get channel index from {self.entity_key}")
        elif self.public_key:
            # For contact-specific message entities
            attributes["public_key"] = self.public_key
            
        return attributes


class MeshCoreContactDiagnosticBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """A diagnostic binary sensor for a single MeshCore contact."""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        contact_name: str,
        public_key: str,
        contact_id: str,
    ) -> None:
        """Initialize the contact diagnostic binary sensor."""
        super().__init__(coordinator)
        
        self.contact_name = contact_name
        self.public_key = public_key
        self.pubkey_prefix = public_key[:12] if public_key else ""
        self._contact_data = {}
        self._remove_contacts_listener = None
        
        # Set unique ID
        self._attr_unique_id = contact_id
        
        self.entity_id = format_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            contact_name,
            self.pubkey_prefix,
            CONTACT_SUFFIX
        )

        # Initial name
        self._attr_name = contact_name
        
        # Set entity category to diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        
        # Set device class to connectivity
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        
        # Icon will be set dynamically
        self._attr_icon = "mdi:radio-tower"
        
        # Get initial data from coordinator
        initial_data = self._get_contact_data()
        if initial_data:
            self._update_from_contact_data(initial_data)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Only update if this contact is marked as dirty (performance optimization)
        if not self.coordinator.is_contact_dirty(self.public_key):
            return

        contact_data = self._get_contact_data()
        if contact_data:
            self._update_from_contact_data(contact_data)
        else:
            # Contact no longer exists, clear data
            self._contact_data = {}

        # Clear dirty flag after updating
        self.coordinator.clear_contact_dirty(self.public_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if contact data exists."""
        return bool(self._contact_data)

    @property
    def device_info(self):
        return DeviceInfo(**self.coordinator.device_info)
        
    def _get_contact_data(self) -> Dict[str, Any]:
        """Get the data for this contact from the coordinator."""
        contacts = self.coordinator.get_all_contacts()
        if not contacts:
            return {}

        # Find this contact by name or by public key
        for contact in contacts:
            if not isinstance(contact, dict):
                continue

            # Match by public key prefix
            if contact.get("public_key", "").startswith(self.public_key):
                return contact

            # Match by name
            if contact.get("adv_name") == self.contact_name:
                return contact

        return {}
    
    def _update_from_contact_data(self, contact: Dict[str, Any]):
        """Update entity state based on contact data."""
        if not contact:
            return
        
        # Store the contact data
        self._contact_data = dict(contact)
        
        # Get the node type and set icon accordingly
        node_type = contact.get("type")
        is_fresh = self.is_on
        
        # Set different icons and names based on node type and state
        if node_type == NodeType.CLIENT:  # Client
            self._attr_icon = "mdi:account" if is_fresh else "mdi:account-off"
            self._attr_name = f"{self.contact_name} (Client)"
        elif node_type == NodeType.REPEATER:  # Repeater
            self._attr_icon = "mdi:radio-tower" if is_fresh else "mdi:radio-tower-off"
            self._attr_name = f"{self.contact_name} (Repeater)"
        elif node_type == NodeType.ROOM_SERVER:  # Room Server
            self._attr_icon = "mdi:forum" if is_fresh else "mdi:forum-outline"
            self._attr_name = f"{self.contact_name} (Room Server)"
        else:
            # Default icon if type is unknown
            self._attr_icon = "mdi:help-network"
            self._attr_name = f"{self.contact_name} (Unknown)"

    @property
    def is_on(self) -> bool:
        """Return True if the contact is fresh/active."""
        if not self._contact_data:
            return False
            
        # Check last advertisement time for contact status
        last_advert = self._contact_data.get("last_advert", 0)
        if last_advert > 0:
            # Calculate time since last advert
            time_since = time.time() - last_advert
            # If less than 12 hour, consider fresh/active
            if time_since < 3600*12:
                return True
        
        return False
        
    @property
    def state(self) -> str:
        """Return the state of the binary sensor as "discovered", "fresh" or "stale"."""
        if self._contact_data and not self._contact_data.get("added_to_node", True):
            return "discovered"
        return "fresh" if self.is_on else "stale"
        
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return the contact data as attributes."""
        if not self._contact_data:
            return {"status": "unknown"}
            
        attributes = {}
        
        if self._contact_data.get("adv_lat") and self._contact_data.get("adv_lat") != 0:
            attributes["latitude"] = self._contact_data["adv_lat"]
        if self._contact_data.get("adv_lon") and self._contact_data.get("adv_lon") != 0:
            attributes["longitude"] = self._contact_data["adv_lon"]
        
        # Add all contact properties as attributes
        for key, value in self._contact_data.items():
            attributes[key] = value
        
        # Get node type string
        node_type = self._contact_data.get("type")
        if node_type == NodeType.CLIENT:
            attributes["node_type_str"] = "Client"
            icon_file = "client-green.svg" if self.is_on else "client.svg"
        elif node_type == NodeType.REPEATER:
            attributes["node_type_str"] = "Repeater"
            icon_file = "repeater-green.svg" if self.is_on else "repeater.svg"
        elif node_type == NodeType.ROOM_SERVER:
            attributes["node_type_str"] = "Room Server"
            icon_file = "room_server-green.svg" if self.is_on else "room_server.svg"
        else:
            attributes["node_type_str"] = "Unknown"
            icon_file = None
            
        # Add entity picture if we have an icon
        if icon_file:
            attributes["entity_picture"] = f"/api/meshcore/static/{icon_file}"
        
        # Format last advertisement time if available
        last_advert = self._contact_data.get("last_advert", 0)
        if last_advert > 0:
            last_advert_time = datetime.fromtimestamp(last_advert)
            attributes["last_advert_formatted"] = last_advert_time.isoformat()
            
        return attributes