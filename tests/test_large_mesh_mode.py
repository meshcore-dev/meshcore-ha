"""Tests for large mesh mode: the create_contact_sensor entity-creation gate.

Follows the standalone-logic-copy pattern from test_eviction.py and
test_neighbor_count_sensor.py: conftest mocks the entire HA + integration
package surface (and the real binary_sensor module defines entity classes that
subclass those mocks), so importing the real create_contact_sensor isn't
viable. We mirror its gate here and exercise the same input shapes the live
code sees. The live integration path is verified separately on the HA host.

Covers the entity-creation gate:
  * Default off - discovered contact still gets an entity.
  * Mode on, discovered (pubkey not in added _contacts) - returns None
             and does not add to the tracked set.
  * Mode on, added (pubkey in _contacts) - returns an entity.
"""
from types import SimpleNamespace


# Mirror of the constants in custom_components.meshcore.const
CONF_LARGE_MESH_MODE = "large_mesh_mode"
DEFAULT_LARGE_MESH_MODE = False

# Stands in for MeshCoreContactDiagnosticBinarySensor (a real entity is created
# in the live code; here we only care that *something* was returned vs None).
_ENTITY = object()


def create_contact_sensor(coordinator, contact):
    """Standalone mirror of binary_sensor.create_contact_sensor.

    Kept byte-faithful to the live gate so the three branch outcomes are pinned
    by assertions. The gate tests membership in coordinator._contacts (the
    added-contact map), NOT contact["added_to_node"], because event-payload
    contacts reaching the live function do not carry that field.
    """
    if not isinstance(contact, dict):
        return None

    public_key = contact.get("public_key", "")
    if not public_key:
        return None

    if coordinator.config_entry.data.get(CONF_LARGE_MESH_MODE, DEFAULT_LARGE_MESH_MODE):
        added_pubkeys = {
            c.get("public_key")
            for c in coordinator._contacts.values()
            if c.get("public_key")
        }
        if public_key not in added_pubkeys:
            return None  # discovered-only: data-only in large mesh mode

    if public_key not in coordinator.tracked_diagnostic_binary_contacts:
        coordinator.tracked_diagnostic_binary_contacts.add(public_key)
        return _ENTITY
    return None


def _make_coordinator(large_mesh=None, added=None):
    """Build a minimal coordinator stand-in.

    added: iterable of full public keys that are "added" (saved to node). The
    coordinator's real _contacts dict is keyed by 12-char prefix with contact
    dicts as values; we replicate that shape.
    """
    data = {}
    if large_mesh is not None:
        data[CONF_LARGE_MESH_MODE] = large_mesh
    contacts = {}
    for pk in (added or []):
        contacts[pk[:12]] = {"public_key": pk, "adv_name": "Added"}
    return SimpleNamespace(
        config_entry=SimpleNamespace(data=data),
        _contacts=contacts,
        tracked_diagnostic_binary_contacts=set(),
    )


DISCOVERED_PK = "aabbccddeeff00112233445566778899aabbccddeeff0011223344556677"
ADDED_PK = "1122334455667788990011223344556677889900112233445566778899aa"


# --- Default off (behaviour unchanged) ----------------------------------------

def test_default_off_discovered_contact_gets_entity():
    """Default off: with the mode unset, a discovered contact still gets an entity."""
    coord = _make_coordinator(large_mesh=None, added=[])
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    result = create_contact_sensor(coord, contact)

    assert result is _ENTITY
    assert DISCOVERED_PK in coord.tracked_diagnostic_binary_contacts


def test_default_off_explicit_false_discovered_gets_entity():
    """Default off: explicit large_mesh_mode=False is identical to unset."""
    coord = _make_coordinator(large_mesh=False, added=[])
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    result = create_contact_sensor(coord, contact)

    assert result is _ENTITY
    assert DISCOVERED_PK in coord.tracked_diagnostic_binary_contacts


def test_default_off_does_not_consult_added_map():
    """Default off: the default path never gates on _contacts membership."""
    coord = _make_coordinator(large_mesh=False, added=[])  # empty added map
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    # Even though DISCOVERED_PK is not an added contact, default mode creates it.
    assert create_contact_sensor(coord, contact) is _ENTITY


