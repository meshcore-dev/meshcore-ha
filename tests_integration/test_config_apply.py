"""Option edits reach a loaded entry without rebuilding the radio."""

import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import PLATFORMS, async_update_options
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

CONTRACTS = Path(__file__).with_name("contracts")
REPEATER_PREFIX = "aaaaaaaaaaaa"
CLIENT_PREFIX = "bbbbbbbbbbbb"
TRACKED_REPEATER = {"name": "Repeater", "pubkey_prefix": REPEATER_PREFIX}

ENTRY_DATA = {
    "connection_type": "tcp",
    "tcp_host": "fixture.invalid",
    "name": "Hub",
    "pubkey": "cc" * 32,
}
BASE_OPTIONS = {
    "repeater_subscriptions": [
        {"name": "Hilltop", "pubkey_prefix": "aabbccddeeff", "update_interval": 900}
    ],
    "tracked_clients": [],
    "self_telemetry_interval": 300,
    "stale_contact_days": 30,
}


@pytest.fixture
async def loaded(hass: HomeAssistant):
    """A loaded entry with a live coordinator over a fake radio."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="apply",
        version=4,
        data=dict(ENTRY_DATA),
        options=dict(BASE_OPTIONS),
    )
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
    api._mesh_core = radio
    api._connected = True
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    coordinator.mqtt_uploader = None
    coordinator.map_uploader = None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    yield entry, coordinator, radio
    await api.close()


def _record_reloads(hass: HomeAssistant) -> list[str]:
    """Capture reload requests instead of tearing the fake entry down."""
    reloads: list[str] = []

    async def fake_reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return True

    hass.config_entries.async_reload = fake_reload
    return reloads


def _edit(hass: HomeAssistant, entry: MockConfigEntry, **changes) -> None:
    """Write an options edit the way the options flow does."""
    hass.config_entries.async_update_entry(
        entry, options={**dict(entry.options), **changes}
    )


async def test_interval_edit_applies_without_a_reload(hass, loaded) -> None:
    """A node interval edit reaches the live coordinator; nothing reconnects."""
    entry, coordinator, radio = loaded
    repeaters = [
        {"name": "Hilltop", "pubkey_prefix": "aabbccddeeff", "update_interval": 1800}
    ]
    coordinator._next_repeater_update_times["aabbccddeeff"] = 12345
    budget = coordinator._rate_limiter

    reloads = _record_reloads(hass)

    _edit(hass, entry, repeater_subscriptions=repeaters)
    await async_update_options(hass, entry)

    assert reloads == []
    assert hass.data[DOMAIN][entry.entry_id] is coordinator
    assert coordinator._tracked_repeaters[0]["update_interval"] == 1800
    # The schedule and the budget survive the edit; a reload would reset both.
    assert coordinator._next_repeater_update_times["aabbccddeeff"] == 12345
    assert coordinator._rate_limiter is budget
    assert radio.connected


async def test_self_telemetry_and_cleanup_edits_apply_in_place(hass, loaded) -> None:
    """Toggles and intervals land on the coordinator without a rebuild."""
    entry, coordinator, _ = loaded

    _edit(
        hass,
        entry,
        self_telemetry_enabled=True,
        self_telemetry_interval=900,
        auto_cleanup_stale_contacts=True,
        stale_contact_days=7,
        messages_interval=30,
    )
    await async_update_options(hass, entry)

    assert coordinator._self_telemetry_enabled is True
    assert coordinator._self_telemetry_interval == 900
    assert coordinator._auto_cleanup_stale_contacts is True
    assert coordinator._stale_contact_days == 7
    assert coordinator.update_interval == timedelta(seconds=30)
    assert coordinator.settings.messages_interval == 30


async def test_traffic_policy_edit_swaps_the_budget_in_place(hass, loaded) -> None:
    """Switching policy replaces the budget without touching the radio."""
    entry, coordinator, radio = loaded

    _edit(hass, entry, traffic_policy="governed")
    await async_update_options(hass, entry)

    assert coordinator.traffic_policy == "governed"
    assert hass.data[DOMAIN][entry.entry_id] is coordinator
    assert radio.connected


@pytest.fixture
async def contract_runtime(recorder_mock, enable_custom_integrations, hass):
    """Load the contract entry's real platforms, tracking the repeaters given.

    The entry id and records are the contract fixture's, so the entity ids a
    live edit produces can be compared with the reviewed ones.
    """
    fixture = json.loads((CONTRACTS / "entry.json").read_text())
    closers = []

    async def load(repeaters: list[dict]) -> SimpleNamespace:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="contract",
            version=4,
            data=fixture["data"],
            options={"repeater_subscriptions": repeaters},
        )
        entry.add_to_hass(hass)
        radio = FakeRadio()
        await radio.start()
        radio.contacts = {c["public_key"]: c for c in fixture["contacts"]}
        api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
        api._mesh_core = radio
        api._connected = True
        coordinator = MeshCoreDataUpdateCoordinator(
            hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
        )
        coordinator.mqtt_uploader = None
        coordinator.map_uploader = None
        coordinator._contacts = {key[:12]: c for key, c in radio.contacts.items()}
        coordinator.data = {"contacts": coordinator.get_all_contacts()}
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        entry.mock_state(hass, ConfigEntryState.LOADED)
        polling = patch.object(
            coordinator, "_async_update_data", return_value=coordinator.data
        )
        polling.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await hass.async_block_till_done()

        async def close() -> None:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            polling.stop()
            await coordinator.async_shutdown()
            await api.disconnect()
            await radio.close()

        closers.append(close)
        return SimpleNamespace(entry=entry, coordinator=coordinator, radio=radio)

    yield load
    for close in closers:
        await close()


def _reviewed(unique_id_prefix: str) -> dict[str, str]:
    """The reviewed entity ids of one tracked node, from the contract fixture."""
    entities = json.loads((CONTRACTS / "entities.json").read_text())
    return {
        entity_id: unique_id
        for entity_id, unique_id in entities.items()
        if unique_id.startswith(unique_id_prefix)
    }


def _registered(hass: HomeAssistant, unique_id_prefix: str) -> dict[str, str]:
    """The registry entries of one tracked node, as {entity_id: unique_id}."""
    return {
        entity.entity_id: entity.unique_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), "contract")
        if (entity.unique_id or "").startswith(unique_id_prefix)
    }


def _device(hass: HomeAssistant, identifier: str) -> Any:
    """Look one node device up by its MeshCore identifier."""
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, identifier)})


async def test_adding_a_repeater_builds_the_entities_a_reload_would(
    contract_runtime, hass
) -> None:
    """A repeater added live gets the entity ids a fresh setup gives it."""
    runtime = await contract_runtime([])
    reloads = _record_reloads(hass)
    reviewed = _reviewed("contract_repeater_aaaaaaaaaaaa_")
    assert "binary_sensor.meshcore_aaaaaaaaaa_online_repeater" in reviewed
    assert "button.meshcore_aaaaaaaaaa_refresh_firmware" in reviewed
    assert _registered(hass, "contract_repeater_aaaaaaaaaaaa_") == {}

    _edit(hass, runtime.entry, repeater_subscriptions=[TRACKED_REPEATER])
    await async_update_options(hass, runtime.entry)
    await hass.async_block_till_done()

    assert reloads == []
    assert _registered(hass, "contract_repeater_aaaaaaaaaaaa_") == reviewed
    assert _device(hass, "contract_repeater_aaaaaaaaaaaa") is not None
    # Seeded exactly as setup seeds it: due now, with no inherited failures.
    assert runtime.coordinator._next_repeater_update_times[REPEATER_PREFIX] == 0
    assert REPEATER_PREFIX not in runtime.coordinator._repeater_consecutive_failures
    assert runtime.coordinator.settings.repeaters[0].pubkey_prefix == REPEATER_PREFIX


async def test_removing_a_tracked_node_applies_in_place(contract_runtime, hass) -> None:
    """Removal drops the node's entities and device, and nothing else."""
    runtime = await contract_runtime([TRACKED_REPEATER])
    coordinator = runtime.coordinator
    reloads = _record_reloads(hass)
    coordinator._next_repeater_update_times[REPEATER_PREFIX] = 999
    coordinator._reliability_stats[f"{REPEATER_PREFIX}_request_failures"] = 3
    coordinator._next_telemetry_update_times[CLIENT_PREFIX] = 4242
    assert _registered(hass, "contract_repeater_aaaaaaaaaaaa_")

    _edit(hass, runtime.entry, repeater_subscriptions=[])
    await async_update_options(hass, runtime.entry)
    await hass.async_block_till_done()

    assert reloads == []
    assert hass.data[DOMAIN]["contract"] is coordinator
    assert runtime.radio.connected
    assert _registered(hass, "contract_repeater_aaaaaaaaaaaa_") == {}
    assert _device(hass, "contract_repeater_aaaaaaaaaaaa") is None
    assert REPEATER_PREFIX not in coordinator._next_repeater_update_times
    assert f"{REPEATER_PREFIX}_request_failures" not in coordinator._reliability_stats
    # The node is still a mesh contact, and the other node is untouched.
    assert er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, "contract_contact_aaaaaaaaaaaa"
    )
    assert coordinator._next_telemetry_update_times[CLIENT_PREFIX] == 4242
    assert _registered(hass, "contract_client_bbbbbbbbbbbb_")


