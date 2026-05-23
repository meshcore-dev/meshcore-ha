"""Tests for async_execute_command_service response normalization in services.py.

Targets the three SDK return shapes the handler must normalize:
  * Event-with-payload-dict (most send_* / set_* commands)
  * Plain dict             (req_*_sync awaited response payloads)
  * None                   (req_*_sync timeout / no response)
"""
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── Module loading ────────────────────────────────────────────────────
# Patch meshcore.events.EventType with the members services.py references.
# conftest inserts a bare MagicMock for meshcore.events; the module-level
# `from meshcore.events import EventType` resolves against whatever attribute
# exists on that mock at import time.
class _ET:
    ERROR = "error"
    TRACE_DATA = "trace_data"
    PATH_RESPONSE = "path_response"
    MSG_SENT = "msg_sent"
    CONTACTS = "contacts"
    ACK = "ack"

_mc_events = sys.modules.get("meshcore.events")
if _mc_events is not None:
    _mc_events.EventType = _ET

_SERVICES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "services.py",
)
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.services", _SERVICES_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)

async_setup_services = _module.async_setup_services
DOMAIN = _module.DOMAIN

# The const module is MagicMock-mocked in conftest, so when services.py does
# `call.data[ATTR_COMMAND]`, the key is a MagicMock attribute, not the string
# "command". Use the same MagicMocks the production code uses as dict keys.
ATTR_COMMAND = _module.ATTR_COMMAND
ATTR_ENTRY_ID = _module.ATTR_ENTRY_ID


# ─── Fixtures ──────────────────────────────────────────────────────────
class _Event:
    """Mimic meshcore.events.Event enough for response normalization."""

    def __init__(self, type_, payload):
        self.type = type_
        self.payload = payload


def _build_coordinator(command_name, return_value, contact=None):
    """Coordinator with a single mocked SDK command and optional contact lookup."""
    coord = MagicMock()
    coord.api = MagicMock()
    coord.api.connected = True
    coord.api.self_info = {"suggested_timeout": 1000}

    mesh_core = MagicMock()
    mesh_core.commands = MagicMock()
    setattr(
        mesh_core.commands,
        command_name,
        AsyncMock(return_value=return_value),
    )

    # Contact lookup for "contact"-typed params. _resolve_contact tries
    # mesh_core.get_contact_by_key_prefix first; returning the contact short-
    # circuits the rest.
    mesh_core.get_contact_by_key_prefix = MagicMock(return_value=contact)
    mesh_core.get_contact_by_name = MagicMock(return_value=None)

    coord.api.mesh_core = mesh_core
    coord._discovered_contacts = {}
    return coord


async def _get_execute_handler(coordinator):
    """Run async_setup_services with stubbed hass and return execute handler."""
    registered = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}

    def _register(domain, service_const, handler, **kwargs):
        registered[getattr(handler, "__name__", repr(handler))] = (handler, kwargs)

    hass.services.async_register = _register
    hass.services.has_service = MagicMock(return_value=False)

    await async_setup_services(hass)
    return registered["async_execute_command_service"][0]


def _call(command):
    call = MagicMock()
    call.data = {ATTR_COMMAND: command, ATTR_ENTRY_ID: None}
    return call


# ─── Tests: response shape normalization ───────────────────────────────
@pytest.mark.asyncio
async def test_event_payload_returned_as_dict():
    """Regression: Event with .payload dict still flows through unchanged."""
    payload = {"status": "ok", "value": 42}
    coord = _build_coordinator("reboot", _Event(_ET.MSG_SENT, payload))
    handler = await _get_execute_handler(coord)

    result = await handler(_call("reboot"))

    assert result == payload


@pytest.mark.asyncio
async def test_event_with_bytes_in_payload_is_hex_encoded():
    """Bytes inside an Event payload are converted to hex strings."""
    payload = {"key": b"\x01\x02\xff", "name": "node"}
    coord = _build_coordinator("reboot", _Event(_ET.MSG_SENT, payload))
    handler = await _get_execute_handler(coord)

    result = await handler(_call("reboot"))

    assert result == {"key": "0102ff", "name": "node"}


@pytest.mark.asyncio
async def test_plain_dict_response_is_passed_through():
    """req_*_sync returns a plain dict — must surface as the response."""
    contact = {
        "adv_name": "repeater",
        "public_key": "c9b4f226ecd1" + "0" * 52,
        "pubkey_prefix": "c9b4f226ecd1",
    }
    coord = _build_coordinator(
        "req_owner_sync",
        return_value={"name": "MyRepeater", "owner": "alice"},
        contact=contact,
    )
    handler = await _get_execute_handler(coord)

    result = await handler(_call("req_owner_sync c9b4f226ecd1"))

    assert result == {"name": "MyRepeater", "owner": "alice"}


@pytest.mark.asyncio
async def test_plain_dict_response_converts_bytes_to_hex():
    """Bytes inside a plain-dict response are also hex-encoded."""
    contact = {
        "adv_name": "node",
        "public_key": "abcdef123456" + "0" * 52,
        "pubkey_prefix": "abcdef123456",
    }
    coord = _build_coordinator(
        "req_owner_sync",
        return_value={"name": "n", "raw": b"\xde\xad\xbe\xef"},
        contact=contact,
    )
    handler = await _get_execute_handler(coord)

    result = await handler(_call("req_owner_sync abcdef123456"))

    assert result == {"name": "n", "raw": "deadbeef"}


@pytest.mark.asyncio
async def test_none_response_returns_structured_error():
    """req_*_sync returning None (timeout) yields a structured error dict
    instead of None — which is what was breaking the HA UI."""
    contact = {
        "adv_name": "repeater",
        "public_key": "c9b4f226ecd1" + "0" * 52,
        "pubkey_prefix": "c9b4f226ecd1",
    }
    coord = _build_coordinator(
        "req_status_sync",
        return_value=None,
        contact=contact,
    )
    handler = await _get_execute_handler(coord)

    result = await handler(_call("req_status_sync c9b4f226ecd1"))

    assert result == {"error": "no_response", "command": "req_status_sync"}


@pytest.mark.asyncio
async def test_empty_event_payload_returns_none():
    """Regression: Event with empty-dict payload preserves the original
    implicit-None return so callers that don't expect data aren't surprised."""
    coord = _build_coordinator("reboot", _Event(_ET.MSG_SENT, {}))
    handler = await _get_execute_handler(coord)

    result = await handler(_call("reboot"))

    assert result is None