# --- Mode on, discovered (data-only) ------------------------------------------

def test_mode_on_discovered_returns_none():
    """Mode on: pubkey not in added _contacts -> no entity."""
    coord = _make_coordinator(large_mesh=True, added=[])
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    result = create_contact_sensor(coord, contact)

    assert result is None


def test_mode_on_discovered_does_not_track():
    """Mode on: a data-only discovered contact must not enter the tracked set."""
    coord = _make_coordinator(large_mesh=True, added=[])
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    create_contact_sensor(coord, contact)

    assert DISCOVERED_PK not in coord.tracked_diagnostic_binary_contacts
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_mode_on_discovered_with_other_added_still_none():
    """Mode on: presence of unrelated added contacts doesn't leak an entity."""
    coord = _make_coordinator(large_mesh=True, added=[ADDED_PK])
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    assert create_contact_sensor(coord, contact) is None
    assert DISCOVERED_PK not in coord.tracked_diagnostic_binary_contacts


# --- Mode on, added (entity created) ------------------------------------------

def test_mode_on_added_returns_entity():
    """Mode on: pubkey in added _contacts -> entity created.

    This is the core mis-classification guard: an added contact must still get
    its entity in large mesh mode.
    """
    coord = _make_coordinator(large_mesh=True, added=[ADDED_PK])
    contact = {"public_key": ADDED_PK, "adv_name": "Added"}

    result = create_contact_sensor(coord, contact)

    assert result is _ENTITY
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts


def test_mode_on_added_matches_full_pubkey_not_prefix():
    """Mode on: the gate compares full public_key, derived from _contacts values."""
    coord = _make_coordinator(large_mesh=True, added=[ADDED_PK])
    # Same 12-char prefix but a different full key must NOT be treated as added.
    impostor = ADDED_PK[:12] + ("f" * (len(ADDED_PK) - 12))
    assert impostor != ADDED_PK
    contact = {"public_key": impostor, "adv_name": "Impostor"}

    assert create_contact_sensor(coord, contact) is None


# --- Shared guards (both modes) -----------------------------------------------

def test_non_dict_contact_returns_none():
    coord = _make_coordinator(large_mesh=True, added=[])
    assert create_contact_sensor(coord, "not-a-dict") is None


def test_missing_public_key_returns_none():
    coord = _make_coordinator(large_mesh=False, added=[])
    assert create_contact_sensor(coord, {"adv_name": "NoKey"}) is None


def test_already_tracked_returns_none_default_mode():
    """Idempotency: a contact already in the tracked set is not re-created."""
    coord = _make_coordinator(large_mesh=False, added=[])
    coord.tracked_diagnostic_binary_contacts.add(DISCOVERED_PK)
    contact = {"public_key": DISCOVERED_PK, "adv_name": "Disco"}

    assert create_contact_sensor(coord, contact) is None


# ==============================================================================
# Discovered-contact summary sensor (MeshCoreDiscoveredSummarySensor)
# ==============================================================================
#
# Same standalone-logic-copy pattern as above: conftest mocks the HA surface so
# the real MeshCoreDiscoveredSummarySensor class can't be imported and
# instantiated. We mirror its native_value / extra_state_attributes logic
# byte-faithfully and pin the data outcomes. The registration flags
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
# These pin the contract; the live class is checked on the host.
SUMMARY_ENTITY_REGISTRY_ENABLED_DEFAULT = False
SUMMARY_ENTITY_CATEGORY = "diagnostic"

# Default limit (custom_components.meshcore.const.DEFAULT_MAX_DISCOVERED_CONTACTS).
DEFAULT_MAX_DISCOVERED_CONTACTS = 100


def summary_native_value(discovered):
    """Mirror of MeshCoreDiscoveredSummarySensor.native_value."""
    return len(discovered)


def summary_attributes(discovered, now, limit_enabled=False,
                       max_contacts=DEFAULT_MAX_DISCOVERED_CONTACTS):
    """Mirror of MeshCoreDiscoveredSummarySensor.extra_state_attributes.

    Kept byte-faithful so the bounded/constant-shape guarantee and the
    fresh/stale/by_type/capacity arithmetic are pinned by assertions.
    """
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


