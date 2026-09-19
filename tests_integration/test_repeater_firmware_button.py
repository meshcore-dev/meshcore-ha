"""Real Home Assistant tests for repeater firmware refresh buttons."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from meshcore.events import EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.button import (
    MeshCoreRepeaterFirmwareRefreshButton,
    async_setup_entry,
)
from custom_components.meshcore.const import CONF_REPEATER_SUBSCRIPTIONS, DOMAIN

PREFIX_ONE = "aabbccddeeff"
PREFIX_TWO = "112233445566"


class _Dispatcher:
    """Minimal dispatcher implementing the query helper's subscription API."""

    def __init__(self, event=None):
        self.event = event
        self.listener_registered = False
        self.callback = None
        self.filters = None

    def subscribe(self, event_type, callback, attribute_filters):
        self.listener_registered = True
        self.callback = callback
        self.filters = attribute_filters
        dispatcher = self

        class _Subscription:
            def unsubscribe(self):
                dispatcher.callback = None

        return _Subscription()

    def dispatch_reply(self):
        if self.event is None or self.callback is None:
            return
        if all(
            self.event.attributes.get(key) == value
            for key, value in self.filters.items()
        ):
            self.callback(self.event)


def _event(prefix: str, text: str):
    return SimpleNamespace(
        type=EventType.CONTACT_MSG_RECV,
        payload={"text": text},
        attributes={"pubkey_prefix": prefix},
    )


def _meshcore(prefix: str, *, event=None, send_error=False, login_timeout=False):
    contact = {"public_key": prefix + "0" * 52}
    dispatcher = _Dispatcher(event)

    async def send_login_sync(sent_contact, password):
        assert sent_contact is contact
        assert password == "secret"
        if login_timeout:
            raise TimeoutError
        return SimpleNamespace(type=EventType.LOGIN_SUCCESS)

    async def send_cmd(sent_contact, command):
        assert dispatcher.listener_registered
        assert sent_contact is contact
        assert command == "ver"
        result = SimpleNamespace(
            type=EventType.ERROR if send_error else EventType.MSG_SENT,
            payload={},
        )
        if not send_error:
            dispatcher.dispatch_reply()
        return result

    return SimpleNamespace(
        dispatcher=dispatcher,
        commands=SimpleNamespace(
            send_login_sync=send_login_sync,
            send_cmd=send_cmd,
        ),
        get_contact_by_key_prefix=lambda requested: (
            contact if requested == prefix else None
        ),
    )


def _repeater(name: str, prefix: str, version: str) -> dict:
    return {
        "name": name,
        "pubkey_prefix": prefix,
        "password": "secret",
        "firmware_version": version,
    }


async def _setup_buttons(hass, meshcore):
    repeaters = [
        _repeater("One", PREFIX_ONE, "1.0.0"),
        _repeater("Two", PREFIX_TWO, "2.0.0"),
    ]
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="hub-one",
        data={CONF_REPEATER_SUBSCRIPTIONS: repeaters},
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.config_entry = entry
    coordinator.api.mesh_core = meshcore
    coordinator.pubkey = "hubpubkey"
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entities = []
    await async_setup_entry(hass, entry, entities.extend)
    buttons = [
        entity
        for entity in entities
        if isinstance(entity, MeshCoreRepeaterFirmwareRefreshButton)
    ]
    for button in buttons:
        button.hass = hass
    return entry, buttons


@pytest.mark.asyncio
async def test_setup_targets_each_repeater_on_its_hub(hass):
    """Create one button per configured repeater with hub-scoped identifiers."""
    _, buttons = await _setup_buttons(hass, _meshcore(PREFIX_ONE))

    assert [button.pubkey_prefix for button in buttons] == [PREFIX_ONE, PREFIX_TWO]
    assert buttons[0].device_info["identifiers"] == {
        (DOMAIN, f"hub-one_repeater_{PREFIX_ONE}")
    }
    assert buttons[1].device_info["identifiers"] == {
        (DOMAIN, f"hub-one_repeater_{PREFIX_TWO}")
    }


@pytest.mark.asyncio
async def test_press_updates_config_entry_and_repeater_device(hass):
    """Persist a successful reply only to the selected repeater and device."""
    version = "MeshCore 1.14.2 (Build: Sep 18 2026)"
    meshcore = _meshcore(PREFIX_ONE, event=_event(PREFIX_ONE, version))
    entry, buttons = await _setup_buttons(hass, meshcore)
    registry = dr.async_get(hass)
    target = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"hub-one_repeater_{PREFIX_ONE}")},
        name="Repeater One",
        sw_version="1.0.0",
    )
    other = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"hub-one_repeater_{PREFIX_TWO}")},
        name="Repeater Two",
        sw_version="2.0.0",
    )

    await buttons[0].async_press()

    repeaters = entry.data[CONF_REPEATER_SUBSCRIPTIONS]
    assert repeaters[0]["firmware_version"] == version
    assert repeaters[1]["firmware_version"] == "2.0.0"
    assert registry.async_get(target.id).sw_version == version
    assert registry.async_get(other.id).sw_version == "2.0.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_press_failure_preserves_previous_versions(hass, failure):
    """Keep config and registry versions on timeout or command rejection."""
    meshcore = _meshcore(
        PREFIX_ONE,
        event=None,
        send_error=failure == "error",
        login_timeout=failure == "timeout",
    )
    entry, buttons = await _setup_buttons(hass, meshcore)
    registry = dr.async_get(hass)
    target = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"hub-one_repeater_{PREFIX_ONE}")},
        name="Repeater One",
        sw_version="1.0.0",
    )

    with pytest.raises(HomeAssistantError):
        await buttons[0].async_press()

    assert entry.data[CONF_REPEATER_SUBSCRIPTIONS][0]["firmware_version"] == "1.0.0"
    assert registry.async_get(target.id).sw_version == "1.0.0"
