"""Logbook integration for MeshCore."""
import asyncio
import logging
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
# Lightweight event for progressive delivery sensor updates (not logged)
EVENT_MESHCORE_DELIVERY_UPDATE = "meshcore_delivery_update"


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
                    # Try to find contact by name to get public key
                    contact = coordinator.api.mesh_core.get_contact_by_name(sender_name)
                    if contact and isinstance(contact, dict):
                        sender_pubkey = contact.get("public_key", "")[:12]

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

        # Correlate with RX_LOG data - delay 500ms to collect multiple receptions
        try:
            timestamp = payload.get("sender_timestamp")

            if channel_idx is not None and timestamp:
                await asyncio.sleep(0.5)
                hash_key = create_message_correlation_key(channel_idx, timestamp)

                # Skip pop if this key is reserved for outgoing delivery tracking.
                # When we send a channel message, the outgoing handler registers its
                # key so re-broadcasts of our own message are left for it to consume.
                if hash_key in coordinator._outgoing_correlation_keys:
                    _LOGGER.debug("Skipping RX_LOG pop for outgoing-reserved key %s", hash_key[:8])
                else:
                    rx_logs = coordinator._pending_rx_logs.pop(hash_key, None)

                    if rx_logs:
                        _LOGGER.debug(f"Correlated channel message with {len(rx_logs)} RX_LOG reception(s)")
                        event_data["rx_log_data"] = rx_logs
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

        # Include ACK delivery status from the send service
        ack_received = event_data.get("ack_received")

        # Create event data for logbook
        logbook_event = {
            "message": message_text,
            "sender_name": device_name,
            "receiver_name": receiver_name,
            "pubkey_prefix": pubkey_prefix,
            "entity_id": entity_id,
            "domain": DOMAIN,
            "timestamp": datetime.now().isoformat(),
            "outgoing": True,
            "message_type": "direct",
            "send_id": event_data.get("send_id"),
        }

        # Add ACK status if available
        if ack_received is not None:
            logbook_event["ack_received"] = ack_received

        # Fire event
        hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, logbook_event)

        _LOGGER.debug(
            "Logged outgoing direct message to %s (%s): %s (ack: %s)",
            receiver_name,
            pubkey_prefix[:6] if pubkey_prefix else "",
            message_text[:50] + ("..." if len(message_text) > 50 else ""),
            "yes" if ack_received else ("no" if ack_received is False else "n/a")
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
            "outgoing": True,
            "message_type": "channel",
            "send_id": event_data.get("send_id"),
        }

        # Correlate with RX_LOG data for outgoing channel messages.
        # When we send a channel message, repeaters re-broadcast it and our
        # radio picks up those re-broadcasts as RX_LOG events. This lets us
        # count how many repeaters relayed our message.
        #
        # We use rolling 1-second collection passes, firing a progressive
        # event after each pass so the sensor updates in near-real-time.
        # Using pop() on a match forces late arrivals into a new cache entry
        # under the same key, which subsequent passes pick up.
        NUM_COLLECTION_PASSES = 4
        PASS_INTERVAL_SECONDS = 1.0

        try:
            send_timestamp = event_data.get("send_timestamp")

            if channel_idx is not None and send_timestamp:
                # Single correlation key using channel + timestamp only.
                # Text is excluded because the HA config name may differ from
                # the on-device advertised name prepended to broadcasts.
                hash_key = create_message_correlation_key(channel_idx, send_timestamp)

                # Reserve this key so the incoming handler doesn't pop() it.
                # The incoming handler fires 500ms faster and would steal entries
                # before our first collection pass at 1000ms.
                coordinator._outgoing_correlation_keys[hash_key] = True

                all_rx_logs = []

                try:
                    for pass_num in range(NUM_COLLECTION_PASSES):
                        await asyncio.sleep(PASS_INTERVAL_SECONDS)

                        batch = coordinator._pending_rx_logs.pop(hash_key, None)
                        if batch:
                            all_rx_logs.extend(batch)
                            _LOGGER.debug(
                                "Pass %d: collected %d new RX_LOG(s), total %d",
                                pass_num + 1, len(batch), len(all_rx_logs)
                            )

                        is_final = (pass_num == NUM_COLLECTION_PASSES - 1)
                        update_event = dict(logbook_event)
                        update_event["rx_log_data"] = list(all_rx_logs)
                        update_event["repeater_count"] = len(all_rx_logs)
                        update_event["progressive"] = not is_final

                        if is_final:
                            # Final pass: fire the real logbook event (single entry)
                            hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, update_event)
                        else:
                            # Intermediate: lightweight event only the sensor listens to
                            hass.bus.async_fire(EVENT_MESHCORE_DELIVERY_UPDATE, update_event)
                finally:
                    # Always release the reservation so the cache key can be
                    # reused by future messages on the same channel+timestamp.
                    coordinator._outgoing_correlation_keys.pop(hash_key, None)

                if not all_rx_logs:
                    # Log diagnostic info to help debug correlation mismatches
                    cache_keys = list(coordinator._pending_rx_logs.keys())
                    _LOGGER.debug(
                        "No RX_LOG correlated with outgoing channel message. "
                        "ch=%s, ts=%s, hash=%s, pending_cache_keys=%s",
                        channel_idx, send_timestamp,
                        hash_key[:8], cache_keys[:5]
                    )
                else:
                    _LOGGER.debug(
                        "Correlated outgoing channel message with "
                        "%d RX_LOG reception(s) total",
                        len(all_rx_logs)
                    )
            else:
                # No timestamp available for correlation, fire single event
                logbook_event["repeater_count"] = 0
                hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, logbook_event)
        except Exception as ex:
            _LOGGER.debug(f"Error correlating outgoing channel message with RX_LOG: {ex}")
            # Fire event even on error so logbook still gets the entry
            hass.bus.async_fire(EVENT_MESHCORE_MESSAGE, logbook_event)

        _LOGGER.debug(
            "Logged outgoing channel message to %s: %s (repeaters: %s)",
            channel_name,
            message_text[:50] + ("..." if len(message_text) > 50 else ""),
            logbook_event.get("repeater_count", "unknown")
        )
