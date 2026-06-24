"""Tests for the CLI console services (cli_command / cli_command_ui) in services.py.

The CLI console services wrap execute_command and additionally:
  * record the command/response pair to the resolved coordinator's console
  * fire the EVENT_CLI_RESPONSE event
  * return the same response execute_command produces

These tests reuse the module-loading approach from test_execute_command.py:
conftest stubs meshcore/const/homeassistant, so services.py is loaded directly
and its handlers are exercised against MagicMock coordinators.
"""
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


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

# create_service_call (used by cli_command_ui) branches on MAJOR_VERSION, which
# conftest leaves as a MagicMock; pin it so the comparison works under test.
_module.MAJOR_VERSION = 2025

async_setup_services = _module.async_setup_services
DOMAIN = _module.DOMAIN
ATTR_COMMAND = _module.ATTR_COMMAND
ATTR_ENTRY_ID = _module.ATTR_ENTRY_ID
EVENT_CLI_RESPONSE = _module.EVENT_CLI_RESPONSE


class _Event:
    """Mimic meshcore.events.Event enough for response normalization."""

    def __init__(self, type_, payload):
        self.type = type_
        self.payload = payload


def _build_coordinator(command_name, return_value, connected=True):
    """Coordinator with a single mocked SDK command. record_cli_console is a
    plain MagicMock so calls can be asserted."""
    coord = MagicMock()
    coord.api = MagicMock()
    coord.api.connected = connected
    coord.api.self_info = {"suggested_timeout": 1000}

    mesh_core = MagicMock()
    mesh_core.commands = MagicMock()
    setattr(
        mesh_core.commands,
        command_name,
        AsyncMock(return_value=return_value),
    )
    coord.api.mesh_core = mesh_core
    coord._discovered_contacts = {}
    return coord


async def _setup(coordinator):
    """Run async_setup_services with stubbed hass; return (hass, registered)."""
    registered = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}

    def _register(domain, service_const, handler, **kwargs):
        registered[getattr(handler, "__name__", repr(handler))] = (handler, kwargs)

    hass.services.async_register = _register
    hass.services.has_service = MagicMock(return_value=False)

    await async_setup_services(hass)
    return hass, registered


def _call(command, entry_id=None):
    call = MagicMock()
    call.data = {ATTR_COMMAND: command, ATTR_ENTRY_ID: entry_id}
    return call


@pytest.mark.asyncio
async def test_cli_command_records_and_returns_response():
    """cli_command returns the execute_command response and records it."""
    payload = {"level": 4100, "status": "ok"}
    coord = _build_coordinator("get_bat", _Event(_ET.MSG_SENT, payload))
    hass, registered = await _setup(coord)
    handler = registered["async_cli_command_service"][0]

    result = await handler(_call("get_bat"))

    assert result == payload
    coord.record_cli_console.assert_called_once()
    args, _ = coord.record_cli_console.call_args
    assert args[0] == "get_bat"      # command
    assert args[1] == payload        # response
    assert args[2] is False          # is_error


@pytest.mark.asyncio
async def test_cli_command_fires_event():
    """cli_command fires EVENT_CLI_RESPONSE with the command and response."""
    payload = {"ok": True}
    coord = _build_coordinator("get_time", _Event(_ET.MSG_SENT, payload))
    hass, registered = await _setup(coord)
    handler = registered["async_cli_command_service"][0]

    await handler(_call("get_time"))

    hass.bus.async_fire.assert_called_once()
    event_name, event_data = hass.bus.async_fire.call_args[0]
    assert event_name == EVENT_CLI_RESPONSE
    assert event_data["command"] == "get_time"
    assert event_data["response"] == payload
    assert event_data["is_error"] is False


@pytest.mark.asyncio
async def test_cli_command_marks_error_on_no_response():
    """A None response (e.g. unknown/failed command) records is_error=True."""
    # An unknown command makes execute_command return None without running.
    coord = _build_coordinator("get_bat", _Event(_ET.MSG_SENT, {"x": 1}))
    hass, registered = await _setup(coord)
    handler = registered["async_cli_command_service"][0]

    result = await handler(_call("definitely_not_a_command"))

    assert result is None
    coord.record_cli_console.assert_called_once()
    args, _ = coord.record_cli_console.call_args
    assert args[2] is True  # is_error


@pytest.mark.asyncio
async def test_cli_command_ui_reads_text_helper():
    """cli_command_ui pulls the command from text.meshcore_command and runs it."""
    payload = {"done": True}
    coord = _build_coordinator("send_advert", _Event(_ET.MSG_SENT, payload))
    hass, registered = await _setup(coord)
    handler = registered["async_cli_command_ui_service"][0]

    state = MagicMock()
    state.state = "send_advert"
    hass.states.get = MagicMock(return_value=state)
    hass.services.async_call = AsyncMock()

    # create_service_call wraps homeassistant.core.ServiceCall, which conftest
    # mocks (its .data is not the dict we pass). Stub it to a plain call object
    # so the delegated cli_command sees the real command string.
    def _fake_call(domain, service, data=None, hass=None):
        # The UI handler builds data with literal "command"/"entry_id" keys;
        # the cli_command handler reads the (mocked) ATTR_* constants. Remap so
        # the delegated handler finds the command string.
        data = data or {}
        c = MagicMock()
        c.data = {
            ATTR_COMMAND: data.get("command"),
            ATTR_ENTRY_ID: data.get("entry_id"),
        }
        return c

    monkeypatched = _module.create_service_call
    _module.create_service_call = _fake_call
    try:
        ui_call = MagicMock()
        ui_call.data = {ATTR_ENTRY_ID: None}
        result = await handler(ui_call)
    finally:
        _module.create_service_call = monkeypatched

    assert result == payload
    coord.record_cli_console.assert_called_once()
    # Input field is cleared after execution.
    hass.services.async_call.assert_awaited()


@pytest.mark.asyncio
async def test_cli_command_ui_noop_on_empty_input():
    """cli_command_ui does nothing when the text helper is empty."""
    coord = _build_coordinator("get_bat", _Event(_ET.MSG_SENT, {"x": 1}))
    hass, registered = await _setup(coord)
    handler = registered["async_cli_command_ui_service"][0]

    state = MagicMock()
    state.state = ""
    hass.states.get = MagicMock(return_value=state)

    result = await handler(MagicMock(data={ATTR_ENTRY_ID: None}))

    assert result is None
    coord.record_cli_console.assert_not_called()


@pytest.mark.asyncio
async def test_cli_clear_service_clears_console():
    """cli_console_clear clears the resolved coordinator's transcript."""
    coord = _build_coordinator("get_bat", _Event(_ET.MSG_SENT, {"x": 1}))
    hass, registered = await _setup(coord)
    handler = registered["async_cli_clear_service"][0]

    await handler(MagicMock(data={ATTR_ENTRY_ID: None}))

    coord.clear_cli_console.assert_called_once()
