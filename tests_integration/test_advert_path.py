"""Integration-tier tests for advert path tracking.

The coordinator fetches the path each advert took (GET_ADVERT_PATH) when the
companion pushes an ADVERTISEMENT event, stores it keyed by pubkey prefix, and
the contact binary sensor merges it into its attributes. These tests drive the
real coordinator methods and the real sensor class; only the MeshCore API is
mocked.
"""
import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from meshcore.commands import CommandHandler
from meshcore.events import Event, EventDispatcher, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.binary_sensor import (
    MeshCoreContactDiagnosticBinarySensor,
)
from custom_components.meshcore.const import ADVERT_PATH_CACHE_MAX_SIZE, DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator

FULL_PK = "a1" * 32
PREFIX = FULL_PK[:12]
ADVERT_PATH_PAYLOAD = {"timestamp": 1754600000, "path_hash_mode": 0, "path_len": 2, "path": "b2c3"}


def _coordinator(hass: HomeAssistant) -> MeshCoreDataUpdateCoordinator:
    config_entry = MockConfigEntry(domain=DOMAIN, data={})
    config_entry.add_to_hass(hass)
    coordinator = MeshCoreDataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        "test",
        timedelta(seconds=60),
        MagicMock(),
        config_entry,
    )
    return coordinator


def _mock_advert_path(coordinator, result: Event) -> AsyncMock:
    mock = AsyncMock(return_value=result)
    coordinator.api.mesh_core.commands.get_advert_path = mock
    return mock


