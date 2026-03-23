"""Logbook integration for MeshCore."""
import asyncio
from calendar import c
import logging
import time
from typing import  Callable
from datetime import datetime

from homeassistant.core import HomeAssistant, callback, Event

from .const import (
    DOMAIN,
    ENTITY_DOMAIN_BINARY_SENSOR,
    DEFAULT_DEVICE_NAME,
)
from .utils import (
    create_message_correlation_key,
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

        # Append route if RX_LOG data is available (incoming messages only).
        # Format: [route:xx,yy,zz] — easily stripped by UI cards.
        rx_logs = data.get("rx_log_data", [])
        if rx_logs and not data.get("outgoing"):
            first_rx = rx_logs[0]
            path_nodes = first_rx.get("path_nodes", [])
            if not path_nodes:
                # Fall back to raw path hex, split into 2-char pairs
                raw_path = first_rx.get("path", "")
                if raw_path:
                    path_nodes = [raw_path[i:i+2] for i in range(0, len(raw_path), 2)]
            if path_nodes:
                description += f" [route:{','.join(path_nodes)}]"

        return {
            # "name": sender,
            "message": description,
            "domain": DOMAIN,
            "icon": icon,
        }
    
    async_describe_event(DOMAIN, EVENT_MESHCORE_MESSAGE, process_message_event)

async def handle_channel_message(event, coordinator) -> None:
    """Handle channel message event."""
    if not event or not event.payload:
        _LOGGER.debug("Invalid event data for channel message")
        return

    try:
        # Extract message data
        payload = event.payload
        message_text = payload.get("text", "")
        channel_idx = payload.get("channel_idx", 0)
        
        # Get channel name from stored channel info
        channel_info = await coordinator.get_channel_info(channel_idx)
        channel_name = channel_info.get("channel_name", "public" if channel_idx == 0 else f"{channel_idx}")

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
                    # Try saved contacts first (SDK), then discovered contacts
                    contact = coordinator.api.mesh_core.get_contact_by_name(sender_name)
                    if contact and isinstance(contact, dict):
                        sender_pubkey = contact.get("public_key", "")[:12]
                    elif hasattr(coordinator, "_discovered_contacts"):
                        # Search discovered contacts by advertised name
                        for full_pk, disc in coordinator._discovered_contacts.items():
                            if disc.get("adv_name") == sender_name:
                                sender_pubkey = full_pk[:12]
                                break

        # Check for Home Assistant instance
        if not hasattr(coordinator, "hass"):
            _LOGGER.warning("Cannot log channel message: coordinator.hass not available")
            return

        hass = coordinator.hass
        device_key = coordinator.pubkey if hasattr(coordinator, "pubkey") else "unknown"

        # Generate entity ID matching MeshCoreMessageEntity
        entity_id = get_channel_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            device_key[:6] if device_key else "unknown",
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
            "message_type": "channel"  # Explicit message type for filtering
        }

        # Add sender pubkey if available
        if sender_pubkey:
            event_data["pubkey_prefix"] = sender_pubkey

        # Correlate with RX_LOG data using multiple collection passes.
        # RX_LOG events may arrive after the channel message event, especially
        # on multi-hop paths. Two passes (500ms + 1000ms) catch late arrivals
        # without delaying the logbook entry excessively.
        INCOMING_COLLECTION_PASSES = 2
        INCOMING_PASS_INTERVALS = [0.5, 1.0]  # seconds to wait before each pass

        try:
            timestamp = payload.get("sender_timestamp")
            original_text = payload.get("text", "")

            if channel_idx is not None and timestamp and original_text:
                await asyncio.sleep(0.5)
                hash_key = create_message_correlation_key(channel_idx, timestamp, original_text)
                rx_logs = coordinator._pending_rx_logs.pop(hash_key, None)

                if rx_logs:
                    _LOGGER.debug(f"Correlated channel message with {len(rx_logs)} RX_LOG reception(s)")
                    event_data["rx_log_data"] = rx_logs

                    # Store route data on the sender's contact entity.
                    # Use the first RX_LOG entry (typically the most direct path).
                    if sender_pubkey:
                        first_rx = rx_logs[0]
                        rx_data = {
                            "last_snr": first_rx.get("snr"),
                            "last_rssi": first_rx.get("rssi"),
                            "last_rx_hops": first_rx.get("hop_count", 0),
                            "last_rx_path": first_rx.get("path", ""),
                            "last_rx_path_nodes": [],  # Not parsed in RX_LOG entries
                            "last_rx_route_type": "channel_msg",
                            "last_rx_payload_type": "GroupText",
                            "last_rx_timestamp": time.time(),
                        }
                        stored = coordinator.update_contact_rx_data(sender_pubkey, rx_data)
                        if stored:
                            _LOGGER.debug(
                                "Stored route data on sender %s from channel message: SNR=%s RSSI=%s hops=%s",
                                sender_pubkey[:6], rx_data["last_snr"],
                                rx_data["last_rssi"], rx_data["last_rx_hops"],
                            )
                            coordinator.async_set_updated_data(coordinator.data or {})
        except Exception as ex:
            _LOGGER.debug(f"Error correlating channel message with RX_LOG: {ex}")

        # Fire event
        hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, event_data)

        _LOGGER.debug(
            "Logged channel message in %s from %s%s: %s",
            channel_name,
            sender_name,
            f" ({sender_pubkey[:6]})" if sender_pubkey else "",
            message_text[:50] + ("..." if len(message_text) > 50 else "")
        )
    except Exception as ex:
        _LOGGER.error("Error handling channel message: %s", ex, exc_info=True)

