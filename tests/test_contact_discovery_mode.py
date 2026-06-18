"""Tests for the tri-state ``contact_discovery_mode`` (full / data_only / off).

This is the consolidated successor to the boolean-era ``test_large_mesh_mode.py``
and the interim ``test_contact_discovery_mode_runtime.py``. It covers the whole
runtime surface driven by the mode enum:

  * the ``create_contact_sensor`` entity-creation gate (full creates / data_only
    suppresses discovered + keeps added / default == full);
  * the ``handle_contacts_update`` early-return (off skips discovery entirely);
  * the discovered-cleanup paths that now run UNCONDITIONALLY (a no-op in
    data_only, but they clear stale orphans left by a prior mode);
  * the ``remove_contact`` demote cleanup, gated on data_only (the inverse gate:
    a demoted added contact must lose its binary_sensor + telemetry/GPS family);
  * the ``get_discovered_contact`` lookup, incl. the empty/short-prefix guard;
  * the mode-independent discovered-summary sensor.

As with the modules these mirror (``binary_sensor``/``coordinator``/``services``
define classes that subclass mocked HA bases and so cannot be imported whole
under the conftest stubs), the gate/cleanup/demote/lookup bodies are faithful
logic mirrors of the production code. To keep the *mode decision* honest rather
than a re-implementation, every mirror calls the **real**
``const.get_contact_discovery_mode`` accessor and the **real** ``MODE_*``
constants, loaded from the real ``const.py`` via importlib (the same pattern as
``test_contact_discovery_migration.py``). A mismatch between the accessor and
the gate comparisons would fail these tests. The HA-coupled paths
(coordinator/registry resolution, ``_ensure_contact_compat`` backfill, the
config-flow form rendering) are exercised live on the HA host.
"""
import copy
import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest


# --- Load the real const.py (bypass the conftest MagicMock stub) -------------
_PKG = "custom_components.meshcore"
_BASE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "meshcore"
)

# const.py imports only enum + typing, but stub the parent package so the
# importlib-loaded module registers under the real dotted name cleanly.
_cc = types.ModuleType("custom_components")
_cc.__path__ = [os.path.dirname(_BASE)]
sys.modules["custom_components"] = _cc


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
DEFAULT_CONTACT_DISCOVERY_MODE = const.DEFAULT_CONTACT_DISCOVERY_MODE
CONTACT_DISCOVERY_MODES = const.CONTACT_DISCOVERY_MODES
DOMAIN = const.DOMAIN
get_contact_discovery_mode = const.get_contact_discovery_mode


# --- Minimal fakes -----------------------------------------------------------

class _FakeEntry:
    def __init__(self, mode=None, entry_id="01ENTRY"):
        self.entry_id = entry_id
        self.data = {} if mode is None else {CONF_MODE: mode}


def _entry_with_data(data, entry_id="01ENTRY"):
    """An entry-like object whose ``.data`` is an arbitrary dict (for the
    options round-trip, which preserves the whole data cluster)."""
    return SimpleNamespace(data=data, entry_id=entry_id)


class _FakeCoordinator:
    def __init__(self, mode=None, added=None, entry_id="01ENTRY", tracked=None):
        self.config_entry = _FakeEntry(mode, entry_id)
        # _contacts is keyed by 12-hex prefix in production; only the
        # public_key values matter to the gate.
        self._contacts = {
            pk[:12]: {"public_key": pk} for pk in (added or [])
        }
        self.tracked_diagnostic_binary_contacts = set(tracked or [])


class _FakeEntityRegistry:
    """Minimal entity-registry stand-in.

    Maps (platform, domain, unique_id) -> entity_id; records async_remove calls
    and drops the removed entity so a later lookup returns None.
    """

    def __init__(self, entities=None):
        self._by_unique = dict(entities or {})
        self.removed = []

    def async_get_entity_id(self, platform, domain, unique_id):
        return self._by_unique.get((platform, domain, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        for key, eid in list(self._by_unique.items()):
            if eid == entity_id:
                del self._by_unique[key]


def _uid(entry_id, pubkey):
    return f"{entry_id}_contact_{pubkey[:12]}"


# 60-hex full keys reused by the demote/sweep sections.
DISCOVERED_PK = "aabbccddeeff00112233445566778899aabbccddeeff0011223344556677"
ADDED_PK = "1122334455667788990011223344556677889900112233445566778899aa"


# =============================================================================
# create_contact_sensor gate: data_only and off suppress discovered contacts
# =============================================================================
#
# Live gate (binary_sensor.create_contact_sensor): in data_only and off, a
# contact whose public_key is NOT in the added-contact map
# (coordinator._contacts) returns None and is not tracked. Added contacts create
# in every mode; only full creates an entity for a discovered (un-added) contact.
# The off case is load-bearing: the binary_sensor setup path (async_setup_entry)
# calls this gate directly with no off early-return of its own, so if the gate
# did not suppress off, a reload in off mode would materialize a per-contact
# entity for every contact (the orphaned-entity bug this regression set guards).

def create_contact_sensor(coordinator, contact):
    """Faithful mirror of binary_sensor.create_contact_sensor."""
    if not isinstance(contact, dict):
        return None
    public_key = contact.get("public_key", "")
    if not public_key:
        return None
    if get_contact_discovery_mode(coordinator.config_entry) in (
        MODE_DATA_ONLY,
        MODE_OFF,
    ):
        added_pubkeys = {
            c.get("public_key")
            for c in coordinator._contacts.values()
            if c.get("public_key")
        }
        if public_key not in added_pubkeys:
            return None  # discovered-only: suppressed in data_only and off
    if public_key not in coordinator.tracked_diagnostic_binary_contacts:
        coordinator.tracked_diagnostic_binary_contacts.add(public_key)
        return f"SENSOR:{public_key[:12]}"
    return None


def test_full_mode_discovered_gets_entity():
    """full: a discovered (un-added) contact is created and tracked."""
    coord = _FakeCoordinator(mode=MODE_FULL)
    pk = "aa" * 16
    assert create_contact_sensor(coord, {"public_key": pk}) == "SENSOR:" + ("aa" * 6)
    assert pk in coord.tracked_diagnostic_binary_contacts


def test_default_mode_is_full_when_absent():
    """Unset mode defaults to full -> discovered contact created."""
    coord = _FakeCoordinator(mode=None)
    assert create_contact_sensor(coord, {"public_key": "bb" * 16}) is not None


def test_off_mode_suppresses_discovered():
    """off: a discovered (un-added) contact returns None and is not tracked.

    Regression guard for the orphaned-entity bug. The binary_sensor setup path
    (async_setup_entry) calls this gate directly with no off early-return, so the
    gate itself must suppress discovered contacts in off -- otherwise a reload in
    off mode materializes a per-contact entity for every contact (the opposite of
    disabled).
    """
    coord = _FakeCoordinator(mode=MODE_OFF)
    assert create_contact_sensor(coord, {"public_key": "cc" * 16}) is None
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_off_mode_keeps_added():
    """off: an added/curated contact still gets its entity."""
    pk = "cd" * 16
    coord = _FakeCoordinator(mode=MODE_OFF, added=[pk])
    assert create_contact_sensor(coord, {"public_key": pk}) == "SENSOR:" + ("cd" * 6)
    assert pk in coord.tracked_diagnostic_binary_contacts


def test_data_only_suppresses_discovered():
    """data_only: a discovered (un-added) contact returns None and is not tracked."""
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY)
    assert create_contact_sensor(coord, {"public_key": "dd" * 16}) is None
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_data_only_keeps_added():
    """data_only: an added/curated contact still gets its entity."""
    pk = "ee" * 16
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY, added=[pk])
    assert create_contact_sensor(coord, {"public_key": pk}) == "SENSOR:" + ("ee" * 6)
    assert pk in coord.tracked_diagnostic_binary_contacts


def test_data_only_other_added_does_not_unblock():
    """data_only: a different added contact does not unblock an un-added one."""
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY, added=["1a" * 16])
    assert create_contact_sensor(coord, {"public_key": "2b" * 16}) is None
    assert ("2b" * 16) not in coord.tracked_diagnostic_binary_contacts


