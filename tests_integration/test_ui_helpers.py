"""UI helpers under real Home Assistant: stable targets, availability, seeding.

The pickers used to re-point at whatever sorted first when a label changed,
every helper went unavailable on a single failed tick, and the six radio-identity
sensors stayed blank because SELF_INFO fires before they subscribe.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Final
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.meshcore import radio as radio_module
from custom_components.meshcore.const import DOMAIN, SELECT_NO_CONTACTS
from custom_components.meshcore.radio import RadioSession
from custom_components.meshcore.select import (
    MeshCoreAddedContactSelect,
    MeshCoreChannelSelect,
    MeshCoreContactSelect,
    MeshCoreDiscoveredContactSelect,
    MeshCoreRecipientTypeSelect,
)
from custom_components.meshcore.sensor import SELF_INFO_FIELDS, SENSORS, MeshCoreSensor
from custom_components.meshcore.text import MeshCoreCommandInput, MeshCoreMessageInput
from tests.support.fake_radio import FakeRadio

NOW: Final = 1700000000
BOB: Final = "bb" * 32
AARON: Final = "aa" * 32


@pytest.fixture(autouse=True)
def fast_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the connect settle delay and freeze the time-sync argument."""
    monkeypatch.setattr(radio_module, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(
        radio_module,
        "time",
        SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic),
    )


def _contact(public_key: str, name: str, added: bool = True) -> dict[str, Any]:
    """Build a merged-contact record as the coordinator publishes one."""
    return {
        "public_key": public_key,
        "pubkey_prefix": public_key[:12],
        "adv_name": name,
        "added_to_node": added,
    }


def _coordinator(hass: HomeAssistant, contacts: list[dict], channels: dict) -> MagicMock:
    """A coordinator stub exposing only what the helper entities read."""
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="helpers")
    config_entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.config_entry = config_entry
    coordinator.last_update_success = False  # a tick has just failed
    coordinator.get_all_contacts.return_value = contacts
    coordinator._channel_info = channels
    coordinator._max_channels = max(len(channels), 1)
    coordinator.get_contact_by_prefix.side_effect = lambda prefix: next(
        (c for c in contacts if c["public_key"].startswith(prefix)), {}
    )
    return coordinator


def _refresh(entity: Any) -> None:
    """Run the coordinator-update callback without a live state machine."""
    with patch.object(entity, "async_write_ha_state"):
        entity._handle_coordinator_update()


def test_every_helper_stays_available_when_a_tick_fails(hass: HomeAssistant) -> None:
    """H-18: helpers hold the user's own input, so a dead link cannot hide them."""
    coordinator = _coordinator(hass, [_contact(BOB, "Bob")], {0: {}})
    helpers = [
        MeshCoreChannelSelect(coordinator),
        MeshCoreContactSelect(coordinator),
        MeshCoreRecipientTypeSelect(coordinator),
        MeshCoreDiscoveredContactSelect(coordinator),
        MeshCoreAddedContactSelect(coordinator),
        MeshCoreMessageInput(coordinator),
        MeshCoreCommandInput(coordinator),
    ]

    assert [helper.available for helper in helpers] == [True] * len(helpers)


def test_the_contact_picker_follows_a_rename(hass: HomeAssistant) -> None:
    """H-19: renaming the selected contact must not redirect the next message."""
    contacts = [_contact(BOB, "Bob"), _contact(AARON, "Aaron")]
    coordinator = _coordinator(hass, contacts, {0: {}})
    select = MeshCoreContactSelect(coordinator)
    _refresh(select)

    select._attr_current_option = f"Bob ({BOB[:12]})"
    contacts[0]["adv_name"] = "Zed"
    _refresh(select)

    assert select.current_option == f"Zed ({BOB[:12]})"
    assert select.extra_state_attributes["public_key_prefix"] == BOB[:12]


def test_the_contact_picker_clears_when_its_target_is_gone(
    hass: HomeAssistant,
) -> None:
    """H-19: a removed contact falls back to the placeholder, not to a stranger."""
    contacts = [_contact(BOB, "Bob"), _contact(AARON, "Aaron")]
    coordinator = _coordinator(hass, contacts, {0: {}})
    select = MeshCoreContactSelect(coordinator)
    select._attr_current_option = f"Bob ({BOB[:12]})"

    contacts.remove(contacts[0])
    _refresh(select)

    assert select.current_option == SELECT_NO_CONTACTS
    assert "public_key_prefix" not in select.extra_state_attributes


