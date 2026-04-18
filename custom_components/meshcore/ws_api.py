"""MeshCore WebSocket API commands."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _get_coordinator(hass: HomeAssistant, entry_id: str | None = None):
    """Get coordinator for the given entry_id, or the first available one."""
    if DOMAIN not in hass.data:
        return None

    if entry_id and entry_id in hass.data[DOMAIN]:
        coord = hass.data[DOMAIN][entry_id]
        if hasattr(coord, "api"):
            return coord
        return None

    # Return first coordinator found
    for key, value in hass.data[DOMAIN].items():
        if hasattr(value, "api"):
            return value
    return None


def async_register_ws_commands(hass: HomeAssistant) -> None:
    """Register all MeshCore WebSocket commands."""
    websocket_api.async_register_command(hass, ws_get_stored_messages)
    websocket_api.async_register_command(hass, ws_get_stored_message_count)
    websocket_api.async_register_command(hass, ws_search_stored_messages)


# ═══════════════════════════════════════════════════════════════════════════
# Message Store Commands
# ═══════════════════════════════════════════════════════════════════════════


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshcore/get_stored_messages",
        vol.Required("entity_id"): str,
        vol.Optional("limit", default=50): int,
        vol.Optional("before"): str,
        vol.Optional("after"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_stored_messages(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Get stored messages for a conversation with cursor pagination."""
    coordinator = _get_coordinator(hass, msg.get("entry_id"))
    if not coordinator:
        connection.send_error(msg["id"], "not_found", "No MeshCore coordinator found")
        return

    messages = await coordinator.get_messages(
        msg["entity_id"],
        limit=msg.get("limit", 50),
        before=msg.get("before"),
        after=msg.get("after"),
    )
    connection.send_result(msg["id"], {
        "messages": messages,
        "has_more": len(messages) == msg.get("limit", 50),
    })


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshcore/get_stored_message_count",
        vol.Required("entity_id"): str,
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_get_stored_message_count(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Get message count for a conversation from the in-memory index."""
    coordinator = _get_coordinator(hass, msg.get("entry_id"))
    if not coordinator:
        connection.send_error(msg["id"], "not_found", "No MeshCore coordinator found")
        return

    index_entry = coordinator.get_message_index().get(msg["entity_id"], {})
    connection.send_result(msg["id"], {"count": index_entry.get("message_count", 0)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "meshcore/search_stored_messages",
        vol.Required("query"): str,
        vol.Optional("entity_id"): str,
        vol.Optional("from_date"): str,
        vol.Optional("to_date"): str,
        vol.Optional("limit", default=20): int,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_search_stored_messages(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Search stored messages by text or sender name.

    Uses _load_for_search() for non-caching disk reads — conversations
    loaded solely for search are not kept in memory after the call returns.
    """
    coordinator = _get_coordinator(hass, msg.get("entry_id"))
    if not coordinator:
        connection.send_error(msg["id"], "not_found", "No MeshCore coordinator found")
        return

    query = msg["query"].lower()
    from_date = msg.get("from_date")
    to_date = msg.get("to_date")
    results = []
    limit = msg.get("limit", 20)

    entities = (
        [msg["entity_id"]] if msg.get("entity_id")
        else list(coordinator.get_message_index().keys())
    )
    for eid in entities:
        messages = await coordinator._load_for_search(eid)

        # Resolve conversation name from HA entity state
        state = hass.states.get(eid)
        conv_name = state.attributes.get("friendly_name", eid) if state else eid

        for m in reversed(messages):
            ts = m.get("timestamp", "")

            # Date range filtering
            if from_date and ts < from_date:
                continue
            if to_date and ts > to_date:
                continue

            if query in (m.get("text", "")).lower() or query in (m.get("sender", "")).lower():
                results.append({
                    **m,
                    "entity_id": eid,
                    "conversation_name": conv_name,
                })
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break

    connection.send_result(msg["id"], {"results": results})
