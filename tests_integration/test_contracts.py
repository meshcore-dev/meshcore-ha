"""Literal public-contract pins exercised through real HA producers and registries."""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import PLATFORMS, async_setup_entry
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.logbook import (
    _collect_incoming_rx_logs,
    handle_channel_message,
    handle_contact_message,
    handle_outgoing_message,
)
from custom_components.meshcore.radio import RadioSession
from custom_components.meshcore.services import async_setup_services
from custom_components.meshcore.utils import create_message_correlation_key
from tests.support.fake_radio import FakeRadio

CONTRACTS: Final = Path(__file__).with_name("contracts")
NOW: Final = 1700000000


def _fixture(name: str) -> Any:
    """Read reviewed expectations; tests never rewrite contract files."""
    return json.loads((CONTRACTS / f"{name}.json").read_text())


def _assert_contract(actual: Any, expected: Any) -> None:
    """Compare recursively, distinguishing bool/int and int/float equality."""
    assert type(actual) is type(expected), (actual, expected)
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), (actual, expected)
        for key in expected:
            _assert_contract(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected), (actual, expected)
        for value, wanted in zip(actual, expected, strict=True):
            _assert_contract(value, wanted)
    else:
        assert actual == expected


def _validator(value: Any) -> Any:
    """Render the actual voluptuous validators without unstable object addresses."""
    if isinstance(value, vol.Schema):
        fields = {}
        for marker, validator in value.schema.items():
            if isinstance(marker, vol.Marker):
                name = f"{type(marker).__name__}:{marker.schema}"
                field = {"validator": _validator(validator)}
                if marker.default is not vol.UNDEFINED:
                    field["default"] = marker.default()
                if isinstance(marker, vol.Exclusive):
                    field["group"] = marker.group_of_exclusion
            else:
                name = f"type:{marker.__name__}"
                field = {"validator": _validator(validator)}
            fields[name] = field
        return {"fields": fields, "required": value.required, "extra": value.extra}
    if isinstance(value, (vol.Any, vol.All)):
        return {type(value).__name__: [_validator(v) for v in value.validators]}
    if isinstance(value, vol.Coerce):
        return {"Coerce": value.type.__name__}
    if isinstance(value, vol.Range):
        return {"Range": [value.min, value.max, value.min_included, value.max_included]}
    if isinstance(value, vol.Length):
        return {"Length": [value.min, value.max]}
    if callable(value):
        return f"{value.__module__}.{value.__qualname__}"
    return value


def _services(hass: HomeAssistant) -> dict:
    """Read every registered integration service, schema, and response mode."""
    return {
        name: {"schema": _validator(service.schema), "response": service.supports_response.value}
        for name, service in hass.services.async_services()[DOMAIN].items()
    }


@pytest.fixture
async def runtime(hass: HomeAssistant) -> AsyncIterator[SimpleNamespace]:
    """Build a real coordinator around a fixture entry and a hardware-free radio."""
    fixture = _fixture("entry")
    entry = MockConfigEntry(domain=DOMAIN, entry_id="contract", data=fixture["data"])
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    radio.contacts = {c["public_key"]: c for c in fixture["contacts"]}
    api = RadioSession(hass=hass, connection_type="tcp", tcp_host="fixture.invalid")
    api._mesh_core = radio
    api._connected = True
    coordinator = MeshCoreDataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        DOMAIN,
        timedelta(seconds=5),
        api,
        entry,
    )
    coordinator._contacts = {key[:12]: c for key, c in radio.contacts.items()}
    coordinator._discovered_contacts = {c["public_key"]: c for c in fixture["discovered_contacts"]}
    coordinator._channel_info = {c["channel_idx"]: c for c in fixture["channels"]}
    coordinator._max_channels = 2
    coordinator.data = {"contacts": coordinator.get_all_contacts()}
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    try:
        yield SimpleNamespace(entry=entry, radio=radio, api=api, coordinator=coordinator)
    finally:
        await coordinator.async_shutdown()
        await api.disconnect()
        await radio.close()
        radio.assert_no_leaked_tasks()


@pytest.fixture
def events(hass: HomeAssistant) -> dict[str, list[dict]]:
    """Capture the real HA bus, with deterministic producer clocks and send IDs."""
    captured = {name: [] for name in _fixture("events")}

    @callback
    def receive(event: Any) -> None:
        """Snapshot event data before a later producer could mutate it."""
        captured[event.event_type].append(dict(event.data))

    for name in captured:
        hass.bus.async_listen(name, receive)
    return captured


@pytest.fixture(autouse=True)
def producer_clock() -> Iterator[None]:
    """Freeze only integration clocks, leaving HA and asyncio scheduling real."""
    with (
        patch("custom_components.meshcore.services.time", SimpleNamespace(time=lambda: NOW)),
        patch(
            "custom_components.meshcore.services.uuid.uuid4",
            return_value=SimpleNamespace(hex="12345678"),
        ),
        patch(
            "custom_components.meshcore.logbook.dt_util",
            SimpleNamespace(utcnow=lambda: datetime.fromtimestamp(NOW, UTC)),
        ),
        patch("custom_components.meshcore.time", SimpleNamespace(time=lambda: float(NOW))),
    ):
        yield


