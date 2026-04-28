"""Tests for structured query services (get_contacts, get_channels, trace).

These tests exercise the service handlers registered by async_setup_services
in services.py. To avoid pulling the full Home Assistant package graph, we
load services.py directly via importlib (same pattern as
test_services_parsing.py) and then drive the registered handlers by
capturing them from a stubbed hass.services.async_register call.

The trace service was ported from the sidebar-panel's ``ws_trace`` — these
tests match that validated behavior:

  * get_contacts uses ``coordinator.get_all_contacts()``
  * get_channels reads ``coordinator._channel_info`` directly
  * trace enforces ``added_to_node``, uses a pre-registered
    PATH_RESPONSE listener + ``commands.send(b"\\x34\\x00"+pubkey)`` for
    flood contacts, and issues ``send_trace(0, tag, 0, bytes)`` with a
    round-trip 1-byte-hash path.
"""
import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── Module loading ────────────────────────────────────────────────────
# Patch meshcore.events.EventType with the handful of members services.py
# references. conftest.py already inserts a MagicMock for meshcore.events,
# but the module-level `from meshcore.events import EventType` grabs
# whatever attribute is on that mock at import time.
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

# The const module is MagicMock-mocked in conftest, so when services.py does
# `from .const import ATTR_ENTRY_ID` etc., it captures MagicMock attributes
# rather than strings. Dict lookups inside the service handlers use those
# MagicMocks as keys — so we must use the exact same MagicMocks as our test
# data keys.
DOMAIN = _module.DOMAIN
ATTR_ENTRY_ID = _module.ATTR_ENTRY_ID
ATTR_PUBKEY_PREFIX = _module.ATTR_PUBKEY_PREFIX


# ─── Shared fixtures ───────────────────────────────────────────────────
class _Event:
    """Mimic meshcore.events.Event just enough for the services to unpack."""

    def __init__(self, type_, payload, attributes=None):
        self.type = type_
        self.payload = payload
        self.attributes = attributes or {}


def _build_coordinator(
    *,
    contacts_dict=None,          # dict[pubkey→contact] for both get_all_contacts and by-prefix lookup
    contacts_list=None,          # explicit list (overrides contacts_dict if provided)
    max_channels=0,
    channel_info=None,           # dict keyed by channel_idx → {channel_name, channel_secret?}
    connected=True,
    trace_send_event=None,       # commands.send_trace return value
    trace_event=None,            # dispatcher.wait_for_event(TRACE_DATA) return
    path_send_event=None,        # commands.send return value for PATH_REQ
    path_send_exc=None,          # commands.send side_effect exception
    path_response_event=None,    # dispatcher.wait_for_event(PATH_RESPONSE) return
    self_info=None,              # api.self_info dict (for trace effective_timeout calc)
):
    """Build a MagicMock coordinator matching the new ws_trace-aligned service logic."""
    coord = MagicMock()
    coord.api = MagicMock()
    coord.api.connected = connected
    coord.api.self_info = (
        self_info if self_info is not None else {"suggested_timeout": 1000}
    )
    coord.max_channels = max_channels
    coord._channel_info = channel_info or {}

    # get_all_contacts returns a list; source is contacts_list if given, else
    # the values of contacts_dict.
    if contacts_list is not None:
        all_list = list(contacts_list)
    else:
        all_list = list((contacts_dict or {}).values())
    coord.get_all_contacts = MagicMock(return_value=all_list)

    # get_contact_by_prefix: matches by pubkey prefix, mirroring the
    # real coordinator helper that searches added + discovered.
    def _by_prefix(prefix):
        for c in all_list:
            pk = c.get("public_key") or ""
            if pk.startswith(prefix):
                return c
        return None
    coord.get_contact_by_prefix = MagicMock(side_effect=_by_prefix)

    mesh_core = MagicMock()
    mesh_core.contacts = contacts_dict or {}

    # Legacy _resolve_contact fallbacks (not normally hit when
    # coordinator.get_contact_by_prefix resolves first).
    def _by_sdk_prefix(prefix):
        return _by_prefix(prefix)
    def _by_name(name):
        for c in all_list:
            if c.get("adv_name") == name:
                return c
        return None
    mesh_core.get_contact_by_key_prefix = MagicMock(side_effect=_by_sdk_prefix)
    mesh_core.get_contact_by_name = MagicMock(side_effect=_by_name)

    mesh_core.commands = MagicMock()
    mesh_core.commands.send_trace = AsyncMock(return_value=trace_send_event)

    # commands.send is used for PATH_REQ (flood contacts).
    if path_send_exc is not None:
        mesh_core.commands.send = AsyncMock(side_effect=path_send_exc)
    else:
        mesh_core.commands.send = AsyncMock(return_value=path_send_event)

    # dispatcher.wait_for_event dispatches by event type:
    #   PATH_RESPONSE → path_response_event (via pre-registered task)
    #   TRACE_DATA    → trace_event
    async def _wait_for_event(event_type, attribute_filters=None, timeout=None):
        if event_type == _ET.PATH_RESPONSE:
            return path_response_event
        if event_type == _ET.TRACE_DATA:
            return trace_event
        return None

    mesh_core.dispatcher = MagicMock()
    mesh_core.dispatcher.wait_for_event = AsyncMock(side_effect=_wait_for_event)

    coord.api.mesh_core = mesh_core

    coord._discovered_contacts = {}
    return coord


