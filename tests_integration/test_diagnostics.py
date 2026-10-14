"""Diagnostics download: shape, redaction and a disconnected radio."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.diagnostics import async_get_config_entry_diagnostics
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

REPEATER = "aabbccddeeff"
CLIENT = "001122334455"
SECRETS = ("repeater-pass-1", "mqtt-pass-2", "chan-secret-3", "mqtt-user-4", "owner@example.invalid")


@pytest.fixture
async def mesh(hass, enable_custom_integrations) -> AsyncIterator[SimpleNamespace]:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="diag",
        data={
            "connection_type": "tcp", "tcp_host": "fixture.invalid",
            "name": "Hub", "pubkey": "cc" * 32, "traffic_policy": "governed",
            "repeater_subscriptions": [{
                "name": "Repeater", "pubkey_prefix": REPEATER, "password": SECRETS[0],
                "update_interval": 7200, "telemetry_enabled": True,
            }],
            "tracked_clients": [
                {"name": "Client", "pubkey_prefix": CLIENT, "update_interval": 3600}
            ],
        },
        options={
            "mqtt_brokers": {"1": {
                "enabled": True, "server": "broker.invalid", "username": SECRETS[3],
                "password": SECRETS[1], "owner_email": SECRETS[4],
            }},
            "channel_secret": SECRETS[2],
        },
    )
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    contact = {"public_key": REPEATER + "11" * 26, "adv_name": "Repeater", "out_path_len": 2}
    radio.contacts = {contact["public_key"]: contact}
    api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid", entry=entry)
    api._mesh_core = radio
    api._connected = True
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    coordinator._channel_info = {0: {"channel_name": "Public", "channel_secret": SECRETS[2]}}
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    try:
        yield SimpleNamespace(entry=entry, api=api, coordinator=coordinator)
    finally:
        await coordinator.async_shutdown()
        await radio.close()


async def test_diagnostics_describe_the_entry_without_secrets(hass, mesh):
    coordinator = mesh.coordinator
    coordinator._repeater_consecutive_failures[REPEATER] = 3
    coordinator._next_repeater_update_times[REPEATER] = 1_800_000_000
    coordinator._increment_success(REPEATER)
    coordinator._increment_failure(REPEATER)

    result = await async_get_config_entry_diagnostics(hass, mesh.entry)
    dumped = json.dumps(result)

    for secret in SECRETS:
        assert secret not in dumped
    assert result["integration_version"]
    assert result["connection"]["connected"] is True
    assert result["connection"]["pubkey_prefix"] == "cc" * 6
    assert "cc" * 32 not in dumped
    assert result["traffic"]["policy"] == "governed"
    assert result["traffic"]["lanes"]["flood_capacity"] == 5
    repeater, client = result["nodes"]
    assert repeater["pubkey_prefix"] == REPEATER
    assert repeater["route"] == {"type": "direct", "path_len": 2}
    assert repeater["status_failures"] == 3
    assert repeater["next_status"].startswith("2027-01-15")
    assert (repeater["request_successes"], repeater["request_failures"]) == (1, 1)
    assert client["route"] is None
    assert result["sizes"]["channel_info"] == 1
    assert result["entry"]["data"]["repeater_subscriptions"][0]["pubkey_prefix"] == REPEATER


async def test_diagnostics_work_while_disconnected(hass, mesh):
    mesh.api._connected = False
    mesh.api._mesh_core = None

    result = await async_get_config_entry_diagnostics(hass, mesh.entry)

    json.dumps(result)
    assert result["connection"]["connected"] is False
    assert len(result["nodes"]) == 2


async def test_diagnostics_without_a_coordinator(hass, mesh):
    hass.data[DOMAIN] = {}

    result = await async_get_config_entry_diagnostics(hass, mesh.entry)

    json.dumps(result)
    assert result["coordinator"] is None
    assert "repeater-pass-1" not in json.dumps(result)