async def test_service_contracts(hass: HomeAssistant) -> None:
    """Pin all 15 registrations, including the unusual send_message schema."""
    await async_setup_services(hass)
    _assert_contract(_services(hass), _fixture("services"))


async def test_service_rename_has_teeth(hass: HomeAssistant) -> None:
    """A producer-side service rename must fail the same contract assertion."""
    with patch("custom_components.meshcore.services.SERVICE_SEND_MESSAGE", "send_message_renamed"):
        await async_setup_services(hass)
    with pytest.raises(AssertionError):
        _assert_contract(_services(hass), _fixture("services"))


@pytest.mark.parametrize("actual,expected", [(True, 1), (1, 1.0), ({"extra": 1}, {})])
def test_contract_comparison_rejects_type_and_shape_changes(actual: Any, expected: Any) -> None:
    """Numeric equality and added fields cannot silently weaken the contract pins."""
    with pytest.raises(AssertionError):
        _assert_contract(actual, expected)


async def test_message_and_delivery_contracts(
    hass: HomeAssistant,
    runtime: SimpleNamespace,
    events: dict,
) -> None:
    """Pin incoming direct/channel and outgoing direct/channel/progressive events."""
    coordinator = runtime.coordinator
    handle_contact_message(
        Event(
            EventType.CONTACT_MSG_RECV,
            {
                "pubkey_prefix": "bbbbbbbbbbbb",
                "text": "Hello",
                "path_len": 255,
                "SNR": 7.5,
            },
        ),
        coordinator,
    )
    await handle_channel_message(
        Event(
            EventType.CHANNEL_MSG_RECV,
            {
                "channel_idx": 1,
                "text": "Client: Hello",
                "path_len": 2,
                "SNR": 4.5,
            },
        ),
        coordinator,
    )
    await handle_outgoing_message(
        {
            "message_type": "direct",
            "message": "Hello",
            "receiver": "Client",
            "contact_public_key": "bbbbbbbbbbbb",
            "send_id": "12345678",
            "ack_received": True,
        },
        coordinator,
    )
    key = create_message_correlation_key(1, NOW)
    coordinator._pending_rx_logs[key] = [{"snr": 3.5, "rssi": -90, "path": "aa"}]
    await handle_outgoing_message(
        {
            "message_type": "channel",
            "message": "Hello",
            "channel_idx": 1,
            "send_timestamp": NOW,
            "send_id": "12345678",
        },
        coordinator,
    )
    await hass.async_block_till_done()
    base = events["meshcore_message"][1]
    coordinator._pending_rx_logs[key] = [{"snr": 3.5, "rssi": -90, "path": "aa"}]
    await _collect_incoming_rx_logs(hass, coordinator, key, base)
    await hass.async_block_till_done()
    expected = _fixture("events")
    _assert_contract(events["meshcore_message"], expected["meshcore_message"])
    _assert_contract(events["meshcore_delivery_update"], expected["meshcore_delivery_update"])


async def test_service_event_contracts(
    hass: HomeAssistant,
    runtime: SimpleNamespace,
    events: dict,
) -> None:
    """Call actual HA service handlers to capture sent-message and CLI envelopes."""
    radio = runtime.radio
    contact = radio.get_contact_by_key_prefix("bbbbbbbbbbbb")
    radio.script[("send_msg", tuple(sorted(contact.items())), "Hello")] = Event(
        EventType.MSG_SENT, {}
    )
    radio.script[("send_chan_msg", 1, "Hello", (("timestamp", NOW),))] = Event(EventType.OK, {})
    radio.script[("get_bat",)] = Event(EventType.BATTERY, {"level": 50})
    await async_setup_services(hass)
    for service, data in [
        ("send_message", {"pubkey_prefix": "bbbbbbbbbbbb", "message": "Hello"}),
        ("send_channel_message", {"channel_idx": 1, "message": "Hello"}),
        ("execute_command", {"command": "get_bat", "record_to_console": True}),
    ]:
        await hass.services.async_call(
            DOMAIN, service, {"entry_id": "contract", **data}, blocking=True
        )
        await hass.async_block_till_done()
    expected = _fixture("events")
    _assert_contract(events["meshcore_message_sent"], expected["meshcore_message_sent"])
    _assert_contract(events["meshcore_cli_response"], expected["meshcore_cli_response"])


