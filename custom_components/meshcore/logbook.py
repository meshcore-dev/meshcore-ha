"""Logbook integration for MeshCore."""
import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_DEVICE_NAME,
    DOMAIN,
    ENTITY_DOMAIN_BINARY_SENSOR,
)
from .events import EVENT_MESSAGE, fire_delivery_update, fire_message
from .utils import create_message_correlation_key, get_channel_entity_id, get_contact_entity_id

_LOGGER = logging.getLogger(__name__)


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

    async_describe_event(DOMAIN, EVENT_MESSAGE, process_message_event)


def channel_label(channel_info: dict | None, channel_idx: int) -> str:
    """Name a channel for display, never returning an empty label.

    The logbook renders a message with no channel as a direct message, so an
    unnamed channel is shown by its index; channel 0 keeps the name it has
    always been given.
    """
    name = (channel_info or {}).get("channel_name") or ""
    if name.strip():
        return name
    return "public" if channel_idx == 0 else str(channel_idx)


def _split_sender(message_text: str, coordinator) -> tuple[str, str, str]:
    """Split "Name: text" into its sender, its text and the sender's key.

    Channel packets carry the sender's advertised name, but message bodies
    contain colons too, so the prefix is only read as a sender when it names a
    contact this node knows. With no contact table to ask (link down) the
    prefix is trusted, as it always was.
    """
    if not message_text or ":" not in message_text:
        return "Unknown", message_text, ""
    name, _, text = message_text.partition(":")
    name, text = name.strip(), text.strip()
    if not name:
        return "Unknown", message_text, ""

    api = getattr(coordinator, "api", None)
    if api is None or not api.connected:
        return name, text, ""
    contact = api.contact_by_name(name)
    if not isinstance(contact, dict):
        return "Unknown", message_text, ""
    return name, text, contact.get("public_key", "")[:12]


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
        channel_name = channel_label(channel_info, channel_idx)
        sender_name, message_text, sender_pubkey = _split_sender(message_text, coordinator)

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

        # path_len semantics for received channel packets mirror the
        # direct-contact path (see handle_contact_message for the
        # empirical notes against the firmware payload):
        #   * Direct reception (no repeaters): SDK returns 255 (0xFF)
        #     — sentinel for "no path bytes processed". -1 also occurs
        #     in some SDK paths.
        #   * Multi-hop reception: SDK returns the literal hop count.
        # path_hash_mode is a separate field — no bit-mask applied here.
        # SNR semantics: V3 CHANNEL_MSG_RECV frames carry SNR directly
        # (uppercase "SNR" key from the SDK reader). V2 frames only
        # surface SNR via the log_channels lookup when channel decryption
        # is enabled, so absence is normal — emit the field only when
        # the SDK actually provided it.
        path_len_raw = payload.get("path_len", 0)
        if not isinstance(path_len_raw, int) or path_len_raw < 0 or path_len_raw == 0xFF:
            hop_count = 0
        else:
            hop_count = path_len_raw
        snr = payload.get("SNR")  # V3 channel frames; V2 only via log_channels

        # Create event data
        event_data = {
            "message": message_text,
            "sender_name": sender_name,
            "channel": channel_name,
            "channel_idx": channel_idx,
            "entity_id": entity_id,
            "domain": DOMAIN,
            "timestamp": dt_util.utcnow().isoformat(),
            "message_type": "channel",  # Explicit message type for filtering
            "hop_count": hop_count,
        }

        if snr is not None:
            event_data["snr"] = snr

        # Add sender pubkey if available
        if sender_pubkey:
            event_data["pubkey_prefix"] = sender_pubkey

        # RX_LOG correlation: match incoming channel messages with radio
        # reception data (SNR, RSSI, hop count, path) from RX_LOG events.
        #
        # Two modes controlled by the adaptive_poll_wait config option:
        #
        # Default (disabled): Wait a fixed 500ms for RX_LOG data to
        #   accumulate, then attach whatever arrived to the event and fire.
        #   This is the original behavior and ensures maximum RX_LOG data
        #   is present on the initial meshcore_message event.
        #
        # Adaptive (enabled): Poll every 50ms up to 500ms and fire as
        #   soon as data arrives. A background task then collects
        #   late-arriving repeater RX_LOGs and delivers them via
        #   progressive meshcore_delivery_update events.
        adaptive = coordinator.settings.adaptive_poll_wait
        hash_key = None
        try:
            timestamp = payload.get("sender_timestamp")

            if channel_idx is not None and timestamp:
                hash_key = create_message_correlation_key(channel_idx, timestamp)

                # Skip if this key is reserved for outgoing delivery tracking.
                if hash_key in coordinator._outgoing_correlation_keys:
                    _LOGGER.debug("Skipping RX_LOG pop for outgoing-reserved key %s", hash_key[:8])
                    hash_key = None
                elif adaptive:
                    # Adaptive poll-wait: check every 50ms, fire as soon as
                    # data arrives, with a 500ms ceiling matching the old behavior.
                    for _ in range(_INCOMING_MAX_POLLS):
                        await asyncio.sleep(_INCOMING_POLL_INTERVAL)
                        batch = coordinator._pending_rx_logs.pop(hash_key, None)
                        if batch:
                            event_data["rx_log_data"] = list(batch)
                            _LOGGER.debug(
                                "Adaptive RX_LOG correlation: %d entry(ies) after poll",
                                len(batch),
                            )
                            break
                else:
                    # Fixed wait: sleep 500ms then pop whatever accumulated.
                    await asyncio.sleep(_INCOMING_FIXED_WAIT)
                    rx_logs = coordinator._pending_rx_logs.pop(hash_key, None)
                    if rx_logs:
                        event_data["rx_log_data"] = list(rx_logs)
                        _LOGGER.debug(
                            "Fixed-wait RX_LOG correlation: %d entry(ies)",
                            len(rx_logs),
                        )
        except Exception as ex:
            _LOGGER.debug("Error in RX_LOG correlation: %s", ex)

        # Fire the meshcore_message event
        fire_message(hass, coordinator.config_entry, event_data)

        # In adaptive mode, start background collection for late-arriving
        # repeater RX_LOGs (progressive delivery updates).
        if adaptive and hash_key is not None:
            hass.async_create_task(
                _collect_incoming_rx_logs(
                    hass, coordinator, hash_key, event_data
                )
            )

        _LOGGER.debug(
            "Logged channel message in %s from %s%s: %s",
            channel_name,
            sender_name,
            f" ({sender_pubkey[:6]})" if sender_pubkey else "",
            message_text[:50] + ("..." if len(message_text) > 50 else "")
        )
    except Exception as ex:
        _LOGGER.error("Error handling channel message: %s", ex, exc_info=True)


