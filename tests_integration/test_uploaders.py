"""Uploader startup, probes and fan-out under real Home Assistant.

Entry setup used to run a device query, a private-key export, a decoder
subprocess and up to four untimed broker connects before it returned; the map
uploader re-asked a firmware that had already refused the key export on every
advert; and every radio event got its own task and executor hop for MQTT even
with no broker connected.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import async_setup_entry, async_unload_entry
from custom_components.meshcore import radio as radio_module
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.map_uploader import MeshCoreMapUploader
from custom_components.meshcore.mqtt_uploader import (
    PUBLISH_QUEUE_MAX,
    MeshCoreMqttUploader,
)
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

NOW: Final = 1700000000
ENTRY_ID: Final = "uploaders"
SETUP_BUDGET_SECONDS: Final = 2.0
BROKERS: Final = {
    "1": {"enabled": True, "server": "broker.invalid", "port": 1883},
}
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
    monkeypatch.setattr(
        radio_module,
        "time",
        SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic),
    )


class _StalledClient:
    """A paho stand-in whose connect never answers, like a dead broker."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Record the calls a client normally receives, doing none of them."""
        self.connect_timeout = 5.0
        self.connected = False

    def __getattr__(self, name: str) -> Any:
        """Accept every other paho call as a no-op."""
        return lambda *args, **kwargs: None

    def connect(self, *args: Any, **kwargs: Any) -> None:
        """Block the way a connect to an unroutable broker blocks."""
        time.sleep(30)


@pytest.fixture
async def runtime(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """Entry, session and coordinator wired to a hardware-free radio."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        data=ENTRY_DATA,
        options={"mqtt_brokers": BROKERS},
    )
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    radio.script[("set_time", NOW)] = Event(EventType.OK, {})
    api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
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


async def _setup(runtime: SimpleNamespace, hass: HomeAssistant) -> float:
    """Run the real entry setup and return how long it took."""
    coordinator = runtime.coordinator
    stack = [
        patch("custom_components.meshcore.RadioSession", return_value=runtime.api),
        patch(
            "custom_components.meshcore.radio.MeshCore.create_tcp",
            return_value=runtime.radio,
        ),
        patch(
            "custom_components.meshcore.MeshCoreDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch.object(coordinator._store, "async_load", return_value={}),
        patch.object(coordinator, "fetch_all_channel_info", return_value=None),
        patch.object(coordinator, "async_load_neighbor_data", return_value=None),
        patch.object(
            coordinator, "async_reconcile_discovered_for_mode", return_value=None
        ),
        patch(
            "custom_components.meshcore.mqtt_uploader.mqtt.Client", _StalledClient
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None),
    ]
    for context in stack:
        context.start()
    started = time.monotonic()
    try:
        assert await async_setup_entry(hass, runtime.entry)
        return time.monotonic() - started
    finally:
        for context in reversed(stack):
            context.stop()


async def test_setup_completes_with_an_unreachable_broker(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """H-09: broker work belongs after platforms, not inside setup."""
    elapsed = await _setup(runtime, hass)

    assert elapsed < SETUP_BUDGET_SECONDS
    uploader = runtime.coordinator.mqtt_uploader
    assert uploader is not None
    assert [broker.server for broker in uploader.get_brokers()] == ["broker.invalid"]
    # The status sensors can enumerate the brokers, but nothing has connected.
    assert uploader.get_brokers()[0].number == 1
    assert uploader.is_broker_connected(1) is False

    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        assert await async_unload_entry(hass, runtime.entry)


def _uploader(hass: HomeAssistant, connected: bool) -> MeshCoreMqttUploader:
    """An uploader with one client, connected or not, and no real broker."""
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="queue", data=ENTRY_DATA, options={"mqtt_brokers": BROKERS}
    )
    entry.add_to_hass(hass)
    uploader = MeshCoreMqttUploader(hass, logging.getLogger(__name__), entry)
    uploader._clients = [
        {
            "broker": uploader.get_brokers()[0],
            "client": MagicMock(),
            "connected": connected,
        }
    ]
    return uploader


def test_no_connected_broker_means_no_work(hass: HomeAssistant) -> None:
    """The fan-out skips the task and the executor hop when nothing is up."""
    uploader = _uploader(hass, connected=False)

    assert uploader.queue_raw_event("EventType.RX_LOG_DATA", {"x": 1}) is False
    assert len(uploader._publish_queue) == 0


def test_the_queue_sheds_packet_logs_before_anything_else(
    hass: HomeAssistant,
) -> None:
    """A stalled broker drops the RX_LOG flood, never a message or advert."""
    uploader = _uploader(hass, connected=True)

    assert uploader.queue_raw_event("EventType.CONTACT_MSG_RECV", {"n": 0}) is True
    for index in range(PUBLISH_QUEUE_MAX * 2):
        uploader.queue_raw_event("EventType.RX_LOG_DATA", {"n": index})

    assert len(uploader._publish_queue) == PUBLISH_QUEUE_MAX
    kept = [event_type for event_type, _payload in uploader._publish_queue]
    assert kept[0] == "EventType.CONTACT_MSG_RECV"
    assert set(kept[1:]) == {"EventType.RX_LOG_DATA"}
    # The oldest packet logs went first, so the newest survive.
    newest = uploader._publish_queue[-1][1]["n"]
    assert newest == PUBLISH_QUEUE_MAX * 2 - 1


async def test_the_queue_drains_on_one_task(hass: HomeAssistant) -> None:
    """Every queued event reaches the publisher, in order, from one consumer."""
    uploader = _uploader(hass, connected=True)
    published: list[tuple[str, Any]] = []

    async def record(event_type: str, payload: Any) -> None:
        """Stand in for the executor publish hop."""
        published.append((event_type, payload))

    with patch.object(uploader, "async_publish_raw_event", record):
        task = asyncio.create_task(uploader._async_publish_loop())
        for index in range(5):
            uploader.queue_raw_event("EventType.RX_LOG_DATA", {"n": index})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [payload["n"] for _event_type, payload in published] == [0, 1, 2, 3, 4]


def _map_uploader(hass: HomeAssistant, api: Any) -> MeshCoreMapUploader:
    """A map uploader over a scripted radio session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="map",
        data=ENTRY_DATA,
        options={"map_upload_enabled": True},
    )
    entry.add_to_hass(hass)
    return MeshCoreMapUploader(hass, logging.getLogger(__name__), entry, api=api)


