"""The traffic policy where it is visible: options, services, command routing.

Legacy leaves service calls unmetered exactly as before; governed charges them
and refuses with the deferred error. Command routing is policy-independent: a
mesh round-trip must never hold the exchange lock, or it stalls every local
command behind it.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from meshcore.events import Event, EventType
from meshcore.packets import BinaryReqType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import coordinator as coordinator_module
from custom_components.meshcore import services
from custom_components.meshcore.const import (
    ATTR_CHANNEL_IDX,
    ATTR_MESSAGE,
    ATTR_PUBKEY_PREFIX,
    CONF_TRAFFIC_POLICY,
    DOMAIN,
    SERVICE_EXECUTE_COMMAND,
    SERVICE_SEND_CHANNEL_MESSAGE,
    SERVICE_SEND_MESSAGE,
)
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.radio import RadioSession
from custom_components.meshcore.sensor import RateLimiterSensor
from custom_components.meshcore.services import async_setup_services
from custom_components.meshcore.traffic import (
    LANE_DIRECT,
    LANE_FLOOD,
    LANE_MESSAGES,
    OP_STATUS,
    PATH_HEAL_ATTEMPTS,
    POLICY_GOVERNED,
    POLICY_LEGACY,
    classify_lane,
    iso_timestamp,
)
from tests.support.fake_radio import FakeRadio

PREFIX: Final = "aabbccddeeff"
CONTACT: Final = {
    "public_key": PREFIX + "11" * 26,
    "adv_name": "Repeater",
    "out_path_len": 2,
    "out_path_hash_mode": 0,
    "added_to_node": True,
}
NOW: Final = 1_700_000_000


async def _build(hass: HomeAssistant, policy: str) -> SimpleNamespace:
    """A real coordinator and the integration's services over a scripted radio."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=f"traffic_{policy}",
        data={
            "connection_type": "tcp",
            "tcp_host": "fixture.invalid",
            "name": "Hub",
            "pubkey": "cc" * 32,
            CONF_TRAFFIC_POLICY: policy,
            "repeater_subscriptions": [
                {"name": "Repeater", "pubkey_prefix": PREFIX, "update_interval": 7200}
            ],
        },
    )
    entry.add_to_hass(hass)

    radio = FakeRadio()
    await radio.start()
    radio.contacts = {CONTACT["public_key"]: dict(CONTACT)}
    api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
    api._mesh_core = radio
    api._connected = True

    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    coordinator.data = {"contacts": [dict(CONTACT)]}
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    await async_setup_services(hass)
    return SimpleNamespace(entry=entry, radio=radio, api=api, coordinator=coordinator)