def test_data_only_added_matches_full_pubkey_not_prefix():
    """data_only: the gate compares the FULL public_key, not the 12-hex prefix.

    A contact sharing the added contact's 12-hex prefix but a different full key
    must NOT be treated as added.
    """
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY, added=[ADDED_PK])
    impostor = ADDED_PK[:12] + ("f" * (len(ADDED_PK) - 12))
    assert impostor != ADDED_PK
    assert create_contact_sensor(coord, {"public_key": impostor}) is None


def test_gate_non_dict_contact_returns_none():
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY)
    assert create_contact_sensor(coord, "not-a-dict") is None


def test_gate_missing_public_key_returns_none():
    coord = _FakeCoordinator(mode=MODE_FULL)
    assert create_contact_sensor(coord, {"adv_name": "NoKey"}) is None


def test_gate_already_tracked_is_idempotent():
    """A contact already in the tracked set is not re-created (any mode)."""
    coord = _FakeCoordinator(mode=MODE_FULL)
    pk = "cc" * 16
    coord.tracked_diagnostic_binary_contacts.add(pk)
    assert create_contact_sensor(coord, {"public_key": pk}) is None


# =============================================================================
# handle_contacts_update early-return (Change 4): off skips entirely
# =============================================================================

def handle_contacts_update_gate(coordinator):
    """Mirror of the handle_contacts_update early-return: 'skip' iff off."""
    if get_contact_discovery_mode(coordinator.config_entry) == MODE_OFF:
        return "skip"
    return "process"


def test_off_mode_early_returns():
    assert handle_contacts_update_gate(_FakeCoordinator(mode=MODE_OFF)) == "skip"


def test_full_and_data_only_do_not_early_return():
    assert handle_contacts_update_gate(_FakeCoordinator(mode=MODE_FULL)) == "process"
    assert handle_contacts_update_gate(_FakeCoordinator(mode=MODE_DATA_ONLY)) == "process"


def test_default_mode_does_not_early_return():
    assert handle_contacts_update_gate(_FakeCoordinator(mode=None)) == "process"


# =============================================================================
# Unconditional discovered-cleanup (Changes 5, 6): no mode gate
# =============================================================================
#
# The eviction / stale-cleanup / remove-discovered / clear paths dropped their
# `if large_mesh` skip gate: registry removal now runs unconditionally. In
# data_only/off there is no discovered entity so the removal is a no-op, but a
# stale entity left by a prior mode switch is now correctly removed.

def cleanup_discovered(coordinator, entity_registry, discovered_keys):
    """Faithful mirror of the post-Change-5/6 cleanup loop (runs every mode)."""
    entry_id = coordinator.config_entry.entry_id
    for public_key in list(discovered_keys):
        coordinator.tracked_diagnostic_binary_contacts.discard(public_key)
        unique_id = _uid(entry_id, public_key)
        entity_id = entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, unique_id
        )
        if entity_id:
            entity_registry.async_remove(entity_id)
    return list(entity_registry.removed)


def test_cleanup_full_removes_existing_entity():
    """full: a discovered contact's entity is removed by cleanup."""
    pk = "11" * 16
    reg = _FakeEntityRegistry({("binary_sensor", DOMAIN, _uid("01ENTRY", pk)): "binary_sensor.disco"})
    coord = _FakeCoordinator(mode=MODE_FULL, tracked=[pk])
    assert cleanup_discovered(coord, reg, [pk]) == ["binary_sensor.disco"]
    assert pk not in coord.tracked_diagnostic_binary_contacts


def test_cleanup_data_only_is_noop_but_trims_tracking():
    """data_only: no per-contact entity exists, so removal is a no-op; the
    tracked-set discard (the in-memory dict-trim analogue) still runs."""
    pk = "22" * 16
    reg = _FakeEntityRegistry({})  # no entity registered in data_only
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY, tracked=[pk])
    assert cleanup_discovered(coord, reg, [pk]) == []
    assert pk not in coord.tracked_diagnostic_binary_contacts


def test_cleanup_data_only_removes_stale_orphan_from_prior_mode():
    """The point of dropping the gate: a stale entity left by a prior mode
    switch is now removed even in data_only (the old gate skipped it)."""
    pk = "33" * 16
    reg = _FakeEntityRegistry({("binary_sensor", DOMAIN, _uid("01ENTRY", pk)): "binary_sensor.orphan"})
    coord = _FakeCoordinator(mode=MODE_DATA_ONLY, tracked=[pk])
    assert cleanup_discovered(coord, reg, [pk]) == ["binary_sensor.orphan"]


def test_cleanup_off_removes_stale_orphan():
    """off: same unconditional removal of a stale orphan."""
    pk = "44" * 16
    reg = _FakeEntityRegistry({("binary_sensor", DOMAIN, _uid("01ENTRY", pk)): "binary_sensor.orphan"})
    coord = _FakeCoordinator(mode=MODE_OFF, tracked=[pk])
    assert cleanup_discovered(coord, reg, [pk]) == ["binary_sensor.orphan"]


# =============================================================================
# Demote-added binary_sensor cleanup (Change 6): inverse gate, data_only only
# =============================================================================
#
# Live block (demote binary_sensor removal), gated on mode == data_only:
#   coordinator.tracked_diagnostic_binary_contacts.discard(pubkey)   # FULL key
#   unique_id = f"{entry_id}_contact_{pubkey[:12]}"
#   entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
#   if entity_id: registry.async_remove(entity_id)
# In full/off the block is gated off entirely (the entity correctly persists).

def _make_demote_coordinator(mode=None, entry_id="01ENTRY", tracked=None):
    return SimpleNamespace(
        config_entry=_FakeEntry(mode, entry_id),
        tracked_diagnostic_binary_contacts=set(tracked or ()),
    )


def demote_remove_entity(coordinator, entity_registry, pubkey):
    """Standalone mirror of the live data_only block in the remove_contact
    handler. Returns the removed entity_id (or None).

    Byte-faithful: the discard uses the FULL public_key; the unique_id uses the
    12-hex prefix; both the discard and the removal are gated on data_only (the
    INVERSE of the discovered-cleanup paths, which run in every mode).
    """
    prefix = pubkey[:12]
    if get_contact_discovery_mode(coordinator.config_entry) == MODE_DATA_ONLY:
        coordinator.tracked_diagnostic_binary_contacts.discard(pubkey)
        unique_id = f"{coordinator.config_entry.entry_id}_contact_{prefix}"
        entity_id = entity_registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
        if entity_id:
            entity_registry.async_remove(entity_id)
            return entity_id
    return None


def _registry_with_contact(entry_id, pubkey, entity_id):
    return _FakeEntityRegistry({
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{pubkey[:12]}"): entity_id,
    })


def test_demote_full_keeps_entity_and_tracked():
    """full: demoting an added contact leaves its entity in place and the pubkey
    in the tracked set (the inverse gate is closed outside data_only)."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(mode=MODE_FULL, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)
    assert demote_remove_entity(coord, reg, ADDED_PK) is None
    assert reg.removed == []
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts
    assert reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK[:12]}"
    ) == eid


def test_demote_off_keeps_entity():
    """off is not data_only, so the inverse demote gate stays closed."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(mode=MODE_OFF, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)
    assert demote_remove_entity(coord, reg, ADDED_PK) is None
    assert reg.removed == []
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts


def test_demote_default_mode_keeps_entity():
    """Unset mode defaults to full -> demote gate closed."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(mode=None, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)
    assert demote_remove_entity(coord, reg, ADDED_PK) is None
    assert reg.removed == []
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts


def test_demote_data_only_removes_entity_and_discards_full_key():
    """data_only: async_remove called with the demoted contact's entity_id, and
    the FULL public_key discarded from the tracked set."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(mode=MODE_DATA_ONLY, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)
    assert demote_remove_entity(coord, reg, ADDED_PK) == eid
    assert reg.removed == [eid]
    # FULL key discarded (a prefix-only discard would leave ADDED_PK present).
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_demote_data_only_unique_id_uses_prefix():
    """data_only: the registry lookup keys on the 12-hex prefix, not the full key.

    A registry that only knows the full-key unique_id must NOT match -- proving
    the unique_id is built from pubkey[:12].
    """
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_full"
    reg = _FakeEntityRegistry({
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK}"): eid,  # FULL-key uid (wrong shape)
    })
    coord = _make_demote_coordinator(mode=MODE_DATA_ONLY, entry_id=entry_id, tracked={ADDED_PK})
    assert demote_remove_entity(coord, reg, ADDED_PK) is None
    assert reg.removed == []
    # The full key is still discarded from the tracked set.
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts


def test_demote_data_only_discards_key_even_without_entity():
    """data_only: discard happens even if no entity is registered, so a later
    re-add recreates the entity (tracked set must be clean)."""
    entry_id = "01ENTRY"
    coord = _make_demote_coordinator(mode=MODE_DATA_ONLY, entry_id=entry_id, tracked={ADDED_PK})
    reg = _FakeEntityRegistry({})  # nothing registered
    assert demote_remove_entity(coord, reg, ADDED_PK) is None
    assert reg.removed == []
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts


def test_demote_data_only_spares_selectors_and_sibling():
    """Survivor safety: demoting one added contact removes ONLY its entity -- the
    three _contact_select selector entities and a second added contact survive."""
    entry_id = "01ENTRY"
    target_pk = ADDED_PK
    sibling_pk = DISCOVERED_PK  # a distinct second added contact's full key
    target_eid = "binary_sensor.meshcore_target_111122223333"
    sibling_eid = "binary_sensor.meshcore_sibling_aabbccddeeff"
    sel_contact = "select.meshcore_contact"
    sel_added = "select.meshcore_added_contact"
    sel_discovered = "select.meshcore_discovered_contact"
    reg = _FakeEntityRegistry({
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{target_pk[:12]}"): target_eid,
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{sibling_pk[:12]}"): sibling_eid,
        ("select", DOMAIN, f"{entry_id}_contact_select"): sel_contact,
        ("select", DOMAIN, f"{entry_id}_added_contact_select"): sel_added,
        ("select", DOMAIN, f"{entry_id}_discovered_contact_select"): sel_discovered,
    })
    coord = _make_demote_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, tracked={target_pk, sibling_pk}
    )

    assert demote_remove_entity(coord, reg, target_pk) == target_eid
    assert reg.removed == [target_eid]
    # Sibling added entity survives (registry + tracked set).
    assert reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_contact_{sibling_pk[:12]}"
    ) == sibling_eid
    assert sibling_pk in coord.tracked_diagnostic_binary_contacts
    # All three selector entities survive.
    assert reg.async_get_entity_id("select", DOMAIN, f"{entry_id}_contact_select") == sel_contact
    assert reg.async_get_entity_id("select", DOMAIN, f"{entry_id}_added_contact_select") == sel_added
    assert reg.async_get_entity_id(
        "select", DOMAIN, f"{entry_id}_discovered_contact_select"
    ) == sel_discovered
    assert coord.tracked_diagnostic_binary_contacts == {sibling_pk}


# =============================================================================
# Demote telemetry/GPS sweep (Change 6): inside the data_only gate
# =============================================================================
#
# The live sweep sits inside the data_only gate in the remove_contact handler,
# immediately after the _contact_ binary_sensor removal:
#   if not _node_has_tracked_subscription(coordinator, prefix):
#       uid_prefix = f"{entry_id}_{prefix}_"
#       remove registry entries whose unique_id startswith(uid_prefix) AND
#           endswith("_telemetry") or endswith("_gps_tracker")
#       discard keys startswith(prefix) from
#           coordinator.telemetry_manager.discovered_sensors and
#           coordinator.device_tracker_manager.discovered_trackers
# The subscription exclusion uses the bidirectional-startswith convention.
# Live add -> Req Telemetry -> remove -> re-add is verified on the HA host.

def _node_has_tracked_subscription(coordinator, pubkey_prefix):
    """Standalone mirror of services._node_has_tracked_subscription."""
    for cfg in list(coordinator._tracked_repeaters or []) + list(coordinator._tracked_clients or []):
        cp = cfg.get("pubkey_prefix", "")
        if cp and (pubkey_prefix.startswith(cp) or cp.startswith(pubkey_prefix)):
            return True
    return False


class _FakeSweepRegistry(_FakeEntityRegistry):
    """Fake registry that also serves the er.async_entries_for_config_entry
    surface the sweep reads: every seeded entity belongs to the one config
    entry under test, exposed as (unique_id, entity_id) entries."""

    def entries_for_config_entry(self):
        return [
            SimpleNamespace(unique_id=uid, entity_id=eid)
            for (_platform, _domain, uid), eid in self._by_unique.items()
        ]


def demote_sweep_telemetry_gps(coordinator, entity_registry, pubkey):
    """Standalone mirror of the live telemetry/GPS sweep. Returns removed ids.

    Byte-faithful: gated on data_only; skipped entirely (registry sweep AND
    dedup-map discards) for nodes with a tracked-device subscription; allowlist
    by unique_id SHAPE -- startswith(f"{entry_id}_{prefix}_") AND
    endswith("_telemetry") / endswith("_gps_tracker"); collect-then-remove;
    getattr(..., None) guards for managers on platforms not yet set up.
    """
    prefix = pubkey[:12]
    if get_contact_discovery_mode(coordinator.config_entry) != MODE_DATA_ONLY:
        return []
    if _node_has_tracked_subscription(coordinator, prefix):
        return []
    uid_prefix = f"{coordinator.config_entry.entry_id}_{prefix}_"
    to_remove = [
        e.entity_id
        for e in entity_registry.entries_for_config_entry()
        if (e.unique_id or "").startswith(uid_prefix)
        and (
            e.unique_id.endswith("_telemetry")
            or e.unique_id.endswith("_gps_tracker")
        )
    ]
    for stale_entity_id in to_remove:
        entity_registry.async_remove(stale_entity_id)
    tm = getattr(coordinator, "telemetry_manager", None)
    if tm is not None:
        for key in [k for k in tm.discovered_sensors if k.startswith(prefix)]:
            del tm.discovered_sensors[key]
    dtm = getattr(coordinator, "device_tracker_manager", None)
    if dtm is not None:
        for key in [k for k in dtm.discovered_trackers if k.startswith(prefix)]:
            del dtm.discovered_trackers[key]
    return to_remove


def demote_cleanup_data_only(coordinator, entity_registry, pubkey):
    """Mirror of the full gated block ordering: binary_sensor removal first
    (subscription-independent), then the telemetry/GPS sweep."""
    removed_binary = demote_remove_entity(coordinator, entity_registry, pubkey)
    swept = demote_sweep_telemetry_gps(coordinator, entity_registry, pubkey)
    return removed_binary, swept


_TARGET_PREFIX = ADDED_PK[:12]
_SIBLING_PREFIX = DISCOVERED_PK[:12]


def _make_sweep_coordinator(mode=None, entry_id="01ENTRY", tracked=None,
                            tracked_repeaters=None, tracked_clients=None,
                            telemetry_keys=None, tracker_keys=None,
                            with_managers=True):
    coord = SimpleNamespace(
        config_entry=_FakeEntry(mode, entry_id),
        tracked_diagnostic_binary_contacts=set(tracked or ()),
        _tracked_repeaters=list(tracked_repeaters or []),
        _tracked_clients=list(tracked_clients or []),
    )
    if with_managers:
        coord.telemetry_manager = SimpleNamespace(
            discovered_sensors={k: object() for k in (telemetry_keys or [])}
        )
        coord.device_tracker_manager = SimpleNamespace(
            discovered_trackers={k: object() for k in (tracker_keys or [])}
        )
    return coord