async def _setup_and_get_handlers(coordinator):
    """Run async_setup_services with a stubbed hass and capture handlers.

    Services are keyed by their handler function's __name__ because the const
    module is MagicMock-mocked in conftest — so SERVICE_GET_CONTACTS etc. are
    MagicMock attributes at registration time, not strings.
    """
    registered = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": coordinator}}

    def _register(domain, service_const, handler, **kwargs):
        name = getattr(handler, "__name__", repr(handler))
        registered[name] = (handler, kwargs)

    hass.services.async_register = _register
    hass.services.has_service = MagicMock(return_value=False)

    await async_setup_services(hass)
    aliases = {
        "get_contacts": "async_get_contacts_service",
        "get_channels": "async_get_channels_service",
        "trace": "async_trace_service",
    }
    for short, fn in aliases.items():
        if fn in registered:
            registered[short] = registered[fn]
    return hass, registered


def _call(data):
    """Build a minimal ServiceCall-like object."""
    call = MagicMock()
    call.data = data
    return call


# ─── get_contacts ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_contacts_returns_structured_list():
    """get_contacts returns the coordinator's merged added+discovered list."""
    pk_a = "abcdef1234567890" + "0" * 48
    pk_b = "fedcba0987654321" + "0" * 48
    contacts = {
        pk_a: {
            "adv_name": "node-a",
            "public_key": pk_a,
            "type": 1,
            "out_path_len": 2,
            "out_path": "aabb",
            "added_to_node": True,
            "pubkey_prefix": pk_a[:12],
        },
        pk_b: {
            "adv_name": "node-b",
            "public_key": pk_b,
            "type": 2,
            "out_path_len": -1,
            "added_to_node": False,
            "pubkey_prefix": pk_b[:12],
        },
    }
    coord = _build_coordinator(contacts_dict=contacts)
    _, regs = await _setup_and_get_handlers(coord)
    handler, kwargs = regs["get_contacts"]
    assert "supports_response" in kwargs

    response = await handler(_call({}))
    assert "contacts" in response
    assert len(response["contacts"]) == 2

    names = {c["adv_name"] for c in response["contacts"]}
    assert names == {"node-a", "node-b"}

    # _ensure_contact_compat should have filled out_path_hash_mode on both,
    # and pubkey_prefix is preserved from what the coordinator supplied.
    by_name = {c["adv_name"]: c for c in response["contacts"]}
    assert by_name["node-a"]["out_path_hash_mode"] == 0   # out_path_len=2
    assert by_name["node-b"]["out_path_hash_mode"] == -1  # flood
    assert by_name["node-a"]["pubkey_prefix"] == pk_a[:12]
    assert by_name["node-b"]["pubkey_prefix"] == pk_b[:12]
    # added_to_node survives the roundtrip.
    assert by_name["node-a"]["added_to_node"] is True
    assert by_name["node-b"]["added_to_node"] is False