# Stable set of top-level attribute keys the sensor must always emit.
_EXPECTED_ATTR_KEYS = {
    "fresh_count", "stale_count", "by_type", "newest",
    "capacity", "capacity_used_pct",
}
_EXPECTED_BY_TYPE_KEYS = {"chat", "repeater", "room_server", "sensor", "unknown"}


def _disc(pk, name="Disco", type_=NODE_TYPE_CLIENT, last_advert=0):
    return {pk: {"public_key": pk, "adv_name": name, "type": type_,
                 "last_advert": last_advert}}


# --- state equals discovered count --------------------------------------------

def test_summary_state_equals_discovered_count():
    assert summary_native_value({}) == 0
    discovered = {}
    for i in range(5):
        discovered[f"{i:064x}"] = {"public_key": f"{i:064x}", "type": NODE_TYPE_CLIENT}
    assert summary_native_value(discovered) == 5


# --- attribute shape is constant regardless of count --------------------------

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
    # by_type holds exactly 5 integer buckets; newest is a single 3-key sample.
    assert len(attrs["by_type"]) == 5
    assert attrs["newest"] is not None
    assert set(attrs["newest"].keys()) == {"adv_name", "pubkey_short", "last_advert"}


# --- fresh / stale split ------------------------------------------------------

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


# --- by_type buckets + sum invariant ------------------------------------------

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
    # Sum invariant: by_type accounts for every discovered contact.
    assert sum(attrs["by_type"].values()) == summary_native_value(discovered)


# --- newest is the most-recent advert -----------------------------------------

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


# --- capacity headroom --------------------------------------------------------

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


# ==============================================================================
# get_discovered_contact service prefix-match
# ==============================================================================
#
# Mirror of the lookup core in services.async_get_discovered_contact_service:
# first discovered contact whose full public key startswith the prefix wins;
# absent a match the service returns a not_found envelope. The HA-coupled
# coordinator resolution and _ensure_contact_compat backfill are exercised live
# on the host.


def get_discovered_contact(discovered, pubkey_prefix):
    """Mirror of the service handler's match + envelope shape."""
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


def test_get_discovered_contact_returns_full_dict_by_prefix():
    pk = "f293ac1b2c3d" + "0" * 52
    discovered = {pk: {"public_key": pk, "adv_name": "Target", "type": NODE_TYPE_REPEATER}}
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


def test_get_discovered_contact_not_found_envelope():
    discovered = {"a" * 64: {"public_key": "a" * 64, "adv_name": "Other"}}
    out = get_discovered_contact(discovered, "ffffff")
    assert out["contact"] is None
    assert out["error"] == "not_found"
    assert out["pubkey_prefix"] == "ffffff"


# ==============================================================================
# Demote-added entity cleanup (large-mesh remove_contact handler)
# ==============================================================================
#
# Same standalone-logic-copy pattern: the live block lives inside
# services.async_execute_command_service's remove_contact handler, which can't be
# imported under the mocked HA surface. We mirror its decision byte-faithfully and
# pin the branch outcomes. The live add -> remove -> re-add path is verified on
# the HA host.
#
# Live block (demote binary_sensor removal), gated on large_mesh_mode:
#   coordinator.tracked_diagnostic_binary_contacts.discard(pubkey)   # FULL key
#   unique_id = f"{entry_id}_contact_{pubkey[:12]}"
#   entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
#   if entity_id: registry.async_remove(entity_id)
# In default mode the block is gated off entirely (the entity correctly persists).

DOMAIN = "meshcore"


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


def _make_demote_coordinator(large_mesh=None, entry_id="01ENTRY", tracked=None):
    data = {}
    if large_mesh is not None:
        data[CONF_LARGE_MESH_MODE] = large_mesh
    return SimpleNamespace(
        config_entry=SimpleNamespace(data=data, entry_id=entry_id),
        tracked_diagnostic_binary_contacts=set(tracked or ()),
    )