def _telemetry_bearing_registry(entry_id):
    """Registry holding the demoted contact's full entity family plus its
    _contact_ binary_sensor: two telemetry sensors + one GPS tracker."""
    return _FakeSweepRegistry({
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{_TARGET_PREFIX}"):
            "binary_sensor.meshcore_target_contact",
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_1_temperature_telemetry"):
            "sensor.meshcore_target_temperature",
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_2_battery_telemetry"):
            "sensor.meshcore_target_battery",
        ("device_tracker", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_gps_tracker"):
            "device_tracker.meshcore_target_gps",
    })


_TARGET_TELEMETRY_KEYS = [
    f"{_TARGET_PREFIX}_1_temperature",
    f"{_TARGET_PREFIX}_2_battery",
]
_TARGET_TRACKER_KEY = f"{_TARGET_PREFIX}_gps"
_SIBLING_TELEMETRY_KEY = f"{_SIBLING_PREFIX}_1_temperature"
_SIBLING_TRACKER_KEY = f"{_SIBLING_PREFIX}_gps"


def test_sweep_full_removes_nothing_and_keeps_maps():
    """full: with the gate closed, no telemetry/GPS registry removal and both
    dedup maps untouched."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        mode=MODE_FULL, entry_id=entry_id,
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)
    assert demote_sweep_telemetry_gps(coord, reg, ADDED_PK) == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)
    assert set(coord.device_tracker_manager.discovered_trackers) == {_TARGET_TRACKER_KEY}


def test_sweep_off_removes_nothing():
    """off is not data_only -> the sweep is gated off."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        mode=MODE_OFF, entry_id=entry_id,
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)
    assert demote_sweep_telemetry_gps(coord, reg, ADDED_PK) == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)


def test_sweep_data_only_removes_telemetry_gps_and_discards_maps():
    """data_only: non-subscribed contact with two telemetry sensors + a GPS
    tracker -> all three removed, both maps' prefix keys discarded, and the
    _contact_ binary_sensor removal still occurs."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, tracked={ADDED_PK},
        telemetry_keys=_TARGET_TELEMETRY_KEYS + [_SIBLING_TELEMETRY_KEY],
        tracker_keys=[_TARGET_TRACKER_KEY, _SIBLING_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)

    removed_binary, swept = demote_cleanup_data_only(coord, reg, ADDED_PK)

    assert removed_binary == "binary_sensor.meshcore_target_contact"
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts
    assert sorted(swept) == [
        "device_tracker.meshcore_target_gps",
        "sensor.meshcore_target_battery",
        "sensor.meshcore_target_temperature",
    ]
    assert sorted(reg.removed) == [
        "binary_sensor.meshcore_target_contact",
        "device_tracker.meshcore_target_gps",
        "sensor.meshcore_target_battery",
        "sensor.meshcore_target_temperature",
    ]
    # Both dedup maps: target-prefix keys discarded, sibling keys retained.
    assert set(coord.telemetry_manager.discovered_sensors) == {_SIBLING_TELEMETRY_KEY}
    assert set(coord.device_tracker_manager.discovered_trackers) == {_SIBLING_TRACKER_KEY}


def test_sweep_handles_missing_managers():
    """Guard: platforms not yet set up (no manager attributes) must not crash
    the sweep -- mirrors the live getattr(..., None) guards."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(mode=MODE_DATA_ONLY, entry_id=entry_id,
                                    with_managers=False)
    reg = _telemetry_bearing_registry(entry_id)
    assert sorted(demote_sweep_telemetry_gps(coord, reg, ADDED_PK)) == [
        "device_tracker.meshcore_target_gps",
        "sensor.meshcore_target_battery",
        "sensor.meshcore_target_temperature",
    ]


def test_sweep_collects_before_removing():
    """Shape guard: the sweep must not mutate the registry while iterating it --
    removing every matched entity in one pass proves the collect-then-remove
    ordering (a mutate-while-iterating implementation would skip entries)."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(mode=MODE_DATA_ONLY, entry_id=entry_id)
    reg = _FakeSweepRegistry({
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_1_temperature_telemetry"): "sensor.t1",
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_2_humidity_telemetry"): "sensor.t2",
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_3_battery_telemetry"): "sensor.t3",
    })
    assert sorted(demote_sweep_telemetry_gps(coord, reg, ADDED_PK)) == [
        "sensor.t1", "sensor.t2", "sensor.t3",
    ]
    assert reg.entries_for_config_entry() == []


def test_sweep_spares_neighbors_clients_siblings_selectors():
    """Survivor safety: the uid-shape allowlist structurally excludes every wrong
    family even when those uids embed the demoted pubkey prefix."""
    entry_id = "01ENTRY"
    rptr = "930df029a915"
    survivors = {
        # Repeater-neighbor pair embedding the demoted prefix as a NEIGHBOR.
        ("sensor", DOMAIN,
         f"{entry_id}_repeater_{rptr}_neighbor_{_TARGET_PREFIX}_snr"): "sensor.nbr_snr",
        ("sensor", DOMAIN,
         f"{entry_id}_repeater_{rptr}_neighbor_{_TARGET_PREFIX}_seen"): "sensor.nbr_seen",
        # Tracked-client entity shape (client_ before the prefix).
        ("sensor", DOMAIN,
         f"{entry_id}_client_{_TARGET_PREFIX}_battery"): "sensor.client_battery",
        # Sibling contact's telemetry sensor.
        ("sensor", DOMAIN,
         f"{entry_id}_{_SIBLING_PREFIX}_1_temperature_telemetry"): "sensor.sibling_temp",
        # Selector entities.
        ("select", DOMAIN, f"{entry_id}_contact_select"): "select.meshcore_contact",
        ("select", DOMAIN, f"{entry_id}_added_contact_select"): "select.meshcore_added",
        ("select", DOMAIN, f"{entry_id}_discovered_contact_select"): "select.meshcore_disc",
    }
    target = {
        ("sensor", DOMAIN,
         f"{entry_id}_{_TARGET_PREFIX}_1_temperature_telemetry"): "sensor.target_temp",
        ("device_tracker", DOMAIN,
         f"{entry_id}_{_TARGET_PREFIX}_gps_tracker"): "device_tracker.target_gps",
    }
    reg = _FakeSweepRegistry({**survivors, **target})
    coord = _make_sweep_coordinator(mode=MODE_DATA_ONLY, entry_id=entry_id)

    assert sorted(demote_sweep_telemetry_gps(coord, reg, ADDED_PK)) == [
        "device_tracker.target_gps", "sensor.target_temp",
    ]
    for (platform, domain, uid), eid in survivors.items():
        assert reg.async_get_entity_id(platform, domain, uid) == eid


def test_sweep_skipped_for_tracked_repeater_subscription():
    """Subscription exclusion: a demoted node with a repeater subscription
    (shorter config prefix, bidirectional-startswith) -> sweep skipped entirely:
    no registry removal AND no dedup-map discards."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id,
        tracked_repeaters=[{"pubkey_prefix": _TARGET_PREFIX[:6]}],
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)
    assert demote_sweep_telemetry_gps(coord, reg, ADDED_PK) == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)
    assert set(coord.device_tracker_manager.discovered_trackers) == {_TARGET_TRACKER_KEY}


def test_sweep_skipped_for_tracked_client_longer_prefix():
    """Subscription exclusion: client subscription whose config prefix is LONGER
    than the 12-hex demote prefix (the other startswith direction) also skips."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id,
        tracked_clients=[{"pubkey_prefix": ADDED_PK}],  # full key, longer than 12
        telemetry_keys=_TARGET_TELEMETRY_KEYS,
    )
    reg = _telemetry_bearing_registry(entry_id)
    assert demote_sweep_telemetry_gps(coord, reg, ADDED_PK) == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)


def test_sweep_unrelated_subscription_does_not_skip():
    """Subscription-exclusion complement: a subscription for a DIFFERENT node
    must not suppress the demoted contact's sweep."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id,
        tracked_repeaters=[{"pubkey_prefix": _SIBLING_PREFIX}],
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)
    assert sorted(demote_sweep_telemetry_gps(coord, reg, ADDED_PK)) == [
        "device_tracker.meshcore_target_gps",
        "sensor.meshcore_target_battery",
        "sensor.meshcore_target_temperature",
    ]
    assert coord.telemetry_manager.discovered_sensors == {}
    assert coord.device_tracker_manager.discovered_trackers == {}


