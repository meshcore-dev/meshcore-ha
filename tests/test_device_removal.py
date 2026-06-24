"""Tests for async_remove_config_entry_device (PR #247 device-delete hook).

The conftest mocks the entire HA + integration package surface, so the real
custom_components.meshcore package can't be imported the normal way. Unlike the
standalone-logic-copy pattern used elsewhere, this hook is small and security
relevant, so we load the *real* function via importlib against the mocked
package and exercise it directly. We reuse the module's own mocked sentinels
(DOMAIN, CONF_REPEATER_SUBSCRIPTIONS, CONF_TRACKED_CLIENTS) as dict keys so the
membership logic operates on the same objects the live code sees.
"""
import importlib.util
import sys
from unittest.mock import MagicMock

import pytest

# Mocks that conftest does not provide but __init__.py imports at module load.
for _name in ("homeassistant.exceptions",
              "custom_components.meshcore.coordinator",
              "custom_components.meshcore.meshcore_api",
              "custom_components.meshcore.map_uploader",
              "custom_components.meshcore.mqtt_uploader",
              "custom_components.meshcore.services"):
    sys.modules.setdefault(_name, MagicMock())


def _load_real_module():
    """Load the real custom_components/meshcore/__init__.py under the mocked package."""
    name = "custom_components.meshcore"
    spec = importlib.util.spec_from_file_location(
        name,
        "custom_components/meshcore/__init__.py",
        submodule_search_locations=["custom_components/meshcore"],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_real_module()
remove = MOD.async_remove_config_entry_device
DOMAIN = MOD.DOMAIN
CONF_REPEATER = MOD.CONF_REPEATER_SUBSCRIPTIONS
CONF_CLIENTS = MOD.CONF_TRACKED_CLIENTS

ENTRY_ID = "abc123entryid"
# A repeater configured with a 12-char prefix.
REPEATER_PREFIX = "aabbccddeeff"
CLIENT_PREFIX = "112233445566"
CONTACT_PREFIX = "778899aabbcc"
# Full 64-char public key whose first 12 chars match a configured repeater.
FULL_PUBKEY = REPEATER_PREFIX + "0" * 52


def _make_config_entry(repeaters=None, clients=None):
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.data = {
        CONF_REPEATER: [{"pubkey_prefix": p} for p in (repeaters or [])],
        CONF_CLIENTS: [{"pubkey_prefix": p} for p in (clients or [])],
    }
    return entry


def _make_device(identifier):
    device = MagicMock()
    device.identifiers = {(DOMAIN, identifier)}
    return device


def _make_hass(contacts=None):
    """hass whose data[DOMAIN][entry_id] is a coordinator exposing .data['contacts']."""
    hass = MagicMock()
    if contacts is None:
        hass.data = {}
    else:
        coordinator = MagicMock()
        coordinator.data = {"contacts": contacts}
        hass.data = {DOMAIN: {ENTRY_ID: coordinator}}
    return hass


def _id(node_type, pubkey):
    return f"{ENTRY_ID}_{node_type}_{pubkey}"


@pytest.fixture(autouse=True)
def _default_device_has_entities():
    """Default every test to the populated-device path.

    Change A allows removing a device that has no entities, keyed off
    er.async_entries_for_device. The existing refusal/orphan tests must run with
    a device that still HAS entities so they exercise PR #247's populated-device
    guards (the empty-device allowance is only reached when that list is empty).
    Individual empty-device tests override the return value to [].
    """
    MOD.er.async_entries_for_device.return_value = [MagicMock()]
    yield


@pytest.mark.asyncio
async def test_hub_device_refused():
    hass = _make_hass()
    entry = _make_config_entry()
    device = _make_device(ENTRY_ID)  # identifier == entry_id
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_live_repeater_refused():
    hass = _make_hass()
    entry = _make_config_entry(repeaters=[REPEATER_PREFIX])
    device = _make_device(_id("repeater", REPEATER_PREFIX))
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_live_client_refused():
    hass = _make_hass()
    entry = _make_config_entry(clients=[CLIENT_PREFIX])
    device = _make_device(_id("client", CLIENT_PREFIX))
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_length_mismatch_repeater_still_refused():
    """Bug #1: identifier carries the full 64-char pubkey; config has 12 chars."""
    hass = _make_hass()
    entry = _make_config_entry(repeaters=[REPEATER_PREFIX])
    device = _make_device(_id("repeater", FULL_PUBKEY))
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_live_contact_refused():
    """Bug #2: a live auto-discovered contact must not be deletable."""
    hass = _make_hass(contacts=[{"pubkey_prefix": CONTACT_PREFIX}])
    entry = _make_config_entry()
    device = _make_device(_id("contact", CONTACT_PREFIX))
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_live_contact_full_pubkey_refused():
    """Bug #1 + #2: contact identifier with a full 64-char pubkey, live in coordinator."""
    full = CONTACT_PREFIX + "f" * 52
    hass = _make_hass(contacts=[{"public_key": full}])
    entry = _make_config_entry()
    device = _make_device(_id("contact", full))
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_live_unknown_refused():
    hass = _make_hass(contacts=[{"pubkey_prefix": CONTACT_PREFIX}])
    entry = _make_config_entry()
    device = _make_device(_id("unknown", CONTACT_PREFIX))
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_orphan_contact_allowed():
    """Contact no longer present in the coordinator may be removed."""
    hass = _make_hass(contacts=[{"pubkey_prefix": "ffffffffffff"}])
    entry = _make_config_entry()
    device = _make_device(_id("contact", CONTACT_PREFIX))
    assert await remove(hass, entry, device) is True


@pytest.mark.asyncio
async def test_orphan_repeater_allowed():
    """Repeater dropped from config is an orphan and may be removed."""
    hass = _make_hass()
    entry = _make_config_entry(repeaters=["000000000000"])
    device = _make_device(_id("repeater", REPEATER_PREFIX))
    assert await remove(hass, entry, device) is True


@pytest.mark.asyncio
async def test_foreign_device_allowed():
    """A device whose identifier belongs to another domain falls through to True."""
    hass = _make_hass()
    entry = _make_config_entry()
    device = MagicMock()
    device.identifiers = {("other_domain", "whatever")}
    assert await remove(hass, entry, device) is True


@pytest.mark.asyncio
async def test_missing_coordinator_orphan_contact_allowed():
    """Defensive: no coordinator in hass.data -> no live contacts -> orphan allowed."""
    hass = _make_hass()  # hass.data == {}
    entry = _make_config_entry()
    device = _make_device(_id("contact", CONTACT_PREFIX))
    assert await remove(hass, entry, device) is True


@pytest.mark.asyncio
async def test_empty_contact_device_allowed():
    """An emptied discovered-contact device is removable even while the contact
    is live in the mesh (the reported 'Node fe3af5' case): no entities -> allow."""
    MOD.er.async_entries_for_device.return_value = []
    hass = _make_hass(contacts=[{"pubkey_prefix": CONTACT_PREFIX}])
    entry = _make_config_entry()
    device = _make_device(_id("contact", CONTACT_PREFIX))
    assert await remove(hass, entry, device) is True


@pytest.mark.asyncio
async def test_empty_device_hub_still_refused():
    """The hub device is excluded from the empty-device allowance: it stays
    non-removable even with no entities."""
    MOD.er.async_entries_for_device.return_value = []
    hass = _make_hass()
    entry = _make_config_entry()
    device = _make_device(ENTRY_ID)  # identifier == entry_id
    assert await remove(hass, entry, device) is False


@pytest.mark.asyncio
async def test_populated_live_contact_refused():
    """A live contact whose device still has entities stays refused -- awolden's
    PR #247 guard is preserved for populated devices."""
    MOD.er.async_entries_for_device.return_value = [MagicMock()]
    hass = _make_hass(contacts=[{"pubkey_prefix": CONTACT_PREFIX}])
    entry = _make_config_entry()
    device = _make_device(_id("contact", CONTACT_PREFIX))
    assert await remove(hass, entry, device) is False
