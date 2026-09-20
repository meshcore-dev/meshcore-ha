"""Tests for on-demand repeater firmware refreshes."""

import asyncio
import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.support.modules import load_module
from tests.support.session import StubSession


class _EventType:
    CONTACT_MSG_RECV = "contact_msg_recv"
    ERROR = "error"
    MSG_SENT = "msg_sent"


sys.modules["meshcore.events"].EventType = _EventType
# The helper reads records through the real settings codec, so load both for real.
load_module("const")
load_module("config")

_SPEC = importlib.util.spec_from_file_location(
    "custom_components.meshcore.repeater_firmware",
    "custom_components/meshcore/repeater_firmware.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

RefreshError = _MODULE.RepeaterFirmwareRefreshError
query = _MODULE.async_query_repeater_firmware
refresh = _MODULE.async_refresh_repeater_firmware


class _Session:
    """Stand in for RadioSession: one live subscription over a scripted radio."""

    def __init__(self, events):
        self.events = events
        self.connected = True
        self.commands = None
        self.contact = None
        self.listener_registered = False
        self.filters = None
        self.callback = None
        self.logins = []

    async def exchange(self, command, /, *args, deadline=None, **kwargs):
        """Resolve a command name the way RadioSession.exchange would."""
        return await getattr(self.commands, command)(*args, **kwargs)

    def contact_by_prefix(self, prefix):
        """Resolve the one contact this scripted radio knows about."""
        return self.contact

    async def login(self, contact, password):
        """Record the login the refresh performs before asking for a version."""
        self.logins.append((contact, password))
        return True

    def subscribe(self, event_type, callback, *, attribute_filters=None):
        self.listener_registered = True
        self.filters = attribute_filters
        self.callback = callback

        def unsubscribe():
            self.listener_registered = False
            self.callback = None

        return unsubscribe

    def dispatch_events(self):
        for event in self.events:
            if all(event.attributes.get(key) == value for key, value in self.filters.items()):
                self.callback(event)


def _event(prefix, text, event_type=_EventType.CONTACT_MSG_RECV):
    return SimpleNamespace(
        type=event_type,
        payload={"text": text},
        attributes={"pubkey_prefix": prefix},
    )


def _session(events, *, send_type=_EventType.MSG_SENT):
    contact = {"public_key": "aabbccddeeff" + "0" * 52}
    session = _Session(events)

    async def send_cmd(sent_contact, command):
        assert session.listener_registered
        assert sent_contact is contact
        assert command == "ver"
        session.dispatch_events()
        return SimpleNamespace(type=send_type, payload={})

    session.commands = SimpleNamespace(send_cmd=send_cmd)
    session.contact = contact
    return session


def _entry(entry_id, prefix="aabbccddeeff", version="1.0.0"):
    return SimpleNamespace(
        entry_id=entry_id,
        options={},
        data={
            "repeater_subscriptions": [
                {
                    "name": "Repeater",
                    "pubkey_prefix": prefix,
                    "firmware_version": version,
                    "password": "secret",
                }
            ]
        },
    )


def _hass_for(entry, device):
    hass = MagicMock()

    def update_entry(updated_entry, *, data):
        assert updated_entry is entry
        updated_entry.data = data

    hass.config_entries.async_update_entry.side_effect = update_entry
    registry = MagicMock()
    registry.async_get_device.return_value = device
    _MODULE.dr.async_get.return_value = registry
    return hass, registry


@pytest.mark.asyncio
async def test_query_registers_listener_first_and_ignores_other_repeater() -> None:
    session = _session(
        [
            _event("112233445566", "wrong-version"),
            _event("aabbccddeeff", "ordinary direct message"),
            _event("aabbccddeeff", " 1.14.2 (Build: 2026-09-18) "),
        ]
    )

    assert await query(session, "aabbccddeeff") == "1.14.2 (Build: 2026-09-18)"
    assert session.filters == {"pubkey_prefix": "aabbccddeeff"}


@pytest.mark.asyncio
async def test_refresh_updates_config_and_repeater_device() -> None:
    entry = _entry("hub-one")
    device = SimpleNamespace(id="repeater-device")
    hass, registry = _hass_for(entry, device)

    version = await refresh(
        hass,
        entry,
        _session([_event("aabbccddeeff", "1.14.2 (Build: 2026-09-18)")]),
        "aabbccddeeff",
    )

    assert version == "1.14.2 (Build: 2026-09-18)"
    assert entry.data["repeater_subscriptions"][0]["firmware_version"] == version
    registry.async_get_device.assert_called_once_with(
        identifiers={("meshcore", "hub-one_repeater_aabbccddeeff")}
    )
    registry.async_update_device.assert_called_once_with("repeater-device", sw_version=version)


@pytest.mark.asyncio
@pytest.mark.parametrize("send_type", [_EventType.MSG_SENT, _EventType.ERROR])
async def test_refresh_failure_preserves_known_version(send_type) -> None:
    entry = _entry("hub-one", version="1.13.0")
    hass, registry = _hass_for(entry, SimpleNamespace(id="unused"))
    events = [] if send_type == _EventType.MSG_SENT else [_event("aabbccddeeff", "ignored")]

    with pytest.raises(RefreshError):
        await refresh(
            hass,
            entry,
            _session(events, send_type=send_type),
            "aabbccddeeff",
            timeout=0.01,
        )

    assert entry.data["repeater_subscriptions"][0]["firmware_version"] == "1.13.0"
    hass.config_entries.async_update_entry.assert_not_called()
    registry.async_update_device.assert_not_called()


@pytest.mark.asyncio
async def test_error_reply_is_not_persisted() -> None:
    entry = _entry("hub-one", version="1.13.0")
    hass, registry = _hass_for(entry, SimpleNamespace(id="unused"))

    with pytest.raises(RefreshError, match="error reply"):
        await refresh(
            hass,
            entry,
            _session([_event("aabbccddeeff", "Error: not logged in")]),
            "aabbccddeeff",
        )

    assert entry.data["repeater_subscriptions"][0]["firmware_version"] == "1.13.0"
    hass.config_entries.async_update_entry.assert_not_called()
    registry.async_update_device.assert_not_called()


@pytest.mark.asyncio
async def test_missing_device_registry_entry_does_not_update_config() -> None:
    entry = _entry("hub-one", version="1.13.0")
    hass, registry = _hass_for(entry, None)

    with pytest.raises(RefreshError, match="device-registry entry"):
        await refresh(
            hass,
            entry,
            _session(
                [_event("aabbccddeeff", "1.14.2 (Build: 2026-09-18)")]
            ),
            "aabbccddeeff",
        )

    assert entry.data["repeater_subscriptions"][0]["firmware_version"] == "1.13.0"
    hass.config_entries.async_update_entry.assert_not_called()
    registry.async_update_device.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_removes_response_listener() -> None:
    listener_removed = asyncio.Event()

    send_started = asyncio.Event()

    async def send_cmd(contact, command):
        send_started.set()
        await asyncio.Future()

    session = StubSession(
        SimpleNamespace(send_cmd=send_cmd),
        connected=True,
        subscribe=lambda *a, **kw: listener_removed.set,
        contact_by_prefix=lambda prefix: {"public_key": "aabbccddeeff" + "0" * 52},
    )
    task = asyncio.create_task(query(session, "aabbccddeeff"))
    await send_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert listener_removed.is_set()


@pytest.mark.asyncio
async def test_concurrent_refresh_for_same_repeater_is_rejected() -> None:
    entry = _entry("hub-one")
    hass, _ = _hass_for(entry, SimpleNamespace(id="target-device"))
    send_started = asyncio.Event()

    async def login(contact, password):
        return True

    async def send_cmd(contact, command):
        send_started.set()
        await asyncio.Future()

    session = StubSession(
        SimpleNamespace(send_cmd=send_cmd),
        connected=True,
        subscribe=lambda *a, **kw: (lambda: None),
        login=login,
        contact_by_prefix=lambda prefix: {"public_key": "aabbccddeeff" + "0" * 52},
    )
    first = asyncio.create_task(refresh(hass, entry, session, "aabbccddeeff"))
    await send_started.wait()
    with pytest.raises(RefreshError, match="already in progress"):
        await refresh(hass, entry, session, "aabbccddeeff")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_refresh_targets_only_supplied_hub_entry() -> None:
    target = _entry("hub-one")
    other = _entry("hub-two", version="9.9.9")
    hass, registry = _hass_for(target, SimpleNamespace(id="target-device"))

    await refresh(
        hass,
        target,
        _session([_event("aabbccddeeff", "1.14.2 (Build: 2026-09-18)")]),
        "aabbccddeeff",
    )

    assert target.data["repeater_subscriptions"][0]["firmware_version"] == (
        "1.14.2 (Build: 2026-09-18)"
    )
    assert other.data["repeater_subscriptions"][0]["firmware_version"] == "9.9.9"
    registry.async_get_device.assert_called_once_with(
        identifiers={("meshcore", "hub-one_repeater_aabbccddeeff")}
    )