# Fixed-wait duration (seconds) — original behavior.
_INCOMING_FIXED_WAIT = 0.5

# Adaptive poll-wait settings.
_INCOMING_POLL_INTERVAL = 0.05   # 50ms per poll
_INCOMING_MAX_POLLS = 10         # 10 polls × 50ms = 500ms ceiling

# Background collection pass intervals (seconds to wait before each pop).
# Repeater relays arrive ~300-400ms per hop; two passes catch 2-3 repeaters.
_INCOMING_BG_PASS_INTERVALS = [0.5, 1.0]


async def _collect_incoming_rx_logs(
    hass, coordinator, hash_key: str, base_event_data: dict
) -> None:
    """Background task to collect late-arriving RX_LOG entries for incoming messages.

    Fires meshcore_delivery_update events as new RX_LOG data arrives,
    matching the progressive pattern used for outgoing channel messages.
    """
    try:
        # Start with any RX_LOG data already attached to the initial event
        all_rx_logs = list(base_event_data.get("rx_log_data", []))

        for pass_idx, interval in enumerate(_INCOMING_BG_PASS_INTERVALS):
            await asyncio.sleep(interval)

            batch = coordinator._pending_rx_logs.pop(hash_key, None)
            if not batch:
                continue

            all_rx_logs.extend(batch)
            _LOGGER.debug(
                "Background pass %d: collected %d new RX_LOG(s), total %d",
                pass_idx + 1, len(batch), len(all_rx_logs)
            )

            # Fire progressive update event
            update_data = {
                "entity_id": base_event_data.get("entity_id"),
                "domain": base_event_data.get("domain", DOMAIN),
                "rx_log_data": list(all_rx_logs),
                "repeater_count": len(all_rx_logs),
                "progressive": True,
                "message_type": "channel",
                "sender_name": base_event_data.get("sender_name"),
                "message": base_event_data.get("message"),
                "timestamp": base_event_data.get("timestamp"),
            }
            # Correlation fields: every meshcore_delivery_update carries entity_id,
            # sender_name, message, and timestamp — enough for downstream
            # listeners to correlate back to a previously-received
            # meshcore_message event without re-hashing the message text.
            fire_delivery_update(hass, coordinator.config_entry, update_data)

    except Exception as ex:
        _LOGGER.debug("Error in background RX_LOG collection: %s", ex)

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

        # Look up contact name from pubkey_prefix using MeshCore API.
        # A sender this node has no contact for is reported as such: the key
        # prefix below identifies them, so there is no name to invent.
        contact_name = None
        if hasattr(coordinator, "api") and coordinator.api.connected:
            # Try to find contact by public key prefix
            contact = coordinator.api.contact_by_prefix(pubkey_prefix)
            if contact and isinstance(contact, dict):
                contact_name = contact.get("adv_name") or None

        # Generate entity ID matching MeshCoreMessageEntity
        entity_id = get_contact_entity_id(
            ENTITY_DOMAIN_BINARY_SENSOR,
            device_key[:6] if device_key else "unknown",
            pubkey_prefix[:6]
        )

        # path_len semantics for received DM packets (verified empirically
        # against firmware payload on 2026-04-23):
        #   * Direct contact (no repeaters): SDK returns 255 (0xFF) — sentinel
        #     for "no path bytes processed". -1 also occurs in some SDK paths.
        #   * Multi-hop contact: SDK returns the literal hop count.
        # path_hash_mode is a SEPARATE field in the payload — there's no
        # packed encoding here, so no bit-mask is applied to path_len.
        path_len_raw = payload.get("path_len", 0)
        if not isinstance(path_len_raw, int) or path_len_raw < 0 or path_len_raw == 0xFF:
            hop_count = 0
        else:
            hop_count = path_len_raw
        snr = payload.get("SNR")  # V3 only, uppercase in SDK

        # Create event data
        event_data = {
            "message": message_text,
            "sender_name": contact_name,
            "pubkey_prefix": pubkey_prefix,
            "receiver_name": DEFAULT_DEVICE_NAME,
            "entity_id": entity_id,
            "domain": DOMAIN,
            "timestamp": dt_util.utcnow().isoformat(),
            "message_type": "direct",  # Explicit message type for filtering
            "hop_count": hop_count,
        }

        if snr is not None:
            event_data["snr"] = snr

        # Fire event
        fire_message(hass, coordinator.config_entry, event_data)

        _LOGGER.debug(
            "Logged direct message from %s (%s): %s",
            contact_name,
            pubkey_prefix[:6],
            message_text[:50] + ("..." if len(message_text) > 50 else "")
        )
    except Exception as ex:
        _LOGGER.error("Error handling contact message: %s", ex, exc_info=True)

