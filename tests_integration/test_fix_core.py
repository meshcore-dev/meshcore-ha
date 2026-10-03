"""Regression coverage for coordinator, traffic and entity fixes."""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.exceptions import ConfigEntryNotReady
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import async_setup_entry
from custom_components.meshcore import coordinator as coordinator_module
from custom_components.meshcore import rate_limiter as limiter_module
from custom_components.meshcore import sensor as sensor_module
from custom_components.meshcore.config import _apply_node_edit
from custom_components.meshcore.const import DOMAIN, NEIGHBOR_STALE_THRESHOLD
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.device_tracker import DeviceTrackerManager, MeshCoreGPSTracker
from custom_components.meshcore.events import EVENT_DELIVERY_UPDATE, EVENT_MESSAGE
from custom_components.meshcore.logbook import handle_outgoing_message
from custom_components.meshcore.radio import RadioSession
from custom_components.meshcore.select import MeshCoreChannelSelect
from custom_components.meshcore.sensor import LastMessageDeliverySensor, MeshCoreNeighborSensor
from custom_components.meshcore.telemetry_sensor import (
    MeshCoreTelemetrySensor,
    TelemetrySensorManager,
)
from custom_components.meshcore.traffic import (
    GOVERNED_BACKOFF_CAP_SECONDS,
    LANE_FLOOD,
    MeshBudget,
    backoff_delay,
)
from tests.support.fake_radio import FakeRadio

PREFIX = "aabbccddeeff"
CONTACT = {"public_key": PREFIX + "11" * 26, "adv_name": "Advert Name", "lastmod": 10}


@pytest.fixture
async def mesh(hass) -> AsyncIterator[SimpleNamespace]:
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id="fix_core",
        data={
            "connection_type": "tcp", "tcp_host": "fixture.invalid",
            "name": "Hub", "pubkey": "cc" * 32, "traffic_policy": "governed",
            "repeater_subscriptions": [
                {"name": "Repeater", "pubkey_prefix": PREFIX,
                 "update_interval": 7200, "telemetry_enabled": True}
            ],
            "tracked_clients": [
                {"name": "Client", "pubkey_prefix": "001122334455", "update_interval": 3600}
            ],
        },
    )
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    radio.contacts = {CONTACT["public_key"]: dict(CONTACT)}
    api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid", entry=entry)
    api._mesh_core = radio
    api._connected = True
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )
    coordinator.data = {"contacts": [dict(CONTACT)]}
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    try:
        yield SimpleNamespace(entry=entry, radio=radio, api=api, coordinator=coordinator)
    finally:
        await coordinator.async_shutdown()
        await radio.close()


def _disable(coordinator, prefix=PREFIX):
    coordinator._auto_disabled_devices.add(prefix)
    coordinator._repeater_consecutive_failures[prefix] = 5
    coordinator._telemetry_consecutive_failures[prefix] = 4
    coordinator._next_repeater_update_times[prefix] = 9999999999
    coordinator._next_telemetry_update_times[prefix] = 9999999999


def _assert_rearmed(coordinator, prefix=PREFIX):
    assert prefix not in coordinator._auto_disabled_devices
    assert prefix not in coordinator._repeater_consecutive_failures
    assert prefix not in coordinator._telemetry_consecutive_failures
    assert coordinator._next_repeater_update_times[prefix] == 0
    assert coordinator._next_telemetry_update_times[prefix] == 0
    assert coordinator._last_successful_request[prefix] > 0


@pytest.mark.parametrize("kind", ["repeaters", "clients"])
async def test_changed_node_edit_rearms_only_that_node(mesh, kind):
    coordinator = mesh.coordinator
    old = getattr(coordinator.settings, kind)[0]
    _disable(coordinator, old.pubkey_prefix)
    coordinator._auto_disabled_devices.add("other")
    _apply_node_edit(coordinator, old, replace(old, update_interval=old.update_interval + 1))
    _assert_rearmed(coordinator, old.pubkey_prefix)
    assert "other" in coordinator._auto_disabled_devices


