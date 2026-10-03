"""Regression coverage for CLI safety, routing, trace and contact services."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.events import REDACTED
from custom_components.meshcore.services import _COMMAND_OPS, async_setup_services
from tests.support.fake_radio import FakeRadio
from tests.support.session import StubSession


@pytest.fixture
async def services(hass):
    """Use a real coordinator and HA registry with scripted radio commands."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="services", data={
        "traffic_policy": "governed", "contact_discovery_mode": "full",
    })
    entry.add_to_hass(hass)
    radio = FakeRadio()
    await radio.start()
    api = StubSession(radio, connected=True)
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=30), api, entry
    )
    coordinator.record_cli_console = MagicMock(wraps=coordinator.record_cli_console)
    coordinator._save_discovered_contacts = MagicMock()
    coordinator._publish_contacts = MagicMock()
    hass.data[DOMAIN] = {entry.entry_id: coordinator}
    await async_setup_services(hass)
    yield SimpleNamespace(entry=entry, radio=radio, api=api, coordinator=coordinator)
    await coordinator.async_shutdown()
    await radio.close()


async def _call(hass, service, data, response=False):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=response
    )


def _helper(hass, entry, suffix, state, attributes=None, domain="select"):
    entity = er.async_get(hass).async_get_or_create(
        domain, DOMAIN, f"{entry.entry_id}_{suffix}", config_entry=entry
    )
    hass.states.async_set(entity.entity_id, state, attributes)
    return entity


@pytest.mark.parametrize("ui", [False, True])
@pytest.mark.parametrize("expose", [False, True])
@pytest.mark.parametrize("command,payload", [
    ("export_private_key", {"private_key": b"secret"}),
    ("get_channel 0", {"channel_secret": b"secret"}),
    ("get_channel 1", {"channels": [{"secret": "channelvalue"}]}),
])
async def test_console_secrets(hass, services, ui, expose, command, payload):
    hass.config_entries.async_update_entry(services.entry, options={"expose_secrets": expose})
    services.coordinator.update_telemetry_settings(services.entry)
    name, *args = command.split()
    services.radio.script[(name, *(int(arg) for arg in args))] = Event(EventType.OK, payload)
    events = []
    hass.bus.async_listen("meshcore_cli_response", lambda event: events.append(event.data))
    data = {"record_to_console": True}
    if ui:
        _helper(hass, services.entry, "command_input", command, domain="text")
    else:
        data["command"] = command
    response = await _call(hass, "execute_command_ui" if ui else "execute_command", data, True)
    await hass.async_block_till_done()
    recorded = services.coordinator.record_cli_console.call_args.args[1]
    assert events[-1]["response"] == recorded
    assert services.coordinator.cli_console_history[-1]["response"] == recorded
    real = "736563726574" if command != "get_channel 1" else "channelvalue"
    assert real in str(response)
    assert (real in str(recorded)) is expose
    assert (REDACTED in str(recorded)) is not expose


@pytest.mark.parametrize("payload", [None, {}, {"error_code": 2, "code_string": "bad"}, {"reason": "bad"}])
async def test_firmware_error_is_error(hass, services, payload):
    services.radio.script[("get_time",)] = Event(EventType.ERROR, payload)
    events = []
    hass.bus.async_listen("meshcore_cli_response", lambda event: events.append(event.data))
    result = await _call(hass, "execute_command", {
        "command": "get_time", "record_to_console": True,
    }, True)
    await hass.async_block_till_done()
    assert result["error"] == "rejected"
    assert all(result[key] == value for key, value in (payload or {}).items())
    assert events[-1]["is_error"] is True
    assert services.coordinator.record_cli_console.call_args.args[2] is True


