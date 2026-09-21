"""What a send tells its listeners, and when.

A message is announced when the radio takes it, not when the reception count
settles four seconds later, and an incoming message is published even when its
sender is a stranger. These tests pin both, plus the flush that keeps a count
from vanishing when Home Assistant shuts down mid-collection.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final

import pytest
from homeassistant.core import HomeAssistant, callback
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.binary_sensor import (
    handle_contact_message as receive_contact_message,
)
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.events import EVENT_DELIVERY_UPDATE, EVENT_MESSAGE
from custom_components.meshcore.logbook import (
    handle_channel_message as receive_channel_message,
)
from custom_components.meshcore.logbook import handle_outgoing_message
from custom_components.meshcore.radio import RadioSession
from custom_components.meshcore.utils import create_message_correlation_key
from tests.support.fake_radio import FakeRadio

NOW: Final = 1700000000
CHANNEL_SEND: Final = {
    "message_type": "channel",
    "message": "Hello",
    "channel_idx": 1,
    "send_timestamp": NOW,
    "send_id": "12345678",
}


@pytest.fixture
async def runtime(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """Build a coordinator over a hardware-free radio with one known contact."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="delivery",
        data={
            "connection_type": "tcp",
            "tcp_host": "fixture.invalid",
            "name": "Hub",
            "pubkey": "cc" * 32,
        },
    )
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    radio.contacts = {
        "b" * 64: {"adv_name": "Client", "public_key": "b" * 64, "type": 1},
    }
    api = RadioSession(
        hass=hass, connection_type="tcp", tcp_host="fixture.invalid", entry=entry
    )
    api._mesh_core = radio
    api._connected = True
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    coordinator._channel_info = {1: {"channel_idx": 1, "channel_name": "Private"}}
    coordinator.tracked_contacts = set()
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    try:
        yield SimpleNamespace(entry=entry, radio=radio, api=api, coordinator=coordinator)
    finally:
        await coordinator.async_shutdown()
        await api.disconnect()
        await radio.close()


@pytest.fixture
def captured(hass: HomeAssistant) -> dict[str, list[dict]]:
    """Record the message and delivery events fired during a test."""
    events: dict[str, list[dict]] = {EVENT_MESSAGE: [], EVENT_DELIVERY_UPDATE: []}

    @callback
    def receive(event: Any) -> None:
        """Snapshot one event as its listeners see it."""
        events[event.event_type].append(dict(event.data))

    for name in events:
        hass.bus.async_listen(name, receive)
    return events


async def test_a_channel_send_is_logged_before_its_count(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """The logbook entry exists immediately; the count follows as updates."""
    key = create_message_correlation_key(1, NOW)
    runtime.coordinator._pending_rx_logs[key] = [{"snr": 3.5, "rssi": -90}]
    task = hass.async_create_task(handle_outgoing_message(CHANNEL_SEND, runtime.coordinator))
    await asyncio.sleep(0)

    assert len(captured[EVENT_MESSAGE]) == 1
    announced = captured[EVENT_MESSAGE][0]
    assert announced["repeater_count"] == 0
    assert announced["collecting"] is True
    assert announced["progressive"] is False
    assert captured[EVENT_DELIVERY_UPDATE] == []

    await task
    counts = [event["repeater_count"] for event in captured[EVENT_DELIVERY_UPDATE]]
    assert counts == [1, 1, 1, 1]
    assert [e["progressive"] for e in captured[EVENT_DELIVERY_UPDATE]] == [
        True,
        True,
        True,
        False,
    ]


async def test_a_cancelled_collection_publishes_what_it_has(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """An unload mid-collection flushes the count instead of losing it."""
    key = create_message_correlation_key(1, NOW)
    task = hass.async_create_task(handle_outgoing_message(CHANNEL_SEND, runtime.coordinator))
    await asyncio.sleep(0)
    runtime.coordinator._pending_rx_logs[key] = [{"snr": 3.5, "rssi": -90}]
    await asyncio.sleep(1.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    terminal = captured[EVENT_DELIVERY_UPDATE][-1]
    assert terminal["progressive"] is False
    assert terminal["repeater_count"] == 1
    assert key not in runtime.coordinator._outgoing_correlation_keys


async def test_a_direct_send_ends_with_a_delivery_update(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """The ACK outcome reaches delivery listeners, not just the logbook."""
    await handle_outgoing_message(
        {
            "message_type": "direct",
            "message": "Hello",
            "receiver": "Client",
            "contact_public_key": "b" * 64,
            "send_id": "12345678",
            "ack_received": True,
        },
        runtime.coordinator,
    )
    await hass.async_block_till_done()

    terminal = captured[EVENT_DELIVERY_UPDATE][-1]
    assert terminal["progressive"] is False
    assert terminal["ack_received"] is True
    assert terminal["send_id"] == "12345678"


async def test_an_unknown_sender_is_published_without_an_entity(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """A message from a stranger is never dropped, and names no one."""
    added: list = []
    receive_contact_message(
        Event(
            EventType.CONTACT_MSG_RECV,
            {"pubkey_prefix": "eeeeeeeeeeee", "text": "Hello", "path_len": 255},
        ),
        runtime.coordinator,
        added.append,
    )
    await hass.async_block_till_done()

    assert added == []
    assert runtime.coordinator.tracked_contacts == set()
    message = captured[EVENT_MESSAGE][0]
    assert message["sender_name"] is None
    assert message["pubkey_prefix"] == "eeeeeeeeeeee"
    assert message["message"] == "Hello"


async def test_a_known_sender_keeps_its_name(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """The stranger path does not change what a known contact looks like."""
    receive_contact_message(
        Event(
            EventType.CONTACT_MSG_RECV,
            {"pubkey_prefix": "b" * 12, "text": "Hello", "path_len": 255},
        ),
        runtime.coordinator,
        lambda entities: None,
    )
    await hass.async_block_till_done()

    assert captured[EVENT_MESSAGE][0]["sender_name"] == "Client"


async def test_an_unnamed_channel_is_shown_by_its_index(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """A blank channel name must not make a channel message look like a DM."""
    runtime.coordinator._channel_info[2] = {"channel_idx": 2, "channel_name": ""}
    await receive_channel_message(
        Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 2, "text": "Hello"}),
        runtime.coordinator,
    )
    await hass.async_block_till_done()

    message = captured[EVENT_MESSAGE][0]
    assert message["channel"] == "2"
    assert message["message_type"] == "channel"


async def test_a_colon_in_the_body_is_not_a_sender(
    hass: HomeAssistant, runtime: SimpleNamespace, captured: dict
) -> None:
    """Only a known contact name before the colon is read as the sender."""
    await receive_channel_message(
        Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "ETA 14:30"}),
        runtime.coordinator,
    )
    await receive_channel_message(
        Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "Client: Hello"}),
        runtime.coordinator,
    )
    await hass.async_block_till_done()

    unprefixed, prefixed = captured[EVENT_MESSAGE]
    assert (unprefixed["sender_name"], unprefixed["message"]) == ("Unknown", "ETA 14:30")
    assert (prefixed["sender_name"], prefixed["message"]) == ("Client", "Hello")
    assert prefixed["pubkey_prefix"] == "b" * 12