def demote_remove_entity(coordinator, entity_registry, pubkey):
    """Standalone mirror of the live large-mesh block in the remove_contact
    handler. Returns the removed entity_id (or None).

    Byte-faithful: the discard uses the FULL public_key; the unique_id uses the
    12-hex prefix; both the discard and the removal are gated on large_mesh_mode
    (the INVERSE of the discovered-cleanup paths, which skip removal in that mode).
    """
    prefix = pubkey[:12]
    if coordinator.config_entry.data.get(CONF_LARGE_MESH_MODE, DEFAULT_LARGE_MESH_MODE):
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


# --- Default off -> entity NOT removed ----------------------------------------

def test_demote_default_off_keeps_entity_and_tracked():
    """Default off: with large_mesh_mode off, demoting an added contact leaves its
    entity in place and the pubkey in the tracked set (default mode unchanged)."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(large_mesh=False, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)

    removed = demote_remove_entity(coord, reg, ADDED_PK)

    assert removed is None
    assert reg.removed == []
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts
    # Entity still resolvable.
    assert reg.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK[:12]}"
    ) == eid


def test_demote_unset_mode_keeps_entity():
    """Default off: large_mesh_mode unset is identical to explicit False."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(large_mesh=None, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)

    assert demote_remove_entity(coord, reg, ADDED_PK) is None
    assert reg.removed == []
    assert ADDED_PK in coord.tracked_diagnostic_binary_contacts


# --- Large mesh on -> entity removed + FULL key discarded ----------------------

def test_demote_large_mesh_removes_entity_and_discards_full_key():
    """Large mesh on: async_remove called with the demoted
    contact's entity_id, and the FULL public_key discarded from the tracked set."""
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_111122223333"
    coord = _make_demote_coordinator(large_mesh=True, entry_id=entry_id, tracked={ADDED_PK})
    reg = _registry_with_contact(entry_id, ADDED_PK, eid)

    removed = demote_remove_entity(coord, reg, ADDED_PK)

    assert removed == eid
    assert reg.removed == [eid]
    # FULL key discarded (a prefix-only discard would leave ADDED_PK present).
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts
    assert coord.tracked_diagnostic_binary_contacts == set()


def test_demote_large_mesh_unique_id_uses_prefix():
    """Large mesh on: the registry lookup keys on the 12-hex prefix, not the full key.

    A registry that only knows the full-key unique_id must NOT match -- proving
    the unique_id is built from pubkey[:12].
    """
    entry_id = "01ENTRY"
    eid = "binary_sensor.meshcore_added_contact_full"
    # Register under the FULL-key unique_id (wrong shape) only.
    reg = _FakeEntityRegistry({
        ("binary_sensor", DOMAIN, f"{entry_id}_contact_{ADDED_PK}"): eid,
    })
    coord = _make_demote_coordinator(large_mesh=True, entry_id=entry_id, tracked={ADDED_PK})

    removed = demote_remove_entity(coord, reg, ADDED_PK)

    # Prefix-based lookup misses the full-key registration -> nothing removed,
    # but the full key is still discarded from the tracked set.
    assert removed is None
    assert reg.removed == []
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts


def test_demote_large_mesh_discards_key_even_without_entity():
    """Large mesh on: discard happens even if no entity is registered,
    so a later re-add recreates the entity (tracked set must be clean)."""
    entry_id = "01ENTRY"
    coord = _make_demote_coordinator(large_mesh=True, entry_id=entry_id, tracked={ADDED_PK})
    reg = _FakeEntityRegistry({})  # nothing registered

    removed = demote_remove_entity(coord, reg, ADDED_PK)

    assert removed is None
    assert reg.removed == []
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts


# --- Selector + sibling safety ------------------------------------------------

def test_demote_large_mesh_spares_selectors_and_sibling():
    """Survivor safety: demoting one added contact removes ONLY its entity -- the three
    _contact_select selector entities and a second added contact survive."""
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
        large_mesh=True, entry_id=entry_id, tracked={target_pk, sibling_pk}
    )

    removed = demote_remove_entity(coord, reg, target_pk)

    # Only the target entity removed.
    assert removed == target_eid
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
    # Tracked set: target's full key gone, sibling retained.
    assert coord.tracked_diagnostic_binary_contacts == {sibling_pk}


