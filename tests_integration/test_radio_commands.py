"""Command gate: who may speak to the radio, and which reply is whose.

FakeRadio drives the real ``meshcore.events`` dispatcher, so these tests pin
the correlation rules against the SDK's actual attribute filtering rather than
against a mock of it.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback
from meshcore.events import Event, EventType
from meshcore.packets import BinaryReqType

from custom_components.meshcore import radio as radio_module
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.radio import RadioSession, RadioUnavailable
from tests.support.contracts import with_identity
from tests.support.fake_radio import FakeRadio

CONTRACTS: Final = Path(__file__).with_name("contracts")
NOW: Final = 1700000000
PREFIX: Final = "aabbccddeeff"
CONTACT: Final = {"public_key": PREFIX + "11" * 26, "adv_name": "Repeater"}
STATUS_ACK: Final = b"\x11\x22\x33\x44"
STATUS_TAG: Final = STATUS_ACK.hex()
DM_ACK: Final = b"\x99\x88\x77\x66"
LOGIN_ACK: Final = b"\x01\x02\x03\x04"


@pytest.fixture(autouse=True)
def fast_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the connect settle delay and freeze the time-sync argument."""
    monkeypatch.setattr(radio_module, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(radio_module, "time", SimpleNamespace(time=lambda: NOW))


def _sent(ack: bytes, suggested: int = 4000) -> Event:
    """Build the companion's local reply to an accepted radio send."""
    return Event(
        EventType.MSG_SENT,
        {"type": 1, "expected_ack": ack, "suggested_timeout": suggested},
        {"type": 1, "expected_ack": ack.hex()},
    )


async def _settle(cycles: int = 8) -> None:
    """Let every ready task run without advancing the clock."""
    for _ in range(cycles):
        await asyncio.sleep(0)


@pytest.fixture
async def session(hass: HomeAssistant):
    """A started session over a scripted radio, closed after the test."""
    radio = FakeRadio()
    await radio.start()
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    radio.script[("set_time", NOW)] = Event(EventType.OK, {})
    radio.contacts[CONTACT["public_key"]] = CONTACT

    with (
        patch.object(radio_module, "RECONNECT_BACKOFF", (600.0,)),
        patch.object(radio_module.MeshCore, "create_tcp", return_value=radio),
    ):
        live = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await live.start()
        try:
            yield SimpleNamespace(session=live, radio=radio)
        finally:
            await live.close()


def _status_key(radio: FakeRadio) -> tuple:
    """The script key for the binary status request the gate sends."""
    return radio.key(
        "send_binary_req", CONTACT, BinaryReqType.STATUS, timeout=0, min_timeout=0.0
    )


def _status_response(tag: str, uptime: int = 42) -> Event:
    """A binary STATUS_RESPONSE as the SDK reader dispatches one."""
    return Event(
        EventType.STATUS_RESPONSE,
        {"pubkey_pre": PREFIX, "uptime": uptime},
        {"pubkey_prefix": PREFIX, "tag": tag},
    )


async def test_concurrent_dm_and_status_each_get_their_own_reply(session) -> None:
    """C-03: a reply tagged for another request never satisfies this one."""
    radio = session.radio
    radio.script[radio.key("send_msg", CONTACT, "hello")] = _sent(DM_ACK)
    radio.script[_status_key(radio)] = _sent(STATUS_ACK)

    status = asyncio.create_task(session.session.req_status(CONTACT))
    dm = asyncio.create_task(
        session.session.exchange(radio.commands.send_msg, CONTACT, "hello")
    )
    await _settle()
    ack = asyncio.create_task(
        session.session.wait_for(EventType.ACK, {"code": DM_ACK.hex()}, 5.0)
    )
    await _settle()

    await radio.emit(
        EventType.STATUS_RESPONSE, {"uptime": 1}, {"pubkey_prefix": PREFIX, "tag": "deadbeef"}
    )
    await radio.emit(EventType.ACK, {"code": DM_ACK.hex()}, {"code": DM_ACK.hex()})
    await radio.emit(*_status_response(STATUS_TAG).__dict__.values())

    assert (await dm).payload["expected_ack"] == DM_ACK
    assert (await ack).payload["code"] == DM_ACK.hex()
    assert (await status).payload["uptime"] == 42


async def test_two_status_requests_serialize_on_the_mesh_lease(session) -> None:
    """C-04: the firmware tracks one request, so the gate sends one at a time."""
    radio = session.radio
    radio.script[_status_key(radio)] = _sent(STATUS_ACK)

    first = asyncio.create_task(session.session.req_status(CONTACT))
    second = asyncio.create_task(session.session.req_status(CONTACT))
    await _settle()
    assert [call[0] for call in radio.calls].count("send_binary_req") == 1

    await radio.emit(*_status_response(STATUS_TAG).__dict__.values())
    assert (await first).payload["uptime"] == 42
    await _settle()
    assert [call[0] for call in radio.calls].count("send_binary_req") == 2

    await radio.emit(*_status_response(STATUS_TAG, uptime=43).__dict__.values())
    assert (await second).payload["uptime"] == 43


async def test_a_local_command_runs_during_a_mesh_wait(session) -> None:
    """The exchange lock is released at the local reply, not at the mesh reply."""
    radio = session.radio
    radio.script[_status_key(radio)] = _sent(STATUS_ACK)
    radio.script[radio.key("get_bat")] = Event(EventType.BATTERY, {"level": 77})

    status = asyncio.create_task(session.session.req_status(CONTACT))
    await _settle()

    battery = await session.session.exchange(radio.commands.get_bat)
    assert battery.payload["level"] == 77

    await radio.emit(*_status_response(STATUS_TAG).__dict__.values())
    assert (await status) is not None


async def test_login_races_success_against_failure(session) -> None:
    """H-11: a rejected password resolves now instead of timing out."""
    radio = session.radio
    radio.script[radio.key("_send_login_raw", CONTACT, "wrong")] = _sent(LOGIN_ACK)

    attempt = asyncio.create_task(session.session.login(CONTACT, "wrong"))
    await _settle()
    await radio.emit(
        EventType.LOGIN_FAILED, {"pubkey_prefix": PREFIX}, {"pubkey_prefix": PREFIX}
    )

    assert await attempt is None


@pytest.mark.parametrize("attributes", [{"pubkey_prefix": PREFIX}, {}])
async def test_login_accepts_identified_and_short_success_frames(
    session, attributes: dict[str, Any]
) -> None:
    """Older firmware answers without a prefix; legacy still treats that as a login."""
    radio = session.radio
    radio.script[radio.key("_send_login_raw", CONTACT, "secret")] = _sent(LOGIN_ACK)

    attempt = asyncio.create_task(session.session.login(CONTACT, "secret"))
    await _settle()
    await radio.emit(EventType.LOGIN_SUCCESS, {"is_admin": True}, dict(attributes))

    result = await attempt
    assert result is not None and result.type is EventType.LOGIN_SUCCESS


def _neighbour_page(radio: FakeRadio, offset: int, pages: list[dict], tag: str = "aa11bb22"):
    """Script one neighbour page that answers itself when the gate sends it."""
    queue = list(pages)

    async def respond() -> Event:
        """Deliver the next scripted page, then report the local send."""
        payload = queue.pop(0)
        await radio.emit(
            EventType.NEIGHBOURS_RESPONSE,
            {"tag": tag, "pubkey_prefix": PREFIX, **payload},
            {"tag": tag, "pubkey_prefix": PREFIX},
        )
        return _sent(bytes.fromhex(tag))

    for floor in (0.0, 25.0):
        radio.script[
            radio.key(
                "req_neighbours_async",
                CONTACT,
                count=255,
                offset=offset,
                pubkey_prefix_length=6,
                timeout=0,
                min_timeout=floor,
            )
        ] = respond


async def test_neighbour_paging_stops_when_a_page_adds_nothing(session) -> None:
    """C-06: a page that returns no rows ends the scan instead of spinning."""
    radio = session.radio
    rows = [{"pubkey": f"{index:012x}"} for index in range(5)]
    _neighbour_page(radio, 0, [{"neighbours_count": 10, "results_count": 5, "neighbours": rows}])
    _neighbour_page(radio, 5, [{"neighbours_count": 10, "results_count": 0, "neighbours": []}])

    result = await session.session.fetch_neighbours(CONTACT, pubkey_prefix_length=6)

    assert result is not None
    assert len(result["neighbours"]) == 5
    assert [call[0] for call in radio.calls].count("req_neighbours_async") == 2


async def test_neighbour_paging_restarts_once_when_the_total_changes(session) -> None:
    """A table that shrank mid-scan is re-read from the beginning exactly once."""
    radio = session.radio
    first = [{"pubkey": f"{index:012x}"} for index in range(5)]
    whole = [{"pubkey": f"{index:012x}"} for index in range(8)]
    _neighbour_page(
        radio,
        0,
        [
            {"neighbours_count": 10, "results_count": 5, "neighbours": first},
            {"neighbours_count": 8, "results_count": 8, "neighbours": whole},
        ],
    )
    _neighbour_page(radio, 5, [{"neighbours_count": 8, "results_count": 3, "neighbours": []}])

    result = await session.session.fetch_neighbours(CONTACT, pubkey_prefix_length=6)

    assert result is not None
    assert len(result["neighbours"]) == 8
    assert result["neighbours_count"] == 8
    assert [call[0] for call in radio.calls].count("req_neighbours_async") == 3


async def test_neighbour_paging_returns_partial_results_after_two_failures(session) -> None:
    """The legacy compatibility shape: two silent pages hand back what was read."""
    radio = session.radio
    rows = [{"pubkey": f"{index:012x}"} for index in range(5)]
    _neighbour_page(radio, 0, [{"neighbours_count": 10, "results_count": 5, "neighbours": rows}])
    radio.script[
        radio.key(
            "req_neighbours_async",
            CONTACT,
            count=255,
            offset=5,
            pubkey_prefix_length=6,
            timeout=0,
            min_timeout=25.0,
        )
    ] = Event(EventType.ERROR, {"reason": "busy"})

    result = await session.session.fetch_neighbours(CONTACT, pubkey_prefix_length=6)

    assert result is not None
    assert len(result["neighbours"]) == 5
    assert [call[0] for call in radio.calls].count("req_neighbours_async") == 3


async def test_transaction_keeps_other_sends_out_of_a_scoped_send(session) -> None:
    """Flood scope set, send and reset are one operation nothing may split."""
    radio = session.radio
    radio.script[radio.key("set_flood_scope", "#region")] = Event(EventType.OK, {})
    radio.script[radio.key("set_flood_scope", None)] = Event(EventType.OK, {})
    radio.script[radio.key("send_chan_msg", 0, "scoped")] = Event(EventType.OK, {})
    radio.script[radio.key("send_chan_msg", 1, "unscoped")] = Event(EventType.OK, {})

    async with session.session.transaction():
        await session.session.exchange(radio.commands.set_flood_scope, "#region")
        other = asyncio.create_task(
            session.session.exchange(radio.commands.send_chan_msg, 1, "unscoped")
        )
        await _settle()
        assert [call[0] for call in radio.calls].count("send_chan_msg") == 0

        await session.session.exchange(radio.commands.send_chan_msg, 0, "scoped")
        await session.session.exchange(radio.commands.set_flood_scope, None)

    await other
    assert [call[:2] for call in radio.calls if call[0] == "send_chan_msg"] == [
        ("send_chan_msg", 0),
        ("send_chan_msg", 1),
    ]


async def test_a_dead_link_during_a_command_crosses_the_edge_once(
    hass: HomeAssistant,
) -> None:
    """A command that hits a closed socket reports the loss, once, and refuses."""
    radio = FakeRadio()
    await radio.start()
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    radio.script[("set_time", NOW)] = Event(EventType.OK, {})
    captured: list[dict] = []
    expected = json.loads((CONTRACTS / "events.json").read_text())

    @callback
    def receive(event: Any) -> None:
        """Record the disconnect payload the integration promises consumers."""
        captured.append(dict(event.data))

    hass.bus.async_listen(f"{DOMAIN}_disconnected", receive)

    with (
        patch.object(radio_module, "RECONNECT_BACKOFF", (600.0,)),
        patch.object(radio_module.MeshCore, "create_tcp", return_value=radio),
    ):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()
        radio.connected = False

        with pytest.raises(RadioUnavailable):
            await session.exchange(radio.commands.get_bat)
        with pytest.raises(RadioUnavailable):
            await session.exchange(radio.commands.get_bat)

        assert session.connected is False
        await session.close()

    await hass.async_block_till_done()
    assert captured == with_identity(expected["meshcore_disconnected"])


async def test_a_hanging_create_gives_up_at_the_timeout(hass: HomeAssistant) -> None:
    """Live QA: a dropped SYN must not hold the recovery loop for minutes."""

    async def never(*args: Any, **kwargs: Any) -> None:
        """Stand in for a factory whose transport never completes."""
        await asyncio.Future()

    with (
        patch.object(radio_module, "CREATE_TIMEOUT", 0.05),
        patch.object(radio_module.MeshCore, "create_tcp", side_effect=never),
    ):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start() is False

    assert session.connected is False
