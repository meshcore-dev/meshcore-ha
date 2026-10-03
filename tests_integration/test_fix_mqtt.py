"""MQTT packet compatibility and live broker reconfiguration regressions."""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.binary_sensor import (
    MeshCoreMqttBrokerConnectionBinarySensor,
    async_setup_entry,
)
from custom_components.meshcore.config import Settings, _apply_uploaders
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.mqtt_uploader import MeshCoreMqttUploader
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

BROKERS = {"1": {"enabled": True, "server": "first.invalid", "port": 1883}}


def make_uploader(hass, entry):
    """Build an uploader without making any broker connections."""
    return MeshCoreMqttUploader(hass, logging.getLogger(__name__), entry)


@pytest.fixture
async def runtime(hass: HomeAssistant):
    """Use a real coordinator and session over a hardware-free radio."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="fix-mqtt",
        data={"name": "Hub", "pubkey": "cc" * 32},
        options={"mqtt_brokers": BROKERS},
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
    coordinator.mqtt_uploader = make_uploader(hass, entry)
    coordinator.map_uploader = None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    yield SimpleNamespace(entry=entry, coordinator=coordinator)
    await coordinator.async_shutdown()
    await api.close()


@pytest.mark.parametrize("route_type,route", [(0, "F"), (1, "F"), (2, "D"), (3, "D")])
def test_firmware_route_letters(hass, runtime, route_type, route):
    """Direct routes include transport-direct, flood routes include transport-flood."""
    packet = runtime.coordinator.mqtt_uploader._normalize_packet_event(
        "EventType.RX_LOG_DATA",
        {"payload": f"{route_type:02x}0000000000", "parsed": {"path": "ABCD"}},
    )
    assert packet["route"] == route
    assert packet.get("path") == ("ABCD" if route == "D" else None)


def test_unknown_route_keeps_existing_fallback(hass, runtime):
    """An undecodable header still uses the supplied route or default flood."""
    uploader = runtime.coordinator.mqtt_uploader
    assert uploader._normalize_packet_event("RX_LOG_DATA", {"payload": "zz"})["route"] == "F"
    assert uploader._normalize_packet_event(
        "RX_LOG_DATA", {"payload": "zz", "route": "D"}
    )["route"] == "D"


@pytest.mark.parametrize("event_type", ["STATS_PACKETS", "EventType.STATS_PACKETS", "PACKET", "FAKE_RX_LOG_DATA"])
def test_packet_mode_rejects_non_packet_events(hass, runtime, event_type):
    """Packet-mode brokers never publish stats, even if a payload key is present."""
    uploader = runtime.coordinator.mqtt_uploader
    client = MagicMock()
    uploader._clients = [{"broker": uploader.get_brokers()[0], "client": client, "connected": True}]
    uploader.publish_raw_event(event_type, {"payload": "0100"})
    client.publish.assert_not_called()


@pytest.mark.parametrize("event_type", ["RX_LOG_DATA", "EventType.RX_LOG_DATA", "RF_LOG", "RF_LOG_DATA", "RAW_LOG", "RAW_LOG_DATA"])
@pytest.mark.parametrize("payload", [{}, {"payload": ""}, {"payload": "   "}, {"payload_length": 10}])
def test_packet_mode_never_publishes_empty_raw(hass, runtime, event_type, payload):
    """Missing packet bytes cannot produce a PACKET record."""
    uploader = runtime.coordinator.mqtt_uploader
    client = MagicMock()
    uploader._clients = [{"broker": uploader.get_brokers()[0], "client": client, "connected": True}]
    uploader.publish_raw_event(event_type, payload)
    client.publish.assert_not_called()


@pytest.mark.parametrize("event_type", ["RX_LOG_DATA", "EventType.RX_LOG_DATA", "RF_LOG", "RF_LOG_DATA", "RAW_LOG", "RAW_LOG_DATA"])
def test_real_packet_events_publish_bytes(hass, runtime, event_type):
    """RF/raw log aliases retain payload and raw_hex support."""
    uploader = runtime.coordinator.mqtt_uploader
    client = MagicMock()
    uploader._clients = [{"broker": uploader.get_brokers()[0], "client": client, "connected": True}]
    uploader.publish_raw_event(event_type, {"raw_hex": "aabb0100"})
    packet = json.loads(client.publish.call_args.args[1])
    assert packet["raw"] == "0100"
    assert packet["type"] == "PACKET"


def test_raw_mode_still_publishes_stats(hass, runtime):
    """The packet filter must not change raw event mode."""
    uploader = runtime.coordinator.mqtt_uploader
    broker = uploader.get_brokers()[0]
    broker.payload_mode = "raw"
    client = MagicMock()
    uploader._clients = [{"broker": broker, "client": client, "connected": True}]
    uploader.publish_raw_event("EventType.STATS_PACKETS", {"recv_errors": 1})
    client.publish.assert_called_once()


async def test_empty_status_topic_does_not_stop_broker_startup(hass, runtime, caplog):
    """paho rejects an empty will topic; skip it and initialize every broker."""
    hass.config_entries.async_update_entry(runtime.entry, options={"mqtt_brokers": {
        "1": {**BROKERS["1"], "topic_status": ""},
        "2": {"enabled": True, "server": "second.invalid"},
    }})
    uploader = make_uploader(hass, runtime.entry)
    clients = [MagicMock(), MagicMock()]

    def will(topic, *args, **kwargs):
        if not topic:
            raise ValueError("Invalid topic.")

    for client in clients:
        client.will_set.side_effect = will
    with (
        patch("custom_components.meshcore.mqtt_uploader.mqtt.Client", side_effect=clients),
        patch.object(uploader, "_async_prime_status_metadata", new=AsyncMock()),
    ):
        try:
            await uploader.async_start()
            assert len(uploader._clients) == 2
            clients[0].will_set.assert_not_called()
            clients[1].will_set.assert_called_once()
            for client in clients:
                client.connect.assert_called_once()
            assert "Empty status topic; skipping MQTT will" in caplog.text
        finally:
            await uploader.async_stop()


async def test_live_broker_edit_add_remove_and_reenable(hass, runtime):
    """The platform adds sensors live and existing entities follow replacements."""
    entry, coordinator = runtime.entry, runtime.coordinator
    added = []
    await async_setup_entry(hass, entry, added.extend)
    sensors = [sensor for sensor in added if isinstance(sensor, MeshCoreMqttBrokerConnectionBinarySensor)]
    assert len(sensors) == 1
    first = sensors[0]
    first.hass = hass
    first.async_write_ha_state = MagicMock()
    await first.async_added_to_hass()
    old_uploader = coordinator.mqtt_uploader
    old_callback = next(iter(old_uploader._connection_state_callbacks))
    old_callback(1, True)
    assert first.is_on

    async def edit(brokers):
        old = Settings.from_entry(entry)
        hass.config_entries.async_update_entry(entry, options={"mqtt_brokers": brokers})
        with patch.object(MeshCoreMqttUploader, "async_start", new=AsyncMock()):
            await _apply_uploaders(hass, entry, coordinator, old, Settings.from_entry(entry))
        await hass.async_block_till_done()

    try:
        await edit({
            "1": {"enabled": True, "server": "replacement.invalid"},
            "2": {"enabled": True, "server": "second.invalid"},
        })
        assert first.available
        assert first.extra_state_attributes["server"] == "replacement.invalid"
        assert not first.is_on
        assert not old_uploader._connection_state_callbacks
        old_callback(1, True)
        assert not first.is_on
        coordinator.mqtt_uploader._notify_connection_state(1, True)
        await hass.async_block_till_done()
        assert first.is_on
        assert len([sensor for sensor in added if isinstance(sensor, MeshCoreMqttBrokerConnectionBinarySensor)]) == 2
        await edit({"2": {"enabled": True, "server": "second.invalid"}})
        assert not first.available
        assert not first.is_on
        await edit(BROKERS)
        assert first.available
        assert first.extra_state_attributes["server"] == "first.invalid"
        assert len([sensor for sensor in added if isinstance(sensor, MeshCoreMqttBrokerConnectionBinarySensor)]) == 2
    finally:
        await first.async_will_remove_from_hass()
    assert not coordinator.mqtt_uploader._connection_state_callbacks


async def test_first_live_broker_and_failed_restart(hass, runtime):
    """An empty initial setup adds its first sensor live; failure makes it unavailable."""
    entry, coordinator = runtime.entry, runtime.coordinator
    hass.config_entries.async_update_entry(entry, options={"mqtt_brokers": {}})
    coordinator.mqtt_uploader = make_uploader(hass, entry)
    added = []
    await async_setup_entry(hass, entry, added.extend)
    assert not any(isinstance(sensor, MeshCoreMqttBrokerConnectionBinarySensor) for sensor in added)
    old = Settings.from_entry(entry)
    hass.config_entries.async_update_entry(entry, options={"mqtt_brokers": BROKERS})
    with patch.object(MeshCoreMqttUploader, "async_start", new=AsyncMock()):
        await _apply_uploaders(hass, entry, coordinator, old, Settings.from_entry(entry))
    await hass.async_block_till_done()
    sensor = next(sensor for sensor in added if isinstance(sensor, MeshCoreMqttBrokerConnectionBinarySensor))
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()
    await sensor.async_added_to_hass()
    try:
        old = Settings.from_entry(entry)
        hass.config_entries.async_update_entry(entry, options={"mqtt_brokers": {}})
        with patch.object(MeshCoreMqttUploader, "async_start", side_effect=RuntimeError("failed")):
            await _apply_uploaders(hass, entry, coordinator, old, Settings.from_entry(entry))
        await hass.async_block_till_done()
        assert not sensor.available
        assert not sensor.is_on
        assert coordinator.mqtt_uploader is None
        assert sensor.async_write_ha_state.call_count >= 2
        # A different entry's signal cannot change this entity's subscription/state.
        count = sensor.async_write_ha_state.call_count
        async_dispatcher_send(hass, "meshcore_mqtt_uploaders_changed_other")
        await hass.async_block_till_done()
        assert sensor.async_write_ha_state.call_count == count
    finally:
        await sensor.async_will_remove_from_hass()