async def test_removing_a_tracked_client_applies_in_place(
    contract_runtime, hass
) -> None:
    """A client is torn down the same way a repeater is."""
    runtime = await contract_runtime([TRACKED_REPEATER])
    reloads = _record_reloads(hass)
    assert _registered(hass, "contract_client_bbbbbbbbbbbb_")

    _edit(hass, runtime.entry, tracked_clients=[])
    await async_update_options(hass, runtime.entry)
    await hass.async_block_till_done()

    assert reloads == []
    assert _registered(hass, "contract_client_bbbbbbbbbbbb_") == {}
    assert _device(hass, "contract_client_bbbbbbbbbbbb") is None
    assert _registered(hass, "contract_repeater_aaaaaaaaaaaa_")


async def test_enabling_neighbours_creates_the_counter_in_place(
    contract_runtime, hass
) -> None:
    """A toggle that owns an entity builds it through the same platform path."""
    runtime = await contract_runtime([TRACKED_REPEATER])
    reloads = _record_reloads(hass)
    registry = er.async_get(hass)
    counter = "contract_repeater_aaaaaaaaaaaa_neighbor_count"
    assert registry.async_get_entity_id("sensor", DOMAIN, counter) is None

    _edit(
        hass,
        runtime.entry,
        repeater_subscriptions=[{**TRACKED_REPEATER, "neighbors_enabled": True}],
    )
    await async_update_options(hass, runtime.entry)
    await hass.async_block_till_done()

    assert reloads == []
    assert registry.async_get_entity_id("sensor", DOMAIN, counter)