def handle_contact_message(event, coordinator) -> None:
    """Handle contact message event."""
    if not event or not hasattr(event, "payload") or not event.payload:
        _LOGGER.debug("Invalid event data for contact message")
        return

    try:
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
        device_key = coordinator.pubkey if hasattr(coordinator, "pubkey") else "unknown"

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
            device_key[:6] if device_key else "unknown",
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
            "message_type": "direct"  # Explicit message type for filtering
        }

        # Store route data on the sender's contact entity.
        # Direct messages come straight from the sender's radio; store any
        # signal data the SDK provides and record that a message was received.
        try:
            rx_data = {
                "last_snr": payload.get("snr"),
                "last_rssi": payload.get("rssi"),
                "last_rx_hops": payload.get("hop_count"),
                "last_rx_path": payload.get("path", ""),
                "last_rx_path_nodes": [],
                "last_rx_route_type": "direct_msg",
                "last_rx_payload_type": "ContactMsg",
                "last_rx_timestamp": time.time(),
            }
            stored = coordinator.update_contact_rx_data(pubkey_prefix, rx_data)
            if stored:
                _LOGGER.debug(
                    "Stored route data on sender %s from direct message: SNR=%s RSSI=%s",
                    pubkey_prefix[:6], rx_data["last_snr"], rx_data["last_rssi"],
                )
                coordinator.async_set_updated_data(coordinator.data or {})
        except Exception as ex:
            _LOGGER.debug(f"Error storing direct message route data: {ex}")

        # Fire event
        hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, event_data)

        _LOGGER.debug(
            "Logged direct message from %s (%s): %s",
            contact_name,
            pubkey_prefix[:6],
            message_text[:50] + ("..." if len(message_text) > 50 else "")
        )
    except Exception as ex:
        _LOGGER.error("Error handling contact message: %s", ex, exc_info=True)

async def handle_outgoing_message(event_data, coordinator) -> None:
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
        # Get actual channel name from stored channel info
        channel_info = await coordinator.get_channel_info(channel_idx)
        channel_name = channel_info.get("channel_name", "public" if channel_idx == 0 else f"{channel_idx}")
        
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