@pytest.mark.parametrize("command", sorted(_COMMAND_OPS))
@pytest.mark.parametrize("routed", [False, True])
async def test_mesh_commands_metered(hass, services, command, routed):
    contact = {"public_key": "ab" * 32, "out_path_len": 0 if routed else -1,
               "out_path_hash_mode": 0 if routed else -1}
    services.coordinator.require_mesh_budget = MagicMock()
    services.api.contacts[contact["public_key"]] = contact
    non_contact = {"send_advert", "send_chan_msg", "send_trace", "send_control_data", "send_node_discover_req"}
    argument = contact if command in non_contact else contact["public_key"]
    # Resolve targets through the same contact lookup as production.
    services.radio.script[services.radio.key(command, contact)] = Event(EventType.OK, {})
    result = await _call(hass, "execute_command", {"command": f"{command}({argument!r})"}, True)
    assert result == {"event": EventType.OK.value, "command": command}
    assert services.radio.calls[-1] == services.radio.key(command, contact)
    messages = {"send_msg", "send_cmd", "send_msg_with_retry", "send_chan_msg", "send_trace"}
    floods = {"send_advert", "share_contact", "send_path_discovery", "send_path_discovery_sync",
              "send_node_discover_req", "send_control_data"}
    lane = "messages" if command in messages else "flood" if command in floods or not routed else "direct"
    services.coordinator.require_mesh_budget.assert_called_once_with(lane)


async def test_legacy_and_local_commands_unmetered(hass, services):
    services.coordinator.apply_traffic_policy("legacy")
    budget = services.coordinator._rate_limiter
    budget.try_consume = MagicMock()
    services.radio.script[("send_chan_msg", 0, "hello")] = Event(EventType.OK, {})
    await _call(hass, "execute_command", {"command": "send_chan_msg 0 hello"}, True)
    budget.try_consume.assert_not_called()
    services.coordinator.require_mesh_budget = MagicMock()
    services.radio.script[("get_time",)] = Event(EventType.OK, {"time": 1})
    await _call(hass, "execute_command", {"command": "get_time"}, True)
    services.coordinator.require_mesh_budget.assert_not_called()


@pytest.mark.parametrize("service", ["add_selected_contact", "remove_selected_contact", "remove_discovered_contact"])
async def test_contact_helpers_refuse_ambiguous_entry(hass, services, service):
    hass.data[DOMAIN]["other"] = SimpleNamespace(api=StubSession(connected=True))
    with pytest.raises(HomeAssistantError, match="ambiguous_config_entry"):
        await _call(hass, service, {})
    assert not services.radio.calls


@pytest.mark.parametrize("prefix_len", [2, 6, 12, 20, 64])
async def test_remove_discovered_resolves_entity_and_tracking(hass, services, prefix_len):
    key = "ab" * 32
    services.coordinator._discovered_contacts[key] = {"public_key": key}
    services.coordinator.tracked_diagnostic_binary_contacts.add(key)
    entity = _helper(hass, services.entry, f"contact_{key[:12]}", "off", domain="binary_sensor")
    await _call(hass, "remove_discovered_contact", {"pubkey_prefix": key[:prefix_len]})
    assert key not in services.coordinator._discovered_contacts
    assert key not in services.coordinator.tracked_diagnostic_binary_contacts
    assert er.async_get(hass).async_get(entity.entity_id) is None


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("threshold", [None, 30])
async def test_clear_preserves_added_and_sweeps_orphans(hass, services, empty, threshold):
    added, discovered, orphan = (pair * 32 for pair in ("aa", "bb", "cc"))
    contact = {"public_key": added, "added_to_node": True}
    services.coordinator._contacts[added[:12]] = contact
    if not empty:
        services.coordinator._discovered_contacts.update({added: contact, discovered: {"public_key": discovered}})
    services.coordinator.tracked_diagnostic_binary_contacts.update({added, discovered})
    entities = {key: _helper(hass, services.entry, f"contact_{key[:12]}", "off", domain="binary_sensor")
                for key in (added, orphan, *(() if empty else (discovered,)))}
    data = {} if threshold is None else {"days_threshold": threshold}
    await _call(hass, "clear_discovered_contacts", data)
    registry = er.async_get(hass)
    assert registry.async_get(entities[added].entity_id)
    assert added in services.coordinator.tracked_diagnostic_binary_contacts
    assert registry.async_get(entities[orphan].entity_id) is None
    if not empty:
        assert registry.async_get(entities[discovered].entity_id) is None
        assert discovered not in services.coordinator.tracked_diagnostic_binary_contacts


