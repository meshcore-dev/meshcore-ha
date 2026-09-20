"""Tests for the contact-discovery-mode constants, accessor, and the v2->v3
config-entry migration that collapses the two legacy discovery booleans into
the single ``contact_discovery_mode`` tri-state.

Unlike the standalone-logic-copy tests in this suite (e.g.
``test_large_mesh_mode.py``, which mirror gate logic because the modules under
test subclass mocked HA entity classes), the migration logic lives in a plain
``async def async_migrate_entry`` in ``custom_components/meshcore/__init__.py``
with no un-loadable dependencies. We therefore load the *real* ``const.py`` and
the *real* ``__init__.py`` via importlib (mirroring ``test_flood_scope.py``'s
real-module load) and exercise the actual migration -- so the v1->v3 chained
case genuinely guards the standalone-``if`` requirement (an accidental ``elif``
would strand v1 entries at v2 and this test would fail).

The heavy package dependencies ``__init__.py`` imports (the coordinator, API,
uploaders, services, the meshcore SDK, and Home Assistant itself) are stubbed;
only ``const.py`` is loaded for real, so the migration sees real CONF_*/MODE_*
values.
"""
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_PKG = "custom_components.meshcore"
_BASE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "meshcore"
)

# --- Stub the heavy deps __init__.py imports at module load -------------------
_STUBS = (
    "meshcore",
    "meshcore.events",
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.components",
    "homeassistant.components.http",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.device_registry",
    f"{_PKG}.coordinator",
    f"{_PKG}.radio",
    f"{_PKG}.map_uploader",
    f"{_PKG}.mqtt_uploader",
    f"{_PKG}.services",
    f"{_PKG}.utils",
)
for _m in _STUBS:
    sys.modules[_m] = MagicMock()

# Real parent package so the relative imports inside __init__.py resolve.
_cc = types.ModuleType("custom_components")
_cc.__path__ = [os.path.dirname(_BASE)]
sys.modules["custom_components"] = _cc


def _load_real(modname: str, filename: str):
    """Load a real integration module from file, bypassing the conftest stub."""
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(_BASE, filename)
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _PKG
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


# Real const first, so __init__.py's ``from .const import ...`` binds real values.
const = _load_real(f"{_PKG}.const", "const.py")

# Real package __init__ (the module under test). Registered as the package
# itself so its relative imports resolve against the stubs / real const above.
_init_spec = importlib.util.spec_from_file_location(
    _PKG, os.path.join(_BASE, "__init__.py"), submodule_search_locations=[_BASE]
)
_meshcore_init = importlib.util.module_from_spec(_init_spec)
_meshcore_init.__package__ = _PKG
sys.modules[_PKG] = _meshcore_init
_init_spec.loader.exec_module(_meshcore_init)

async_migrate_entry = _meshcore_init.async_migrate_entry

CONF_MODE = const.CONF_CONTACT_DISCOVERY_MODE
MODE_FULL = const.MODE_FULL
MODE_DATA_ONLY = const.MODE_DATA_ONLY
MODE_OFF = const.MODE_OFF

_LEGACY_DISABLE = "disable_contact_discovery"
_LEGACY_LARGE_MESH = "large_mesh_mode"


class _FakeEntry:
    """Minimal ConfigEntry stand-in carrying mutable version, data and options."""

    def __init__(self, version, data, options=None):
        self.version = version
        self.data = dict(data)
        self.options = dict(options or {})
        self.unique_id = None
        self.entry_id = "entry-one"


class _FakeConfigEntries:
    """Mirror of hass.config_entries.async_update_entry's mutate-in-place effect."""

    def __init__(self, entries=()):
        self._entries = list(entries)

    def async_entries(self, _domain):
        return list(self._entries)

    def async_update_entry(
        self, entry, data=None, options=None, version=None, unique_id=None, **_kwargs
    ):
        if data is not None:
            entry.data = data
        if options is not None:
            entry.options = options
        if version is not None:
            entry.version = version
        if unique_id is not None:
            entry.unique_id = unique_id


