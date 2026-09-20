"""Entry migration, identity and removal under a real Home Assistant."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import (
    async_migrate_entry,
    async_remove_entry,
    async_update_options,
)
from custom_components.meshcore.const import (
    CONF_CONNECTION_TYPE,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONNECTION_TYPE_TCP,
    DOMAIN,
)

PUBKEY = "ab" * 32
V3_DATA = {
    "connection_type": "tcp",
    "tcp_host": "10.0.0.5",
    "tcp_port": 5000,
    "name": "Hub",
    "pubkey": PUBKEY.upper(),
    "contact_discovery_mode": "data_only",
    "self_telemetry_enabled": True,
    "self_telemetry_interval": 600,
    "stale_contact_days": 30,
    "consume_incoming_messages": False,
    "repeater_subscriptions": [
        {
            "name": "Hilltop",
            "pubkey_prefix": "AABBCCDDEEFF",
            "password": "secret",
            "update_interval": 900,
            "firmware_version": "1.14.2 (Build: x)",
        },
        {"name": "Short", "pubkey_prefix": "C0FFEE"},
    ],
    "tracked_clients": [{"name": "Handheld", "pubkey_prefix": "BBBBBBBBBBBB"}],
    "mqtt_brokers": {
        "1": {"enabled": True, "server": "mqtt.example", "iata": "PDX"},
        "2": {"enabled": False, "server": "", "client_id_prefix": "custom_"},
    },
}


def _v3_entry(hass: HomeAssistant, **kwargs) -> MockConfigEntry:
    """Add a version 3 entry shaped like a real installation."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, data=dict(V3_DATA), **kwargs)
    entry.add_to_hass(hass)
    return entry


async def test_v3_entry_moves_settings_to_options(hass: HomeAssistant) -> None:
    """Settings land in options, connection identity stays in data."""
    entry = _v3_entry(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 4
    assert entry.options["contact_discovery_mode"] == "data_only"
    assert entry.options["self_telemetry_interval"] == 600
    assert entry.options["consume_incoming_messages"] is False
    assert entry.options["mqtt_brokers"]["2"]["client_id_prefix"] == "custom_"
    assert CONF_TCP_HOST not in entry.options
    assert entry.data[CONF_TCP_HOST] == "10.0.0.5"
    # The data copies stay behind so a downgrade still finds the settings.
    assert entry.data["contact_discovery_mode"] == "data_only"


async def test_v3_entry_normalises_prefixes_and_claims_its_radio(
    hass: HomeAssistant,
) -> None:
    """Stored prefixes lower-case at their stored width; the pubkey is the identity."""
    entry = _v3_entry(hass)

    await async_migrate_entry(hass, entry)

    repeaters = entry.options["repeater_subscriptions"]
    assert [item["pubkey_prefix"] for item in repeaters] == ["aabbccddeeff", "c0ffee"]
    assert entry.options["tracked_clients"][0]["pubkey_prefix"] == "bbbbbbbbbbbb"
    assert entry.unique_id == PUBKEY


async def test_migration_moves_firmware_to_the_device_registry(
    hass: HomeAssistant,
) -> None:
    """The observed version leaves the entry and seeds the repeater device."""
    entry = _v3_entry(hass, entry_id="hub-one")
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "hub-one_repeater_aabbccddeeff")},
        name="Hilltop",
    )

    await async_migrate_entry(hass, entry)

    assert registry.async_get(device.id).sw_version == "1.14.2 (Build: x)"
    for mapping in (entry.data, entry.options):
        assert "firmware_version" not in mapping["repeater_subscriptions"][0]


async def test_migration_is_idempotent(hass: HomeAssistant) -> None:
    """Re-running on a migrated entry changes nothing."""
    entry = _v3_entry(hass)
    await async_migrate_entry(hass, entry)
    options = dict(entry.options)
    data = dict(entry.data)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 4
    assert dict(entry.options) == options
    assert dict(entry.data) == data


async def test_future_version_is_refused(hass: HomeAssistant) -> None:
    """A downgrade from a newer entry version is refused, not guessed at."""
    entry = MockConfigEntry(domain=DOMAIN, version=5, data={})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False


async def test_duplicate_radio_is_refused(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """A second entry for the same radio aborts instead of being created."""
    entry = _v3_entry(hass)
    await async_migrate_entry(hass, entry)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP}
    )
    with patch(
        "custom_components.meshcore.config_flow.validate_tcp_input",
        return_value={"title": "MeshCore Hub", "name": "Hub", "pubkey": PUBKEY},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TCP_HOST: "10.0.0.9", CONF_TCP_PORT: 5000}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_new_entry_claims_its_radio_and_splits_settings(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """A fresh install stores identity in data and its settings in options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP}
    )
    with patch(
        "custom_components.meshcore.config_flow.validate_tcp_input",
        return_value={"title": "MeshCore Hub", "name": "Hub", "pubkey": PUBKEY},
    ), patch("custom_components.meshcore.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TCP_HOST: "10.0.0.9", CONF_TCP_PORT: 5000}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.version == 4
    assert entry.unique_id == PUBKEY
    assert entry.data[CONF_TCP_HOST] == "10.0.0.9"
    assert entry.options["repeater_subscriptions"] == []
    assert entry.options["contact_discovery_mode"] == "full"


async def test_remove_entry_deletes_only_its_own_stores(
    hass: HomeAssistant, hass_storage
) -> None:
    """Removing an entry takes its three stores and its repair issue with it."""
    entry = _v3_entry(hass, entry_id="hub-one")
    other = {"version": 1, "data": {}}
    for key in (
        "meshcore.hub-one.discovered_contacts",
        "meshcore.hub-one.neighbor_data",
        "meshcore.traffic_hub-one",
        "meshcore.hub-two.discovered_contacts",
    ):
        hass_storage[key] = dict(other)

    await async_remove_entry(hass, entry)

    assert "meshcore.hub-one.discovered_contacts" not in hass_storage
    assert "meshcore.hub-one.neighbor_data" not in hass_storage
    assert "meshcore.traffic_hub-one" not in hass_storage
    assert "meshcore.hub-two.discovered_contacts" in hass_storage


@pytest.mark.parametrize("new_host", ["10.0.0.9", "10.0.0.5"])
async def test_reconfigure_reloads_exactly_once(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant, new_host
) -> None:
    """A reconfigure sets the entry up once more, whether or not it changed."""
    entry = _v3_entry(hass)
    await async_migrate_entry(hass, entry)
    setups: list[str] = []

    async def fake_setup(hass_in, entry_in):
        """Stand in for setup, keeping only the entry update listener."""
        setups.append(entry_in.entry_id)
        entry_in.async_on_unload(entry_in.add_update_listener(async_update_options))
        return True

    with patch(
        "custom_components.meshcore.async_setup_entry", side_effect=fake_setup
    ), patch("custom_components.meshcore.async_unload_entry", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert setups == [entry.entry_id]

        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP}
        )
        with patch(
            "custom_components.meshcore.config_flow.validate_tcp_input",
            return_value={"title": "MeshCore Hub", "name": "Hub", "pubkey": PUBKEY},
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_TCP_HOST: new_host, CONF_TCP_PORT: 5000}
            )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert len(setups) == 2
    assert entry.data[CONF_TCP_HOST] == new_host