@pytest.fixture
async def legacy(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """An entry running the default policy."""
    mesh = await _build(hass, POLICY_LEGACY)
    try:
        yield mesh
    finally:
        await mesh.coordinator.async_shutdown()
        await mesh.radio.close()


@pytest.fixture
async def governed(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """An entry running the governed policy."""
    mesh = await _build(hass, POLICY_GOVERNED)
    try:
        yield mesh
    finally:
        await mesh.coordinator.async_shutdown()
        await mesh.radio.close()


def _credits(mesh: SimpleNamespace, lane: str) -> int:
    """Read one lane's remaining credits off the budget's own report."""
    attributes = mesh.coordinator._rate_limiter.attributes()
    assert attributes is not None
    return attributes[f"{lane}_credits"]


def _drain(mesh: SimpleNamespace, lane: str = LANE_DIRECT) -> None:
    """Spend every credit one lane has; legacy drains its single bucket."""
    while mesh.coordinator._rate_limiter.try_consume(lane):
        pass


async def test_options_flow_round_trips_the_policy(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """Global settings offers the policy and stores the chosen value."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="options",
        data={
            "connection_type": "tcp",
            "tcp_host": "fixture.invalid",
            "name": "Hub",
            "pubkey": "cc" * 32,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "global_settings"}
    )
    assert result["step_id"] == "global_settings"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if hasattr(marker, "default")
    }
    assert defaults[CONF_TRAFFIC_POLICY] == POLICY_LEGACY

    await hass.config_entries.options.async_configure(
        result["flow_id"], {**defaults, CONF_TRAFFIC_POLICY: POLICY_GOVERNED}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_TRAFFIC_POLICY] == POLICY_GOVERNED


async def test_legacy_never_meters_service_calls(
    hass: HomeAssistant, legacy: SimpleNamespace
) -> None:
    """An empty budget does not stop a user-driven send under legacy."""
    _drain(legacy)
    legacy.radio.script[legacy.radio.key("send_msg", CONTACT, "hi")] = Event(
        EventType.MSG_SENT, {"expected_ack": b"\x01\x02\x03\x04", "suggested_timeout": 80}
    )

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_PUBKEY_PREFIX: PREFIX, ATTR_MESSAGE: "hi"},
        blocking=True,
    )

    assert legacy.radio.key("send_msg", CONTACT, "hi") in legacy.radio.calls
    assert legacy.coordinator._rate_limiter.get_tokens() == 0


async def test_governed_charges_a_direct_message_and_defers_when_short(
    hass: HomeAssistant, governed: SimpleNamespace
) -> None:
    """The send is charged to the message lane; a dry lane refuses, never queues."""
    governed.radio.script[governed.radio.key("send_msg", CONTACT, "hi")] = Event(
        EventType.MSG_SENT, {"expected_ack": b"\x01\x02\x03\x04", "suggested_timeout": 80}
    )
    before = _credits(governed, LANE_MESSAGES)

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_PUBKEY_PREFIX: PREFIX, ATTR_MESSAGE: "hi"},
        blocking=True,
    )
    assert _credits(governed, LANE_MESSAGES) == before - 1
    assert governed.coordinator._rate_limiter.get_tokens() == 20

    _drain(governed, LANE_MESSAGES)
    with pytest.raises(HomeAssistantError) as refused:
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_PUBKEY_PREFIX: PREFIX, ATTR_MESSAGE: "hi"},
            blocking=True,
        )
    assert refused.value.translation_key == "traffic_deferred"
    assert refused.value.translation_placeholders["lane"] == LANE_MESSAGES
    assert "seconds" in refused.value.translation_placeholders


async def test_governed_charges_a_channel_message_to_the_message_lane(
    hass: HomeAssistant, governed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel message is the user's own send, so it spends their lane."""
    monkeypatch.setattr(services, "time", SimpleNamespace(time=lambda: NOW))
    key = governed.radio.key("send_chan_msg", 0, "hi", timestamp=NOW)
    governed.radio.script[key] = Event(EventType.OK, {})
    before = _credits(governed, LANE_MESSAGES)

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_CHANNEL_MESSAGE, {ATTR_CHANNEL_IDX: 0, ATTR_MESSAGE: "hi"},
        blocking=True,
    )

    assert key in governed.radio.calls
    assert _credits(governed, LANE_MESSAGES) == before - 1
    assert _credits(governed, LANE_FLOOD) == 5


async def test_mesh_round_trip_does_not_block_a_local_command(
    hass: HomeAssistant, legacy: SimpleNamespace
) -> None:
    """execute_command req_status_sync runs on the lease, not the exchange lock."""
    radio = legacy.radio
    release = asyncio.Event()

    async def slow_status() -> dict:
        """A mesh request that answers only when the test lets it."""
        await release.wait()
        return {"uptime": 42}

    radio.script[radio.key("req_status_sync", CONTACT)] = slow_status
    radio.script[("get_bat",)] = Event(EventType.BATTERY, {"level": 90})

    status = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            SERVICE_EXECUTE_COMMAND,
            {"command": f"req_status_sync {PREFIX}"},
            blocking=True,
            return_response=True,
        )
    )
    for _ in range(8):
        await asyncio.sleep(0)

    battery = await asyncio.wait_for(
        legacy.api.exchange("get_bat"), 1
    )
    assert battery.payload["level"] == 90

    release.set()
    assert await status == {"uptime": 42}


async def test_governed_state_survives_a_restart(
    hass: HomeAssistant, governed: SimpleNamespace
) -> None:
    """Node schedules are persisted under governed and read back on load."""
    coordinator = governed.coordinator
    coordinator._next_repeater_update_times[PREFIX] = 1234
    coordinator._telemetry_consecutive_failures[PREFIX] = 3
    coordinator._auto_disabled_devices.add(PREFIX)

    store = coordinator._traffic_store
    assert store is not None
    snapshot = coordinator._traffic_snapshot()
    assert snapshot[PREFIX] == {
        "next_due": {"status": 1234, "telemetry": 0},
        "failures": {"status": 0, "telemetry": 3},
        "auto_disabled": True,
    }

    restored = MagicMock(wraps=store)
    restored.async_load = _returning(snapshot)
    coordinator._traffic_store = restored
    coordinator._next_repeater_update_times.clear()
    coordinator._telemetry_consecutive_failures.clear()
    coordinator._auto_disabled_devices.clear()

    await coordinator.async_load_traffic_state()
    assert coordinator._next_repeater_update_times[PREFIX] == 1234
    assert coordinator._telemetry_consecutive_failures[PREFIX] == 3
    assert PREFIX in coordinator._auto_disabled_devices