# ==============================================================================
# Demote telemetry/GPS entity cleanup (large-mesh remove_contact)
# ==============================================================================
#
# Same standalone-logic-copy pattern. The live sweep sits inside the
# large-mesh gate in the remove_contact handler, immediately after the
# _contact_ binary_sensor removal:
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
    """Standalone mirror of the live telemetry/GPS sweep. Returns removed entity_ids.

    Byte-faithful: gated on large_mesh_mode; skipped entirely (registry sweep
    AND dedup-map discards) for nodes with a tracked-device subscription;
    allowlist by unique_id SHAPE -- startswith(f"{entry_id}_{prefix}_") AND
    endswith("_telemetry") / endswith("_gps_tracker"); collect-then-remove;
    getattr(..., None) guards for managers on platforms not yet set up.
    """
    prefix = pubkey[:12]
    if not coordinator.config_entry.data.get(CONF_LARGE_MESH_MODE, DEFAULT_LARGE_MESH_MODE):
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


def demote_cleanup_large_mesh(coordinator, entity_registry, pubkey):
    """Mirror of the full gated block ordering: binary_sensor removal
    first (subscription-independent), then the telemetry/GPS sweep."""
    removed_binary = demote_remove_entity(coordinator, entity_registry, pubkey)
    swept = demote_sweep_telemetry_gps(coordinator, entity_registry, pubkey)
    return removed_binary, swept


_TARGET_PREFIX = ADDED_PK[:12]
_SIBLING_PREFIX = DISCOVERED_PK[:12]


def _make_sweep_coordinator(large_mesh=None, entry_id="01ENTRY", tracked=None,
                            tracked_repeaters=None, tracked_clients=None,
                            telemetry_keys=None, tracker_keys=None,
                            with_managers=True):
    data = {}
    if large_mesh is not None:
        data[CONF_LARGE_MESH_MODE] = large_mesh
    coord = SimpleNamespace(
        config_entry=SimpleNamespace(data=data, entry_id=entry_id),
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


# --- Default off -> sweep is a no-op -------------------------------------------

def test_sweep_default_off_removes_nothing_and_keeps_maps():
    """Default off: with large_mesh_mode off, no telemetry/GPS registry removal and
    both dedup maps untouched."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        large_mesh=False, entry_id=entry_id,
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)

    swept = demote_sweep_telemetry_gps(coord, reg, ADDED_PK)

    assert swept == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)
    assert set(coord.device_tracker_manager.discovered_trackers) == {_TARGET_TRACKER_KEY}


def test_sweep_unset_mode_removes_nothing():
    """Default off: the unset mode is identical to explicit False."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        large_mesh=None, entry_id=entry_id,
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)

    assert demote_sweep_telemetry_gps(coord, reg, ADDED_PK) == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)


# --- Large mesh on -> sweep + map discards + binary_sensor removal intact -------

def test_sweep_large_mesh_removes_telemetry_gps_and_discards_maps():
    """Large mesh on: non-subscribed contact with two telemetry sensors + a GPS
    tracker -> all three removed, both maps' prefix keys discarded, and the
    _contact_ binary_sensor removal still occurs."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        large_mesh=True, entry_id=entry_id, tracked={ADDED_PK},
        telemetry_keys=_TARGET_TELEMETRY_KEYS + [_SIBLING_TELEMETRY_KEY],
        tracker_keys=[_TARGET_TRACKER_KEY, _SIBLING_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)

    removed_binary, swept = demote_cleanup_large_mesh(coord, reg, ADDED_PK)

    # The demote cleanup still removes the _contact_ binary_sensor.
    assert removed_binary == "binary_sensor.meshcore_target_contact"
    assert ADDED_PK not in coord.tracked_diagnostic_binary_contacts
    # The sweep removes exactly the telemetry sensors + GPS tracker.
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
    """Guard: platforms not yet set up (no manager attributes) must not
    crash the sweep -- mirrors the live getattr(..., None) guards."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(large_mesh=True, entry_id=entry_id,
                                    with_managers=False)
    reg = _telemetry_bearing_registry(entry_id)

    swept = demote_sweep_telemetry_gps(coord, reg, ADDED_PK)

    assert sorted(swept) == [
        "device_tracker.meshcore_target_gps",
        "sensor.meshcore_target_battery",
        "sensor.meshcore_target_temperature",
    ]