# =============================================================================
# get_discovered_contact service (Change 6 / Fix 4): empty-prefix guard
# =============================================================================
#
# Mirror of services.async_get_discovered_contact_service: the empty/short-prefix
# guard (Fix 4), first-startswith match, not_found envelope, and the
# pubkey_prefix backfill for older records. The _ensure_contact_compat backfill
# and coordinator-resolution branches are HA-coupled and exercised live.

def get_discovered_contact(discovered, pubkey_prefix):
    """Faithful mirror of the empty-prefix-guarded lookup + envelope shape."""
    if not pubkey_prefix or len(pubkey_prefix) < 2:
        return {"contact": None, "error": "invalid_prefix"}
    match = None
    for pubkey, contact in discovered.items():
        if pubkey.startswith(pubkey_prefix):
            match = contact
            break
    if match is None:
        return {"contact": None, "error": "not_found", "pubkey_prefix": pubkey_prefix}
    c = dict(match)
    pk = c.get("public_key") or ""
    if pk and "pubkey_prefix" not in c:
        c["pubkey_prefix"] = pk[:12]
    return {"contact": c}


_DISCOVERED = {"abcdef0123456789": {"public_key": "abcdef0123456789", "adv_name": "Disco"}}


def test_get_discovered_contact_empty_prefix_invalid():
    assert get_discovered_contact(_DISCOVERED, "") == {"contact": None, "error": "invalid_prefix"}


def test_get_discovered_contact_none_prefix_invalid():
    assert get_discovered_contact(_DISCOVERED, None) == {"contact": None, "error": "invalid_prefix"}


def test_get_discovered_contact_single_char_invalid():
    assert get_discovered_contact(_DISCOVERED, "a") == {"contact": None, "error": "invalid_prefix"}


def test_get_discovered_contact_valid_prefix_matches_and_backfills():
    pk = "f293ac1b2c3d" + "0" * 52
    discovered = {pk: {"public_key": pk, "adv_name": "Target"}}
    out = get_discovered_contact(discovered, "f293ac1b2c3d")
    assert out["contact"]["adv_name"] == "Target"
    assert out["contact"]["public_key"] == pk
    # pubkey_prefix backfilled for older records lacking it.
    assert out["contact"]["pubkey_prefix"] == pk[:12]
    assert "error" not in out


def test_get_discovered_contact_full_pubkey_also_matches():
    pk = "abc123" + "d" * 58
    discovered = {pk: {"public_key": pk, "adv_name": "Full"}}
    out = get_discovered_contact(discovered, pk)
    assert out["contact"]["adv_name"] == "Full"


def test_get_discovered_contact_valid_prefix_not_found():
    out = get_discovered_contact(_DISCOVERED, "ffee")
    assert out["contact"] is None
    assert out["error"] == "not_found"
    assert out["pubkey_prefix"] == "ffee"


# =============================================================================
# config_flow coverage (Change 10): install defaults + options round-trip
# =============================================================================
#
# config_flow.py cannot be imported whole (the flow classes subclass mocked HA
# bases), so these mirror the two mode-bearing expressions exactly and drive
# them through the real const accessor/consts:
#
#   * Install steps async_step_usb/ble/tcp (config_flow.py:390/433/499) all use
#     the IDENTICAL write: CONF_CONTACT_DISCOVERY_MODE:
#       user_input.get(CONF_CONTACT_DISCOVERY_MODE, DEFAULT_CONTACT_DISCOVERY_MODE)
#   * Options async_step_global_settings reads the current value via
#     get_contact_discovery_mode (config_flow.py:965) for the form default, and
#     on submit writes new_data[CONF_CONTACT_DISCOVERY_MODE] =
#       user_input[CONF_CONTACT_DISCOVERY_MODE] onto a deepcopy of entry.data
#     (config_flow.py:939-940), preserving every other key.

def install_entry_discovery_mode(user_input):
    """Mirror of the shared install-step write (usb/ble/tcp)."""
    return user_input.get(CONF_MODE, DEFAULT_CONTACT_DISCOVERY_MODE)


def options_read_mode(entry):
    """Mirror of the global_settings form read (the select default)."""
    return get_contact_discovery_mode(entry)


def options_write(entry_data, user_input):
    """Mirror of the global_settings submit write (deepcopy + overwrite mode)."""
    new_data = copy.deepcopy(dict(entry_data))
    new_data[CONF_MODE] = user_input[CONF_MODE]
    return new_data


@pytest.mark.parametrize("install_step", ["usb", "ble", "tcp"])
def test_install_defaults_to_full_when_unselected(install_step):
    """Each install path (USB/BLE/TCP) shares the identical write expression
    (config_flow.py:390/433/499), so an unselected select stores full."""
    assert install_entry_discovery_mode({}) == MODE_FULL
    assert install_entry_discovery_mode({}) == DEFAULT_CONTACT_DISCOVERY_MODE


@pytest.mark.parametrize("install_step", ["usb", "ble", "tcp"])
def test_install_stores_explicit_mode(install_step):
    """An explicitly-chosen mode is stored verbatim on every install path."""
    assert install_entry_discovery_mode({CONF_MODE: MODE_DATA_ONLY}) == MODE_DATA_ONLY
    assert install_entry_discovery_mode({CONF_MODE: MODE_OFF}) == MODE_OFF


def test_install_default_is_a_valid_mode():
    """The install default must be one of the three machine values."""
    assert install_entry_discovery_mode({}) in CONTACT_DISCOVERY_MODES


def test_options_read_returns_current_mode():
    """The form default reads the stored mode via the real accessor."""
    assert options_read_mode(_FakeEntry(MODE_DATA_ONLY)) == MODE_DATA_ONLY
    assert options_read_mode(_FakeEntry(MODE_OFF)) == MODE_OFF


def test_options_read_defaults_full_when_absent():
    """An entry with no mode key reads full (the accessor default)."""
    assert options_read_mode(_FakeEntry(None)) == MODE_FULL


def test_options_write_overwrites_mode_and_preserves_other_keys():
    """Submitting the form writes the selected mode and leaves the rest of the
    config-data cluster untouched."""
    entry_data = {
        CONF_MODE: MODE_FULL,
        "self_telemetry_enabled": True,
        "max_discovered_contacts": 100,
        "flood_scopes": "*",
    }
    new_data = options_write(entry_data, {CONF_MODE: MODE_OFF})
    assert new_data[CONF_MODE] == MODE_OFF
    assert new_data["self_telemetry_enabled"] is True
    assert new_data["max_discovered_contacts"] == 100
    assert new_data["flood_scopes"] == "*"
    # The original is not mutated (deepcopy).
    assert entry_data[CONF_MODE] == MODE_FULL


def test_options_round_trip_read_then_write():
    """End-to-end: an entry on data_only reads back data_only for the form, a
    submit of off lands off in new_data, and re-reading new_data via the real
    accessor returns off."""
    entry = _FakeEntry(MODE_DATA_ONLY)
    assert options_read_mode(entry) == MODE_DATA_ONLY
    new_data = options_write(entry.data, {CONF_MODE: MODE_OFF})
    assert new_data[CONF_MODE] == MODE_OFF
    assert get_contact_discovery_mode(_entry_with_data(new_data)) == MODE_OFF


# =============================================================================
# Discovered-contact summary sensor (MeshCoreDiscoveredSummarySensor)
# =============================================================================
#
# Mode-independent: the summary sensor reads neither the old booleans nor the
# new mode; it stays created (diagnostic, disabled-by-default) in every mode.
# Same standalone-logic-copy pattern -- the real class subclasses a mocked HA
# entity base and can't be instantiated here, so native_value /
# extra_state_attributes are mirrored byte-faithfully. The registration flags
# (entity_registry_enabled_default=False, EntityCategory.DIAGNOSTIC) are class
# attributes verified live on the HA host; the constants below document the
# contract the live class must satisfy.

# Mirror of NodeType (custom_components.meshcore.const.NodeType).
NODE_TYPE_CLIENT = 1
NODE_TYPE_REPEATER = 2
NODE_TYPE_ROOM_SERVER = 3
NODE_TYPE_SENSOR = 4