async def test_fetch_stores_path_and_marks_dirty(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    _mock_advert_path(coordinator, Event(EventType.ADVERT_PATH, ADVERT_PATH_PAYLOAD))

    await coordinator._fetch_advert_path(FULL_PK)

    expected = {"adv_path": "b2c3", "adv_path_len": 2, "adv_path_time": 1754600000}
    assert coordinator.get_advert_path_data(FULL_PK) == expected
    assert coordinator.get_advert_path_data(PREFIX) == expected  # prefix lookup too
    assert coordinator.is_contact_dirty(FULL_PK)


async def test_unknown_contact_returns_empty(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    assert coordinator.get_advert_path_data("") == {}
    assert coordinator.get_advert_path_data("ff" * 32) == {}


async def test_disabled_after_three_consecutive_failures(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    mock = _mock_advert_path(coordinator, Event(EventType.ERROR, {}))

    for _ in range(4):
        await coordinator._fetch_advert_path(FULL_PK)

    # Fourth call short-circuits: the latch opened after the third failure.
    assert mock.await_count == 3
    assert coordinator.get_advert_path_data(FULL_PK) == {}


async def test_success_resets_failure_count(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    err = Event(EventType.ERROR, {})
    ok = Event(EventType.ADVERT_PATH, ADVERT_PATH_PAYLOAD)
    mock = _mock_advert_path(coordinator, None)
    mock.side_effect = [err, err, ok, err, err]

    for _ in range(5):
        await coordinator._fetch_advert_path(FULL_PK)

    # The success in the middle resets the consecutive-failure count, so the
    # latch never opens and all five calls reach the device.
    assert mock.await_count == 5


async def test_lookups_queued_before_latch_do_not_reach_device(hass: HomeAssistant):
    coordinator = _coordinator(hass)

    async def slow_error(_key):
        await asyncio.sleep(0)  # suspend so the other lookups queue behind this one
        return Event(EventType.ERROR, {})

    mock = _mock_advert_path(coordinator, None)
    mock.side_effect = slow_error

    await asyncio.gather(*(coordinator._fetch_advert_path(f"{i:02x}" * 32) for i in range(5)))

    assert mock.await_count == 3


class _FakeCompanion:
    """Answers GET_ADVERT_PATH like the firmware: the reply doesn't name the contact."""

    def __init__(self, dispatcher: EventDispatcher, paths: dict[str, str]) -> None:
        self._dispatcher = dispatcher
        self._paths = paths
        self.replies: list[asyncio.Task] = []

    async def send(self, data: bytes) -> None:
        path = self._paths[data[2:].hex()]
        self.replies.append(asyncio.create_task(self._reply(path)))

    async def _reply(self, path: str) -> None:
        await asyncio.sleep(0.01)  # device latency: gives a second request time to go out
        payload = {"timestamp": 1754600000, "path_len": len(path) // 2, "path": path}
        await self._dispatcher.dispatch(Event(EventType.ADVERT_PATH, payload))


async def test_overlapping_lookups_each_get_their_own_path(hass: HomeAssistant):
    """Uses the real meshcore command handler, whose waiters take any ADVERT_PATH."""
    pk_a, pk_b = "a1" * 32, "b2" * 32
    dispatcher = EventDispatcher()
    await dispatcher.start()
    companion = _FakeCompanion(dispatcher, {pk_a: "c3d4", pk_b: "e5f6"})
    commands = CommandHandler()
    commands.set_dispatcher(dispatcher)
    commands.set_connection(companion)
    coordinator = _coordinator(hass)
    coordinator.api.mesh_core.commands = commands

    try:
        await asyncio.gather(
            coordinator._fetch_advert_path(pk_a),
            coordinator._fetch_advert_path(pk_b),
        )
    finally:
        await asyncio.gather(*companion.replies)
        await dispatcher.stop()

    assert coordinator.get_advert_path_data(pk_a)["adv_path"] == "c3d4"
    assert coordinator.get_advert_path_data(pk_b)["adv_path"] == "e5f6"


async def test_cache_is_bounded(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    _mock_advert_path(coordinator, Event(EventType.ADVERT_PATH, ADVERT_PATH_PAYLOAD))
    keys = [f"{i:012x}" + "00" * 26 for i in range(ADVERT_PATH_CACHE_MAX_SIZE + 5)]

    for key in keys:
        await coordinator._fetch_advert_path(key)

    assert len(coordinator._advert_paths) == ADVERT_PATH_CACHE_MAX_SIZE
    assert coordinator.get_advert_path_data(keys[0]) == {}  # oldest evicted
    assert coordinator.get_advert_path_data(keys[-1])["adv_path"] == "b2c3"


async def test_advertisement_event_triggers_fetch(hass: HomeAssistant):
    """End to end: the subscribed handler fetches and stores on ADVERTISEMENT."""
    coordinator = _coordinator(hass)
    _mock_advert_path(coordinator, Event(EventType.ADVERT_PATH, ADVERT_PATH_PAYLOAD))

    coordinator._setup_advert_path_listener()
    subscribe_mock = coordinator.api.mesh_core.dispatcher.subscribe
    event_type, handler = subscribe_mock.call_args[0]
    assert event_type == EventType.ADVERTISEMENT

    handler(Event(EventType.ADVERTISEMENT, {"public_key": FULL_PK}))
    await hass.async_block_till_done()

    assert coordinator.get_advert_path_data(FULL_PK)["adv_path"] == "b2c3"


async def test_contact_sensor_merges_advert_path_attributes(hass: HomeAssistant):
    contact = {"adv_name": "peer", "public_key": FULL_PK, "last_advert": 1754600000}
    coordinator = MagicMock()
    coordinator.get_contact_by_prefix.return_value = contact
    coordinator.get_advert_path_data.return_value = {
        "adv_path": "b2c3",
        "adv_path_len": 2,
        "adv_path_time": 1754600000,
    }
    sensor = MeshCoreContactDiagnosticBinarySensor(coordinator, "peer", FULL_PK, "uid")

    attributes = sensor.extra_state_attributes
    assert attributes["adv_path"] == "b2c3"
    assert attributes["adv_path_len"] == 2
    assert attributes["adv_path_time"] == 1754600000
    coordinator.get_advert_path_data.assert_called_with(FULL_PK)


async def test_contact_sensor_without_advert_path_has_no_attributes(hass: HomeAssistant):
    contact = {"adv_name": "peer", "public_key": FULL_PK, "last_advert": 1754600000}
    coordinator = MagicMock()
    coordinator.get_contact_by_prefix.return_value = contact
    coordinator.get_advert_path_data.return_value = {}
    sensor = MeshCoreContactDiagnosticBinarySensor(coordinator, "peer", FULL_PK, "uid")

    assert "adv_path" not in sensor.extra_state_attributes
