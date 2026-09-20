"""Multi-hub UI helper resolution and radio dispatch tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from meshcore.events import Event, EventType

from custom_components.meshcore.const import (
    ATTR_CHANNEL_IDX,
    ATTR_ENTRY_ID,
    ATTR_MESSAGE,
    DOMAIN,
    SERVICE_EXECUTE_COMMAND_UI,
    SERVICE_MESSAGE_SCRIPT,
    SERVICE_SEND_CHANNEL_MESSAGE,
)
from custom_components.meshcore.services import async_setup_services
from tests.support.session import stub_session


def _coordinator(contacts=None):
    commands = SimpleNamespace(
        get_bat=AsyncMock(return_value=Event(EventType.BATTERY, {"level": 50})),
        send_chan_msg=AsyncMock(return_value=Event(EventType.OK, {})),
        send_msg=AsyncMock(return_value=Event(EventType.ERROR, {"reason": "test"})),
    )
    mesh_core = SimpleNamespace(
        commands=commands,
        get_contact_by_key_prefix=MagicMock(
            side_effect=lambda prefix: (contacts or {}).get(prefix)
        ),
        get_contact_by_name=MagicMock(return_value=None),
    )
    return SimpleNamespace(
        api=SimpleNamespace(connected=True, mesh_core=mesh_core, session=stub_session()),
        name="test",
    )


def _helper(
    hass: HomeAssistant,
    entry_id: str,
    domain: str,
    suffix: str,
    state: str,
    attributes=None,
    renamed_entity_id: str | None = None,
) -> str:
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain,
        DOMAIN,
        f"{entry_id}_{suffix}",
        suggested_object_id=f"{entry_id}_{suffix}",
    )
    entity_id = entry.entity_id
    if renamed_entity_id:
        registry.async_update_entity(entity_id, new_entity_id=renamed_entity_id)
        entity_id = renamed_entity_id
    hass.states.async_set(entity_id, state, attributes or {})
    return entity_id


async def test_ui_channel_uses_selected_hub_and_renamed_helpers(hass: HomeAssistant):
    """The selected hub supplies both helper values and the transmitting radio."""
    clear_calls = []

    async def capture_clear(call):
        clear_calls.append(dict(call.data))

    hass.services.async_register("text", "set_value", capture_clear)
    hub_a = _coordinator()
    hub_b = _coordinator()
    hass.data[DOMAIN] = {"hub_a": hub_a, "hub_b": hub_b}
    await async_setup_services(hass)

    _helper(hass, "hub_a", "select", "recipient_type", "Channel")
    _helper(hass, "hub_a", "select", "channel_select", "Alpha (1)", {"channel_idx": 1})
    _helper(hass, "hub_a", "text", "message_input", "wrong hub")

    _helper(
        hass,
        "hub_b",
        "select",
        "recipient_type",
        "Channel",
        renamed_entity_id="select.radio_b_recipient",
    )
    _helper(
        hass,
        "hub_b",
        "select",
        "channel_select",
        "Bravo (3)",
        {"channel_idx": 3},
        renamed_entity_id="select.radio_b_channel",
    )
    message_entity_id = _helper(
        hass,
        "hub_b",
        "text",
        "message_input",
        "from hub b",
        renamed_entity_id="text.radio_b_message",
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        {ATTR_ENTRY_ID: "hub_b"},
        blocking=True,
    )

    hub_b.api.mesh_core.commands.send_chan_msg.assert_awaited_once()
    args = hub_b.api.mesh_core.commands.send_chan_msg.await_args
    assert args.args[:2] == (3, "from hub b")
    hub_a.api.mesh_core.commands.send_chan_msg.assert_not_awaited()
    await hass.async_block_till_done()
    # The clear call must target the renamed helper, not a global entity ID.
    assert clear_calls == [{"entity_id": message_entity_id, "value": ""}]


async def test_ui_contact_and_direct_service_dispatch_to_requested_radios(
    hass: HomeAssistant,
):
    """Contact UI and direct channel calls preserve their explicit entry IDs."""
    contact_a = {"public_key": "aa" * 32, "adv_name": "alpha"}
    contact_b = {"public_key": "bb" * 32, "adv_name": "bravo"}
    hub_a = _coordinator({"aaaaaaaaaaaa": contact_a})
    hub_b = _coordinator({"bbbbbbbbbbbb": contact_b})
    hass.data[DOMAIN] = {"hub_a": hub_a, "hub_b": hub_b}
    await async_setup_services(hass)

    _helper(hass, "hub_a", "select", "recipient_type", "Contact")
    _helper(
        hass,
        "hub_a",
        "select",
        "contact_select",
        "alpha",
        {"public_key_prefix": "aaaaaaaaaaaa"},
    )
    _helper(hass, "hub_a", "text", "message_input", "direct alpha")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        {ATTR_ENTRY_ID: "hub_a"},
        blocking=True,
    )
    hub_a.api.mesh_core.commands.send_msg.assert_awaited_once_with(contact_a, "direct alpha")
    hub_b.api.mesh_core.commands.send_msg.assert_not_awaited()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_CHANNEL_MESSAGE,
        {ATTR_ENTRY_ID: "hub_b", ATTR_CHANNEL_IDX: 7, ATTR_MESSAGE: "direct service"},
        blocking=True,
    )
    hub_b.api.mesh_core.commands.send_chan_msg.assert_awaited_once()
    assert hub_b.api.mesh_core.commands.send_chan_msg.await_args.args[:2] == (
        7,
        "direct service",
    )
    hub_a.api.mesh_core.commands.send_chan_msg.assert_not_awaited()


async def test_execute_ui_uses_selected_entry_command_helper(hass: HomeAssistant):
    """Command helper selection and command dispatch stay on the same hub."""
    hub_a = _coordinator()
    hub_b = _coordinator()
    hass.data[DOMAIN] = {"hub_a": hub_a, "hub_b": hub_b}
    await async_setup_services(hass)
    _helper(hass, "hub_a", "text", "command_input", "reboot")
    _helper(
        hass,
        "hub_b",
        "text",
        "command_input",
        "get_bat",
        renamed_entity_id="text.radio_b_command",
    )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXECUTE_COMMAND_UI,
        {ATTR_ENTRY_ID: "hub_b"},
        blocking=True,
        return_response=True,
    )

    assert response == {"level": 50}
    hub_b.api.mesh_core.commands.get_bat.assert_awaited_once()
    hub_a.api.mesh_core.commands.get_bat.assert_not_awaited()


async def test_ui_service_rejects_ambiguous_or_unknown_entry(hass: HomeAssistant):
    """Multiple hubs never fall back to an arbitrary helper or radio."""
    hub_a = _coordinator()
    hub_b = _coordinator()
    hass.data[DOMAIN] = {"hub_a": hub_a, "hub_b": hub_b}
    await async_setup_services(hass)

    ambiguous = await hass.services.async_call(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        {},
        blocking=True,
        return_response=True,
    )
    unknown = await hass.services.async_call(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        {ATTR_ENTRY_ID: "missing"},
        blocking=True,
        return_response=True,
    )

    assert ambiguous["error"] == "ambiguous_config_entry"
    assert ambiguous["entry_ids"] == ["hub_a", "hub_b"]
    assert unknown["error"] == "config_entry_not_found"
    hub_a.api.mesh_core.commands.send_chan_msg.assert_not_awaited()
    hub_b.api.mesh_core.commands.send_chan_msg.assert_not_awaited()


async def test_ui_service_reports_missing_entry_owned_helper(hass: HomeAssistant):
    """A selected entry cannot borrow a helper registered to another hub."""
    hub_a = _coordinator()
    hub_b = _coordinator()
    hass.data[DOMAIN] = {"hub_a": hub_a, "hub_b": hub_b}
    await async_setup_services(hass)
    _helper(hass, "hub_a", "select", "recipient_type", "Channel")

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        {ATTR_ENTRY_ID: "hub_b"},
        blocking=True,
        return_response=True,
    )

    assert response["error"] == "helper_not_found"
    assert response["entry_id"] == "hub_b"
    assert response["helper"] == "recipient_type"


async def test_ui_services_reject_unavailable_or_unknown_input(hass: HomeAssistant):
    """HA sentinel states are never sent as messages or executed as commands."""
    hub = _coordinator()
    hass.data[DOMAIN] = {"hub_a": hub}
    await async_setup_services(hass)
    _helper(hass, "hub_a", "select", "recipient_type", "Channel")
    _helper(hass, "hub_a", "text", "message_input", STATE_UNAVAILABLE)
    _helper(hass, "hub_a", "text", "command_input", STATE_UNKNOWN)

    message_response = await hass.services.async_call(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        {ATTR_ENTRY_ID: "hub_a"},
        blocking=True,
        return_response=True,
    )
    command_response = await hass.services.async_call(
        DOMAIN,
        SERVICE_EXECUTE_COMMAND_UI,
        {ATTR_ENTRY_ID: "hub_a"},
        blocking=True,
        return_response=True,
    )

    assert message_response["error"] == "helper_state_unavailable"
    assert message_response["helper"] == "message"
    assert command_response["error"] == "helper_state_unavailable"
    assert command_response["helper"] == "command"
    hub.api.mesh_core.commands.send_chan_msg.assert_not_awaited()
    hub.api.mesh_core.commands.get_bat.assert_not_awaited()