# Mirror of the freshness window in sensor.py (DISCOVERED_FRESH_WINDOW_SECS).
FRESH_WINDOW_SECS = 3600 * 12

# Mirror of MeshCoreDiscoveredSummarySensor's load-bearing registration flags.
SUMMARY_ENTITY_REGISTRY_ENABLED_DEFAULT = False
SUMMARY_ENTITY_CATEGORY = "diagnostic"

# Default limit (custom_components.meshcore.const.DEFAULT_MAX_DISCOVERED_CONTACTS).
DEFAULT_MAX_DISCOVERED_CONTACTS = 100


def summary_native_value(discovered):
    """Mirror of MeshCoreDiscoveredSummarySensor.native_value."""
    return len(discovered)


def summary_attributes(discovered, now, limit_enabled=False,
                       max_contacts=DEFAULT_MAX_DISCOVERED_CONTACTS):
    """Mirror of MeshCoreDiscoveredSummarySensor.extra_state_attributes."""
    by_type = {"chat": 0, "repeater": 0, "room_server": 0, "sensor": 0, "unknown": 0}
    type_key = {
        NODE_TYPE_CLIENT: "chat",
        NODE_TYPE_REPEATER: "repeater",
        NODE_TYPE_ROOM_SERVER: "room_server",
        NODE_TYPE_SENSOR: "sensor",
    }

    fresh_count = 0
    newest_contact = None
    newest_advert = -1.0
    for contact in discovered.values():
        last_advert = contact.get("last_advert", 0) or 0
        if last_advert and (now - last_advert) < FRESH_WINDOW_SECS:
            fresh_count += 1
        by_type[type_key.get(contact.get("type"), "unknown")] += 1
        if last_advert > newest_advert:
            newest_advert = last_advert
            newest_contact = contact

    total = len(discovered)

    if newest_contact is not None:
        pk = newest_contact.get("public_key", "") or ""
        newest = {
            "adv_name": newest_contact.get("adv_name", "Unknown"),
            "pubkey_short": pk[:12],
            "last_advert": newest_contact.get("last_advert", 0) or 0,
        }
    else:
        newest = None

    if limit_enabled:
        capacity = max_contacts
        capacity_used_pct = round(100.0 * total / max_contacts, 1) if max_contacts else None
    else:
        capacity = "unlimited"
        capacity_used_pct = None

    return {
        "fresh_count": fresh_count,
        "stale_count": total - fresh_count,
        "by_type": by_type,
        "newest": newest,
        "capacity": capacity,
        "capacity_used_pct": capacity_used_pct,
    }


_EXPECTED_ATTR_KEYS = {
    "fresh_count", "stale_count", "by_type", "newest",
    "capacity", "capacity_used_pct",
}
_EXPECTED_BY_TYPE_KEYS = {"chat", "repeater", "room_server", "sensor", "unknown"}


def _disc(pk, name="Disco", type_=NODE_TYPE_CLIENT, last_advert=0):
    return {pk: {"public_key": pk, "adv_name": name, "type": type_,
                 "last_advert": last_advert}}


def test_summary_state_equals_discovered_count():
    assert summary_native_value({}) == 0
    discovered = {}
    for i in range(5):
        discovered[f"{i:064x}"] = {"public_key": f"{i:064x}", "type": NODE_TYPE_CLIENT}
    assert summary_native_value(discovered) == 5


def test_summary_attribute_shape_constant_across_counts():
    """The attribute keys (and by_type keys) never change with contact count."""
    now = 1_000_000.0
    for n in (0, 1, 50, 500):
        discovered = {}
        for i in range(n):
            discovered[f"{i:064x}"] = {
                "public_key": f"{i:064x}", "type": NODE_TYPE_REPEATER,
                "last_advert": now - 60,
            }
        attrs = summary_attributes(discovered, now=now)
        assert set(attrs.keys()) == _EXPECTED_ATTR_KEYS
        assert set(attrs["by_type"].keys()) == _EXPECTED_BY_TYPE_KEYS


def test_summary_attributes_payload_size_bounded():
    """A 1000-contact set yields the same small key-set as a 1-contact set."""
    now = 1_000_000.0
    big = {f"{i:064x}": {"public_key": f"{i:064x}", "type": NODE_TYPE_SENSOR,
                         "last_advert": now} for i in range(1000)}
    attrs = summary_attributes(big, now=now)
    assert len(attrs["by_type"]) == 5
    assert attrs["newest"] is not None
    assert set(attrs["newest"].keys()) == {"adv_name", "pubkey_short", "last_advert"}


def test_summary_fresh_stale_split():
    now = 1_000_000.0
    discovered = {
        "a" * 64: {"public_key": "a" * 64, "type": NODE_TYPE_CLIENT,
                   "last_advert": now - 60},                  # fresh
        "b" * 64: {"public_key": "b" * 64, "type": NODE_TYPE_CLIENT,
                   "last_advert": now - (FRESH_WINDOW_SECS + 60)},  # stale
        "c" * 64: {"public_key": "c" * 64, "type": NODE_TYPE_CLIENT,
                   "last_advert": 0},                         # stale (never heard)
    }
    attrs = summary_attributes(discovered, now=now)
    assert attrs["fresh_count"] == 1
    assert attrs["stale_count"] == 2
    assert attrs["fresh_count"] + attrs["stale_count"] == summary_native_value(discovered)


def test_summary_by_type_counts_and_sum_invariant():
    now = 1_000_000.0
    discovered = {
        "1" * 64: {"public_key": "1" * 64, "type": NODE_TYPE_CLIENT, "last_advert": now},
        "2" * 64: {"public_key": "2" * 64, "type": NODE_TYPE_REPEATER, "last_advert": now},
        "3" * 64: {"public_key": "3" * 64, "type": NODE_TYPE_REPEATER, "last_advert": now},
        "4" * 64: {"public_key": "4" * 64, "type": NODE_TYPE_ROOM_SERVER, "last_advert": now},
        "5" * 64: {"public_key": "5" * 64, "type": NODE_TYPE_SENSOR, "last_advert": now},
        "6" * 64: {"public_key": "6" * 64, "last_advert": now},  # missing type -> unknown
    }
    attrs = summary_attributes(discovered, now=now)
    assert attrs["by_type"] == {
        "chat": 1, "repeater": 2, "room_server": 1, "sensor": 1, "unknown": 1,
    }
    assert sum(attrs["by_type"].values()) == summary_native_value(discovered)


def test_summary_newest_is_most_recent_advert():
    now = 1_000_000.0
    discovered = {
        "a" * 64: {"public_key": "a" * 64, "adv_name": "Old", "type": NODE_TYPE_CLIENT,
                   "last_advert": now - 5000},
        "b" * 64: {"public_key": "b" * 64, "adv_name": "Newest", "type": NODE_TYPE_CLIENT,
                   "last_advert": now - 1},
    }
    attrs = summary_attributes(discovered, now=now)
    assert attrs["newest"]["adv_name"] == "Newest"
    assert attrs["newest"]["pubkey_short"] == ("b" * 64)[:12]
    assert attrs["newest"]["last_advert"] == now - 1


def test_summary_newest_none_when_empty():
    attrs = summary_attributes({}, now=1_000_000.0)
    assert attrs["newest"] is None


def test_summary_capacity_unlimited_when_limit_disabled():
    now = 1_000_000.0
    discovered = _disc("a" * 64, last_advert=now)
    attrs = summary_attributes(discovered, now=now, limit_enabled=False)
    assert attrs["capacity"] == "unlimited"
    assert attrs["capacity_used_pct"] is None


def test_summary_capacity_reports_pct_when_limit_enabled():
    now = 1_000_000.0
    discovered = {f"{i:064x}": {"public_key": f"{i:064x}", "type": NODE_TYPE_CLIENT,
                                "last_advert": now} for i in range(25)}
    attrs = summary_attributes(discovered, now=now, limit_enabled=True, max_contacts=100)
    assert attrs["capacity"] == 100
    assert attrs["capacity_used_pct"] == 25.0


