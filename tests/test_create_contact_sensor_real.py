"""Regression tests that exercise the REAL ``binary_sensor.create_contact_sensor``.

Companion to ``test_contact_discovery_mode.py``, which tests a hand-written
*mirror* of the gate. The mirror exists because ``binary_sensor`` cannot be
imported whole under the conftest stubs: its entity classes subclass mocked HA
bases, and conftest replaces the module itself with a ``MagicMock``. But a mirror
passes whether or not production matches it, so a production-only regression in
``create_contact_sensor`` would not be caught -- which is exactly how the
2026-06-15 off-mode orphaned-entity bug shipped past the green unit suite (the
mirror had replicated the buggy gate).

This module closes that gap WITHOUT importing ``binary_sensor``: it parses
``binary_sensor.py``, extracts only the ``create_contact_sensor`` ``FunctionDef``,
and ``exec``s it with its free names bound (the real ``const`` accessor + the
``MODE_*`` constants, plus a sentinel for the entity class). The assertions
therefore run the real production source -- a regression in the gate (e.g.
dropping the ``off`` branch) flips them red. The suppression paths return before
the entity class is ever instantiated, so the sentinel only matters on the
create paths.
"""
import ast
import importlib.util
import os
import sys
import types

_PKG = "custom_components.meshcore"
_BASE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "meshcore"
)


# --- Load the real const.py (bypass the conftest MagicMock stub) -------------
_cc = types.ModuleType("custom_components")
_cc.__path__ = [os.path.dirname(_BASE)]
sys.modules["custom_components"] = _cc
_ccm = types.ModuleType(_PKG)
_ccm.__path__ = [_BASE]
sys.modules[_PKG] = _ccm


def _load_real(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(_BASE, filename)
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = _PKG
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


const = _load_real(f"{_PKG}.const", "const.py")
MODE_FULL = const.MODE_FULL
MODE_DATA_ONLY = const.MODE_DATA_ONLY
MODE_OFF = const.MODE_OFF
CONF_MODE = const.CONF_CONTACT_DISCOVERY_MODE


# --- Extract the real create_contact_sensor without importing the module -----

class _SentinelSensor:
    """Stand-in for MeshCoreContactDiagnosticBinarySensor on the create path."""

    def __init__(self, coordinator, name, public_key, unique_id):
        self.name = name
        self.public_key = public_key
        self.unique_id = unique_id


def _extract_create_contact_sensor():
    with open(os.path.join(_BASE, "binary_sensor.py"), encoding="utf-8") as fh:
        src = fh.read()
    fn = next(
        (n for n in ast.parse(src).body
         if isinstance(n, ast.FunctionDef) and n.name == "create_contact_sensor"),
        None,
    )
    assert fn is not None, "create_contact_sensor not found in binary_sensor.py"
    code = compile(ast.Module(body=[fn], type_ignores=[]), "binary_sensor.py", "exec")
    namespace = {
        "get_contact_discovery_mode": const.get_contact_discovery_mode,
        "MODE_DATA_ONLY": MODE_DATA_ONLY,
        "MODE_OFF": MODE_OFF,
        "MeshCoreContactDiagnosticBinarySensor": _SentinelSensor,
    }
    exec(code, namespace)  # noqa: S102 -- executing our own production source
    return namespace["create_contact_sensor"]


create_contact_sensor = _extract_create_contact_sensor()


# --- Fakes (match test_contact_discovery_mode.py shapes) ---------------------

class _FakeEntry:
    def __init__(self, mode=None, entry_id="01ENTRY"):
        self.entry_id = entry_id
        self.data = {} if mode is None else {CONF_MODE: mode}


class _FakeCoordinator:
    def __init__(self, mode=None, added=None, entry_id="01ENTRY"):
        self.config_entry = _FakeEntry(mode, entry_id)
        # _contacts is keyed by 12-hex prefix in production; only the
        # public_key values matter to the gate.
        self._contacts = {pk[:12]: {"public_key": pk} for pk in (added or [])}
        self.tracked_diagnostic_binary_contacts = set()


# --- Suppression / create matrix against the REAL function -------------------

def test_full_discovered_creates_and_tracks():
    """full: a discovered (un-added) contact is created and tracked."""
    coord = _FakeCoordinator(MODE_FULL)
    pk = "aa" * 16
    sensor = create_contact_sensor(coord, {"public_key": pk})
    assert isinstance(sensor, _SentinelSensor)
    assert pk in coord.tracked_diagnostic_binary_contacts


def test_default_mode_creates():
    """Unset mode defaults to full -> discovered contact created."""
    sensor = create_contact_sensor(_FakeCoordinator(None), {"public_key": "bb" * 16})
    assert isinstance(sensor, _SentinelSensor)


def test_off_suppresses_discovered():
    """off: a discovered (un-added) contact returns None and is not tracked.

    Regression guard for the orphaned-entity bug -- the setup path calls this
    gate directly with no off early-return of its own, so the gate itself must
    suppress, or a reload in off mode materializes an entity per contact.
    """
    coord = _FakeCoordinator(MODE_OFF)
    assert create_contact_sensor(coord, {"public_key": "cc" * 16}) is None
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_off_keeps_added():
    """off: an added/curated contact still gets its entity."""
    pk = "cd" * 16
    coord = _FakeCoordinator(MODE_OFF, added=[pk])
    assert isinstance(create_contact_sensor(coord, {"public_key": pk}), _SentinelSensor)


def test_data_only_suppresses_discovered():
    """data_only: a discovered (un-added) contact returns None and is not tracked."""
    coord = _FakeCoordinator(MODE_DATA_ONLY)
    assert create_contact_sensor(coord, {"public_key": "dd" * 16}) is None
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_data_only_keeps_added_with_entry_scoped_uid():
    """data_only: an added contact gets its entity, with the entry-scoped uid."""
    pk = "ee" * 16
    coord = _FakeCoordinator(MODE_DATA_ONLY, added=[pk])
    sensor = create_contact_sensor(coord, {"public_key": pk})
    assert isinstance(sensor, _SentinelSensor)
    assert sensor.unique_id == f"01ENTRY_contact_{pk[:12]}"


def test_data_only_other_added_does_not_unblock():
    """data_only: a different added contact does not unblock an un-added one."""
    coord = _FakeCoordinator(MODE_DATA_ONLY, added=["1a" * 16])
    assert create_contact_sensor(coord, {"public_key": "2b" * 16}) is None
    assert ("2b" * 16) not in coord.tracked_diagnostic_binary_contacts


def test_non_dict_returns_none():
    assert create_contact_sensor(_FakeCoordinator(MODE_FULL), "not-a-dict") is None


def test_missing_public_key_returns_none():
    assert create_contact_sensor(_FakeCoordinator(MODE_FULL), {"adv_name": "NoKey"}) is None


def test_already_tracked_is_idempotent():
    """A contact already in the tracked set is not re-created (any mode)."""
    coord = _FakeCoordinator(MODE_FULL)
    pk = "cc" * 16
    coord.tracked_diagnostic_binary_contacts.add(pk)
    assert create_contact_sensor(coord, {"public_key": pk}) is None
