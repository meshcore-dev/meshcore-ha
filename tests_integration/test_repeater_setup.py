"""Drive repeater selection, route updates and retries through real HA forms."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.repeater_setup import contact_path, normalize_path

pytestmark = pytest.mark.usefixtures("recorder_mock")

PREFIX = "7ba6bb9f620e"
KEY = PREFIX + "00" * 26


@pytest.fixture
async def setup_flow(recorder_mock, hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, version=4, data={"name": "Hub"})
    entry.add_to_hass(hass)
    contact = {
        "public_key": KEY,
        "adv_name": "Jacinto North",
        "type": 2,
        "flags": 0,
        "out_path_len": 1,
        "out_path": "7a",
        "out_path_hash_mode": 0,
        "last_advert": 1,
        "adv_lat": 0,
        "adv_lon": 0,
    }
    other = {**contact, "public_key": "aa" * 32, "adv_name": "A first repeater"}
    table = {PREFIX: contact}
    session = SimpleNamespace(
        connected=True,
        contact_by_prefix=lambda prefix: table.get(prefix),
        login=AsyncMock(return_value=None),
        exchange=AsyncMock(return_value=Event(EventType.OK, {})),
        mark_contacts_dirty=Mock(),
        ensure_contacts=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        api=session,
        get_all_contacts=lambda: [other, contact],
        _contacts={},
        check_interactive_budget=Mock(return_value=0),
        mark_contact_dirty=Mock(),
        _publish_contacts=Mock(),
        set_repeater_firmware=Mock(),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"action": "add_repeater"}
    )
    assert result["step_id"] == "add_repeater"
    yield entry, coordinator, contact, table, result


async def select(hass, state):
    return await hass.config_entries.options.async_configure(
        state[-1]["flow_id"], {"repeater_name": PREFIX}
    )


async def submit(hass, result, **changes):
    values = {
        "repeater_path": "7A",
        "password": "secret",
        "disable_path_reset": True,
        "telemetry_enabled": True,
        "neighbors_enabled": False,
        "update_interval": 900,
    }
    values.update(changes)
    return await hass.config_entries.options.async_configure(result["flow_id"], values)


def defaults(result):
    return {field.schema: field.default() for field in result["data_schema"].schema}


async def test_prefills_saved_route(hass, setup_flow):
    result = await select(hass, setup_flow)
    assert result["step_id"] == "repeater_settings"
    assert result["description_placeholders"]["pubkey_prefix"] == PREFIX
    assert defaults(result)["repeater_path"] == "7A"
    setup_flow[1].api.login.assert_not_awaited()


async def test_failure_keeps_selection_and_every_field(hass, setup_flow):
    result = await submit(hass, await select(hass, setup_flow))
    assert result["step_id"] == "repeater_settings"
    assert result["errors"] == {"base": "login_failed"}
    assert result["description_placeholders"]["pubkey_prefix"] == PREFIX
    assert defaults(result) == {
        "repeater_path": "7A",
        "password": "secret",
        "disable_path_reset": True,
        "telemetry_enabled": True,
        "neighbors_enabled": False,
        "update_interval": 900,
    }
    assert setup_flow[1].api.login.await_count == 3
    assert setup_flow[0].options.get("repeater_subscriptions", []) == []


async def test_retry_succeeds_once(hass, setup_flow):
    entry, coordinator, _, _, _ = setup_flow
    coordinator.api.login.side_effect = [None, None, Event(EventType.LOGIN_SUCCESS, {})]
    with patch(
        "custom_components.meshcore.config_flow.OptionsFlowHandler._query_repeater_firmware",
        new=AsyncMock(return_value="Unknown"),
    ):
        result = await submit(hass, await select(hass, setup_flow))
    assert result["step_id"] == "init"
    assert coordinator.api.login.await_count == 3
    saved = entry.options["repeater_subscriptions"]
    assert len(saved) == 1
    assert saved[0]["pubkey_prefix"] == PREFIX
    assert saved[0]["disable_path_reset"] is True
    coordinator.api.exchange.assert_not_awaited()


async def test_changed_path_saved_before_login(hass, setup_flow):
    _, coordinator, contact, _, _ = setup_flow
    calls = []

    async def exchange(command, updated, path):
        assert command == "change_contact_path"
        assert path == "7b:0"
        assert updated is not contact
        updated.update(out_path="7b", out_path_len=1, out_path_hash_mode=0)
        calls.append("path")
        return Event(EventType.OK, {})

    async def login(c, password):
        assert c["out_path"] == "7b"
        calls.append("login")
        return Event(EventType.LOGIN_SUCCESS, {})

    coordinator.api.exchange.side_effect = exchange
    coordinator.api.login.side_effect = login
    with patch(
        "custom_components.meshcore.config_flow.OptionsFlowHandler._query_repeater_firmware",
        new=AsyncMock(return_value="Unknown"),
    ):
        result = await submit(hass, await select(hass, setup_flow), repeater_path="7B")
    assert result["step_id"] == "init"
    assert calls == ["path", "login"]


async def test_invalid_path_preserved_without_io(hass, setup_flow):
    result = await submit(hass, await select(hass, setup_flow), repeater_path="XYZ")
    assert result["errors"] == {"repeater_path": "invalid_path"}
    assert defaults(result)["repeater_path"] == "XYZ"
    setup_flow[1].api.exchange.assert_not_awaited()
    setup_flow[1].api.login.assert_not_awaited()


async def test_rejected_path_does_not_mutate_live_contact(hass, setup_flow):
    _, coordinator, contact, _, _ = setup_flow

    async def fail(command, updated, path):
        updated["out_path"] = "7b"
        return Event(EventType.ERROR, {})

    coordinator.api.exchange.side_effect = fail
    result = await submit(hass, await select(hass, setup_flow), repeater_path="7B")
    assert result["errors"] == {"base": "path_update_failed"}
    assert contact["out_path"] == "7a"
    coordinator.api.login.assert_not_awaited()


async def test_discovered_contact_saved_and_verified_before_login(hass, setup_flow):
    _, coordinator, contact, table, _ = setup_flow
    table.clear()
    order = []

    async def add(command, saved):
        assert command == "add_contact"
        assert saved["public_key"] == KEY
        order.append("save")
        return Event(EventType.OK, {})

    async def sync():
        order.append("verify")
        table[PREFIX] = dict(contact)

    async def login(c, password):
        order.append("login")
        return Event(EventType.LOGIN_SUCCESS, {})

    coordinator.api.exchange.side_effect = add
    coordinator.api.ensure_contacts.side_effect = sync
    coordinator.api.login.side_effect = login
    with patch(
        "custom_components.meshcore.config_flow.OptionsFlowHandler._query_repeater_firmware",
        new=AsyncMock(return_value="Unknown"),
    ):
        result = await submit(hass, await select(hass, setup_flow))
    assert result["step_id"] == "init"
    assert order == ["save", "verify", "login"]


@pytest.mark.parametrize("accepted", [False, True])
async def test_save_failure_or_unverified_save_blocks_login(hass, setup_flow, accepted):
    _, coordinator, _, table, _ = setup_flow
    table.clear()
    coordinator.api.exchange.return_value = Event(EventType.OK if accepted else EventType.ERROR, {})
    result = await submit(hass, await select(hass, setup_flow))
    assert result["errors"] == {"base": "contact_save_failed"}
    coordinator.api.login.assert_not_awaited()


async def test_retry_respects_budget(hass, setup_flow):
    _, coordinator, _, _, _ = setup_flow
    coordinator.check_interactive_budget.side_effect = [0, 45]
    result = await submit(hass, await select(hass, setup_flow))
    assert result["errors"] == {"base": "traffic_deferred"}
    assert coordinator.api.login.await_count == 1


async def test_blank_path_explicitly_switches_to_flood(hass, setup_flow):
    _, coordinator, contact, _, _ = setup_flow
    result = await submit(hass, await select(hass, setup_flow), repeater_path="")
    command, updated = coordinator.api.exchange.call_args.args
    assert command == "add_contact"
    assert updated["out_path_len"] == -1
    assert contact_path(contact) == ""
    assert result["errors"] == {"base": "login_failed"}


@pytest.mark.parametrize(
    "value,expected",
    [
        ("7A", "7a:0"),
        ("7A:0", "7a:0"),
        ("1234:1", "1234:1"),
        ("123456:2", "123456:2"),
        ("direct", ":0"),
        ("", ""),
    ],
)
def test_valid_paths(value, expected):
    assert normalize_path(value) == expected


@pytest.mark.parametrize("value", ["7", "GG", "7a:3", "7a:1", "1234:2", "aa" * 64, "aabb:0:0"])
def test_invalid_paths(value):
    with pytest.raises(ValueError):
        normalize_path(value)


async def test_disconnected_keeps_form_values(hass, setup_flow):
    result = await select(hass, setup_flow)
    setup_flow[1].api.connected = False
    result = await submit(hass, result)
    assert result["errors"] == {"base": "cannot_connect"}
    assert defaults(result)["password"] == "secret"
    assert result["description_placeholders"]["pubkey_prefix"] == PREFIX
    setup_flow[1].api.login.assert_not_awaited()


@pytest.mark.parametrize(
    "hops,route,mode,expected",
    [
        (-1, "", -1, ""),
        (0, "", 0, "direct"),
        (1, "7a" + "00" * 63, 0, "7A"),
        (1, "aabb", 1, "AABB:1"),
        (2, "aabbccddeeff", 2, "AABBCCDDEEFF:2"),
    ],
)
def test_saved_path_display(hops, route, mode, expected):
    assert (
        contact_path({"out_path_len": hops, "out_path": route, "out_path_hash_mode": mode})
        == expected
    )
