"""Contact bookkeeping under real Home Assistant: stores, FIFO, sync backoff.

Adverts are the hot path on a busy mesh. Each one used to rewrite the whole
discovered-contact store and reschedule the coordinator tick, the first tick
re-loaded the store over anything heard since setup, and a silent node was
asked for its entire contact table every five seconds.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry, flush_store

from custom_components.meshcore import async_setup_entry
from custom_components.meshcore import radio as radio_module
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import (
    CONTACT_SYNC_BACKOFF_MAX,
    CONTACT_SYNC_BACKOFF_MIN,
    STORE_SAVE_DELAY,
    MeshCoreDataUpdateCoordinator,
)
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

NOW: Final = 1700000000
ENTRY_ID: Final = "contacts"
STORE_KEY: Final = f"meshcore.{ENTRY_ID}.discovered_contacts"
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


def _advert(index: int) -> dict[str, Any]:
    """Build one NEW_CONTACT payload with a distinct public key."""
    public_key = f"{index:02x}" + "ab" * 31
    return {"public_key": public_key, "adv_name": f"Node {index}", "lastmod": index}


@pytest.fixture
async def runtime(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """Entry, session and coordinator wired to a hardware-free radio."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id=ENTRY_ID, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    radio.script[("set_time", NOW)] = Event(EventType.OK, {})
    radio.script[("get_bat",)] = Event(EventType.BATTERY, {"level": 90})
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


async def _setup(runtime: SimpleNamespace, hass: HomeAssistant) -> bool:
    """Run the real entry setup against the fake radio, platforms stubbed out."""
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
        patch.object(coordinator, "fetch_all_channel_info", return_value=None),
        patch.object(coordinator, "async_load_neighbor_data", return_value=None),
        patch.object(
            coordinator, "async_reconcile_discovered_for_mode", return_value=None
        ),
        patch(
            "custom_components.meshcore.MeshCoreMqttUploader",
            side_effect=RuntimeError("disabled"),
        ),
        patch(
            "custom_components.meshcore.MeshCoreMapUploader",
            side_effect=RuntimeError("disabled"),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None),
    ]
    for context in stack:
        context.start()
    try:
        return await async_setup_entry(hass, runtime.entry)
    finally:
        for context in reversed(stack):
            context.stop()


async def test_adverts_write_the_store_once(
    hass: HomeAssistant, hass_storage: dict[str, Any], runtime: SimpleNamespace
) -> None:
    """C-08: twenty adverts queue one debounced write, not twenty fsyncs."""
    assert await _setup(runtime, hass)
    store = runtime.coordinator._store
    writes: list[None] = []
    original = store._async_handle_write_data

    async def counted(*args: Any, **kwargs: Any) -> None:
        """Count every write that actually reaches the store."""
        writes.append(None)
        await original(*args, **kwargs)

    with (
        patch.object(store, "_async_handle_write_data", counted),
        patch.object(store, "async_save", side_effect=AssertionError("immediate save")),
    ):
        for index in range(20):
            await runtime.radio.emit(EventType.NEW_CONTACT, _advert(index))
        assert writes == []
        await flush_store(store)

    assert len(writes) == 1
    assert len(hass_storage[STORE_KEY]["data"]) == 20


