"""Real-import tests for binary_sensor.create_contact_sensor.

This replaces the AST-extraction shim (test_create_contact_sensor_real.py) and
the hand-written mirror in tests/test_contact_discovery_mode.py: it imports and
calls the REAL function under real Home Assistant, so the discovery-mode gate is
verified against shipped code rather than a copy of it. This is the gate where
the off-mode orphaned-entity bug (2026-06-15) and the #236 scoping bug shipped.

A lightweight MagicMock coordinator is sufficient — the gate only reads
config_entry.data, _contacts, and tracked_diagnostic_binary_contacts, and the
entity is constructed but not added to hass.
"""
from unittest.mock import MagicMock

from custom_components.meshcore.binary_sensor import create_contact_sensor
from custom_components.meshcore.const import (
    CONF_CONTACT_DISCOVERY_MODE,
    MODE_DATA_ONLY,
    MODE_FULL,
    MODE_OFF,
)

DISCOVERED_PK = "aa" * 32
ADDED_PK = "bb" * 32
DISCOVERED = {"adv_name": "peer", "public_key": DISCOVERED_PK}
ADDED = {"adv_name": "mine", "public_key": ADDED_PK}


def _coordinator(mode: str, added_pubkeys=()):
    coord = MagicMock()
    coord.config_entry.data = {CONF_CONTACT_DISCOVERY_MODE: mode}
    coord._contacts = {pk: {"public_key": pk} for pk in added_pubkeys}
    coord.tracked_diagnostic_binary_contacts = set()
    return coord


def test_full_mode_creates_entity_for_discovered():
    assert create_contact_sensor(_coordinator(MODE_FULL), DISCOVERED) is not None


def test_data_only_suppresses_discovered():
    assert create_contact_sensor(_coordinator(MODE_DATA_ONLY), DISCOVERED) is None


def test_off_suppresses_discovered():
    assert create_contact_sensor(_coordinator(MODE_OFF), DISCOVERED) is None


def test_data_only_keeps_added_contact():
    coord = _coordinator(MODE_DATA_ONLY, added_pubkeys=[ADDED_PK])
    assert create_contact_sensor(coord, ADDED) is not None


def test_off_keeps_added_contact():
    coord = _coordinator(MODE_OFF, added_pubkeys=[ADDED_PK])
    assert create_contact_sensor(coord, ADDED) is not None


def test_full_keeps_added_contact():
    coord = _coordinator(MODE_FULL, added_pubkeys=[ADDED_PK])
    assert create_contact_sensor(coord, ADDED) is not None


def test_missing_public_key_returns_none():
    assert create_contact_sensor(_coordinator(MODE_FULL), {"adv_name": "x"}) is None


def test_non_dict_returns_none():
    assert create_contact_sensor(_coordinator(MODE_FULL), "not-a-dict") is None


def test_already_tracked_returns_none():
    coord = _coordinator(MODE_FULL)
    coord.tracked_diagnostic_binary_contacts.add(DISCOVERED_PK)
    assert create_contact_sensor(coord, DISCOVERED) is None


def test_creating_entity_marks_contact_tracked():
    coord = _coordinator(MODE_FULL)
    create_contact_sensor(coord, DISCOVERED)
    assert DISCOVERED_PK in coord.tracked_diagnostic_binary_contacts
