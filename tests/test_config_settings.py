"""Settings precedence and record codecs, exercised against the real module."""

from types import SimpleNamespace

import pytest

from tests.support.modules import load_module

load_module("const")
CONFIG = load_module("config")

BrokerConfig = CONFIG.BrokerConfig
ClientConfig = CONFIG.ClientConfig
RepeaterConfig = CONFIG.RepeaterConfig
Settings = CONFIG.Settings


def entry(data=None, options=None):
    """Build the minimal config-entry shape the readers use."""
    return SimpleNamespace(data=data or {}, options=options or {})


def test_defaults_when_nothing_is_stored():
    """An empty entry produces the documented defaults."""
    settings = Settings.from_entry(entry())

    assert settings.messages_interval == 5
    assert settings.contact_discovery_mode == "full"
    assert settings.consume_incoming_messages is True
    assert settings.max_discovered_contacts == 100
    assert settings.stale_contact_days == 30
    assert settings.stale_neighbor_days == 7
    assert settings.traffic_policy == "legacy"
    assert settings.repeaters == ()
    assert settings.clients == ()
    assert settings.brokers == {}


@pytest.mark.parametrize(
    ("key", "field", "data_value", "options_value"),
    [
        ("messages_interval", "messages_interval", 5, 30),
        ("contact_discovery_mode", "contact_discovery_mode", "full", "off"),
        ("limit_discovered_contacts", "limit_discovered_contacts", True, False),
        ("max_discovered_contacts", "max_discovered_contacts", 100, 10),
        ("self_telemetry_enabled", "self_telemetry_enabled", True, False),
        ("self_telemetry_interval", "self_telemetry_interval", 300, 900),
        ("self_diagnostics_enabled", "self_diagnostics_enabled", True, False),
        ("self_diagnostics_interval", "self_diagnostics_interval", 300, 600),
        ("cli_console_enabled", "cli_console_enabled", True, False),
        ("map_upload_enabled", "map_upload_enabled", True, False),
        ("auto_cleanup_stale_contacts", "auto_cleanup_stale_contacts", True, False),
        ("stale_contact_days", "stale_contact_days", 30, 5),
        ("auto_cleanup_stale_neighbors", "auto_cleanup_stale_neighbors", True, False),
        ("stale_neighbor_days", "stale_neighbor_days", 7, 2),
        ("consume_incoming_messages", "consume_incoming_messages", True, False),
        ("adaptive_poll_wait", "adaptive_poll_wait", True, False),
        ("flood_scopes", "flood_scopes", "pl-mz", ""),
        ("traffic_policy", "traffic_policy", "legacy", "governed"),
        ("mqtt_iata", "mqtt_iata", "AAA", "BBB"),
        ("mqtt_decoder_cmd", "mqtt_decoder_cmd", "a", "b"),
        ("mqtt_token_ttl_seconds", "mqtt_token_ttl_seconds", 3600, 60),
    ],
)
def test_options_win_over_data_even_when_falsey(key, field, data_value, options_value):
    """Every setting reads options first, by membership rather than truthiness."""
    assert getattr(Settings.from_entry(entry({key: data_value})), field) == data_value
    settings = Settings.from_entry(entry({key: data_value}, {key: options_value}))
    assert getattr(settings, field) == options_value


def test_unusable_values_fall_back_to_the_default():
    """A malformed stored value never propagates into the typed settings."""
    settings = Settings.from_entry(
        entry({"stale_contact_days": "many", "flood_scopes": 7, "messages_interval": None})
    )

    assert settings.stale_contact_days == 30
    assert settings.flood_scopes == ""
    assert settings.messages_interval == 5


def test_repeater_record_round_trips_and_normalises():
    """Stored repeater fields survive the codec; the prefix is lower-cased."""
    raw = {
        "name": "Hilltop",
        "pubkey_prefix": "AABBCCDDEEFF",
        "password": "secret",
        "update_interval": 900,
        "telemetry_enabled": True,
        "neighbors_enabled": True,
        "disable_path_reset": True,
        "disabled": True,
        "firmware_version": "1.2.3 (Build: x)",
    }
    record = RepeaterConfig.from_dict(raw)

    assert record.pubkey_prefix == "aabbccddeeff"
    assert record.password == "secret"
    assert record.update_interval == 900
    assert record.extra == {"firmware_version": "1.2.3 (Build: x)"}
    assert record.to_dict() == {**raw, "pubkey_prefix": "aabbccddeeff"}
    assert RepeaterConfig.from_dict(record.to_dict()) == record


def test_repeater_record_defaults_fill_absent_fields():
    """A minimal historical record reads as today's defaults."""
    record = RepeaterConfig.from_dict({"name": "Old", "pubkey_prefix": "aabbcc"})

    assert record.pubkey_prefix == "aabbcc"
    assert record.update_interval == 7200
    assert record.telemetry_enabled is False
    assert record.neighbors_enabled is False
    assert record.disabled is False


def test_client_record_round_trips():
    """Client records are their own shape, not a partial repeater."""
    raw = {
        "name": "Handheld",
        "pubkey_prefix": "BBBBBBBBBBBB",
        "update_interval": 600,
        "disable_path_reset": True,
        "disabled": False,
    }
    record = ClientConfig.from_dict(raw)

    assert record.pubkey_prefix == "bbbbbbbbbbbb"
    assert record.to_dict() == {**raw, "pubkey_prefix": "bbbbbbbbbbbb"}
    assert ClientConfig.from_dict(record.to_dict()) == record


def test_broker_record_keeps_values_and_inherits_globals():
    """Broker slots keep stored values verbatim and omit inherited fields."""
    record = BrokerConfig.from_dict(
        {"enabled": True, "server": "mqtt.example", "client_id_prefix": "custom_"}
    )

    assert record.port == 1883
    assert record.iata is None
    assert record.token_ttl_seconds is None
    assert record.extra == {"client_id_prefix": "custom_"}

    stored = record.to_dict()
    assert "iata" not in stored
    assert "token_ttl_seconds" not in stored
    assert stored["client_id_prefix"] == "custom_"
    assert stored["topic_status"] == "meshcore/{IATA}/{PUBLIC_KEY}/status"
    assert BrokerConfig.from_dict(stored) == record


def test_settings_render_records_back_to_stored_shape():
    """The record views hand legacy consumers the mapping shape they expect."""
    settings = Settings.from_entry(
        entry(
            {
                "repeater_subscriptions": [{"name": "R", "pubkey_prefix": "AAAA"}],
                "tracked_clients": [{"name": "C", "pubkey_prefix": "bbbb"}],
                "mqtt_brokers": {"1": {"enabled": True, "server": "mqtt.example"}},
            }
        )
    )

    assert settings.repeater_records[0]["pubkey_prefix"] == "aaaa"
    assert settings.client_records[0]["name"] == "C"
    assert settings.broker_records["1"]["server"] == "mqtt.example"


def test_junk_record_lists_are_ignored():
    """A corrupted list never raises while the entry is being read."""
    settings = Settings.from_entry(
        entry({"repeater_subscriptions": "nonsense", "tracked_clients": [None, 3]})
    )

    assert settings.repeaters == ()
    assert settings.clients == ()


def test_get_conf_prefers_options_membership():
    """get_conf is the single reader: options membership, then data, then default."""
    assert CONFIG.get_conf(entry({"k": 1}, {"k": 0}), "k", 9) == 0
    assert CONFIG.get_conf(entry({"k": 1}), "k", 9) == 1
    assert CONFIG.get_conf(entry(), "k", 9) == 9