async def test_equivalent_edit_does_nothing(hass, loaded) -> None:
    """An update that changes no setting neither reloads nor re-applies."""
    entry, coordinator, _ = loaded
    reloads = _record_reloads(hass)
    before = coordinator.settings

    await async_update_options(hass, entry)

    assert reloads == []
    assert coordinator.settings is before


async def test_repeated_edits_in_one_session_each_reach_the_entry(
    recorder_mock, enable_custom_integrations, hass, loaded
) -> None:
    """Two edits in one options session both reach the entry and the coordinator."""
    entry, coordinator, _radio = loaded
    entry.add_update_listener(async_update_options)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    for interval in (1200, 2400):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"action": "manage_devices"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"selected_device": "repeater_aabbccddeeff", "device_action": "edit"},
        )
        assert result["step_id"] == "edit_repeater"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "telemetry_enabled": False,
                "neighbors_enabled": False,
                "update_interval": interval,
                "disable_path_reset": False,
                "disabled": True,
            },
        )
        await hass.async_block_till_done()

        stored = entry.options["repeater_subscriptions"][0]
        assert stored["update_interval"] == interval
        # A blank password field keeps the saved one.
        assert stored["password"] == ""
        # Each edit is a real entry change, so the live coordinator sees it.
        assert coordinator._tracked_repeaters[0]["update_interval"] == interval

    # Finishing keeps everything the sub-steps saved.
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "done"}
    )
    await hass.async_block_till_done()
    assert entry.options["repeater_subscriptions"][0]["update_interval"] == 2400
    assert entry.options["stale_contact_days"] == 30