async def test_a_refused_key_export_is_asked_once_per_connect(
    hass: HomeAssistant,
) -> None:
    """H-10: firmware export is off by default; do not re-ask on every advert."""
    api = MagicMock()
    api.exchange = AsyncMock(
        return_value=SimpleNamespace(type=EventType.DISABLED, payload={})
    )
    uploader = _map_uploader(hass, api)

    for _ in range(5):
        assert await uploader._ensure_private_key() is False
    assert api.exchange.await_count == 1

    # The connect hook the session runs is what re-opens the question.
    uploader._on_radio_connected()
    assert await uploader._ensure_private_key() is False
    assert api.exchange.await_count == 2


async def test_two_adverts_never_export_the_key_at_once(
    hass: HomeAssistant,
) -> None:
    """H-10: concurrent adverts share one export, not two radio commands."""
    release = asyncio.Event()

    async def slow_export(*args: Any, **kwargs: Any) -> Any:
        """Hold the export open so a second caller arrives mid-flight."""
        await release.wait()
        return SimpleNamespace(
            type=EventType.PRIVATE_KEY, payload={"private_key": "ab" * 64}
        )

    api = MagicMock()
    api.exchange = AsyncMock(side_effect=slow_export)
    uploader = _map_uploader(hass, api)

    first = asyncio.create_task(uploader._ensure_private_key())
    second = asyncio.create_task(uploader._ensure_private_key())
    await asyncio.sleep(0)
    release.set()

    assert await first is True
    assert await second is True
    assert api.exchange.await_count == 1