async def test_advert_rearms_governed_but_not_legacy(mesh, monkeypatch):
    coordinator = mesh.coordinator
    monkeypatch.setattr(coordinator, "_fetch_advert_path", AsyncMock())
    coordinator._setup_advert_path_listener()
    _disable(coordinator)
    await mesh.radio.emit(EventType.ADVERTISEMENT, {"public_key": CONTACT["public_key"]})
    await asyncio.sleep(0)
    _assert_rearmed(coordinator)
    coordinator._traffic_policy = "legacy"
    _disable(coordinator)
    await mesh.radio.emit(EventType.ADVERTISEMENT, {"public_key": CONTACT["public_key"]})
    await asyncio.sleep(0)
    assert PREFIX in coordinator._auto_disabled_devices


async def test_contact_lastmod_update_rearms(mesh, monkeypatch):
    coordinator = mesh.coordinator
    coordinator._contacts = {PREFIX: dict(CONTACT)}
    mesh.radio.contacts[CONTACT["public_key"]]["lastmod"] = 11
    async def sync(**kwargs):
        mesh.api._contacts_reported_at += 1
        return True
    monkeypatch.setattr(mesh.api, "ensure_contacts", sync)
    _disable(coordinator)
    await coordinator._sync_contacts(100)
    _assert_rearmed(coordinator)


async def test_governed_loops_disable_once_and_stop_polling(mesh, monkeypatch, caplog):
    coordinator = mesh.coordinator
    coordinator._manual_mode_initialized = True
    coordinator._device_info_initialized = True
    coordinator._initial_drain_done = True
    coordinator._coordinator_start_time = 1
    coordinator._last_msg_activity = asyncio.get_running_loop().time()
    mesh.radio.script[("get_bat",)] = Event(EventType.BATTERY, {"level": 90})
    status, telemetry = AsyncMock(), AsyncMock()
    monkeypatch.setattr(coordinator, "_update_repeater", status)
    monkeypatch.setattr(coordinator, "_update_node_telemetry", telemetry)
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await asyncio.sleep(0)
    status.assert_not_called()
    telemetry.assert_not_called()
    assert caplog.text.count("Automatically disabling") == 2
    assert "Edit the node or wait for its next advert to resume." in caplog.text


