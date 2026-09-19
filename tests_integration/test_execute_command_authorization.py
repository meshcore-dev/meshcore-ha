"""Authorization tests for the generic MeshCore command services."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized, UnknownUser

from custom_components.meshcore.button import MeshCoreCLIRunButton
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.services import async_setup_services


@pytest.fixture
async def command_services(hass):
    """Register command services backed by a fake connected radio."""
    # Ensure later non-admin users do not become HA's first-user owner.
    await hass.auth.async_create_user("owner", group_ids=[GROUP_ID_ADMIN])

    commands = MagicMock()
    commands.export_private_key = AsyncMock(return_value={"private_key": "secret"})

    coordinator = MagicMock()
    coordinator.api.connected = True
    coordinator.api.mesh_core.commands = commands
    coordinator.api.self_info = {"suggested_timeout": 1000}
    coordinator._discovered_contacts = {}
    coordinator.pubkey = "abcdef123456"
    coordinator.config_entry.entry_id = "entry1"
    hass.data[DOMAIN] = {"entry1": coordinator}

    await async_setup_services(hass)
    return coordinator


async def _user_context(hass, name: str, group_id: str) -> Context:
    """Create a user in the requested group and return its service context."""
    user = await hass.auth.async_create_user(name, group_ids=[group_id])
    return Context(user_id=user.id)


@pytest.mark.asyncio
async def test_execute_command_requires_admin(hass, command_services):
    """Reject a non-admin before dispatching a radio command."""
    context = await _user_context(hass, "regular user", GROUP_ID_USER)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "execute_command",
            {"command": "export_private_key"},
            blocking=True,
            return_response=True,
            context=context,
        )

    command_services.api.mesh_core.commands.export_private_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_command_rejects_unknown_user(hass, command_services):
    """Reject a missing or deleted user before dispatching a radio command."""
    with pytest.raises(UnknownUser):
        await hass.services.async_call(
            DOMAIN,
            "execute_command",
            {"command": "export_private_key"},
            blocking=True,
            return_response=True,
            context=Context(user_id="deleted-user"),
        )

    command_services.api.mesh_core.commands.export_private_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_command_allows_admin(hass, command_services):
    """Allow an admin to dispatch a radio command."""
    context = await _user_context(hass, "admin user", GROUP_ID_ADMIN)

    response = await hass.services.async_call(
        DOMAIN,
        "execute_command",
        {"command": "export_private_key"},
        blocking=True,
        return_response=True,
        context=context,
    )

    assert response == {"private_key": "secret"}
    command_services.api.mesh_core.commands.export_private_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_command_ui_preserves_non_admin_context(hass, command_services):
    """Reject the UI wrapper caller before dispatching a radio command."""
    state = MagicMock(state="export_private_key")
    hass.states.async_set("text.meshcore_command", state.state)
    context = await _user_context(hass, "ui user", GROUP_ID_USER)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "execute_command_ui",
            {},
            blocking=True,
            return_response=True,
            context=context,
        )

    command_services.api.mesh_core.commands.export_private_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_command_ui_dispatches_with_admin_context(hass, command_services):
    """Allow the UI wrapper for an admin and dispatch once."""
    hass.states.async_set("text.meshcore_command", "export_private_key")
    context = await _user_context(hass, "ui admin", GROUP_ID_ADMIN)

    response = await hass.services.async_call(
        DOMAIN,
        "execute_command_ui",
        {},
        blocking=True,
        return_response=True,
        context=context,
    )

    assert response == {"private_key": "secret"}
    command_services.api.mesh_core.commands.export_private_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_cli_run_button_rejects_non_admin(hass, command_services):
    """Forward button caller context so a non-admin cannot bypass the service."""
    hass.states.async_set("text.meshcore_command", "export_private_key")
    context = await _user_context(hass, "button user", GROUP_ID_USER)
    button = MeshCoreCLIRunButton(command_services)
    button.hass = hass
    button._context = context

    with pytest.raises(Unauthorized):
        await button.async_press()

    command_services.api.mesh_core.commands.export_private_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_run_button_allows_admin(hass, command_services):
    """Forward button caller context and dispatch for an admin."""
    hass.states.async_set("text.meshcore_command", "export_private_key")
    context = await _user_context(hass, "button admin", GROUP_ID_ADMIN)
    button = MeshCoreCLIRunButton(command_services)
    button.hass = hass
    button._context = context

    await button.async_press()

    command_services.api.mesh_core.commands.export_private_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_command_allows_internal_call_without_user(
    hass, command_services
):
    """Keep Home Assistant's admin-service behavior for trusted internal calls."""
    response = await hass.services.async_call(
        DOMAIN,
        "execute_command",
        {"command": "export_private_key"},
        blocking=True,
        return_response=True,
    )

    assert response == {"private_key": "secret"}
    command_services.api.mesh_core.commands.export_private_key.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_command_ui_allows_internal_call_without_user(
    hass, command_services
):
    """Keep trusted internal calls working through the UI wrapper."""
    hass.states.async_set("text.meshcore_command", "export_private_key")

    response = await hass.services.async_call(
        DOMAIN,
        "execute_command_ui",
        {},
        blocking=True,
        return_response=True,
    )

    assert response == {"private_key": "secret"}
    command_services.api.mesh_core.commands.export_private_key.assert_awaited_once()
