"""Radio-session ownership: replay across reconnects, bounded close, rollback.

FakeRadio drives the real ``meshcore.events`` dispatcher, which only exists in
this tier (the unit tier MagicMock-stubs the ``meshcore`` package), so the
session tests live here rather than in ``tests/``.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.meshcore import radio as radio_module
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.meshcore_api import MeshCoreAPI
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

CONTRACTS: Final = Path(__file__).with_name("contracts")
NOW: Final = 1700000000
ENTRY_DATA: Final = {
    "connection_type": "tcp",
    "tcp_host": "fixture.invalid",
    "name": "Hub",
    "pubkey": "cc" * 32,
    "contact_discovery_mode": "full",
}


@pytest.fixture(autouse=True)
def fast_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the connect settle delay and freeze the time-sync argument."""
    monkeypatch.setattr(radio_module, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(radio_module, "time", SimpleNamespace(time=lambda: NOW))


def _arm(radio: FakeRadio) -> FakeRadio:
    """Script the handshake the session runs on every connect."""
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    radio.script[("set_time", NOW)] = Event(EventType.OK, {})
    return radio


async def _fresh_radio() -> FakeRadio:
    """Return a started, armed radio."""
    radio = FakeRadio()
    await radio.start()
    return _arm(radio)


def _bus(hass: HomeAssistant) -> dict[str, list[dict]]:
    """Capture the two connection events this track owns."""
    captured: dict[str, list[dict]] = {
        f"{DOMAIN}_connected": [],
        f"{DOMAIN}_disconnected": [],
    }

    @callback
    def receive(event: Any) -> None:
        captured[event.event_type].append(dict(event.data))

    for name in captured:
        hass.bus.async_listen(name, receive)
    return captured


async def _wait_connected(session: RadioSession, timeout: float = 5.0) -> None:
    """Poll until the session reports the link back up."""
    deadline = time.monotonic() + timeout
    while not session.connected:
        assert time.monotonic() < deadline, "session never reconnected"
        await asyncio.sleep(0.01)


async def test_subscriptions_survive_a_reconnect(hass: HomeAssistant) -> None:
    """C-01: a registered handler runs again on the dispatcher built by recovery."""
    first = await _fresh_radio()
    second = await _fresh_radio()
    seen: list[Event] = []

    with (
        patch.object(radio_module, "RECONNECT_BACKOFF", (0.01,)),
        patch.object(radio_module.MeshCore, "create_tcp", side_effect=[first, second]),
    ):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        session.subscribe(EventType.BATTERY, seen.append)

        await first.emit(EventType.BATTERY, {"level": 1})
        assert [event.payload for event in seen] == [{"level": 1}]

        await first.drop_link()
        await _wait_connected(session)

        await second.emit(EventType.BATTERY, {"level": 2})
        assert [event.payload for event in seen] == [{"level": 1}, {"level": 2}]

        await session.close()

    assert first.transport_closed and second.transport_closed
    first.assert_no_leaked_tasks()
    second.assert_no_leaked_tasks()


async def test_unsubscribe_removes_the_registration_for_good(hass: HomeAssistant) -> None:
    """An unsubscribed handler is not replayed onto the next dispatcher."""
    first = await _fresh_radio()
    second = await _fresh_radio()
    seen: list[Event] = []

    with (
        patch.object(radio_module, "RECONNECT_BACKOFF", (0.01,)),
        patch.object(radio_module.MeshCore, "create_tcp", side_effect=[first, second]),
    ):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        unsubscribe = session.subscribe(EventType.BATTERY, seen.append)
        unsubscribe()

        await first.drop_link()
        await _wait_connected(session)
        await second.emit(EventType.BATTERY, {"level": 2})
        assert seen == []

        await session.close()


async def test_attribute_filters_and_wildcards_reach_the_sdk(hass: HomeAssistant) -> None:
    """The session passes SDK filters and the event_type=None wildcard through."""
    radio = await _fresh_radio()
    filtered: list[Event] = []
    everything: list[Event] = []

    with patch.object(radio_module.MeshCore, "create_tcp", return_value=radio):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        session.subscribe(
            EventType.STATUS_RESPONSE, filtered.append, attribute_filters={"pubkey_prefix": "aa"}
        )
        session.subscribe(None, everything.append)

        await radio.emit(EventType.STATUS_RESPONSE, {"n": 1}, {"pubkey_prefix": "bb"})
        await radio.emit(EventType.STATUS_RESPONSE, {"n": 2}, {"pubkey_prefix": "aa"})

        assert [event.payload for event in filtered] == [{"n": 2}]
        assert len(everything) == 2

        await session.close()


async def test_paused_forwarding_silences_handlers(hass: HomeAssistant) -> None:
    """Unload pauses dispatch to entities; resume restores it without resubscribing."""
    radio = await _fresh_radio()
    seen: list[Event] = []

    with patch.object(radio_module.MeshCore, "create_tcp", return_value=radio):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        session.subscribe(EventType.BATTERY, seen.append)

        session.pause_forwarding()
        await radio.emit(EventType.BATTERY, {"level": 1})
        assert seen == []

        session.resume_forwarding()
        await radio.emit(EventType.BATTERY, {"level": 2})
        assert [event.payload for event in seen] == [{"level": 2}]

        await session.close()


async def test_connection_events_fire_once_per_edge(hass: HomeAssistant) -> None:
    """The frozen connected/disconnected payloads are emitted once per edge."""
    radio = await _fresh_radio()
    captured = _bus(hass)
    expected = json.loads((CONTRACTS / "events.json").read_text())

    with (
        patch.object(radio_module, "RECONNECT_BACKOFF", (600.0,)),
        patch.object(radio_module.MeshCore, "create_tcp", return_value=radio),
    ):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        # Two link-loss reports in one batch: only the first crosses the edge.
        radio.connected = False
        await radio.dispatcher.dispatch(Event(EventType.DISCONNECTED, {}))
        await radio.dispatcher.dispatch(Event(EventType.DISCONNECTED, {}))
        await radio.dispatcher.queue.join()
        await session.close()
        await session.close()

    await hass.async_block_till_done()
    assert captured[f"{DOMAIN}_connected"] == expected["meshcore_connected"]
    assert captured[f"{DOMAIN}_disconnected"] == expected["meshcore_disconnected"]


async def test_failed_validation_leaves_no_open_handle(hass: HomeAssistant) -> None:
    """H-01: a rejected appstart closes the transport the factory just opened."""
    radio = await _fresh_radio()
    radio.script[("send_appstart",)] = Event(EventType.ERROR, {"reason": "bad state"})

    with patch.object(radio_module.MeshCore, "create_tcp", return_value=radio):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start() is False

    assert radio.transport_closed
    assert session.mesh_core is None
    assert session.connected is False
    radio.assert_no_leaked_tasks()


async def test_close_is_bounded_with_queued_events(hass: HomeAssistant) -> None:
    """C-02: queued events cannot hold the close past its deadline."""
    radio = await _fresh_radio()

    with patch.object(radio_module.MeshCore, "create_tcp", return_value=radio):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        for level in range(10):
            await radio.dispatcher.dispatch(Event(EventType.BATTERY, {"level": level}))

        started = time.monotonic()
        await session.close(deadline=0.2)
        elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert radio.transport_closed
    radio.assert_no_leaked_tasks()


@pytest.fixture
async def runtime(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """Entry, API and coordinator wired to a hardware-free radio."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="session", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    radio = await _fresh_radio()
    api = MeshCoreAPI(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    hass.data["meshcore_static_path_registered"] = True
    try:
        yield SimpleNamespace(entry=entry, radio=radio, api=api, coordinator=coordinator)
    finally:
        await coordinator.async_shutdown()
        await api.disconnect()
        await radio.close()


def _setup_patches(runtime: SimpleNamespace, hass: HomeAssistant) -> list:
    """Patch stack that keeps entry setup on the fake radio and off storage."""
    coordinator = runtime.coordinator
    return [
        patch("custom_components.meshcore.MeshCoreAPI", return_value=runtime.api),
        patch("custom_components.meshcore.radio.MeshCore.create_tcp", return_value=runtime.radio),
        patch(
            "custom_components.meshcore.MeshCoreDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch.object(coordinator._store, "async_load", return_value={}),
        patch.object(coordinator, "fetch_all_channel_info", return_value=None),
        patch.object(coordinator, "async_load_neighbor_data", return_value=None),
        patch.object(coordinator, "async_reconcile_discovered_for_mode", return_value=None),
        patch(
            "custom_components.meshcore.MeshCoreMqttUploader",
            side_effect=RuntimeError("disabled"),
        ),
        patch(
            "custom_components.meshcore.MeshCoreMapUploader",
            side_effect=RuntimeError("disabled"),
        ),
    ]


async def _setup(runtime: SimpleNamespace, hass: HomeAssistant, **forward: Any) -> bool:
    """Run the real entry setup against the fake radio."""
    with patch.object(hass.config_entries, "async_forward_entry_setups", **forward):
        for context in (stack := _setup_patches(runtime, hass)):
            context.start()
        try:
            return await async_setup_entry(hass, runtime.entry)
        finally:
            for context in reversed(stack):
                context.stop()


async def test_setup_failure_rolls_the_runtime_back(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """C-11: a failure after coordinator creation leaves no residue and can retry."""
    with pytest.raises(RuntimeError, match="platform boom"):
        await _setup(runtime, hass, side_effect=RuntimeError("platform boom"))

    assert runtime.entry.entry_id not in hass.data.get(DOMAIN, {})
    assert runtime.api.connected is False
    assert runtime.radio.transport_closed

    runtime.radio = await _fresh_radio()
    assert await _setup(runtime, hass, return_value=None)
    assert hass.data[DOMAIN][runtime.entry.entry_id] is runtime.coordinator
    assert runtime.api.connected is True


async def test_refused_platform_unload_keeps_the_entry_working(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """A platform that refuses unload leaves the runtime live and forwarding."""
    assert await _setup(runtime, hass, return_value=None)
    seen: list[Event] = []
    runtime.api.session.subscribe(EventType.BATTERY, seen.append)

    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert await async_unload_entry(hass, runtime.entry) is False

    assert hass.data[DOMAIN][runtime.entry.entry_id] is runtime.coordinator
    assert runtime.api.connected is True
    await runtime.radio.emit(EventType.BATTERY, {"level": 1})
    assert [event.payload for event in seen] == [{"level": 1}]


async def test_unload_releases_the_runtime_without_leaking_tasks(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """A clean unload drops hass.data, closes the radio and cancels node tasks."""
    assert await _setup(runtime, hass, return_value=None)

    never = asyncio.get_running_loop().create_future()
    node_task = asyncio.create_task(asyncio.wait_for(never, None))
    runtime.coordinator._active_repeater_tasks["aabbccddeeff"] = node_task

    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await async_unload_entry(hass, runtime.entry) is True

    assert runtime.entry.entry_id not in hass.data.get(DOMAIN, {})
    assert runtime.api.connected is False
    assert runtime.radio.transport_closed
    assert node_task.cancelled()
    runtime.radio.assert_no_leaked_tasks()