@pytest.mark.asyncio
async def test_get_contacts_backfills_pubkey_prefix_if_missing():
    """If a coordinator record is missing pubkey_prefix, service derives it from public_key."""
    pk = "deadbeef" + "c0" * 28  # 32 bytes
    contacts = {
        pk: {
            "adv_name": "bare",
            "public_key": pk,
            "out_path_len": 1,
            "out_path": "aa",
            # no pubkey_prefix, no added_to_node
        }
    }
    coord = _build_coordinator(contacts_dict=contacts)
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["get_contacts"]

    response = await handler(_call({}))
    assert len(response["contacts"]) == 1
    assert response["contacts"][0]["pubkey_prefix"] == pk[:12]


@pytest.mark.asyncio
async def test_get_contacts_no_coordinator():
    """No coordinator registered for the entry_id → structured error."""
    registered = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    def _register(domain, service_const, handler, **kwargs):
        registered[getattr(handler, "__name__", repr(handler))] = (handler, kwargs)

    hass.services.async_register = _register
    hass.services.has_service = MagicMock(return_value=False)
    await async_setup_services(hass)
    handler, _ = registered["async_get_contacts_service"]

    response = await handler(_call({}))
    assert response == {"contacts": [], "error": "no_coordinator"}


@pytest.mark.asyncio
async def test_get_contacts_coordinator_raises_returns_structured_error():
    """If coordinator.get_all_contacts raises, service returns coordinator_error."""
    coord = _build_coordinator(contacts_dict={})
    coord.get_all_contacts = MagicMock(side_effect=RuntimeError("boom"))
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["get_contacts"]

    response = await handler(_call({}))
    assert response == {"contacts": [], "error": "coordinator_error"}


# ─── get_channels ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_channels_returns_structured_list_without_secret():
    """Each channel entry has {channel_idx, channel_name, shared_secret_present}."""
    channel_info = {
        0: {"channel_name": "Public", "channel_secret": b"\x00" * 16},
        1: {"channel_name": "Private", "channel_secret": b"\x11" * 16},
        2: {"channel_name": "NoSecret"},  # name but no secret
    }
    coord = _build_coordinator(max_channels=3, channel_info=channel_info)
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["get_channels"]

    response = await handler(_call({}))
    assert "channels" in response
    assert len(response["channels"]) == 3

    by_idx = {c["channel_idx"]: c for c in response["channels"]}
    assert by_idx[0]["channel_name"] == "Public"
    assert by_idx[0]["shared_secret_present"] is True
    assert "channel_secret" not in by_idx[0]  # never leak the raw secret

    assert by_idx[1]["channel_name"] == "Private"
    assert by_idx[1]["shared_secret_present"] is True

    assert by_idx[2]["channel_name"] == "NoSecret"
    assert by_idx[2]["shared_secret_present"] is False


@pytest.mark.asyncio
async def test_get_channels_skips_unused_and_empty_slots():
    """Empty dicts, None slots, empty names, and ``(unused)`` names are filtered."""
    channel_info = {
        0: {"channel_name": "Real"},
        1: {},                               # empty dict → skipped
        2: None,                             # None → skipped
        3: {"channel_name": ""},             # empty name → skipped
        4: {"channel_name": "(unused)"},     # sentinel → skipped
        5: {"channel_name": "AlsoReal"},
    }
    coord = _build_coordinator(max_channels=6, channel_info=channel_info)
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["get_channels"]

    response = await handler(_call({}))
    returned_names = {c["channel_name"] for c in response["channels"]}
    assert returned_names == {"Real", "AlsoReal"}
    returned_idxs = {c["channel_idx"] for c in response["channels"]}
    assert returned_idxs == {0, 5}


