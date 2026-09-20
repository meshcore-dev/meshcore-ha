"""Exercise FakeRadio only where meshcore.events is the real installed module."""

import asyncio

import pytest
from meshcore.events import Event, EventDispatcher, EventType

from tests.support.fake_radio import FakeRadio


async def test_script_and_filtered_dispatch() -> None:
    """Script matching, SDK filters, and unsubscribe all have real effects."""
    radio = FakeRadio()
    await radio.start()
    try:
        assert type(radio.dispatcher) is EventDispatcher
        seen = []
        subscription = radio.subscribe(EventType.BATTERY, seen.append, {"slot": 1})
        response = Event(EventType.BATTERY, {"level": 50}, {"slot": 1})
        radio.script[("get_bat",)] = response
        assert await radio.commands.get_bat() is response
        assert [event.payload for event in seen] == [{"level": 50}]
        await radio.emit(EventType.BATTERY, {"level": 10}, {"slot": 2})
        subscription.unsubscribe()
        await radio.commands.get_bat()
        assert len(seen) == 1
        radio.script[("get_channel", 1)] = Event(EventType.CHANNEL_INFO, {"channel_idx": 1})
        assert (await radio.commands.get_channel(1)).payload == {"channel_idx": 1}
        with pytest.raises(KeyError):
            await radio.commands.get_channel(2)
    finally:
        await radio.close()
    radio.assert_no_leaked_tasks()


async def test_reconnect_orphans_subscriptions_and_cleans_generations() -> None:
    """Reconnect replaces dispatchers; only freshly registered handlers run."""
    radio = FakeRadio()
    await radio.start()
    seen = []
    radio.subscribe(None, seen.append)
    previous = radio.dispatcher
    try:
        await radio.drop_link()
        assert seen[-1].type == EventType.DISCONNECTED
        with pytest.raises(ConnectionError):
            await radio.commands.get_bat()
        await radio.restore_link()
        assert radio.dispatcher is not previous
        await radio.emit(EventType.BATTERY, {"level": 20})
        assert len(seen) == 1
        radio.subscribe(EventType.BATTERY, seen.append)
        await radio.emit(EventType.BATTERY, {"level": 30})
        assert seen[-1].payload == {"level": 30}
        with pytest.raises(AssertionError):
            radio.assert_no_leaked_tasks()
    finally:
        await radio.close()
    radio.assert_no_leaked_tasks()


async def test_async_callbacks_and_scripted_failures() -> None:
    """The harness settles actual SDK background callbacks and command failures."""
    radio = FakeRadio()
    await radio.start()
    seen = []

    async def receive(event: Event) -> None:
        """Yield once to ensure asynchronous callback work is awaited."""
        await asyncio.sleep(0)
        seen.append(event.payload)

    radio.subscribe(EventType.BATTERY, receive)
    try:
        await radio.emit(EventType.BATTERY, {"level": 42})
        assert seen == [{"level": 42}]
        radio.script[("get_bat",)] = TimeoutError("scripted")
        with pytest.raises(TimeoutError, match="scripted"):
            await radio.commands.get_bat()
    finally:
        await radio.close()
    radio.assert_no_leaked_tasks()
