"""Logbook integration for MeshCore."""
import logging
from typing import Any, Callable, Dict
from datetime import datetime

from homeassistant.core import HomeAssistant, callback, Event

from .const import (
    DOMAIN,
    ENTITY_DOMAIN_BINARY_SENSOR,
    DEFAULT_DEVICE_NAME,
)
from .utils import (
    get_channel_entity_id,
    get_contact_entity_id
)

_LOGGER = logging.getLogger(__name__)

# Single event type for all messages
EVENT_MESHCORE_MESSAGE = "meshcore_message"

@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""
    
    @callback
    def process_message_event(event: Event) -> dict[str, str]:
        """Process MeshCore message events for logbook."""
        data = event.data
        message = data.get("message", "")
        channel = data.get("channel", "")
        sender = data.get("sender_name", "Unknown")
        
        # Format description based on message type and direction
        if channel:
            # Channel message
            description = f"<{channel}> {sender}: {message}"
            icon = "mdi:message-bulleted"
        else:
            # Direct message
            description = f"{sender}: {message}"
            icon = "mdi:message-text"
        
        return {
            # "name": sender,
            "message": description,
            "domain": DOMAIN,
            "icon": icon,
        }
    
    async_describe_event(DOMAIN, EVENT_MESHCORE_MESSAGE, process_message_event)

