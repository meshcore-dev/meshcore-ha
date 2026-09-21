"""Tests for BLE PIN handling in meshcore_api.py.

The integration forwards an optional BLE pairing PIN to the SDK's
MeshCore.create_ble(pin=...). These tests cover:
  * PIN normalization in MeshCoreAPI.__init__ (blank/whitespace -> None)
  * connect() forwarding the normalized PIN to create_ble

conftest stubs meshcore/const/homeassistant in sys.modules, so meshcore_api.py
is loaded directly via importlib (same approach as test_execute_command.py).
"""
import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Module loading ────────────────────────────────────────────────────
_API_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "meshcore_api.py",
)
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.meshcore_api", _API_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)

MeshCoreAPI = _module.MeshCoreAPI

# meshcore_api imports CONNECTION_TYPE_* from the (mocked) const module; reuse
# the same objects production code compares against.
CONNECTION_TYPE_BLE = _module.CONNECTION_TYPE_BLE

# The pairing-agent module has no package-relative imports, so it can be loaded
# standalone (dbus_fast is imported lazily inside register_pairing_agent).
_AGENT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "ble_pairing_agent.py",
)
_agent_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.ble_pairing_agent", _AGENT_PATH
)
_agent_module = importlib.util.module_from_spec(_agent_spec)
_agent_spec.loader.exec_module(_agent_module)


def _make_api(ble_pin):
    return MeshCoreAPI(
        hass=MagicMock(),
        connection_type=CONNECTION_TYPE_BLE,
        ble_address="AA:BB:CC:DD:EE:FF",
        ble_pin=ble_pin,
    )


# ─── PIN normalization ─────────────────────────────────────────────────
def test_pin_value_preserved():
    assert _make_api("123456").ble_pin == "123456"


def test_pin_whitespace_trimmed():
    assert _make_api("  123456  ").ble_pin == "123456"


def test_blank_pin_becomes_none():
    # Empty string is what the config flow stores when no PIN is entered;
    # it must not trigger a pairing attempt with an empty passkey.
    assert _make_api("").ble_pin is None
    assert _make_api("   ").ble_pin is None


def test_missing_pin_defaults_none():
    assert _make_api(None).ble_pin is None


# ─── connect() forwards the PIN ────────────────────────────────────────
@pytest.mark.asyncio
async def test_connect_forwards_pin_to_create_ble():
    api = _make_api("123456")

    appstart_evt = MagicMock()
    appstart_evt.type = "ok"
    _module.EventType.ERROR = "error"  # so the != ERROR check passes

    mesh_core = MagicMock()
    mesh_core.commands.send_appstart = AsyncMock(return_value=appstart_evt)
    mesh_core.commands.set_time = AsyncMock()

    _module.MeshCore.create_ble = AsyncMock(return_value=mesh_core)

    # Skip the 1s connection-stability sleep.
    with patch.object(_module.asyncio, "sleep", AsyncMock()):
        ok = await api.connect()

    assert ok is True
    _, kwargs = _module.MeshCore.create_ble.call_args
    assert kwargs.get("pin") == "123456"


@pytest.mark.asyncio
async def test_connect_forwards_none_pin_when_unset():
    api = _make_api("")  # normalizes to None

    appstart_evt = MagicMock()
    appstart_evt.type = "ok"
    _module.EventType.ERROR = "error"

    mesh_core = MagicMock()
    mesh_core.commands.send_appstart = AsyncMock(return_value=appstart_evt)
    mesh_core.commands.set_time = AsyncMock()
    _module.MeshCore.create_ble = AsyncMock(return_value=mesh_core)

    with patch.object(_module.asyncio, "sleep", AsyncMock()):
        ok = await api.connect()

    assert ok is True
    _, kwargs = _module.MeshCore.create_ble.call_args
    assert kwargs.get("pin") is None


# ─── Pairing agent graceful fallback ───────────────────────────────────
@pytest.mark.asyncio
async def test_register_pairing_agent_returns_none_without_dbus():
    # dbus_fast is not installed in the test venv, so registration must fail
    # softly and return None rather than raising.
    result = await _agent_module.register_pairing_agent("123456")
    assert result is None