async def test_message_optional_fields_and_fallbacks(
    hass: HomeAssistant,
    runtime: SimpleNamespace,
    events: dict,
) -> None:
    """Freeze omitted optional fields and both outgoing correlation fallbacks."""
    coordinator = runtime.coordinator
    handle_contact_message(
        Event(
            EventType.CONTACT_MSG_RECV,
            {
                "pubkey_prefix": "eeeeeeeeeeee",
                "text": "Hello",
                "path_len": -1,
            },
        ),
        coordinator,
    )
    await handle_channel_message(
        Event(
            EventType.CHANNEL_MSG_RECV,
            {
                "channel_idx": 0,
                "text": "Hello",
                "path_len": 255,
            },
        ),
        coordinator,
    )
    await handle_outgoing_message(
        {
            "message_type": "direct",
            "message": "Hello",
            "receiver": "Client",
            "contact_public_key": "bbbbbbbbbbbb",
        },
        coordinator,
    )
    await handle_outgoing_message(
        {
            "message_type": "channel",
            "message": "Hello",
            "channel_idx": 0,
        },
        coordinator,
    )
    with patch(
        "custom_components.meshcore.logbook.create_message_correlation_key",
        side_effect=ValueError("fixture error"),
    ):
        await handle_outgoing_message(
            {
                "message_type": "channel",
                "message": "Hello",
                "channel_idx": 0,
                "send_timestamp": NOW,
            },
            coordinator,
        )
    await hass.async_block_till_done()
    _assert_contract(events["meshcore_message"], _fixture("message_fallbacks"))


async def test_connection_event_contracts(
    hass: HomeAssistant,
    runtime: SimpleNamespace,
    events: dict,
) -> None:
    """Use the actual API connect, link-loss callback, and disconnect producers."""
    radio = runtime.radio
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    with (
        patch("custom_components.meshcore.radio.MeshCore.create_tcp", return_value=radio),
        patch("custom_components.meshcore.radio.time", SimpleNamespace(time=lambda: NOW)),
    ):
        radio.script[("set_time", NOW)] = Event(EventType.OK, {})
        assert await runtime.api.connect()
        await radio.drop_link()
        await runtime.api.disconnect()
    await hass.async_block_till_done()
    expected = _fixture("events")
    _assert_contract(events["meshcore_connected"], expected["meshcore_connected"])
    _assert_contract(events["meshcore_disconnected"], expected["meshcore_disconnected"])


async def test_raw_event_contracts(
    hass: HomeAssistant,
    runtime: SimpleNamespace,
    events: dict,
) -> None:
    """Install the real setup closure and dispatch through it, including fallback."""
    hass.data[DOMAIN].pop("contract")
    hass.data["meshcore_static_path_registered"] = True
    coordinator = runtime.coordinator
    with (
        patch("custom_components.meshcore.RadioSession", return_value=runtime.api),
        patch.object(runtime.api, "connect", return_value=True),
        patch("custom_components.meshcore.MeshCoreDataUpdateCoordinator", return_value=coordinator),
        patch.object(coordinator._store, "async_load", return_value={}),
        patch.object(coordinator, "fetch_all_channel_info", return_value=None),
        patch.object(coordinator, "async_load_neighbor_data", return_value=None),
        patch.object(coordinator, "async_reconcile_discovered_for_mode", return_value=None),
        patch(
            "custom_components.meshcore.MeshCoreMqttUploader", side_effect=RuntimeError("disabled")
        ),
        patch(
            "custom_components.meshcore.MeshCoreMapUploader", side_effect=RuntimeError("disabled")
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None),
    ):
        assert await async_setup_entry(hass, runtime.entry)
    await runtime.radio.emit(EventType.BATTERY, {"level": 50, "raw": b"\x01\x02"})
    with patch(
        "custom_components.meshcore.sanitize_event_data", side_effect=ValueError("fixture error")
    ):
        await runtime.radio.emit(EventType.BATTERY, {"level": 50})
    await hass.async_block_till_done()
    _assert_contract(events["meshcore_raw_event"], _fixture("events")["meshcore_raw_event"])


async def test_entity_contracts(
    recorder_mock: Any,
    enable_custom_integrations: Any,
    hass: HomeAssistant,
    runtime: SimpleNamespace,
) -> None:
    """Register every entity from the fixture via the real HA platform lifecycle."""
    runtime.entry.mock_state(hass, ConfigEntryState.LOADED)
    with patch.object(
        runtime.coordinator, "_async_update_data", return_value=runtime.coordinator.data
    ):
        await hass.config_entries.async_forward_entry_setups(runtime.entry, PLATFORMS)
        await hass.async_block_till_done()
        for idx in (0, 1):
            await runtime.radio.emit(
                EventType.CHANNEL_MSG_RECV,
                {
                    "channel_idx": idx,
                    "text": "Client: Hello",
                    "path_len": 0,
                },
            )
        await hass.async_block_till_done()
        actual = {
            entity.entity_id: entity.unique_id
            for entity in er.async_entries_for_config_entry(er.async_get(hass), "contract")
        }
        _assert_contract(actual, _fixture("entities"))
        await hass.config_entries.async_unload_platforms(runtime.entry, PLATFORMS)