def handle_channel_message(event, coordinator) -> None:
    """Handle channel message event."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return

    # Extract message data
    payload = event.payload
    message_text = payload.get("text", "")
    channel_idx = payload.get("channel_idx", 0)
    channel_name = "public" if channel_idx == 0 else f"{channel_idx}"

    # Try to extract sender name from message format "Name: Message"
    sender_name = "Unknown"
    sender_pubkey = ""
    if message_text and ":" in message_text:
        parts = message_text.split(":", 1)
        if len(parts) == 2 and parts[0].strip():
            sender_name = parts[0].strip()
            message_text = parts[1].strip()

            # Use the provided coordinator for contact lookup
            if coordinator and hasattr(coordinator, "api") and coordinator.api.mesh_core:
                # Try to find contact by name to get public key
                contact = coordinator.api.mesh_core.get_contact_by_name(sender_name)
                if contact and isinstance(contact, dict):
                    sender_pubkey = contact.get("public_key", "")[:12]

    # Get device key directly from the coordinator
    if not hasattr(coordinator, "hass"):
        _LOGGER.warning("Cannot log channel message: coordinator.hass not available")
        return

    hass = coordinator.hass
    device_key = (coordinator)

    # Generate entity ID matching MeshCoreMessageEntity
    entity_id = get_channel_entity_id(
        ENTITY_DOMAIN_BINARY_SENSOR,
        device_key[:6],
        channel_idx
    )

    # Create event data
    event_data = {
        "message": message_text,
        "sender_name": sender_name,
        "channel": channel_name,
        "channel_idx": channel_idx,
        "entity_id": entity_id,
        "domain": DOMAIN,
        "timestamp": datetime.now().isoformat(),
    }

    # Add sender pubkey if available
    if sender_pubkey:
        event_data["pubkey_prefix"] = sender_pubkey

    # Fire event
    hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, event_data)

    _LOGGER.debug(
        "Logged channel message in %s from %s%s: %s",
        channel_name,
        sender_name,
        f" ({sender_pubkey[:6]})" if sender_pubkey else "",
        message_text[:50] + ("..." if len(message_text) > 50 else "")
    )

def handle_contact_message(event, coordinator) -> None:
    """Handle contact message event."""
    if not event or not hasattr(event, "payload") or not event.payload:
        return

    # Extract message data from the event
    payload = event.payload
    message_text = payload.get("text", "")
    pubkey_prefix = payload.get("pubkey_prefix", "")

    if not pubkey_prefix:
        _LOGGER.warning("Contact message received without pubkey_prefix")
        return

    # Get coordinator info
    if not hasattr(coordinator, "hass"):
        _LOGGER.warning("Cannot log contact message: coordinator.hass not available")
        return

    hass = coordinator.hass
    device_key = coordinator.pubkey

    # Look up contact name from pubkey_prefix using MeshCore API
    contact_name = "Unknown"
    if hasattr(coordinator, "api") and coordinator.api.mesh_core:
        # Try to find contact by public key prefix
        contact = coordinator.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
        if contact and isinstance(contact, dict):
            contact_name = contact.get("adv_name", "Unknown")

    if contact_name == "Unknown" and pubkey_prefix:
        contact_name = f"Unknown ({pubkey_prefix[:6]})"

    # Generate entity ID matching MeshCoreMessageEntity
    entity_id = get_contact_entity_id(
        ENTITY_DOMAIN_BINARY_SENSOR,
        device_key[:6],
        pubkey_prefix[:6]
    )

    # Create event data
    event_data = {
        "message": message_text,
        "sender_name": contact_name,
        "pubkey_prefix": pubkey_prefix,
        "receiver_name": DEFAULT_DEVICE_NAME,
        "entity_id": entity_id,
        "domain": DOMAIN,
        "timestamp": datetime.now().isoformat(),
    }

    # Fire event
    hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, event_data)

    _LOGGER.debug(
        "Logged direct message from %s (%s): %s",
        contact_name,
        pubkey_prefix[:6],
        message_text[:50] + ("..." if len(message_text) > 50 else "")
    )

def handle_outgoing_message(event_data, coordinator) -> None:
    """Handle outgoing message events from the new message_sent event."""
    if not event_data:
        return
        
    # Get coordinator info
    if not hasattr(coordinator, "hass"):
        _LOGGER.warning("Cannot log outgoing message: coordinator.hass not available")
        return
        
    hass = coordinator.hass
    message_type = event_data.get("message_type")
    message_text = event_data.get("message", "")
    device_key = coordinator.pubkey
    device_name = coordinator.name
    
    # Format and send the appropriate event based on message type
    if message_type == "direct":
        # Direct message to a contact
        pubkey_prefix = event_data.get("contact_public_key", "")[:12]
        receiver_name = event_data.get("receiver", "Unknown")
        
        # Generate entity ID matching MeshCoreMessageEntity
        entity_id = get_contact_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            device_key[:6],
            pubkey_prefix[:6]
        )
        
        # Create event data for logbook
        logbook_event = {
            "message": message_text,
            "sender_name": device_name,
            "receiver_name": receiver_name,
            "pubkey_prefix": pubkey_prefix,
            "entity_id": entity_id,
            "domain": DOMAIN,
            "timestamp": datetime.now().isoformat(),
            "outgoing": True
        }
        
        # Fire event
        hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, logbook_event)
        
        _LOGGER.debug(
            "Logged outgoing direct message to %s (%s): %s",
            receiver_name,
            pubkey_prefix[:6] if pubkey_prefix else "",
            message_text[:50] + ("..." if len(message_text) > 50 else "")
        )
        
    elif message_type == "channel":
        # Channel message
        channel_idx = event_data.get("channel_idx", 0)
        channel_name = "public" if channel_idx == 0 else f"{channel_idx}"
        
        # Generate entity ID matching MeshCoreMessageEntity
        entity_id = get_channel_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            device_key[:6],
            channel_idx
        )
        
        # Create event data for logbook
        logbook_event = {
            "message": message_text,
            "sender_name": device_name,
            "channel": channel_name,
            "channel_idx": channel_idx,
            "entity_id": entity_id,
            "domain": DOMAIN,
            "timestamp": datetime.now().isoformat(),
            "outgoing": True
        }
        
        # Fire event
        hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, logbook_event)
        
        _LOGGER.debug(
            "Logged outgoing channel message to %s: %s",
            channel_name,
            message_text[:50] + ("..." if len(message_text) > 50 else "")
        )