@pytest.mark.asyncio
async def test_get_channels_max_channels_zero_returns_empty():
    """max_channels=0 yields an empty channel list without error."""
    coord = _build_coordinator(max_channels=0, channel_info={0: {"channel_name": "X"}})
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["get_channels"]

    response = await handler(_call({}))
    assert response == {"channels": []}


# ─── trace ─────────────────────────────────────────────────────────────
# pubkey format note: public_key[:2] = first 1 byte = target hash.
# With out_path="aabb" and out_path_hash_mode=0 (1-byte width, 2 hex chars):
#   outbound_hops = ["aa", "bb"]
#   target_hash    = "ab"      (first byte of "abcdef...")
#   return_hops    = ["bb", "aa"]
#   full_path_hex  = "aa" + "bb" + "ab" + "bb" + "aa" = "aabbabbbaa"
#   trace bytes    = bytes.fromhex("aabbabbbaa") (5 bytes)


@pytest.mark.asyncio
async def test_trace_happy_path_returns_structured_response(monkeypatch):
    """Non-flood contact: send_trace is issued with round-trip 1-byte-hash path."""
    # Fix the tag so we can assert the call args exactly.
    monkeypatch.setattr(_module, "random", MagicMock(randint=lambda lo, hi: 42))

    pubkey = "abcdef" + "12" * 29  # 32 bytes; first byte = "ab"
    contacts = {
        pubkey: {
            "adv_name": "target",
            "public_key": pubkey,
            "out_path_len": 2,
            "out_path": "aabb",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    send_event = _Event(_ET.MSG_SENT, {"tag": 42, "suggested_timeout": 2000})
    trace_event = _Event(
        _ET.TRACE_DATA,
        {
            "tag": 42,
            "path_len": 2,
            "path": [
                {"hash": "aa", "snr": -5.5},
                {"hash": "bb", "snr": -4.0},
                {"snr": -3.0},
            ],
        },
    )
    coord = _build_coordinator(
        contacts_dict=contacts,
        trace_send_event=send_event,
        trace_event=trace_event,
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, kwargs = regs["trace"]
    assert "supports_response" in kwargs

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef", "timeout": 5}))
    assert response["trace"] is not None
    t = response["trace"]
    assert t["hops"] == 2
    assert len(t["path"]) == 3
    assert t["tag"] == 42
    assert t["final_snr"] == -3.0
    assert isinstance(t["round_trip_ms"], int)

    # send_trace(0, tag, flags=0, bytes.fromhex("aabbabbbaa"))
    sent_call = coord.api.mesh_core.commands.send_trace.call_args
    assert sent_call.args == (0, 42, 0, bytes.fromhex("aabbabbbaa"))

    # No path discovery on a non-flood contact.
    coord.api.mesh_core.commands.send.assert_not_called()


@pytest.mark.asyncio
async def test_trace_flood_contact_runs_path_discovery_then_traces(monkeypatch):
    """Flood contact: send(PATH_REQ) → PATH_RESPONSE → send_trace along discovered path."""
    monkeypatch.setattr(_module, "random", MagicMock(randint=lambda lo, hi: 99))

    pubkey = "abcdef" + "34" * 29
    contacts = {
        pubkey: {
            "adv_name": "flood",
            "public_key": pubkey,
            "out_path_len": -1,
            "out_path": "",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    # PATH_REQ accepted (MSG_SENT).
    path_send = _Event(_ET.MSG_SENT, {"suggested_timeout": 2000})
    # Path discovery resolves to a 2-hop path (out_path_hash_len=1 → mode 0).
    path_response = _Event(
        _ET.PATH_RESPONSE,
        {"out_path": "aabb", "out_path_len": 2, "out_path_hash_len": 1},
    )
    send_event = _Event(_ET.MSG_SENT, {"tag": 99, "suggested_timeout": 2000})
    trace_event = _Event(
        _ET.TRACE_DATA,
        {
            "tag": 99,
            "path_len": 2,
            "path": [
                {"hash": "aa", "snr": -5.0},
                {"hash": "bb", "snr": -4.0},
                {"snr": -3.5},
            ],
        },
    )
    coord = _build_coordinator(
        contacts_dict=contacts,
        path_send_event=path_send,
        path_response_event=path_response,
        trace_send_event=send_event,
        trace_event=trace_event,
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response["trace"] is not None
    assert response["trace"]["hops"] == 2
    assert response["trace"]["tag"] == 99
    assert response["trace"]["final_snr"] == -3.5

    # PATH_REQ was sent as b"\x34\x00" + pubkey bytes, awaiting MSG_SENT/ERROR.
    path_call = coord.api.mesh_core.commands.send.call_args
    assert path_call.args[0] == b"\x34\x00" + bytes.fromhex(pubkey)
    assert path_call.args[1] == [_ET.MSG_SENT, _ET.ERROR]

    # send_trace used the discovered path, truncated to 1-byte hashes.
    sent_call = coord.api.mesh_core.commands.send_trace.call_args
    assert sent_call.args == (0, 99, 0, bytes.fromhex("aabbabbbaa"))


@pytest.mark.asyncio
async def test_trace_path_discovery_timeout_returns_structured_error():
    """PATH_REQ acked (MSG_SENT) but no PATH_RESPONSE arrives → path_discovery_timeout."""
    pubkey = "abcdef" + "35" * 29
    contacts = {
        pubkey: {
            "adv_name": "flood",
            "public_key": pubkey,
            "out_path_len": -1,
            "out_path": "",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    # Firmware accepted the request but never emits PATH_RESPONSE.
    path_send = _Event(_ET.MSG_SENT, {"suggested_timeout": 2000})
    coord = _build_coordinator(
        contacts_dict=contacts,
        path_send_event=path_send,
        path_response_event=None,
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response == {"trace": None, "error": "path_discovery_timeout"}
    coord.api.mesh_core.commands.send_trace.assert_not_called()


@pytest.mark.asyncio
async def test_trace_path_discovery_rejected_surfaces_reason():
    """Firmware ERROR on PATH_REQ → path_discovery_rejected with reason."""
    pubkey = "abcdef" + "42" * 29
    contacts = {
        pubkey: {
            "adv_name": "flood",
            "public_key": pubkey,
            "out_path_len": -1,
            "out_path": "",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    err_event = _Event(
        _ET.ERROR,
        {"code_string": "ERR_CODE_NOT_FOUND", "error_code": 1},
    )
    coord = _build_coordinator(
        contacts_dict=contacts,
        path_send_event=err_event,
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response["trace"] is None
    assert response["error"] == "path_discovery_rejected"
    assert response["reason"] == "ERR_CODE_NOT_FOUND"
    coord.api.mesh_core.commands.send_trace.assert_not_called()


@pytest.mark.asyncio
async def test_trace_path_discovery_send_returns_none_is_failed():
    """commands.send returning None (no firmware ack) → path_discovery_failed."""
    pubkey = "abcdef" + "43" * 29
    contacts = {
        pubkey: {
            "adv_name": "flood",
            "public_key": pubkey,
            "out_path_len": -1,
            "out_path": "",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    coord = _build_coordinator(
        contacts_dict=contacts,
        path_send_event=None,  # commands.send returns None
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response["trace"] is None
    assert response["error"] == "path_discovery_failed"
    coord.api.mesh_core.commands.send_trace.assert_not_called()


@pytest.mark.asyncio
async def test_trace_path_discovery_send_raises_is_failed():
    """commands.send raising → path_discovery_failed."""
    pubkey = "abcdef" + "37" * 29
    contacts = {
        pubkey: {
            "adv_name": "flood",
            "public_key": pubkey,
            "out_path_len": -1,
            "out_path": "",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    coord = _build_coordinator(
        contacts_dict=contacts,
        path_send_exc=RuntimeError("boom"),
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response == {"trace": None, "error": "path_discovery_failed"}
    coord.api.mesh_core.commands.send_trace.assert_not_called()


@pytest.mark.asyncio
async def test_trace_path_discovery_malformed_response():
    """PATH_RESPONSE arrives but out_path_len<0 → path_discovery_failed (malformed)."""
    pubkey = "abcdef" + "44" * 29
    contacts = {
        pubkey: {
            "adv_name": "flood",
            "public_key": pubkey,
            "out_path_len": -1,
            "out_path": "",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    path_send = _Event(_ET.MSG_SENT, {"suggested_timeout": 2000})
    bad_response = _Event(
        _ET.PATH_RESPONSE,
        {"out_path": "", "out_path_len": -1},
    )
    coord = _build_coordinator(
        contacts_dict=contacts,
        path_send_event=path_send,
        path_response_event=bad_response,
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response["trace"] is None
    assert response["error"] == "path_discovery_failed"
    assert response.get("reason") == "malformed_path_response"
    coord.api.mesh_core.commands.send_trace.assert_not_called()


@pytest.mark.asyncio
async def test_trace_contact_not_on_device_short_circuits():
    """Discovered-only contact (added_to_node=False) → contact_not_on_device."""
    pubkey = "abcdef" + "55" * 29
    contacts = {
        pubkey: {
            "adv_name": "ghost",
            "public_key": pubkey,
            "out_path_len": 2,
            "out_path": "aabb",
            "added_to_node": False,
            "pubkey_prefix": pubkey[:12],
        }
    }
    coord = _build_coordinator(contacts_dict=contacts)
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response == {"trace": None, "error": "contact_not_on_device"}
    coord.api.mesh_core.commands.send.assert_not_called()
    coord.api.mesh_core.commands.send_trace.assert_not_called()


@pytest.mark.asyncio
async def test_trace_timeout_returns_structured_error(monkeypatch):
    """send_trace MSG_SENT but TRACE_DATA never arrives → timeout with round_trip_ms."""
    monkeypatch.setattr(_module, "random", MagicMock(randint=lambda lo, hi: 7))

    pubkey = "abcdef" + "56" * 29
    contacts = {
        pubkey: {
            "adv_name": "slow",
            "public_key": pubkey,
            "out_path_len": 1,
            "out_path": "cc",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    send_event = _Event(_ET.MSG_SENT, {"tag": 7, "suggested_timeout": 1000})
    coord = _build_coordinator(
        contacts_dict=contacts,
        trace_send_event=send_event,
        trace_event=None,  # dispatcher returns None → timeout
        self_info={"suggested_timeout": 100},  # keeps effective_timeout small
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef", "timeout": 2}))
    assert response["trace"] is None
    assert response["error"] == "timeout"
    assert "round_trip_ms" in response


@pytest.mark.asyncio
async def test_trace_contact_not_found_returns_structured_error():
    """No matching contact anywhere → contact_not_found."""
    coord = _build_coordinator(contacts_dict={})
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "deadbe"}))
    assert response == {"trace": None, "error": "contact_not_found"}


@pytest.mark.asyncio
async def test_trace_send_error_propagates_reason():
    """send_trace returning an ERROR event → error string from payload.reason."""
    pubkey = "abcdef" + "78" * 29
    contacts = {
        pubkey: {
            "adv_name": "broken",
            "public_key": pubkey,
            "out_path_len": 1,
            "out_path": "dd",
            "added_to_node": True,
            "pubkey_prefix": pubkey[:12],
        }
    }
    err_event = _Event(_ET.ERROR, {"reason": "invalid_path_format"})
    coord = _build_coordinator(
        contacts_dict=contacts,
        trace_send_event=err_event,
    )
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response == {"trace": None, "error": "invalid_path_format"}


@pytest.mark.asyncio
async def test_trace_not_connected_returns_structured_error():
    """api.connected=False → not_connected error."""
    coord = _build_coordinator(contacts_dict={}, connected=False)
    _, regs = await _setup_and_get_handlers(coord)
    handler, _ = regs["trace"]

    response = await handler(_call({ATTR_PUBKEY_PREFIX: "abcdef"}))
    assert response == {"trace": None, "error": "not_connected"}
