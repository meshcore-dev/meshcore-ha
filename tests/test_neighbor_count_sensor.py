"""Tests for MeshCoreNeighborCountSensor.

The shared conftest.py preemptively replaces ``custom_components.meshcore``
and friends with MagicMocks so the legacy importlib-spec tests work. This
module wants real imports instead — the ``_real_sensor_module`` fixture
swaps the mocks out for the duration of the test session, then restores
them so legacy tests still see what they expect.
"""
import importlib
import sys
from unittest.mock import MagicMock

import pytest


_PACKAGES_TO_UNMOCK = [
    "custom_components",
    "custom_components.meshcore",
    "custom_components.meshcore.const",
    "custom_components.meshcore.coordinator",
    "custom_components.meshcore.meshcore_api",
    "custom_components.meshcore.utils",
    "custom_components.meshcore.mqtt_uploader",
    "custom_components.meshcore.binary_sensor",
]


@pytest.fixture(scope="module")
def sensor_module():
    """Import the real sensor module, restoring conftest mocks afterward."""
    saved = {name: sys.modules.pop(name, None) for name in _PACKAGES_TO_UNMOCK}
    try:
        module = importlib.import_module("custom_components.meshcore.sensor")
        yield module
    finally:
        for name in _PACKAGES_TO_UNMOCK:
            if saved[name] is not None:
                sys.modules[name] = saved[name]
            else:
                sys.modules.pop(name, None)
        sys.modules.pop("custom_components.meshcore.sensor", None)


def _make_sensor(sensor_module, neighbors: dict, *, repeater_pubkey: str = "abcdef123456"):
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = "entry123"
    coordinator._repeater_neighbors = {"abcdef123456": neighbors}
    return sensor_module.MeshCoreNeighborCountSensor(
        coordinator=coordinator,
        repeater_pubkey=repeater_pubkey,
        repeater_name="Test Repeater",
    )


def test_native_value_counts_all_neighbors(sensor_module):
    sensor = _make_sensor(sensor_module, {
        "n1": {"secs_ago": 10},
        "n2": {"secs_ago": 100},
        "n3": {"secs_ago": 999999},  # stale
    })
    assert sensor.native_value == 3


def test_native_value_zero_when_no_neighbors(sensor_module):
    sensor = _make_sensor(sensor_module, {})
    assert sensor.native_value == 0


def test_native_value_zero_when_repeater_unknown(sensor_module):
    sensor = _make_sensor(sensor_module, {"n1": {"secs_ago": 5}}, repeater_pubkey="missing")
    assert sensor.native_value == 0


def test_attributes_split_active_and_stale(sensor_module):
    sensor = _make_sensor(sensor_module, {
        "fresh1": {"secs_ago": 10},
        "fresh2": {"secs_ago": 259199},  # just under threshold
        "stale1": {"secs_ago": 259200},  # at threshold (stale)
        "stale2": {"secs_ago": 999999},
    })
    assert sensor.native_value == 4
    assert sensor.extra_state_attributes == {"active": 2, "stale": 2}


def test_attributes_treat_missing_secs_ago_as_zero(sensor_module):
    """Neighbors without secs_ago are treated as just-heard (active)."""
    sensor = _make_sensor(sensor_module, {
        "n1": {},
        "n2": {"secs_ago": 0},
    })
    assert sensor.extra_state_attributes == {"active": 2, "stale": 0}


def test_attributes_all_stale(sensor_module):
    sensor = _make_sensor(sensor_module, {
        "s1": {"secs_ago": 999999},
        "s2": {"secs_ago": 500000},
    })
    assert sensor.native_value == 2
    assert sensor.extra_state_attributes == {"active": 0, "stale": 2}


def test_attributes_empty_dict(sensor_module):
    sensor = _make_sensor(sensor_module, {})
    assert sensor.extra_state_attributes == {"active": 0, "stale": 0}