@pytest.mark.parametrize("width", [1, 2, 3, 4])
@pytest.mark.parametrize("discover", [False, True])
async def test_trace_hash_widths(hass, services, width, discover):
    key = "ab" * 32
    path = "11" + "00" * (width - 1) + "22" + "00" * (width - 1)
    contact = {"public_key": key, "added_to_node": True, "out_path_len": -1 if discover else 2,
               "out_path_hash_mode": width - 1, "out_path": path}
    services.api.contacts[key] = contact
    services.api.path_discovery = AsyncMock(return_value=(Event(EventType.OK, {}), Event(
        EventType.PATH_RESPONSE, {"out_path_len": 2, "out_path_hash_len": width, "out_path": path})))
    services.api.exchange = AsyncMock(return_value=Event(EventType.OK, {}))
    services.api.wait_for = AsyncMock(return_value=None)
    await _call(hass, "trace", {"pubkey_prefix": key[:6]}, True)
    assert services.api.exchange.call_args.args[-1] == bytes.fromhex("1122ab2211")


@pytest.mark.parametrize("requested,expected", [(1, 5), (10, 10), (120, 60)])
async def test_trace_wait_bounds(hass, services, requested, expected):
    key = "ab" * 32
    services.api.contacts[key] = {"public_key": key, "added_to_node": True, "out_path_len": 0}
    services.api.self_info = {"suggested_timeout": 59000}
    services.api.exchange = AsyncMock(return_value=Event(EventType.OK, {}))
    services.api.wait_for = AsyncMock(return_value=None)
    await _call(hass, "trace", {"pubkey_prefix": key[:6], "timeout": requested}, True)
    assert services.api.wait_for.call_args.args[-1] == expected


async def test_ui_message_returns_success(hass, services, monkeypatch):
    monkeypatch.setattr("custom_components.meshcore.services.time.time", lambda: 1700000000)
    _helper(hass, services.entry, "recipient_type", "Channel")
    _helper(hass, services.entry, "channel_select", "Public", {"channel_idx": 0})
    _helper(hass, services.entry, "message_input", "hello", domain="text")
    services.radio.script[services.radio.key("send_chan_msg", 0, "hello", timestamp=1700000000)] = Event(EventType.MSG_SENT, {})
    assert await _call(hass, "send_ui_message", {}, True) == {"success": True}
    assert services.radio.calls[-1] == services.radio.key("send_chan_msg", 0, "hello", timestamp=1700000000)


@pytest.mark.parametrize("service,helper,command", [
    ("add_selected_contact", "discovered_contact_select", "add_contact"),
    ("remove_selected_contact", "added_contact_select", "remove_contact"),
    ("remove_discovered_contact", "discovered_contact_select", None),
])
@pytest.mark.parametrize("multiple", [False, True])
async def test_contact_helper_uses_resolved_entry(hass, services, service, helper, command, multiple):
    key = "ab" * 32
    contact = {"public_key": key}
    services.coordinator._discovered_contacts[key] = contact
    services.api.contacts[key] = contact
    _helper(hass, services.entry, helper, f"Node ({key[:12]})")
    if multiple:
        other = MockConfigEntry(domain=DOMAIN, entry_id="other")
        other.add_to_hass(hass)
        _helper(hass, other, helper, "Other (cccccccccccc)")
        # Put the other entry first so implicit radio selection would be wrong.
        hass.data[DOMAIN] = {"other": SimpleNamespace(api=StubSession(connected=True)),
                             services.entry.entry_id: services.coordinator}
    if command:
        services.radio.script[services.radio.key(command, contact)] = Event(EventType.ERROR, {})
    data = {"entry_id": services.entry.entry_id} if multiple else {}
    await _call(hass, service, data)
    if command:
        assert services.radio.calls[-1] == services.radio.key(command, contact)
    else:
        assert not services.coordinator._discovered_contacts


async def test_discovered_ambiguous_prefix_does_not_remove(hass, services):
    keys = ["ab" * 32, "ab" + "cd" * 31]
    services.coordinator._discovered_contacts.update({key: {"public_key": key} for key in keys})
    await _call(hass, "remove_discovered_contact", {"pubkey_prefix": "ab"})
    assert set(services.coordinator._discovered_contacts) == set(keys)


@pytest.mark.parametrize("expose", [False, True])
async def test_structured_get_channels_already_hides_secrets(hass, services, expose):
    hass.config_entries.async_update_entry(services.entry, options={"expose_secrets": expose})
    services.coordinator.update_telemetry_settings(services.entry)
    services.coordinator._max_channels = 1
    services.coordinator._channel_info = {0: {"channel_name": "Public", "channel_secret": "secretvalue"}}
    result = await _call(hass, "get_channels", {}, True)
    assert "secretvalue" not in str(result)
    assert result["channels"][0]["shared_secret_present"] is True
