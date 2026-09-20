"""Legacy node-schedule timeline, pinned on a fake clock.

Drives the real per-node update paths over a scripted radio and freezes every
number a sensor or a later tick reads back: consecutive failure counters,
next-due timestamps, reliability stats, the login trigger and its cooldown,
the path reset and the auto-disable quirks. Written against the coordinator as
it behaved before the traffic policy existed, so the refactor cannot move a
single legacy timestamp.

The unit tier cannot host this: the coordinator needs real Home Assistant,
``cachetools`` and the SDK dispatcher, all of which only exist in this tier.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Final

import pytest
from homeassistant.core import HomeAssistant
from meshcore.events import Event, EventType
from meshcore.packets import BinaryReqType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import coordinator as coordinator_module
from custom_components.meshcore import rate_limiter as rate_limiter_module
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.meshcore_api import MeshCoreAPI
from tests.support.fake_radio import FakeRadio

START: Final = 1_700_000_000
REPEATER_PREFIX: Final = "aabbccddeeff"
CLIENT_PREFIX: Final = "0011223344ff"
ACK: Final = b"\x11\x22\x33\x44"
FAST_SUGGESTED: Final = 80  # 0.1 s reply deadline: /800 as the SDK scales it

REPEATER_CONTACT: Final = {
    "public_key": REPEATER_PREFIX + "11" * 26,
    "adv_name": "Repeater",
    "out_path_len": 2,
}
CLIENT_CONTACT: Final = {
    "public_key": CLIENT_PREFIX + "22" * 26,
    "adv_name": "Client",
    "out_path_len": 1,
}
REPEATER_CONFIG: Final = {
    "name": "Repeater",
    "pubkey_prefix": REPEATER_PREFIX,
    "password": "secret",
    "update_interval": 7200,
    "telemetry_enabled": True,
    "neighbors_enabled": False,
}
CLIENT_CONFIG: Final = {
    "name": "Client",
    "pubkey_prefix": CLIENT_PREFIX,
    "update_interval": 3600,
}


class _Clock:
    """A clock the test advances by hand."""

    def __init__(self, start: float) -> None:
        """Start the clock at a fixed epoch second."""
        self.value = float(start)

    def now(self) -> float:
        """Return the current fake time."""
        return self.value

    def set(self, value: float) -> None:
        """Jump to an absolute fake time."""
        self.value = float(value)


class _FastAsyncio:
    """The coordinator's asyncio with instant sleeps and real everything else."""

    def __getattr__(self, name: str) -> Any:
        """Delegate every untouched attribute to the real module."""
        return getattr(asyncio, name)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        """Return at once so a fake clock is the only source of time."""
        return result


def _sent(suggested: int = FAST_SUGGESTED) -> Event:
    """The companion's local reply to an accepted mesh send."""
    return Event(
        EventType.MSG_SENT,
        {"type": 1, "expected_ack": ACK, "suggested_timeout": suggested},
        {"type": 1, "expected_ack": ACK.hex()},
    )


def _answers(radio: FakeRadio, answer: Event):
    """Script a send that the node answers before the local reply returns."""

    def respond() -> Any:
        """Build the awaitable FakeRadio runs for this invocation."""

        async def run() -> Event:
            """Deliver the remote frame, then report the local send."""
            await radio.emit(answer.type, answer.payload, answer.attributes)
            return _sent()

        return run()

    return respond


def _status(prefix: str, uptime: int) -> Event:
    """A binary STATUS_RESPONSE as the SDK reader dispatches one."""
    return Event(
        EventType.STATUS_RESPONSE,
        {"pubkey_pre": prefix, "uptime": uptime},
        {"pubkey_prefix": prefix, "tag": ACK.hex()},
    )


def _telemetry(prefix: str) -> Event:
    """A binary TELEMETRY_RESPONSE carrying one LPP reading."""
    return Event(
        EventType.TELEMETRY_RESPONSE,
        {"pubkey_pre": prefix, "lpp": [{"type": 2, "value": 3.9}]},
        {"pubkey_prefix": prefix, "tag": ACK.hex()},
    )


def _binary_key(radio: FakeRadio, contact: dict, kind: BinaryReqType) -> tuple:
    """The script key for the binary request the gate sends."""
    return radio.key("send_binary_req", contact, kind, timeout=0, min_timeout=0.0)