async def test_the_queued_save_copies_the_discovered_set(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """C-08: the write takes its own copy, so a later insert cannot corrupt it."""
    assert await _setup(runtime, hass)
    coordinator = runtime.coordinator
    queued: list[tuple[Any, float]] = []

    with patch.object(
        coordinator._store,
        "async_delay_save",
        side_effect=lambda func, delay=0: queued.append((func, delay)),
    ):
        await runtime.radio.emit(EventType.NEW_CONTACT, _advert(1))

    assert [delay for _func, delay in queued] == [STORE_SAVE_DELAY]
    snapshot = queued[0][0]()
    assert snapshot == coordinator._discovered_contacts
    assert snapshot is not coordinator._discovered_contacts

    coordinator._discovered_contacts["late"] = _advert(2)
    assert "late" not in snapshot


async def test_an_advert_never_reschedules_the_tick(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """C-07: adverts publish contacts without touching the refresh timer."""
    assert await _setup(runtime, hass)
    coordinator = runtime.coordinator
    updates: list[None] = []
    coordinator.async_add_listener(lambda: updates.append(None))
    scheduled = coordinator._unsub_refresh
    assert scheduled is not None

    await runtime.radio.emit(EventType.NEW_CONTACT, _advert(3))

    assert coordinator._unsub_refresh is scheduled
    assert updates  # listeners still see the new contact list
    assert coordinator.data["contacts"][0]["public_key"] == _advert(3)["public_key"]


async def test_the_fifo_loads_once_and_keeps_live_adverts(
    hass: HomeAssistant, hass_storage: dict[str, Any], runtime: SimpleNamespace
) -> None:
    """H-29: stored contacts merge under adverts already in memory."""
    stored = {_advert(index)["public_key"]: _advert(index) for index in range(3)}
    hass_storage[STORE_KEY] = {"version": 1, "data": stored}
    coordinator = runtime.coordinator
    live = _advert(9)
    coordinator._discovered_contacts[live["public_key"]] = live

    await coordinator.async_load_discovered_contacts()

    assert list(coordinator._discovered_contacts) == [
        *stored,
        live["public_key"],
    ]

    with patch.object(
        coordinator._store, "async_load", side_effect=AssertionError("reloaded")
    ):
        await coordinator.async_load_discovered_contacts()


async def test_the_tick_never_reloads_the_fifo(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """H-29: a reconnect re-sends manual-add mode but never re-reads the store."""
    assert await _setup(runtime, hass)
    coordinator = runtime.coordinator
    await runtime.radio.emit(EventType.NEW_CONTACT, _advert(4))
    live_key = _advert(4)["public_key"]

    coordinator._on_radio_connected()
    with patch.object(
        coordinator._store, "async_load", side_effect=AssertionError("reloaded")
    ):
        await coordinator._async_update_data()

    assert coordinator._manual_mode_initialized is True
    assert live_key in coordinator._discovered_contacts


async def test_contact_sync_backs_off_while_the_node_stays_silent(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """H-28: a fetch that never lands is retried on a 5->60 s ladder."""
    assert await _setup(runtime, hass)
    coordinator = runtime.coordinator
    issued = AsyncMock(return_value=True)

    with patch.object(coordinator.api, "ensure_contacts", issued):
        await coordinator._sync_contacts(1000)
        assert coordinator._next_contact_sync == 1000 + CONTACT_SYNC_BACKOFF_MIN

        await coordinator._sync_contacts(1001)
        assert issued.await_count == 1  # inside the backoff window

        delays = []
        now = 1005
        for _ in range(6):
            await coordinator._sync_contacts(now)
            delays.append(coordinator._next_contact_sync - now)
            now = coordinator._next_contact_sync

    assert delays == [10, 20, 40, CONTACT_SYNC_BACKOFF_MAX, 60, 60]

    async def answered(follow: bool = True) -> bool:
        """Issue a fetch the node actually answers with a contact table."""
        await runtime.radio.emit(EventType.CONTACTS, {})
        return True

    with patch.object(coordinator.api, "ensure_contacts", answered):
        await coordinator._sync_contacts(now)

    assert coordinator._next_contact_sync == 0.0
    assert coordinator._contact_sync_backoff == CONTACT_SYNC_BACKOFF_MIN


async def test_an_unsynced_table_is_not_treated_as_a_failure(
    hass: HomeAssistant, runtime: SimpleNamespace
) -> None:
    """H-28: 'already in sync' issues no fetch and never arms the backoff."""
    assert await _setup(runtime, hass)
    coordinator = runtime.coordinator
    coordinator._next_contact_sync = 999.0
    coordinator._contact_sync_backoff = CONTACT_SYNC_BACKOFF_MAX

    with patch.object(coordinator.api, "ensure_contacts", AsyncMock(return_value=False)):
        await coordinator._sync_contacts(1000)

    assert coordinator._next_contact_sync == 0.0
    assert coordinator._contact_sync_backoff == CONTACT_SYNC_BACKOFF_MIN


async def test_unload_flushes_the_queued_store_write(
    hass: HomeAssistant, hass_storage: dict[str, Any], runtime: SimpleNamespace
) -> None:
    """A debounced write still lands when the entry goes away before the delay."""
    assert await _setup(runtime, hass)
    coordinator = runtime.coordinator
    await runtime.radio.emit(EventType.NEW_CONTACT, _advert(5))
    assert STORE_KEY not in hass_storage

    await coordinator.async_flush_stores()
    await asyncio.sleep(0)

    assert _advert(5)["public_key"] in hass_storage[STORE_KEY]["data"]