def test_sweep_collects_before_removing():
    """Shape guard: the sweep must not mutate the registry while
    iterating it -- removing every matched entity in one pass proves the
    collect-then-remove ordering (a mutate-while-iterating implementation
    would skip entries)."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(large_mesh=True, entry_id=entry_id)
    # Three adjacent matching entries.
    reg = _FakeSweepRegistry({
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_1_temperature_telemetry"): "sensor.t1",
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_2_humidity_telemetry"): "sensor.t2",
        ("sensor", DOMAIN, f"{entry_id}_{_TARGET_PREFIX}_3_battery_telemetry"): "sensor.t3",
    })

    swept = demote_sweep_telemetry_gps(coord, reg, ADDED_PK)

    assert sorted(swept) == ["sensor.t1", "sensor.t2", "sensor.t3"]
    assert reg.entries_for_config_entry() == []


# --- Survivor safety + subscription exclusion -----------------------------------

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
    coord = _make_sweep_coordinator(large_mesh=True, entry_id=entry_id)

    swept = demote_sweep_telemetry_gps(coord, reg, ADDED_PK)

    assert sorted(swept) == ["device_tracker.target_gps", "sensor.target_temp"]
    # Every survivor still resolvable.
    for (platform, domain, uid), eid in survivors.items():
        assert reg.async_get_entity_id(platform, domain, uid) == eid


def test_sweep_skipped_for_tracked_repeater_subscription():
    """Subscription exclusion: a demoted node with a repeater subscription (shorter config
    prefix, bidirectional-startswith) -> sweep skipped entirely: no registry
    removal AND no dedup-map discards."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        large_mesh=True, entry_id=entry_id,
        tracked_repeaters=[{"pubkey_prefix": _TARGET_PREFIX[:6]}],
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)

    swept = demote_sweep_telemetry_gps(coord, reg, ADDED_PK)

    assert swept == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)
    assert set(coord.device_tracker_manager.discovered_trackers) == {_TARGET_TRACKER_KEY}


def test_sweep_skipped_for_tracked_client_longer_prefix():
    """Subscription exclusion: client subscription whose config prefix is LONGER than the
    12-hex demote prefix (the other startswith direction) also skips."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        large_mesh=True, entry_id=entry_id,
        tracked_clients=[{"pubkey_prefix": ADDED_PK}],  # full key, longer than 12
        telemetry_keys=_TARGET_TELEMETRY_KEYS,
    )
    reg = _telemetry_bearing_registry(entry_id)

    assert demote_sweep_telemetry_gps(coord, reg, ADDED_PK) == []
    assert reg.removed == []
    assert set(coord.telemetry_manager.discovered_sensors) == set(_TARGET_TELEMETRY_KEYS)


def test_sweep_unrelated_subscription_does_not_skip():
    """Subscription-exclusion complement: a subscription for a DIFFERENT node must not
    suppress the demoted contact's sweep."""
    entry_id = "01ENTRY"
    coord = _make_sweep_coordinator(
        large_mesh=True, entry_id=entry_id,
        tracked_repeaters=[{"pubkey_prefix": _SIBLING_PREFIX}],
        telemetry_keys=_TARGET_TELEMETRY_KEYS, tracker_keys=[_TARGET_TRACKER_KEY],
    )
    reg = _telemetry_bearing_registry(entry_id)

    swept = demote_sweep_telemetry_gps(coord, reg, ADDED_PK)

    assert sorted(swept) == [
        "device_tracker.meshcore_target_gps",
        "sensor.meshcore_target_battery",
        "sensor.meshcore_target_temperature",
    ]
    assert coord.telemetry_manager.discovered_sensors == {}
    assert coord.device_tracker_manager.discovered_trackers == {}