class _FakeHass:
    def __init__(self, entries=()):
        self.config_entries = _FakeConfigEntries(entries)


async def _run(version, data, options=None, others=()):
    """Run the real async_migrate_entry against a fake entry; return (ok, entry)."""
    entry = _FakeEntry(version, data, options)
    hass = _FakeHass((entry, *others))
    ok = await async_migrate_entry(hass, entry)
    return ok, entry


# --- Change 1: constants + accessor ------------------------------------------

def test_mode_constant_values():
    assert const.CONF_CONTACT_DISCOVERY_MODE == "contact_discovery_mode"
    assert const.MODE_FULL == "full"
    assert const.MODE_DATA_ONLY == "data_only"
    assert const.MODE_OFF == "off"
    assert const.DEFAULT_CONTACT_DISCOVERY_MODE == const.MODE_FULL
    assert const.CONTACT_DISCOVERY_MODES == ("full", "data_only", "off")


def test_accessor_returns_stored_mode():
    entry = _FakeEntry(3, {CONF_MODE: MODE_DATA_ONLY})
    assert const.get_contact_discovery_mode(entry) == MODE_DATA_ONLY


def test_accessor_reads_off():
    entry = _FakeEntry(3, {CONF_MODE: MODE_OFF})
    assert const.get_contact_discovery_mode(entry) == MODE_OFF


def test_accessor_defaults_to_full_when_absent():
    entry = _FakeEntry(3, {})
    assert const.get_contact_discovery_mode(entry) == MODE_FULL


# --- Change 2: v2->v3 migration mappings -------------------------------------

@pytest.mark.asyncio
async def test_migrate_disable_maps_to_off():
    ok, entry = await _run(2, {_LEGACY_DISABLE: True})
    assert ok is True
    assert entry.version == 4
    assert entry.data[CONF_MODE] == MODE_OFF
    assert _LEGACY_DISABLE not in entry.data
    assert _LEGACY_LARGE_MESH not in entry.data


@pytest.mark.asyncio
async def test_migrate_large_mesh_maps_to_data_only():
    ok, entry = await _run(2, {_LEGACY_LARGE_MESH: True})
    assert ok is True
    assert entry.version == 4
    assert entry.data[CONF_MODE] == MODE_DATA_ONLY
    assert _LEGACY_LARGE_MESH not in entry.data


@pytest.mark.asyncio
async def test_migrate_neither_maps_to_full():
    ok, entry = await _run(2, {})
    assert ok is True
    assert entry.version == 4
    assert entry.data[CONF_MODE] == MODE_FULL


@pytest.mark.asyncio
async def test_migrate_both_true_off_wins_tiebreak():
    ok, entry = await _run(2, {_LEGACY_DISABLE: True, _LEGACY_LARGE_MESH: True})
    assert entry.data[CONF_MODE] == MODE_OFF
    assert _LEGACY_DISABLE not in entry.data
    assert _LEGACY_LARGE_MESH not in entry.data


@pytest.mark.asyncio
async def test_migrate_drops_explicit_false_legacy_keys():
    """Even when the legacy flags are explicitly False, they are dropped."""
    ok, entry = await _run(2, {_LEGACY_DISABLE: False, _LEGACY_LARGE_MESH: False})
    assert entry.data[CONF_MODE] == MODE_FULL
    assert _LEGACY_DISABLE not in entry.data
    assert _LEGACY_LARGE_MESH not in entry.data


@pytest.mark.asyncio
async def test_migrate_preserves_unrelated_keys():
    ok, entry = await _run(2, {_LEGACY_LARGE_MESH: True, "name": "MattDub", "baudrate": 115200})
    assert entry.data["name"] == "MattDub"
    assert entry.data["baudrate"] == 115200
    assert entry.data[CONF_MODE] == MODE_DATA_ONLY


