"""Tests for MeshCoreNeighborCountSensor count and active/stale split logic.

Follows the standalone-logic-copy pattern from test_eviction.py: the conftest
mocks the entire HA + integration package surface so importing the real
sensor module isn't viable. The class's two interesting properties are tiny,
so we mirror them here and exercise the same input shapes the live code sees.
"""

# Mirror of NEIGHBOR_STALE_THRESHOLD in custom_components.meshcore.const
NEIGHBOR_STALE_THRESHOLD = 259200  # 72 hours


def _native_value(repeater_neighbors: dict, repeater_pubkey: str) -> int:
    """Mirror of MeshCoreNeighborCountSensor.native_value."""
    return len(repeater_neighbors.get(repeater_pubkey, {}))


def _extra_state_attributes(repeater_neighbors: dict, repeater_pubkey: str) -> dict:
    """Mirror of MeshCoreNeighborCountSensor.extra_state_attributes."""
    neighbors = repeater_neighbors.get(repeater_pubkey, {})
    active = sum(
        1 for n in neighbors.values()
        if n.get("secs_ago", 0) < NEIGHBOR_STALE_THRESHOLD
    )
    return {"active": active, "stale": len(neighbors) - active}


PUBKEY = "abcdef123456"


def test_native_value_counts_all_neighbors():
    repeater_neighbors = {PUBKEY: {
        "n1": {"secs_ago": 10},
        "n2": {"secs_ago": 100},
        "n3": {"secs_ago": 999999},
    }}
    assert _native_value(repeater_neighbors, PUBKEY) == 3


def test_native_value_zero_when_no_neighbors():
    assert _native_value({PUBKEY: {}}, PUBKEY) == 0


def test_native_value_zero_when_repeater_unknown():
    repeater_neighbors = {PUBKEY: {"n1": {"secs_ago": 5}}}
    assert _native_value(repeater_neighbors, "missingpubkey") == 0


def test_attributes_split_active_and_stale():
    repeater_neighbors = {PUBKEY: {
        "fresh1": {"secs_ago": 10},
        "fresh2": {"secs_ago": NEIGHBOR_STALE_THRESHOLD - 1},
        "stale1": {"secs_ago": NEIGHBOR_STALE_THRESHOLD},
        "stale2": {"secs_ago": 999999},
    }}
    assert _native_value(repeater_neighbors, PUBKEY) == 4
    assert _extra_state_attributes(repeater_neighbors, PUBKEY) == {"active": 2, "stale": 2}


def test_attributes_treat_missing_secs_ago_as_zero():
    """Neighbors without secs_ago are treated as just-heard (active)."""
    repeater_neighbors = {PUBKEY: {
        "n1": {},
        "n2": {"secs_ago": 0},
    }}
    assert _extra_state_attributes(repeater_neighbors, PUBKEY) == {"active": 2, "stale": 0}


def test_attributes_all_stale():
    repeater_neighbors = {PUBKEY: {
        "s1": {"secs_ago": 999999},
        "s2": {"secs_ago": 500000},
    }}
    assert _native_value(repeater_neighbors, PUBKEY) == 2
    assert _extra_state_attributes(repeater_neighbors, PUBKEY) == {"active": 0, "stale": 2}


def test_attributes_empty_dict():
    assert _extra_state_attributes({PUBKEY: {}}, PUBKEY) == {"active": 0, "stale": 0}