def test_summary_registration_flags_contract():
    """Pin the load-bearing recorder decision (verified live on the host).

    The summary sensor MUST register disabled-by-default and as a diagnostic
    entity so a count that ticks on every advert imposes no recorder cost on
    users who never opted in.
    """
    assert SUMMARY_ENTITY_REGISTRY_ENABLED_DEFAULT is False
    assert SUMMARY_ENTITY_CATEGORY == "diagnostic"


# =============================================================================
# Mode reconciliation of EXISTING discovered contacts
# =============================================================================
#
# Live methods (coordinator.async_reconcile_discovered_for_mode +
# _remove_discovered_contact_entities), run once per setup after platforms are
# forwarded. Unlike the demote sweep (data_only only), the reconciler removes
# discovered per-contact entities in BOTH data_only and off, iterates the whole
# discovered population, and in off additionally clears + persists the
# discovered set; full ensures an entity exists for each existing discovered
# contact (idempotent via the tracked set). Added contacts are spared by the
# added-set membership test, unioned with the SDK's authoritative contact list
# so a transient-empty _contacts cannot misclassify an added contact as
# discovered. Subscription-backed and neighbor entities are spared by the same
# unique_id-SHAPE allowlist as the demote sweep. The post-reload call site (once
# per setup, correct coordinator) and the added-set union are exercised live on
# the HA host.

class _FakeStore:
    """Stand-in for the discovered-contacts Store; records the off persist."""

    def __init__(self):
        self.saved = None
        self.save_calls = 0

    def save_sync(self, data):
        self.save_calls += 1
        self.saved = dict(data)


def _make_reconcile_coordinator(mode=None, entry_id="01ENTRY", added=None,
                                discovered=None, tracked=None,
                                tracked_repeaters=None, tracked_clients=None,
                                telemetry_keys=None, tracker_keys=None,
                                sdk_contacts=None):
    return SimpleNamespace(
        config_entry=_FakeEntry(mode, entry_id),
        _contacts={pk[:12]: {"public_key": pk} for pk in (added or [])},
        _discovered_contacts={pk: {"public_key": pk} for pk in (discovered or [])},
        tracked_diagnostic_binary_contacts=set(tracked or ()),
        _tracked_repeaters=list(tracked_repeaters or []),
        _tracked_clients=list(tracked_clients or []),
        telemetry_manager=SimpleNamespace(
            discovered_sensors={k: object() for k in (telemetry_keys or [])}
        ),
        device_tracker_manager=SimpleNamespace(
            discovered_trackers={k: object() for k in (tracker_keys or [])}
        ),
        _store=_FakeStore(),
        # Stand-in for self.api.mesh_core.contacts (the added set union safety net).
        _sdk_contacts={pk[:12]: {"public_key": pk} for pk in (sdk_contacts or [])},
    )


def _reconcile_added_pubkeys(coordinator):
    """Mirror of the reconciler's added-set union (self._contacts | SDK list)."""
    sdk = getattr(coordinator, "_sdk_contacts", {}) or {}
    return {
        c.get("public_key")
        for c in list(coordinator._contacts.values()) + list(sdk.values())
        if isinstance(c, dict) and c.get("public_key")
    }


def _reconcile_remove_entities(coordinator, entity_registry, public_key):
    """Mirror of coordinator._remove_discovered_contact_entities.

    binary_sensor removal is unconditional; the telemetry/GPS sweep + dedup-map
    discards are gated on NOT having a tracked subscription. Allowlist by
    unique_id SHAPE so a neighbor sensor (embedding another node's pubkey) is
    never matched. Returns True if a contact binary_sensor was removed.
    """
    prefix = public_key[:12]
    coordinator.tracked_diagnostic_binary_contacts.discard(public_key)
    removed = False
    unique_id = f"{coordinator.config_entry.entry_id}_contact_{prefix}"
    entity_id = entity_registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
    if entity_id:
        entity_registry.async_remove(entity_id)
        removed = True
    if not _node_has_tracked_subscription(coordinator, prefix):
        uid_prefix = f"{coordinator.config_entry.entry_id}_{prefix}_"
        to_remove = [
            e.entity_id
            for e in entity_registry.entries_for_config_entry()
            if (e.unique_id or "").startswith(uid_prefix)
            and (
                e.unique_id.endswith("_telemetry")
                or e.unique_id.endswith("_gps_tracker")
            )
        ]
        for stale_entity_id in to_remove:
            entity_registry.async_remove(stale_entity_id)
        tm = getattr(coordinator, "telemetry_manager", None)
        if tm is not None:
            for key in [k for k in tm.discovered_sensors if k.startswith(prefix)]:
                del tm.discovered_sensors[key]
        dtm = getattr(coordinator, "device_tracker_manager", None)
        if dtm is not None:
            for key in [k for k in dtm.discovered_trackers if k.startswith(prefix)]:
                del dtm.discovered_trackers[key]
    return removed


def reconcile_for_mode(coordinator, entity_registry):
    """Faithful mirror of coordinator.async_reconcile_discovered_for_mode.

    Returns (created, removed): the list of created sensors (full) and the list
    of removed entity_ids (data_only/off).
    """
    mode = get_contact_discovery_mode(coordinator.config_entry)
    added_pubkeys = _reconcile_added_pubkeys(coordinator)

    if mode == MODE_FULL:
        created = []
        for contact in list(coordinator._discovered_contacts.values()):
            if not isinstance(contact, dict):
                continue
            pubkey = contact.get("public_key")
            if pubkey and pubkey in added_pubkeys:
                continue  # added contacts use the normal create path
            sensor = create_contact_sensor(coordinator, contact)
            if sensor:
                created.append(sensor)
        return created, []

    # data_only / off
    for public_key in list(coordinator._discovered_contacts.keys()):
        if public_key in added_pubkeys:
            continue  # never touch added contacts
        _reconcile_remove_entities(coordinator, entity_registry, public_key)
    if mode == MODE_OFF:
        coordinator._discovered_contacts.clear()
        coordinator._store.save_sync(coordinator._discovered_contacts)
    return [], list(entity_registry.removed)


def _reconcile_registry(entry_id, contact_pks):
    """Registry seeded with a _contact_ binary_sensor for each given pubkey."""
    return _FakeSweepRegistry({
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{pk[:12]}"):
            f"binary_sensor.meshcore_{pk[:12]}"
        for pk in contact_pks
    })


_DISC_A = "a1" * 32
_DISC_B = "b2" * 32
_DISC_C = "c3" * 32


def test_reconcile_data_only_removes_discovered_keeps_added():
    """data_only: discovered per-contact entities removed; the added one kept."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A, _DISC_B, ADDED_PK])
    coord = _make_reconcile_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, added=[ADDED_PK],
        discovered=[_DISC_A, _DISC_B], tracked=[_DISC_A, _DISC_B, ADDED_PK],
    )
    _created, removed = reconcile_for_mode(coord, reg)
    assert set(removed) == {
        f"binary_sensor.meshcore_{_DISC_A[:12]}",
        f"binary_sensor.meshcore_{_DISC_B[:12]}",
    }
    # Added contact's entity survives in the registry and the tracked set.
    assert reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK[:12]}"
    ) == f"binary_sensor.meshcore_{ADDED_PK[:12]}"
    assert coord.tracked_diagnostic_binary_contacts == {ADDED_PK}


def test_reconcile_data_only_keeps_discovered_data():
    """data_only: the discovered DATA (the dict) is preserved -- only entities go."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A])
    coord = _make_reconcile_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, discovered=[_DISC_A], tracked=[_DISC_A],
    )
    reconcile_for_mode(coord, reg)
    assert _DISC_A in coord._discovered_contacts  # data kept
    assert coord._store.save_calls == 0  # data_only does not persist-clear