def _drain_budget(mesh: SimpleNamespace) -> None:
    """Empty the shared mesh budget the way a busy hour would."""
    while mesh.coordinator._rate_limiter.try_consume(1):
        pass


@pytest.fixture
async def mesh(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SimpleNamespace]:
    """A real coordinator over a scripted radio, on a clock the test owns."""
    clock = _Clock(START)
    monkeypatch.setattr(coordinator_module, "time", SimpleNamespace(time=clock.now))
    monkeypatch.setattr(
        coordinator_module, "random", SimpleNamespace(uniform=lambda _low, _high: 0.0)
    )
    monkeypatch.setattr(coordinator_module, "asyncio", _FastAsyncio())
    monkeypatch.setattr(
        rate_limiter_module,
        "time",
        SimpleNamespace(time=clock.now, monotonic=clock.now),
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="timeline",
        data={
            "connection_type": "tcp",
            "tcp_host": "fixture.invalid",
            "name": "Hub",
            "pubkey": "cc" * 32,
            "repeater_subscriptions": [dict(REPEATER_CONFIG)],
            "tracked_clients": [dict(CLIENT_CONFIG)],
        },
    )
    entry.add_to_hass(hass)

    radio = FakeRadio()
    await radio.start()
    radio.contacts = {
        REPEATER_CONTACT["public_key"]: dict(REPEATER_CONTACT),
        CLIENT_CONTACT["public_key"]: dict(CLIENT_CONTACT),
    }
    api = MeshCoreAPI(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
    api.session._mesh_core = radio
    api.session._connected = True

    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    coordinator.data = {"contacts": []}
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    try:
        yield SimpleNamespace(
            clock=clock, radio=radio, api=api, coordinator=coordinator, entry=entry
        )
    finally:
        await coordinator.async_shutdown()
        await radio.close()


async def test_repeater_ladder_pins_failures_backoff_login_and_path_reset(
    mesh: SimpleNamespace,
) -> None:
    """Every legacy next-due, counter and recovery step of a failing repeater."""
    coordinator, radio, clock = mesh.coordinator, mesh.radio, mesh.clock
    key = _binary_key(radio, REPEATER_CONTACT, BinaryReqType.STATUS)
    config = dict(REPEATER_CONFIG)

    clock.set(START)
    radio.script[key] = _answers(radio, _status(REPEATER_PREFIX, 42))
    await coordinator._update_repeater(config)
    assert coordinator._repeater_consecutive_failures[REPEATER_PREFIX] == 0
    assert coordinator._next_repeater_update_times[REPEATER_PREFIX] == START + 7200
    assert coordinator._last_successful_request[REPEATER_PREFIX] == START
    assert coordinator._reliability_stats == {f"{REPEATER_PREFIX}_request_successes": 1}

    # base_interval = max(1, 7200 // 62) = 116; delay = min(116 * 2**failures, 7200)
    radio.script[key] = _sent()
    for step, (now, failures, delay) in enumerate(
        [(START + 7200, 1, 232), (START + 7432, 2, 464), (START + 7896, 3, 928)], start=1
    ):
        clock.set(now)
        await coordinator._update_repeater(config)
        assert coordinator._repeater_consecutive_failures[REPEATER_PREFIX] == failures
        assert coordinator._next_repeater_update_times[REPEATER_PREFIX] == now + delay
        assert coordinator._reliability_stats[f"{REPEATER_PREFIX}_request_failures"] == step

    # Third failure with an established path resets it; the path reset is local.
    assert radio.key("reset_path", REPEATER_CONTACT) in radio.calls

    clock.set(START + 8824)
    radio.script[key] = _answers(radio, _status(REPEATER_PREFIX, 0))
    await coordinator._update_repeater(config)
    assert coordinator._repeater_consecutive_failures[REPEATER_PREFIX] == 4
    assert coordinator._next_repeater_update_times[REPEATER_PREFIX] == START + 8824 + 1856

    clock.set(START + 10680)
    _drain_budget(mesh)
    await coordinator._update_repeater(config)
    assert coordinator._repeater_consecutive_failures[REPEATER_PREFIX] == 5
    assert coordinator._next_repeater_update_times[REPEATER_PREFIX] == START + 10680 + 3712
    assert coordinator._reliability_stats[f"{REPEATER_PREFIX}_request_failures"] == 5

    # Five failures trigger a login; a fresh status failure still counts from the
    # stale local failure_count (5), not from the reset counter.
    clock.set(START + 14392)
    radio.script[radio.key("_send_login_raw", REPEATER_CONTACT, "secret")] = _answers(
        radio, Event(EventType.LOGIN_SUCCESS, {"is_admin": True}, {"pubkey_prefix": REPEATER_PREFIX})
    )
    radio.script[key] = _sent()
    await coordinator._update_repeater(config)
    assert coordinator._repeater_login_times[REPEATER_PREFIX] == START + 14392
    assert coordinator._repeater_consecutive_failures[REPEATER_PREFIX] == 6
    assert coordinator._next_repeater_update_times[REPEATER_PREFIX] == START + 14392 + 7200
    assert coordinator._reliability_stats[f"{REPEATER_PREFIX}_request_successes"] == 2

    # The 3600 s cooldown suppresses the next login attempt even at six failures.
    clock.set(START + 14392 + 3599)
    await coordinator._update_repeater(config)
    assert radio.calls.count(radio.key("_send_login_raw", REPEATER_CONTACT, "secret")) == 1


async def test_client_telemetry_ladder_pins_its_own_counters(
    mesh: SimpleNamespace,
) -> None:
    """Telemetry keeps deadlines and failures separate from the status loop."""
    coordinator, radio, clock = mesh.coordinator, mesh.radio, mesh.clock
    key = _binary_key(radio, CLIENT_CONTACT, BinaryReqType.TELEMETRY)
    config = dict(CLIENT_CONFIG)

    clock.set(START)
    radio.script[key] = _answers(radio, _telemetry(CLIENT_PREFIX))
    await coordinator._update_node_telemetry(dict(CLIENT_CONTACT), config)
    assert coordinator._telemetry_consecutive_failures[CLIENT_PREFIX] == 0
    assert coordinator._next_telemetry_update_times[CLIENT_PREFIX] == START + 3600
    assert coordinator._next_repeater_update_times == {}

    # base_interval = max(1, 3600 // 62) = 58
    radio.script[key] = _sent()
    for now, failures, delay in [
        (START + 3600, 1, 116),
        (START + 3716, 2, 232),
        (START + 3948, 3, 464),
    ]:
        clock.set(now)
        await coordinator._update_node_telemetry(dict(CLIENT_CONTACT), config)
        assert coordinator._telemetry_consecutive_failures[CLIENT_PREFIX] == failures
        assert coordinator._next_telemetry_update_times[CLIENT_PREFIX] == now + delay

    assert radio.key("reset_path", CLIENT_CONTACT) in radio.calls

    clock.set(START + 4412)
    _drain_budget(mesh)
    await coordinator._update_node_telemetry(dict(CLIENT_CONTACT), config)
    assert coordinator._telemetry_consecutive_failures[CLIENT_PREFIX] == 4
    assert coordinator._next_telemetry_update_times[CLIENT_PREFIX] == START + 4412 + 928
    assert coordinator._reliability_stats[f"{CLIENT_PREFIX}_request_failures"] == 4


async def test_tick_auto_disables_only_repeaters_and_only_the_status_loop(
    mesh: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy quirks: clients never auto-disable, telemetry ignores the set."""
    coordinator, radio, clock = mesh.coordinator, mesh.radio, mesh.clock
    started: list[str] = []

    async def record_repeater(config: dict) -> None:
        """Stand in for the status task so the loop decision is what is tested."""
        started.append(f"status:{config['name']}")

    async def record_telemetry(_contact: dict, config: dict) -> None:
        """Stand in for the telemetry task."""
        started.append(f"telemetry:{config['name']}")

    monkeypatch.setattr(coordinator, "_update_repeater", record_repeater)
    monkeypatch.setattr(coordinator, "_update_node_telemetry", record_telemetry)
    radio.script[("get_bat",)] = Event(EventType.BATTERY, {"level": 90})
    coordinator._manual_mode_initialized = True
    coordinator._device_info_initialized = True
    coordinator._initial_drain_done = True

    clock.set(START + 121 * 3600)
    coordinator._last_msg_activity = clock.now()
    await coordinator._async_update_data()
    await asyncio.sleep(0)

    assert coordinator._auto_disabled_devices == {REPEATER_PREFIX}
    assert started == [f"telemetry:{REPEATER_CONFIG['name']}", f"telemetry:{CLIENT_CONFIG['name']}"]