async def test_legacy_persists_nothing(hass: HomeAssistant, legacy: SimpleNamespace) -> None:
    """Legacy keeps no store at all, as it always has."""
    assert legacy.coordinator._traffic_store is None
    legacy.coordinator._save_traffic_state()
    await legacy.coordinator.async_load_traffic_state()


def _returning(value: Any):
    """Build an awaitable stand-in returning a fixed value."""

    async def load() -> Any:
        """Return the stored snapshot."""
        return value

    return load


UNROUTED_PREFIX: Final = "0011223344ff"
UNROUTED_CONTACT: Final = {
    "public_key": UNROUTED_PREFIX + "22" * 26,
    "adv_name": "Flooder",
    "out_path_len": -1,
    "added_to_node": True,
}
ACK: Final = b"\x11\x22\x33\x44"


def _status_answer(radio: FakeRadio):
    """Script a status request the node answers before the local reply lands."""

    def respond() -> Any:
        """Build the awaitable FakeRadio runs for this invocation."""

        async def run() -> Event:
            """Deliver the remote status frame, then report the local send."""
            await radio.emit(
                EventType.STATUS_RESPONSE,
                {"pubkey_pre": PREFIX, "uptime": 42},
                {"pubkey_prefix": PREFIX, "tag": ACK.hex()},
            )
            return Event(
                EventType.MSG_SENT,
                {"type": 1, "expected_ack": ACK, "suggested_timeout": 80},
                {"type": 1, "expected_ack": ACK.hex()},
            )

        return run()

    return respond