def test_partial_refill_remaining_time_and_legacy_spacing(monkeypatch):
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(limiter_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    governed, legacy = MeshBudget("governed"), MeshBudget("legacy")
    while governed.try_consume(LANE_FLOOD):
        pass
    while legacy.try_consume():
        pass
    clock.value += 45
    assert governed.next_eligible(LANE_FLOOD) == 135
    assert legacy.next_eligible() == 120
    clock.value += 135
    assert governed.next_eligible(LANE_FLOOD) == 0


def test_flood_backoff_cap_applies_after_jitter(monkeypatch):
    monkeypatch.setattr("custom_components.meshcore.traffic.random.uniform", lambda *_: 0.1)
    assert backoff_delay("governed", 20, 7200) == GOVERNED_BACKOFF_CAP_SECONDS


async def test_delivery_listeners_ignore_other_entries(hass, mesh, monkeypatch):
    entities = []
    await sensor_module.async_setup_entry(hass, mesh.entry, entities.extend)
    delivery = next(entity for entity in entities if isinstance(entity, LastMessageDeliverySensor))
    waiting, update = MagicMock(), MagicMock()
    monkeypatch.setattr(delivery, "set_waiting", waiting)
    monkeypatch.setattr(delivery, "update_from_event", update)
    for entry_id in ("another", mesh.entry.entry_id):
        data = {"entry_id": entry_id, "outgoing": True, "message_type": "channel"}
        for event_type in (f"{DOMAIN}_message_sent", EVENT_DELIVERY_UPDATE, EVENT_MESSAGE):
            hass.bus.async_fire(event_type, data)
        await hass.async_block_till_done()
        assert waiting.call_count == (entry_id == mesh.entry.entry_id)
        assert update.call_count == 2 * (entry_id == mesh.entry.entry_id)


async def test_companion_ids_include_pubkey_and_keep_unique_ids(mesh):
    sensors, trackers = [], []
    for key in ("aa" * 32, "bb" * 32):
        node = {"type": "root", "name": "Hub", "pubkey_prefix": key}
        sensor = MeshCoreTelemetrySensor(
            mesh.coordinator, SensorEntityDescription(key="temperature", name="Temperature"),
            key[:12], 1, 103, node,
        )
        tracker = MeshCoreGPSTracker(mesh.coordinator, key[:12], node)
        assert sensor.entity_id == f"sensor.meshcore_{key[:6]}_temperature_ch1_hub"
        assert tracker.entity_id == f"device_tracker.meshcore_{key[:6]}_gps_hub"
        assert sensor.unique_id == f"{mesh.entry.entry_id}_{key[:12]}_1_103_telemetry"
        assert tracker.unique_id == f"{mesh.entry.entry_id}_{key[:12]}_gps_tracker"
        sensors.append(sensor.entity_id)
        trackers.append(tracker.entity_id)
    assert len(set(sensors)) == len(set(trackers)) == 2


@pytest.mark.parametrize("manager_type", [TelemetrySensorManager, DeviceTrackerManager])
async def test_untracked_contact_uses_advert_name(mesh, manager_type):
    coordinator = mesh.coordinator
    coordinator._tracked_repeaters = []
    coordinator.settings = replace(coordinator.settings, repeaters=())
    manager = manager_type(coordinator, lambda _: None)
    assert manager._get_node_info(PREFIX)["name"] == "Advert Name"
    coordinator.data["contacts"][0]["adv_name"] = ""
    assert manager._get_node_info(PREFIX)["name"] == f"Node {PREFIX[:6]}"
    assert manager._get_node_info("abcdef")["name"] == "Unknown Node abcdef"


async def test_neighbor_restart_does_not_double_count_and_absence_ages(mesh, monkeypatch):
    coordinator = mesh.coordinator
    clock = SimpleNamespace(value=200000.0)
    monkeypatch.setattr(coordinator_module, "time", SimpleNamespace(time=lambda: clock.value))
    stored = {PREFIX: {"11223344": {"secs_ago": 100, "last_updated": 199000, "snr": 5}}}
    monkeypatch.setattr(coordinator._neighbor_store, "async_load", AsyncMock(return_value=stored))
    await coordinator.async_load_neighbor_data()
    data = coordinator._repeater_neighbors[PREFIX]["11223344"]
    assert data["secs_ago"] == 100
    assert coordinator.neighbor_age(data) == 1100
    monkeypatch.setattr(mesh.api, "fetch_neighbours", AsyncMock(return_value={
        "neighbours": [], "neighbours_count": 0, "results_count": 0,
    }))
    await coordinator._fetch_repeater_neighbors(CONTACT, "Repeater", PREFIX)
    sensor = MeshCoreNeighborSensor(coordinator, PREFIX, "Repeater", "11223344")
    assert sensor.available
    clock.value = 199000 + NEIGHBOR_STALE_THRESHOLD
    assert not sensor.available
    assert sensor.extra_state_attributes["secs_ago"] == NEIGHBOR_STALE_THRESHOLD + 100
    # 86400 seconds since last heard is retained, rather than adding downtime twice.
    clock.value = 199000 + 86300
    assert await coordinator._cleanup_stale_neighbors(1) == 0
    clock.value += 1
    assert await coordinator._cleanup_stale_neighbors(1) == 1


@pytest.mark.parametrize("slot,label", [(0, "public"), (2, "2")])
async def test_outgoing_empty_channel_uses_fallback(hass, mesh, slot, label):
    coordinator = mesh.coordinator
    coordinator._channel_info[slot] = {"channel_name": ""}
    received = []
    hass.bus.async_listen(EVENT_MESSAGE, lambda event: received.append(event.data))
    await handle_outgoing_message(
        {"message_type": "channel", "message": "hello", "channel_idx": slot,
         "send_timestamp": 1700000000}, coordinator,
    )
    await hass.async_block_till_done()
    assert received[0]["channel"] == label


async def test_channel_select_empty_name_is_unused(mesh):
    mesh.coordinator._channel_info[2] = {"channel_name": ""}
    assert MeshCoreChannelSelect(mesh.coordinator).options[2] == "(unused) (2)"


@pytest.mark.parametrize("connection,address", [
    ("usb", "/dev/ttyUSB0"), ("ble", "AA:BB:CC:DD:EE:FF"), ("tcp", "fixture.invalid:5001"),
])
async def test_connection_failure_reports_transport_address(hass, monkeypatch, connection, address):
    entry = MockConfigEntry(domain=DOMAIN, data={
        "connection_type": connection, "usb_path": "/dev/ttyUSB0",
        "ble_address": "AA:BB:CC:DD:EE:FF", "tcp_host": "fixture.invalid", "tcp_port": 5001,
    })
    api = MagicMock()
    api.connect = AsyncMock(return_value=False)
    monkeypatch.setattr("custom_components.meshcore.RadioSession", lambda **_: api)
    # Replace this module's asyncio reference so HA's own scheduling is unaffected.
    monkeypatch.setattr("custom_components.meshcore.asyncio", SimpleNamespace(sleep=AsyncMock()))
    with pytest.raises(ConfigEntryNotReady, match=address):
        await async_setup_entry(hass, entry)
    assert api.connect.await_count == 3


async def test_new_contact_rearms_even_when_discovery_is_off(hass, mesh, monkeypatch):
    coordinator = mesh.coordinator
    hass.data[DOMAIN].pop(mesh.entry.entry_id)
    hass.data["meshcore_static_path_registered"] = True
    hass.config_entries.async_update_entry(mesh.entry, options={"contact_discovery_mode": "off"})
    monkeypatch.setattr("custom_components.meshcore.RadioSession", lambda **_: mesh.api)
    monkeypatch.setattr("custom_components.meshcore.MeshCoreDataUpdateCoordinator", lambda *a, **k: coordinator)
    monkeypatch.setattr(mesh.api, "connect", AsyncMock(return_value=True))
    monkeypatch.setattr(mesh.api, "ensure_contacts", AsyncMock(return_value=False))
    monkeypatch.setattr(coordinator, "fetch_all_channel_info", AsyncMock())
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    monkeypatch.setattr("custom_components.meshcore.MeshCoreMqttUploader", MagicMock(side_effect=RuntimeError("disabled")))
    monkeypatch.setattr("custom_components.meshcore.MeshCoreMapUploader", MagicMock(side_effect=RuntimeError("disabled")))
    assert await async_setup_entry(hass, mesh.entry)
    _disable(coordinator)
    await mesh.radio.emit(EventType.NEW_CONTACT, CONTACT)
    _assert_rearmed(coordinator)
    assert not coordinator._discovered_contacts


async def test_unchanged_edit_save_rearms(recorder_mock, enable_custom_integrations, hass, mesh):
    """Saving Edit Device without changes still resumes an auto-disabled node."""
    coordinator = mesh.coordinator
    _disable(coordinator, "001122334455")

    result = await hass.config_entries.options.async_init(mesh.entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "manage_devices"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"selected_device": "client_001122334455", "device_action": "edit"},
    )
    assert result["step_id"] == "edit_client"
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if hasattr(marker, "default")
    }
    await hass.config_entries.options.async_configure(result["flow_id"], defaults)

    _assert_rearmed(coordinator, "001122334455")