def test_a_new_contact_does_not_move_the_picker(hass: HomeAssistant) -> None:
    """H-19: a contact sorting before the selected one must not steal it."""
    contacts = [_contact(BOB, "Bob")]
    coordinator = _coordinator(hass, contacts, {0: {}})
    select = MeshCoreContactSelect(coordinator)
    select._attr_current_option = f"Bob ({BOB[:12]})"

    contacts.insert(0, _contact(AARON, "Aaron"))
    _refresh(select)

    assert select.current_option == f"Bob ({BOB[:12]})"


def test_the_channel_picker_follows_a_rename(hass: HomeAssistant) -> None:
    """H-19: renaming channel 2 kept sending to channel 0."""
    channels = {
        0: {"channel_name": "Public"},
        1: {"channel_name": "Ops"},
        2: {"channel_name": "Family"},
    }
    coordinator = _coordinator(hass, [], channels)
    select = MeshCoreChannelSelect(coordinator)
    select._attr_current_option = "Family (2)"

    channels[2]["channel_name"] = "Home"
    _refresh(select)

    assert select.current_option == "Home (2)"
    assert select.extra_state_attributes["channel_idx"] == 2


def test_the_discovered_picker_follows_a_rename(hass: HomeAssistant) -> None:
    """H-19: the discovered picker is keyed by prefix too."""
    contacts = [_contact(BOB, "Bob", added=False)]
    coordinator = _coordinator(hass, contacts, {0: {}})
    select = MeshCoreDiscoveredContactSelect(coordinator)
    select._attr_current_option = f"Bob ({BOB[:12]})"

    contacts[0]["adv_name"] = "Zed"
    _refresh(select)

    assert select.current_option == f"Zed ({BOB[:12]})"


def test_the_added_picker_clears_when_its_target_is_gone(hass: HomeAssistant) -> None:
    """H-19: the added picker returns to its placeholder, never to a stranger."""
    contacts = [_contact(BOB, "Bob"), _contact(AARON, "Aaron")]
    coordinator = _coordinator(hass, contacts, {0: {}})
    select = MeshCoreAddedContactSelect(coordinator)
    select._attr_current_option = f"Bob ({BOB[:12]})"

    contacts.remove(contacts[0])
    _refresh(select)

    assert select.current_option == SELECT_NO_CONTACTS


async def test_self_info_sensors_fill_at_setup_and_after_a_reconnect(
    hass: HomeAssistant,
) -> None:
    """H-35: SELF_INFO fires before these sensors exist, on every connect."""
    first = FakeRadio()
    second = FakeRadio()
    for radio, power in ((first, 17), (second, 20)):
        await radio.start()
        radio.script[("send_appstart",)] = Event(
            EventType.SELF_INFO,
            {
                "name": "Hub",
                "tx_power": power,
                "adv_lat": 1.5,
                "adv_lon": -2.5,
                "radio_freq": 910.525,
                "radio_bw": 62.5,
                "radio_sf": 7,
            },
        )
        radio.script[("set_time", NOW)] = Event(EventType.OK, {})

    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="selfinfo")
    config_entry.add_to_hass(hass)
    platform = MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN)
    platform.config_entry = config_entry

    with (
        patch.object(radio_module, "RECONNECT_BACKOFF", (0.01,)),
        patch.object(radio_module.MeshCore, "create_tcp", side_effect=[first, second]),
    ):
        session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
        assert await session.start()

        coordinator = MagicMock()
        coordinator.config_entry = config_entry
        coordinator.api = session
        coordinator.name = "Hub"
        coordinator.pubkey = "cc" * 32
        coordinator.last_update_success = True

        sensors = [
            MeshCoreSensor(coordinator, description)
            for description in SENSORS
            if description.key in SELF_INFO_FIELDS
        ]
        platform._async_schedule_add_entities(sensors)
        await hass.async_block_till_done()

        values = {s.entity_description.key: s.native_value for s in sensors}
        assert values == {
            "tx_power": 17,
            "latitude": 1.5,
            "longitude": -2.5,
            "frequency": 910.525,
            "bandwidth": 62.5,
            "spreading_factor": 7,
        }

        await first.drop_link()
        deadline = time.monotonic() + 5
        while not session.connected:
            assert time.monotonic() < deadline, "session never reconnected"
            await asyncio.sleep(0.01)
        await hass.async_block_till_done()

        assert sensors[0].native_value == 20

        await session.close()

    await first.close()
    await second.close()


def test_the_self_info_table_matches_the_sensor_keys() -> None:
    """Every seeded key is a real main-device sensor."""
    keys = {description.key for description in SENSORS}
    assert set(SELF_INFO_FIELDS) <= keys
