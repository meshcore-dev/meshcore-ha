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
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
from custom_components.meshcore.meshcore_api import MeshCoreAPI
from custom_components.meshcore.services import async_setup_services
from custom_components.meshcore.traffic import POLICY_GOVERNED, POLICY_LEGACY
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
    api = MeshCoreAPI(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
    api.session._mesh_core = radio
    api.session._connected = True

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


def _drain(mesh: SimpleNamespace) -> None:
    """Spend every credit the budget has, interactive reserve included."""
    while mesh.coordinator._rate_limiter.try_consume(1, interactive=True):
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
    assert entry.data[CONF_TRAFFIC_POLICY] == POLICY_GOVERNED


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
    """The send is charged; once credit runs out the call is refused, not queued."""
    governed.radio.script[governed.radio.key("send_msg", CONTACT, "hi")] = Event(
        EventType.MSG_SENT, {"expected_ack": b"\x01\x02\x03\x04", "suggested_timeout": 80}
    )
    before = governed.coordinator._rate_limiter.get_tokens()

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_PUBKEY_PREFIX: PREFIX, ATTR_MESSAGE: "hi"},
        blocking=True,
    )
    assert governed.coordinator._rate_limiter.get_tokens() == before - 1

    _drain(governed)
    with pytest.raises(HomeAssistantError) as refused:
        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_MESSAGE, {ATTR_PUBKEY_PREFIX: PREFIX, ATTR_MESSAGE: "hi"},
            blocking=True,
        )
    assert refused.value.translation_key == "traffic_deferred"
    assert "seconds" in refused.value.translation_placeholders


async def test_governed_charges_a_channel_message_the_flood_cost(
    hass: HomeAssistant, governed: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel message reaches the whole mesh and is priced accordingly."""
    monkeypatch.setattr(services, "time", SimpleNamespace(time=lambda: NOW))
    key = governed.radio.key("send_chan_msg", 0, "hi", timestamp=NOW)
    governed.radio.script[key] = Event(EventType.OK, {})
    before = governed.coordinator._rate_limiter.get_tokens()

    await hass.services.async_call(
        DOMAIN, SERVICE_SEND_CHANNEL_MESSAGE, {ATTR_CHANNEL_IDX: 0, ATTR_MESSAGE: "hi"},
        blocking=True,
    )

    assert key in governed.radio.calls
    assert governed.coordinator._rate_limiter.get_tokens() == before - 8


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
        legacy.api.session.exchange(radio.commands.get_bat), 1
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