@pytest.mark.asyncio
async def test_migrate_v1_chains_to_v3():
    """A v1 entry must run v1->v2 then fall through to v2->v3 in one pass.

    Guards the standalone-``if`` requirement: an ``elif`` here would leave a v1
    entry at version 2 with no contact_discovery_mode key.
    """
    ok, entry = await _run(1, {})
    assert ok is True
    assert entry.version == 4
    assert entry.data[CONF_MODE] == MODE_FULL
    assert _LEGACY_DISABLE not in entry.data
    assert _LEGACY_LARGE_MESH not in entry.data


@pytest.mark.asyncio
async def test_migrate_v1_with_large_mesh_chains_to_data_only():
    """A v1 entry that already carried large_mesh_mode=True lands on data_only at v3."""
    ok, entry = await _run(1, {_LEGACY_LARGE_MESH: True})
    assert ok is True
    assert entry.version == 4
    assert entry.data[CONF_MODE] == MODE_DATA_ONLY
    assert _LEGACY_LARGE_MESH not in entry.data


@pytest.mark.asyncio
async def test_migrate_rejects_downgrade_from_future_version():
    ok, entry = await _run(5, {})
    assert ok is False
    assert entry.version == 5
    assert CONF_MODE not in entry.data


# --- Change 3: v3->v4 settings move into entry options ------------------------

@pytest.mark.asyncio
async def test_migrate_copies_settings_into_options_and_keeps_data():
    """Settings land in options; the data copies stay for a downgrade."""
    ok, entry = await _run(
        3,
        {
            "tcp_host": "10.0.0.5",
            "pubkey": "AABBCCDDEEFF00112233445566778899AABBCCDDEEFF001122334455667788AA",
            CONF_MODE: MODE_DATA_ONLY,
            "self_telemetry_enabled": True,
            "repeater_subscriptions": [{"name": "R", "pubkey_prefix": "AABBCCDDEEFF"}],
            "tracked_clients": [{"name": "C", "pubkey_prefix": "BBBBBB"}],
        },
    )

    assert ok is True
    assert entry.version == 4
    assert entry.options[CONF_MODE] == MODE_DATA_ONLY
    assert entry.options["self_telemetry_enabled"] is True
    assert entry.data[CONF_MODE] == MODE_DATA_ONLY
    assert entry.data["tcp_host"] == "10.0.0.5"
    assert "tcp_host" not in entry.options


@pytest.mark.asyncio
async def test_migrate_normalises_prefixes_and_claims_the_pubkey():
    """Stored prefixes are lower-cased and the radio key identifies the entry."""
    pubkey = "AA" * 32
    ok, entry = await _run(
        3,
        {
            "pubkey": pubkey,
            "repeater_subscriptions": [{"name": "R", "pubkey_prefix": "AABBCCDDEEFF"}],
            "tracked_clients": [{"name": "C", "pubkey_prefix": "BBBBBB"}],
        },
    )

    assert ok is True
    assert entry.options["repeater_subscriptions"][0]["pubkey_prefix"] == "aabbccddeeff"
    assert entry.options["tracked_clients"][0]["pubkey_prefix"] == "bbbbbb"
    assert entry.data["repeater_subscriptions"][0]["pubkey_prefix"] == "aabbccddeeff"
    assert entry.unique_id == pubkey.lower()


@pytest.mark.asyncio
async def test_migrate_keeps_explicit_options_and_leaves_a_taken_key_unclaimed():
    """An existing options value wins, and a duplicate radio stays unclaimed."""
    pubkey = "cc" * 32
    owner = _FakeEntry(4, {"pubkey": pubkey})
    owner.entry_id = "entry-two"
    owner.unique_id = pubkey
    ok, entry = await _run(
        3,
        {"pubkey": pubkey, "stale_contact_days": 30},
        options={"stale_contact_days": 5},
        others=(owner,),
    )

    assert ok is True
    assert entry.options["stale_contact_days"] == 5
    assert entry.unique_id is None