async def test_an_empty_flood_lane_defers_only_the_unrouted_node(
    hass: HomeAssistant,
    governed: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flood exhaustion holds back an unrouted poll; routed polls and messages run."""
    monkeypatch.setattr(
        coordinator_module, "random", SimpleNamespace(uniform=lambda _low, _high: 0.0)
    )
    coordinator, radio = governed.coordinator, governed.radio
    radio.contacts[UNROUTED_CONTACT["public_key"]] = dict(UNROUTED_CONTACT)
    radio.script[
        radio.key(
            "send_binary_req", CONTACT, BinaryReqType.STATUS, timeout=0, min_timeout=0.0
        )
    ] = _status_answer(radio)
    radio.script[radio.key("send_msg", CONTACT, "hi")] = Event(
        EventType.MSG_SENT, {"expected_ack": ACK, "suggested_timeout": 80}
    )

    _drain(governed, LANE_FLOOD)
    await coordinator._update_repeater(
        {"name": "Flooder", "pubkey_prefix": UNROUTED_PREFIX, "update_interval": 7200}
    )

    unrouted_request = radio.key(
        "send_binary_req", UNROUTED_CONTACT, BinaryReqType.STATUS, timeout=0, min_timeout=0.0
    )
    assert unrouted_request not in radio.calls
    assert coordinator._repeater_consecutive_failures.get(UNROUTED_PREFIX, 0) == 0
    due = coordinator._next_repeater_update_times[UNROUTED_PREFIX]
    assert 170 <= due - coordinator._current_time() <= 180
    deferred = coordinator.deferred_nodes()
    assert deferred == [{"name": "Flooder", "lane": LANE_FLOOD, "until": iso_timestamp(due)}]
    assert coordinator.traffic_attributes()["deferred_nodes"] == deferred
    assert (
        f"Deferring status for Flooder (flood lane empty, next at {iso_timestamp(due)})"
        in caplog.text
    )

    await coordinator._update_repeater(
        {"name": "Repeater", "pubkey_prefix": PREFIX, "update_interval": 7200}
    )
    assert coordinator._repeater_consecutive_failures[PREFIX] == 0
    assert _credits(governed, LANE_DIRECT) == 19

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_PUBKEY_PREFIX: PREFIX, ATTR_MESSAGE: "hi"},
        blocking=True,
    )
    assert radio.key("send_msg", CONTACT, "hi") in radio.calls
    assert _credits(governed, LANE_MESSAGES) == 9


async def test_the_rate_limiter_sensor_publishes_the_lane_rates(
    governed: SimpleNamespace, legacy: SimpleNamespace
) -> None:
    """Governed exposes every lane on the existing sensor; legacy adds nothing."""
    assert RateLimiterSensor(legacy.coordinator).extra_state_attributes is None

    sensor = RateLimiterSensor(governed.coordinator)
    assert sensor.native_value == 20
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["policy"] == POLICY_GOVERNED
    assert attributes["flood_capacity"] == 5
    assert attributes["flood_refill_per_hour"] == 20
    assert attributes["direct_capacity"] == 20
    assert attributes["direct_refill_per_hour"] == 120
    assert attributes["messages_capacity"] == 10
    assert attributes["messages_refill_per_hour"] == 60
    assert attributes["deferred_nodes"] == []
    assert attributes["direct_next_eligible"] is None

    _drain(governed, LANE_FLOOD)
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["flood_credits"] == 0
    assert attributes["flood_next_eligible"].endswith("+00:00")
    assert set(RateLimiterSensor._unrecorded_attributes) == {
        "deferred_nodes",
        "flood_credits",
        "direct_credits",
        "messages_credits",
        "flood_next_eligible",
        "direct_next_eligible",
        "messages_next_eligible",
    }


# -- Route healing -----------------------------------------------------------

REPEATER_CONFIG: Final = {"name": "Repeater", "pubkey_prefix": PREFIX, "update_interval": 7200}


def _path(out_path_len: int, out_path: str = "") -> Event:
    """A PATH_RESPONSE as the session hands it back from a path discovery."""
    payload = {
        "pubkey_pre": PREFIX,
        "out_path_len": out_path_len,
        "out_path_hash_len": 1,
        "out_path": out_path,
    }
    return Event(EventType.PATH_RESPONSE, payload)


def _arm_reset(mesh: SimpleNamespace) -> dict:
    """Script the path reset and pin the clock; return the live contact."""
    contact = mesh.api.contact_by_prefix(PREFIX)
    mesh.radio.script[mesh.radio.key("reset_path", contact)] = Event(EventType.OK, {})
    mesh.coordinator._current_time = lambda: NOW
    return contact


async def _fail(mesh: SimpleNamespace, failures: int, contact: dict) -> None:
    """Book one failed routed status poll against the repeater."""
    await mesh.coordinator._record_node_failure(
        PREFIX, failures, 7200, "repeater",
        node_config=REPEATER_CONFIG, contact=contact, has_path=True, lane=LANE_DIRECT,
    )


async def test_governed_routed_failure_retries_on_the_legacy_spacing(
    governed: SimpleNamespace,
) -> None:
    contact = _arm_reset(governed)

    await _fail(governed, 1, contact)

    assert governed.coordinator._next_repeater_update_times[PREFIX] == NOW + 232


async def test_governed_reset_heals_the_route_for_one_flood_credit(
    governed: SimpleNamespace,
) -> None:
    contact = _arm_reset(governed)
    discover = AsyncMock(side_effect=[(None, None), (None, _path(1, "f5"))])
    governed.api.path_discovery = discover
    flood_before = _credits(governed, LANE_FLOOD)

    await _fail(governed, 3, contact)

    coordinator = governed.coordinator
    assert discover.await_count == 2
    assert _credits(governed, LANE_FLOOD) == flood_before - 1
    assert coordinator._repeater_consecutive_failures[PREFIX] == 0
    assert coordinator._next_repeater_update_times[PREFIX] == NOW
    assert PREFIX not in coordinator._path_reset_pending
    assert (contact["out_path_len"], contact["out_path"]) == (1, "f5")
    assert classify_lane(OP_STATUS, contact) == LANE_DIRECT


async def test_governed_reset_without_a_route_pays_the_flood_backoff(
    governed: SimpleNamespace,
) -> None:
    contact = _arm_reset(governed)
    discover = AsyncMock(return_value=(None, None))
    governed.api.path_discovery = discover

    await _fail(governed, 3, contact)

    coordinator = governed.coordinator
    due = coordinator._next_repeater_update_times[PREFIX]
    assert discover.await_count == PATH_HEAL_ATTEMPTS
    assert coordinator._repeater_consecutive_failures[PREFIX] == 3
    assert PREFIX in coordinator._path_reset_pending
    assert 0.9 * 7200 * 8 <= due - NOW <= 1.1 * 7200 * 8


async def test_governed_heal_needs_a_flood_credit(governed: SimpleNamespace) -> None:
    contact = _arm_reset(governed)
    discover = AsyncMock()
    governed.api.path_discovery = discover
    _drain(governed, LANE_FLOOD)

    await _fail(governed, 3, contact)

    discover.assert_not_awaited()
    assert governed.coordinator._repeater_consecutive_failures[PREFIX] == 3


async def test_legacy_reset_never_sends_a_path_discovery(legacy: SimpleNamespace) -> None:
    contact = _arm_reset(legacy)
    discover = AsyncMock()
    legacy.api.path_discovery = discover

    await _fail(legacy, 3, contact)

    discover.assert_not_awaited()
    assert legacy.coordinator._next_repeater_update_times[PREFIX] == NOW + 928
    assert PREFIX in legacy.coordinator._path_reset_pending