def _collected(
    base: dict, rx_logs: list, *, progressive: bool = False, collecting: bool | None = None
) -> dict:
    """Return an outgoing channel event carrying the receptions heard so far.

    ``collecting`` says whether more receptions may still arrive, which is what
    tells a listener that an empty count is "nothing yet" rather than "nobody".
    """
    event = {
        **base,
        "rx_log_data": list(rx_logs),
        "repeater_count": len(rx_logs),
        "progressive": progressive,
    }
    if collecting is not None:
        event["collecting"] = collecting
    return event


async def handle_outgoing_message(event_data, coordinator) -> None:
    """Handle outgoing message events from the new message_sent event."""
    if not event_data:
        return
        
    # Get coordinator info
    if not hasattr(coordinator, "hass"):
        _LOGGER.warning("Cannot log outgoing message: coordinator.hass not available")
        return
        
    hass = coordinator.hass
    entry = coordinator.config_entry
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
            "timestamp": dt_util.utcnow().isoformat(),
            "outgoing": True,
            "message_type": "direct",
            "send_id": event_data.get("send_id"),
        }

        # Add ACK status if available
        if ack_received is not None:
            logbook_event["ack_received"] = ack_received

        # Fire event
        fire_message(hass, entry, logbook_event)
        # The ACK wait is over either way, so this is the message's last word:
        # delivery listeners get the outcome without re-reading the logbook.
        fire_delivery_update(hass, entry, {**logbook_event, "progressive": False})

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
            "timestamp": dt_util.utcnow().isoformat(),
            "outgoing": True,
            "message_type": "channel",
            "send_id": event_data.get("send_id"),
        }

        # Correlate with RX_LOG data for outgoing channel messages.
        # When we send a channel message, repeaters re-broadcast it and our
        # radio picks up those re-broadcasts as RX_LOG events. This lets us
        # count how many repeaters relayed our message.
        #
        # The message itself is logged now, not four seconds from now: a send
        # is a fact as soon as the radio takes it, and a shutdown in between
        # used to lose the entry entirely. What the repeaters heard follows as
        # delivery updates, in rolling 1-second passes. Using pop() on a match
        # forces late arrivals into a new cache entry under the same key,
        # which subsequent passes pick up.
        NUM_COLLECTION_PASSES = 4
        PASS_INTERVAL_SECONDS = 1.0

        hash_key = None
        send_timestamp = event_data.get("send_timestamp")
        if channel_idx is not None and send_timestamp:
            try:
                # Single correlation key using channel + timestamp only.
                # Text is excluded because the HA config name may differ from
                # the on-device advertised name prepended to broadcasts.
                hash_key = create_message_correlation_key(channel_idx, send_timestamp)
            except Exception as ex:
                _LOGGER.debug("Could not build the RX_LOG correlation key: %s", ex)

        fire_message(
            hass, entry, _collected(logbook_event, [], collecting=hash_key is not None)
        )
        _LOGGER.debug(
            "Logged outgoing channel message to %s: %s",
            channel_name,
            message_text[:50] + ("..." if len(message_text) > 50 else ""),
        )
        if hash_key is None:
            return

        # Reserve this key so the incoming handler doesn't pop() it.
        # The incoming handler fires 500ms faster and would steal entries
        # before our first collection pass at 1000ms.
        coordinator._outgoing_correlation_keys[hash_key] = True
        all_rx_logs: list = []

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

                # Correlation fields: every meshcore_delivery_update carries
                # entity_id, sender_name, message, send_id and timestamp —
                # enough to correlate back to the meshcore_message that
                # announced this send.
                fire_delivery_update(
                    hass,
                    entry,
                    _collected(
                        logbook_event,
                        all_rx_logs,
                        progressive=pass_num < NUM_COLLECTION_PASSES - 1,
                    ),
                )
        except asyncio.CancelledError:
            # Unload or shutdown mid-collection: publish the count reached so
            # far rather than leaving listeners on a progressive update forever.
            fire_delivery_update(hass, entry, _collected(logbook_event, all_rx_logs))
            raise
        except Exception as ex:
            _LOGGER.debug("Error correlating outgoing channel message with RX_LOG: %s", ex)
            fire_delivery_update(hass, entry, _collected(logbook_event, all_rx_logs))
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
                channel_idx, send_timestamp, hash_key[:8], cache_keys[:5]
            )
        else:
            _LOGGER.debug(
                "Correlated outgoing channel message with %d RX_LOG reception(s) total",
                len(all_rx_logs)
            )
