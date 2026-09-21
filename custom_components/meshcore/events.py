"""The integration's Home Assistant event surface.

Every ``meshcore_*`` event is fired here and nowhere else, so one module owns
what listeners see: the legacy payload of each event, plus the entry identity
(``entry_id`` and the root device's registry id) a multi-radio install needs to
tell which radio spoke. Producers that own a clock or a payload variant build
that payload; this module stamps identity onto it, redacts node secrets and
puts it on the bus.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, EVENT_CLI_RESPONSE

_LOGGER = logging.getLogger(__name__)

EVENT_MESSAGE: Final = f"{DOMAIN}_message"
EVENT_DELIVERY_UPDATE: Final = f"{DOMAIN}_delivery_update"
EVENT_MESSAGE_SENT: Final = f"{DOMAIN}_message_sent"
EVENT_MESSAGE_SEND_FAILED: Final = f"{DOMAIN}_message_send_failed"
EVENT_RAW: Final = f"{DOMAIN}_raw_event"
EVENT_CONNECTED: Final = f"{DOMAIN}_connected"
EVENT_DISCONNECTED: Final = f"{DOMAIN}_disconnected"

# What a redacted value reads as, and the payload keys that carry a shared
# secret. An SDK event that is nothing but a node secret is dropped whole.
REDACTED: Final = "<redacted>"
SECRET_KEYS: Final = frozenset({"channel_secret", "secret"})
SECRET_EVENT: Final = "PRIVATE_KEY"


def is_secret_event(event_type: str) -> bool:
    """Return whether an SDK event's whole payload is a node secret."""
    return SECRET_EVENT in event_type


def sanitize_event_data(data: Any, *, redact: bool = True) -> Any:
    """Make event data JSON serializable, hiding shared secrets by default.

    Bytes become hex strings and objects become their ``vars()`` so the result
    survives the bus and MQTT. With ``redact`` set (the default), any value
    stored under a secret-bearing key is replaced rather than forwarded.
    """
    if isinstance(data, dict):
        return {
            key: REDACTED
            if redact and key in SECRET_KEYS
            else sanitize_event_data(value, redact=redact)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [sanitize_event_data(value, redact=redact) for value in data]
    if isinstance(data, tuple):
        return tuple(sanitize_event_data(value, redact=redact) for value in data)
    if isinstance(data, bytes):
        return data.hex()
    if hasattr(data, "__dict__") and not isinstance(data, type):
        # Objects carry their attributes; class objects have a __dict__ too and
        # are left alone.
        return sanitize_event_data(vars(data), redact=redact)
    return data


def _identity(hass: HomeAssistant, entry: ConfigEntry | None) -> dict[str, Any]:
    """Return the entry identity every event carries, unknowns as None."""
    if entry is None:
        return {"entry_id": None, "device_id": None}
    return {"entry_id": entry.entry_id, "device_id": _device_id(hass, entry)}


def _device_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return the root device's registry id, or None before it is registered.

    The registry is missing entirely in the stubbed test tier and empty until
    the first platform registers the hub device, so anything but a real id
    reads as unknown rather than raising inside an event producer.
    """
    try:
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    except Exception:
        return None
    device_id = getattr(device, "id", None)
    return device_id if isinstance(device_id, str) else None


def _fire(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    event: str,
    payload: Mapping[str, Any],
) -> None:
    """Put one event on the bus with its entry identity stamped on.

    Payload keys win over identity: the only producer that sets ``entry_id``
    itself is the CLI, whose legacy field is the entry the caller asked for.
    """
    hass.bus.async_fire(event, {**_identity(hass, entry), **payload})


def fire_message(
    hass: HomeAssistant, entry: ConfigEntry | None, payload: Mapping[str, Any]
) -> None:
    """Fire the logbook-visible message event, incoming or outgoing."""
    _fire(hass, entry, EVENT_MESSAGE, payload)


def fire_delivery_update(
    hass: HomeAssistant, entry: ConfigEntry | None, payload: Mapping[str, Any]
) -> None:
    """Fire one progressive delivery update for a message already announced."""
    _fire(hass, entry, EVENT_DELIVERY_UPDATE, payload)


def fire_message_sent(
    hass: HomeAssistant, entry: ConfigEntry | None, payload: Mapping[str, Any]
) -> None:
    """Fire the local send acknowledgement for an outgoing message."""
    _fire(hass, entry, EVENT_MESSAGE_SENT, payload)


def fire_send_failed(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    *,
    reason: str,
    message_type: str,
    **details: Any,
) -> None:
    """Fire the event that says a send never left the radio."""
    _fire(
        hass,
        entry,
        EVENT_MESSAGE_SEND_FAILED,
        {
            "reason": reason,
            "message_type": message_type,
            "timestamp": int(time.time()),
            **details,
        },
    )


def fire_cli_response(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    *,
    command: str,
    response: Any,
    is_error: bool,
    requested_entry_id: str | None,
) -> None:
    """Fire the CLI console transcript event for one command.

    ``entry_id`` stays the entry the caller named (``None`` when they named
    none); the entry the command actually ran on is the additive
    ``resolved_entry_id``.
    """
    _fire(
        hass,
        entry,
        EVENT_CLI_RESPONSE,
        {
            "command": command,
            "response": response,
            "is_error": is_error,
            "entry_id": requested_entry_id,
            "timestamp": int(time.time()),
            "resolved_entry_id": entry.entry_id if entry is not None else None,
        },
    )


def fire_raw_event(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    *,
    event_type: str,
    payload: Any,
    error: str | None = None,
) -> None:
    """Fire one SDK event onto the bus, already sanitized by the caller."""
    data: dict[str, Any] = {
        "event_type": event_type,
        "payload": payload,
        "timestamp": time.time(),
    }
    if error is not None:
        data["serialization_error"] = error
    _fire(hass, entry, EVENT_RAW, data)


def fire_connected(
    hass: HomeAssistant, entry: ConfigEntry | None, *, connection_type: str
) -> None:
    """Fire the link-up edge for one radio."""
    _fire(hass, entry, EVENT_CONNECTED, {"connection_type": connection_type})


def fire_disconnected(
    hass: HomeAssistant, entry: ConfigEntry | None, *, unexpected: bool = False
) -> None:
    """Fire the link-down edge; ``unexpected`` marks a loss, not a teardown."""
    _fire(hass, entry, EVENT_DISCONNECTED, {"unexpected": True} if unexpected else {})
