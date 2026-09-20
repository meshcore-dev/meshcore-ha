"""What the services refuse, and what they touch when they act.

Every one of these was a way for a service call to do something other than
what the caller asked: send to nobody, trace a contact and time out on its own
reply, delete another radio's entities, or run a command that resets the node.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import entity_registry as er
from meshcore.events import Event, EventType

from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.services import async_setup_services
from tests.support.session import StubSession


def _coordinator(entry_id: str, contact: dict | None = None) -> SimpleNamespace:
    """A connected coordinator whose radio answers everything successfully."""
    commands = MagicMock()
    commands.get_time = AsyncMock(return_value=Event(EventType.OK, {"time": 1}))
    commands.reboot = AsyncMock(return_value=Event(EventType.OK, {}))
    api = StubSession(
        commands,
        connected=True,
        self_info={"suggested_timeout": 1000},
        contact_by_prefix=MagicMock(return_value=contact),
        contact_by_name=MagicMock(return_value=contact),
    )
    api.path_discovery = AsyncMock(return_value=(None, None))
    return SimpleNamespace(
        api=api,
        name=entry_id,
        pubkey="cc" * 32,
        config_entry=SimpleNamespace(entry_id=entry_id),
        _discovered_contacts={},
        require_mesh_budget=lambda *args, **kwargs: None,
        record_cli_console=MagicMock(),
        get_contact_by_prefix=lambda prefix: contact or {},
    )


@pytest.fixture
async def services(hass: HomeAssistant) -> SimpleNamespace:
    """Register the services over one connected radio with one contact."""
    await hass.auth.async_create_user("owner", group_ids=[GROUP_ID_ADMIN])
    contact = {
        "public_key": "b" * 64,
        "adv_name": "Client",
        "added_to_node": True,
        "out_path_len": -1,
        "out_path_hash_mode": 0,
    }
    coordinator = _coordinator("entry1", contact)
    hass.data[DOMAIN] = {"entry1": coordinator}
    await async_setup_services(hass)
    return SimpleNamespace(coordinator=coordinator, contact=contact)


async def test_send_message_without_a_recipient_is_refused(
    hass: HomeAssistant, services: SimpleNamespace
) -> None:
    """A call naming neither target says so instead of raising KeyError."""
    with pytest.raises(HomeAssistantError) as refusal:
        await hass.services.async_call(
            DOMAIN, "send_message", {"message": "Hello"}, blocking=True
        )

    assert refusal.value.translation_key == "send_message_target_required"


async def test_a_denied_command_never_reaches_the_radio(
    hass: HomeAssistant, services: SimpleNamespace
) -> None:
    """Commands that reset the node are refused with a translated error."""
    with pytest.raises(HomeAssistantError) as refusal:
        await hass.services.async_call(
            DOMAIN, "execute_command", {"command": "reboot"}, blocking=True
        )

    assert refusal.value.translation_key == "command_denied"
    services.coordinator.api.commands.reboot.assert_not_awaited()


async def test_an_allowed_command_still_runs(
    hass: HomeAssistant, services: SimpleNamespace
) -> None:
    """The denial list is a list, not a gate on everything."""
    response = await hass.services.async_call(
        DOMAIN,
        "execute_command",
        {"command": "get_time"},
        blocking=True,
        return_response=True,
    )

    assert response == {"time": 1}


async def test_contact_services_require_an_admin(
    hass: HomeAssistant, services: SimpleNamespace
) -> None:
    """Adding or removing a contact changes the node: admins only."""
    user = await hass.auth.async_create_user("regular", group_ids=[GROUP_ID_USER])
    context = Context(user_id=user.id)

    for service in ("add_selected_contact", "remove_selected_contact"):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, service, {}, blocking=True, context=context
            )


async def test_trace_waits_on_the_contacts_own_prefix(
    hass: HomeAssistant, services: SimpleNamespace
) -> None:
    """A six-character request must listen for the key the firmware echoes."""
    result = await hass.services.async_call(
        DOMAIN,
        "trace",
        {"pubkey_prefix": "bbbbbb"},
        blocking=True,
        return_response=True,
    )

    assert result["error"] == "path_discovery_failed"
    _, kwargs = services.coordinator.api.path_discovery.call_args
    assert "identity" not in kwargs, "the session derives the canonical prefix"


async def test_cleanup_only_touches_the_named_entrys_contacts(
    hass: HomeAssistant, services: SimpleNamespace
) -> None:
    """Unavailable neighbours of other entries, and other sensors, survive."""
    registry = er.async_get(hass)
    kept: list[str] = []
    removed: list[str] = []
    for entry_id, unique_id, doomed in (
        ("entry1", "entry1_contact_aaaaaaaaaaaa", removed),
        ("entry1", "entry1_ch_1_messages", kept),
        ("entry1", "entry1_mqtt_broker_1", kept),
        ("entry2", "entry2_contact_bbbbbbbbbbbb", kept),
    ):
        entity = registry.async_get_or_create(
            "binary_sensor", DOMAIN, unique_id, config_entry=_entry(hass, entry_id)
        )
        hass.states.async_set(entity.entity_id, "unavailable")
        doomed.append(entity.entity_id)

    await hass.services.async_call(
        DOMAIN, "cleanup_unavailable_contacts", {"entry_id": "entry1"}, blocking=True
    )

    assert [registry.async_get(entity_id) for entity_id in removed] == [None]
    assert all(registry.async_get(entity_id) for entity_id in kept)


def _entry(hass: HomeAssistant, entry_id: str):
    """Return a config entry the registry will accept as an owner."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = hass.config_entries.async_get_entry(entry_id)
    if existing is not None:
        return existing
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, data={})
    entry.add_to_hass(hass)
    return entry