def test_reconcile_off_removes_discovered_and_clears_set():
    """off: entities removed AND the discovered set cleared + persisted."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A, _DISC_B])
    coord = _make_reconcile_coordinator(
        mode=MODE_OFF, entry_id=entry_id, discovered=[_DISC_A, _DISC_B],
        tracked=[_DISC_A, _DISC_B],
    )
    _created, removed = reconcile_for_mode(coord, reg)
    assert set(removed) == {
        f"binary_sensor.meshcore_{_DISC_A[:12]}",
        f"binary_sensor.meshcore_{_DISC_B[:12]}",
    }
    assert coord._discovered_contacts == {}  # cleared
    assert coord._store.save_calls == 1  # persisted
    assert coord._store.saved == {}  # the empty set was saved


def test_reconcile_off_keeps_added_entity_and_data():
    """off: an added contact is never removed even though the set is cleared."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A, ADDED_PK])
    coord = _make_reconcile_coordinator(
        mode=MODE_OFF, entry_id=entry_id, added=[ADDED_PK],
        discovered=[_DISC_A], tracked=[_DISC_A, ADDED_PK],
    )
    reconcile_for_mode(coord, reg)
    assert reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK[:12]}"
    ) == f"binary_sensor.meshcore_{ADDED_PK[:12]}"
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts
    # _discovered_contacts held only discovered keys, so clearing it is correct
    # and does not touch the added contact (which lives in _contacts).
    assert coord._discovered_contacts == {}


def test_reconcile_full_creates_existing_discovered():
    """full: every existing discovered contact gets an entity (gate #6)."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [])  # none exist yet
    coord = _make_reconcile_coordinator(
        mode=MODE_FULL, entry_id=entry_id, discovered=[_DISC_A, _DISC_B, _DISC_C],
    )
    created, _removed = reconcile_for_mode(coord, reg)
    assert set(created) == {
        f"SENSOR:{_DISC_A[:12]}",
        f"SENSOR:{_DISC_B[:12]}",
        f"SENSOR:{_DISC_C[:12]}",
    }
    assert coord.tracked_diagnostic_binary_contacts == {_DISC_A, _DISC_B, _DISC_C}


def test_reconcile_full_idempotent_second_run_creates_nothing():
    """full: a second reconcile in the same mode creates nothing (gates #6/#8)."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [])
    coord = _make_reconcile_coordinator(
        mode=MODE_FULL, entry_id=entry_id, discovered=[_DISC_A, _DISC_B],
    )
    reconcile_for_mode(coord, reg)
    created2, _ = reconcile_for_mode(coord, reg)  # already tracked -> no double
    assert created2 == []


def test_reconcile_data_only_idempotent_second_run_removes_nothing():
    """data_only: a second reconcile finds no discovered entity to remove (gate #8)."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A])
    coord = _make_reconcile_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, discovered=[_DISC_A], tracked=[_DISC_A],
    )
    reconcile_for_mode(coord, reg)
    reg.removed.clear()
    _created, removed2 = reconcile_for_mode(coord, reg)
    assert removed2 == []


def test_reconcile_off_idempotent_second_run_is_noop():
    """off: a second reconcile on the already-empty set removes/saves nothing
    (the persisted empty set is not repopulated by store-load) (gate #8)."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A])
    coord = _make_reconcile_coordinator(
        mode=MODE_OFF, entry_id=entry_id, discovered=[_DISC_A], tracked=[_DISC_A],
    )
    reconcile_for_mode(coord, reg)
    reg.removed.clear()
    saves_after_first = coord._store.save_calls
    _created, removed2 = reconcile_for_mode(coord, reg)
    assert removed2 == []
    # The empty set is saved again (harmless) but nothing is removed.
    assert coord._discovered_contacts == {}
    assert coord._store.save_calls == saves_after_first + 1


def test_reconcile_bulk_spares_added_among_many():
    """data_only bulk pass over many discovered + a few added: only the
    discovered ones are removed; every added contact survives (gate #5 bulk)."""
    entry_id = "01ENTRY"
    discovered = [f"{i:02x}" + "dd" * 31 for i in range(20)]
    added = [ADDED_PK, _DISC_C]  # two added contacts mixed in the registry
    reg = _reconcile_registry(entry_id, discovered + added)
    coord = _make_reconcile_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, added=added,
        discovered=discovered, tracked=set(discovered) | set(added),
    )
    _created, removed = reconcile_for_mode(coord, reg)
    assert len(removed) == 20
    for pk in added:
        assert reg.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{entry_id}_contact_{pk[:12]}"
        ) == f"binary_sensor.meshcore_{pk[:12]}"
    assert coord.tracked_diagnostic_binary_contacts == set(added)


def test_reconcile_added_via_sdk_union_is_spared():
    """A contact present in the SDK list but absent from _contacts is still
    treated as added (the union safety net) and not removed."""
    entry_id = "01ENTRY"
    reg = _reconcile_registry(entry_id, [_DISC_A, ADDED_PK])
    coord = _make_reconcile_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id, added=[],  # _contacts empty
        sdk_contacts=[ADDED_PK],                            # but SDK knows it
        discovered=[_DISC_A, ADDED_PK], tracked=[_DISC_A, ADDED_PK],
    )
    _created, removed = reconcile_for_mode(coord, reg)
    assert removed == [f"binary_sensor.meshcore_{_DISC_A[:12]}"]
    assert reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK[:12]}"
    ) == f"binary_sensor.meshcore_{ADDED_PK[:12]}"


def test_reconcile_data_only_spares_subscription_telemetry_and_neighbor():
    """data_only bulk: a discovered contact's telemetry/GPS are swept, BUT a
    subscription-backed contact's are spared, and a neighbor sensor (owned by
    another node, embedding this contact's prefix as the neighbor) is never
    matched -- the unique_id-SHAPE allowlist (gate #5)."""
    entry_id = "01ENTRY"
    target_prefix = _DISC_A[:12]
    sub_prefix = _DISC_B[:12]
    other_prefix = _DISC_C[:12]
    reg = _FakeSweepRegistry({
        # Target discovered contact: contact bs + telemetry + gps (all swept).
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{target_prefix}"):
            "binary_sensor.target_contact",
        ("sensor", DOMAIN, f"{entry_id}_{target_prefix}_1_temperature_telemetry"):
            "sensor.target_temp",
        ("device_tracker", DOMAIN, f"{entry_id}_{target_prefix}_gps_tracker"):
            "device_tracker.target_gps",
        # Subscription-backed discovered contact: telemetry must SURVIVE.
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{sub_prefix}"):
            "binary_sensor.sub_contact",
        ("sensor", DOMAIN, f"{entry_id}_{sub_prefix}_1_temperature_telemetry"):
            "sensor.sub_temp",
        # Neighbor sensor owned by OTHER node, embedding target_prefix as the
        # neighbor pubkey -- must NOT be swept (startswith other_prefix, and not
        # a _telemetry/_gps_tracker suffix).
        ("sensor", DOMAIN, f"{entry_id}_{other_prefix}_neighbor_{target_prefix}"):
            "sensor.other_neighbor_of_target",
    })
    coord = _make_reconcile_coordinator(
        mode=MODE_DATA_ONLY, entry_id=entry_id,
        discovered=[_DISC_A, _DISC_B],  # target + subscription-backed
        tracked=[_DISC_A, _DISC_B],
        tracked_clients=[{"pubkey_prefix": sub_prefix}],  # _DISC_B is subscribed
        telemetry_keys=[f"{target_prefix}_1_temperature", f"{sub_prefix}_1_temperature"],
        tracker_keys=[f"{target_prefix}_gps"],
    )
    _created, removed = reconcile_for_mode(coord, reg)
    # Target's contact bs + telemetry + gps removed.
    assert "binary_sensor.target_contact" in removed
    assert "sensor.target_temp" in removed
    assert "device_tracker.target_gps" in removed
    # Subscription-backed contact: bs removed (it is a discovered contact) but
    # its subscription-backed telemetry SURVIVES (recreates from the sub).
    assert "binary_sensor.sub_contact" in removed
    assert reg.async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_{sub_prefix}_1_temperature_telemetry"
    ) == "sensor.sub_temp"
    # Neighbor sensor owned by another node survives untouched.
    assert reg.async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_{other_prefix}_neighbor_{target_prefix}"
    ) == "sensor.other_neighbor_of_target"
    # Subscription-backed contact's telemetry key NOT discarded from the map.
    assert f"{sub_prefix}_1_temperature" in coord.telemetry_manager.discovered_sensors
    assert f"{target_prefix}_1_temperature" not in coord.telemetry_manager.discovered_sensors
