"""Option edits reach a loaded entry without rebuilding the radio."""

import logging
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import async_update_options
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

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

    _edit(hass, entry, repeater_subscriptions=repeaters)
    await async_update_options(hass, entry)

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


async def test_adding_a_repeater_reloads_so_its_entities_appear(hass, loaded) -> None:
    """A tracked-node change still rebuilds the entry, as the platforms need."""
    entry, coordinator, _ = loaded
    reloads = _record_reloads(hass)

    _edit(
        hass,
        entry,
        repeater_subscriptions=[
            *entry.options["repeater_subscriptions"],
            {"name": "Ridge", "pubkey_prefix": "112233445566"},
        ],
    )
    await async_update_options(hass, entry)

    assert reloads == [entry.entry_id]


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
