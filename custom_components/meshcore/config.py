"""Typed configuration records and settings for one MeshCore config entry.

Every user-owned setting is read here and nowhere else. Reads are options
first by membership (an options value wins even when it is ``False``, ``0`` or
``""``), then the legacy copy in ``entry.data``, then the default below.
Connection identity (transport, path/address/host/port, name, pubkey) is not a
setting: it stays in ``entry.data`` and is read directly by its owners.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

from .const import (
    CONF_ADAPTIVE_POLL_WAIT,
    CONF_AUTO_CLEANUP_STALE_CONTACTS,
    CONF_AUTO_CLEANUP_STALE_NEIGHBORS,
    CONF_CLI_CONSOLE_ENABLED,
    CONF_CLIENT_DISABLE_PATH_RESET,
    CONF_CLIENT_UPDATE_INTERVAL,
    CONF_CONSUME_INCOMING_MESSAGES,
    CONF_CONTACT_DISCOVERY_MODE,
    CONF_DEVICE_DISABLED,
    CONF_FLOOD_SCOPES,
    CONF_LIMIT_DISCOVERED_CONTACTS,
    CONF_MAP_UPLOAD_ENABLED,
    CONF_MAX_DISCOVERED_CONTACTS,
    CONF_MESSAGES_INTERVAL,
    CONF_MQTT_BROKERS,
    CONF_MQTT_DECODER_CMD,
    CONF_MQTT_IATA,
    CONF_MQTT_TOKEN_TTL_SECONDS,
    CONF_REPEATER_DISABLE_PATH_RESET,
    CONF_REPEATER_NEIGHBORS_ENABLED,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_REPEATER_UPDATE_INTERVAL,
    CONF_SELF_DIAGNOSTICS_ENABLED,
    CONF_SELF_DIAGNOSTICS_INTERVAL,
    CONF_SELF_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_INTERVAL,
    CONF_STALE_CONTACT_DAYS,
    CONF_STALE_NEIGHBOR_DAYS,
    CONF_TRACKED_CLIENTS,
    CONF_TRAFFIC_POLICY,
    DEFAULT_CLIENT_UPDATE_INTERVAL,
    DEFAULT_CONTACT_DISCOVERY_MODE,
    DEFAULT_MAX_DISCOVERED_CONTACTS,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    DEFAULT_SELF_DIAGNOSTICS_INTERVAL,
    DEFAULT_SELF_TELEMETRY_INTERVAL,
    DEFAULT_STALE_CONTACT_DAYS,
    DEFAULT_STALE_NEIGHBOR_DAYS,
    DEFAULT_TRAFFIC_POLICY,
    DEFAULT_UPDATE_TICK,
)

# Hex characters of a public key used to identify a node in stored records.
# Only applied when minting a new record; stored widths are left as they are.
PUBKEY_PREFIX_LENGTH: Final = 12

DEFAULT_MQTT_TOPIC_STATUS: Final = "meshcore/{IATA}/{PUBLIC_KEY}/status"
DEFAULT_MQTT_TOPIC_EVENTS: Final = "meshcore/{IATA}/{PUBLIC_KEY}/packets"
DEFAULT_MQTT_IATA: Final = "XYZ"
DEFAULT_MQTT_DECODER_CMD: Final = "meshcore-decoder"
MAX_MQTT_BROKERS: Final = 4

# Settings that live in entry.options once an entry has been migrated; the
# migration keeps the entry.data copies so a downgrade still finds them.
SETTINGS_KEYS: Final = (
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_TRACKED_CLIENTS,
    CONF_MESSAGES_INTERVAL,
    CONF_CONTACT_DISCOVERY_MODE,
    CONF_LIMIT_DISCOVERED_CONTACTS,
    CONF_MAX_DISCOVERED_CONTACTS,
    CONF_SELF_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_INTERVAL,
    CONF_SELF_DIAGNOSTICS_ENABLED,
    CONF_SELF_DIAGNOSTICS_INTERVAL,
    CONF_CLI_CONSOLE_ENABLED,
    CONF_MAP_UPLOAD_ENABLED,
    CONF_AUTO_CLEANUP_STALE_CONTACTS,
    CONF_STALE_CONTACT_DAYS,
    CONF_AUTO_CLEANUP_STALE_NEIGHBORS,
    CONF_STALE_NEIGHBOR_DAYS,
    CONF_CONSUME_INCOMING_MESSAGES,
    CONF_ADAPTIVE_POLL_WAIT,
    CONF_FLOOD_SCOPES,
    CONF_TRAFFIC_POLICY,
    CONF_MQTT_BROKERS,
    CONF_MQTT_IATA,
    CONF_MQTT_DECODER_CMD,
    CONF_MQTT_TOKEN_TTL_SECONDS,
)


def get_conf(entry: Any, key: str, default: Any = None) -> Any:
    """Read one setting: options by membership, then entry data, then default."""
    options = getattr(entry, "options", None) or {}
    if isinstance(options, Mapping) and key in options:
        return options[key]
    data = getattr(entry, "data", None)
    if isinstance(data, Mapping):
        return data.get(key, default)
    return default


def normalise_prefix(value: Any) -> str:
    """Lower-case a stored pubkey prefix, keeping the width it was stored at."""
    return value.strip().lower() if isinstance(value, str) else ""


def _as_int(value: Any, default: int) -> int:
    """Read an integer setting, falling back to the default when unusable."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    """Read a boolean setting, falling back to the default when unusable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_str(value: Any, default: str) -> str:
    """Read a string setting, falling back to the default when unusable."""
    return value if isinstance(value, str) else default


def _records(value: Any) -> list[Mapping[str, Any]]:
    """Return the stored mappings of a record list, ignoring junk entries."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _extras(raw: Mapping[str, Any], known: Sequence[str]) -> dict[str, Any]:
    """Keep the record fields this integration does not own, so edits are lossless."""
    return {key: value for key, value in raw.items() if key not in known}


@dataclass(frozen=True)
class RepeaterConfig:
    """One configured repeater subscription; observed firmware is not a setting."""

    name: str = ""
    pubkey_prefix: str = ""
    password: str = ""
    update_interval: int = DEFAULT_REPEATER_UPDATE_INTERVAL
    telemetry_enabled: bool = False
    neighbors_enabled: bool = False
    disable_path_reset: bool = False
    disabled: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "pubkey_prefix",
        CONF_REPEATER_PASSWORD,
        CONF_REPEATER_UPDATE_INTERVAL,
        CONF_REPEATER_TELEMETRY_ENABLED,
        CONF_REPEATER_NEIGHBORS_ENABLED,
        CONF_REPEATER_DISABLE_PATH_RESET,
        CONF_DEVICE_DISABLED,
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RepeaterConfig:
        """Build a record from its stored mapping, keeping unknown fields."""
        return cls(
            name=_as_str(raw.get("name"), ""),
            pubkey_prefix=normalise_prefix(raw.get("pubkey_prefix")),
            password=_as_str(raw.get(CONF_REPEATER_PASSWORD), ""),
            update_interval=_as_int(
                raw.get(CONF_REPEATER_UPDATE_INTERVAL), DEFAULT_REPEATER_UPDATE_INTERVAL
            ),
            telemetry_enabled=_as_bool(raw.get(CONF_REPEATER_TELEMETRY_ENABLED), False),
            neighbors_enabled=_as_bool(raw.get(CONF_REPEATER_NEIGHBORS_ENABLED), False),
            disable_path_reset=_as_bool(raw.get(CONF_REPEATER_DISABLE_PATH_RESET), False),
            disabled=_as_bool(raw.get(CONF_DEVICE_DISABLED), False),
            extra=_extras(raw, cls.FIELDS),
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the record back to the stored mapping shape."""
        record = dict(self.extra)
        record.update(
            {
                "name": self.name,
                "pubkey_prefix": self.pubkey_prefix,
                CONF_REPEATER_PASSWORD: self.password,
                CONF_REPEATER_UPDATE_INTERVAL: self.update_interval,
                CONF_REPEATER_TELEMETRY_ENABLED: self.telemetry_enabled,
                CONF_REPEATER_NEIGHBORS_ENABLED: self.neighbors_enabled,
                CONF_REPEATER_DISABLE_PATH_RESET: self.disable_path_reset,
                CONF_DEVICE_DISABLED: self.disabled,
            }
        )
        return record


@dataclass(frozen=True)
class ClientConfig:
    """One tracked client; distinct from a repeater, not a partial one."""

    name: str = ""
    pubkey_prefix: str = ""
    update_interval: int = DEFAULT_CLIENT_UPDATE_INTERVAL
    disable_path_reset: bool = False
    disabled: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "pubkey_prefix",
        CONF_CLIENT_UPDATE_INTERVAL,
        CONF_CLIENT_DISABLE_PATH_RESET,
        CONF_DEVICE_DISABLED,
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ClientConfig:
        """Build a record from its stored mapping, keeping unknown fields."""
        return cls(
            name=_as_str(raw.get("name"), ""),
            pubkey_prefix=normalise_prefix(raw.get("pubkey_prefix")),
            update_interval=_as_int(
                raw.get(CONF_CLIENT_UPDATE_INTERVAL), DEFAULT_CLIENT_UPDATE_INTERVAL
            ),
            disable_path_reset=_as_bool(raw.get(CONF_CLIENT_DISABLE_PATH_RESET), False),
            disabled=_as_bool(raw.get(CONF_DEVICE_DISABLED), False),
            extra=_extras(raw, cls.FIELDS),
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the record back to the stored mapping shape."""
        record = dict(self.extra)
        record.update(
            {
                "name": self.name,
                "pubkey_prefix": self.pubkey_prefix,
                CONF_CLIENT_UPDATE_INTERVAL: self.update_interval,
                CONF_CLIENT_DISABLE_PATH_RESET: self.disable_path_reset,
                CONF_DEVICE_DISABLED: self.disabled,
            }
        )
        return record


@dataclass(frozen=True)
class BrokerConfig:
    """One persisted MQTT broker slot, before the uploader expands its topics.

    ``iata`` and ``token_ttl_seconds`` stay ``None`` when the slot inherits the
    entry-wide value; the uploader owns that fallback and every other
    normalisation, so stored values are carried through unchanged.
    """

    enabled: bool = False
    server: str = ""
    port: Any = 1883
    transport: str = "tcp"
    use_tls: bool = False
    tls_verify: bool = True
    keepalive: Any = 60
    username: str = ""
    password: str = ""
    use_auth_token: bool = False
    token_audience: str = ""
    owner_public_key: str = ""
    owner_email: str = ""
    topic_status: str = DEFAULT_MQTT_TOPIC_STATUS
    topic_events: str = DEFAULT_MQTT_TOPIC_EVENTS
    payload_mode: str = "packet"
    iata: Any = None
    token_ttl_seconds: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    FIELDS: ClassVar[tuple[str, ...]] = (
        "enabled",
        "server",
        "port",
        "transport",
        "use_tls",
        "tls_verify",
        "keepalive",
        "username",
        "password",
        "use_auth_token",
        "token_audience",
        "owner_public_key",
        "owner_email",
        "topic_status",
        "topic_events",
        "payload_mode",
        "iata",
        "token_ttl_seconds",
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BrokerConfig:
        """Build a slot from its stored mapping, keeping values verbatim."""
        defaults = cls()
        values = {name: raw.get(name, getattr(defaults, name)) for name in cls.FIELDS}
        return cls(**values, extra=_extras(raw, cls.FIELDS))

    def to_dict(self) -> dict[str, Any]:
        """Render the slot back to the stored mapping shape."""
        record = dict(self.extra)
        record.update(
            {
                name: getattr(self, name)
                for name in self.FIELDS
                if getattr(self, name) is not None
            }
        )
        return record


@dataclass(frozen=True)
class Settings:
    """Every user-owned setting of one entry, read options first."""

    messages_interval: int = DEFAULT_UPDATE_TICK
    contact_discovery_mode: str = DEFAULT_CONTACT_DISCOVERY_MODE
    limit_discovered_contacts: bool = False
    max_discovered_contacts: int = DEFAULT_MAX_DISCOVERED_CONTACTS
    self_telemetry_enabled: bool = False
    self_telemetry_interval: int = DEFAULT_SELF_TELEMETRY_INTERVAL
    self_diagnostics_enabled: bool = False
    self_diagnostics_interval: int = DEFAULT_SELF_DIAGNOSTICS_INTERVAL
    cli_console_enabled: bool = False
    map_upload_enabled: bool = False
    auto_cleanup_stale_contacts: bool = False
    stale_contact_days: int = DEFAULT_STALE_CONTACT_DAYS
    auto_cleanup_stale_neighbors: bool = False
    stale_neighbor_days: int = DEFAULT_STALE_NEIGHBOR_DAYS
    consume_incoming_messages: bool = True
    adaptive_poll_wait: bool = False
    flood_scopes: str = ""
    traffic_policy: str = DEFAULT_TRAFFIC_POLICY
    mqtt_iata: Any = DEFAULT_MQTT_IATA
    mqtt_decoder_cmd: Any = DEFAULT_MQTT_DECODER_CMD
    mqtt_token_ttl_seconds: Any = None
    repeaters: tuple[RepeaterConfig, ...] = ()
    clients: tuple[ClientConfig, ...] = ()
    brokers: Mapping[str, BrokerConfig] = field(default_factory=dict)

    @classmethod
    def from_entry(cls, entry: Any) -> Settings:
        """Build the effective settings of a config entry."""
        brokers = get_conf(entry, CONF_MQTT_BROKERS, {}) or {}
        return cls(
            messages_interval=_as_int(
                get_conf(entry, CONF_MESSAGES_INTERVAL), DEFAULT_UPDATE_TICK
            ),
            contact_discovery_mode=_as_str(
                get_conf(entry, CONF_CONTACT_DISCOVERY_MODE),
                DEFAULT_CONTACT_DISCOVERY_MODE,
            ),
            limit_discovered_contacts=_as_bool(
                get_conf(entry, CONF_LIMIT_DISCOVERED_CONTACTS), False
            ),
            max_discovered_contacts=_as_int(
                get_conf(entry, CONF_MAX_DISCOVERED_CONTACTS),
                DEFAULT_MAX_DISCOVERED_CONTACTS,
            ),
            self_telemetry_enabled=_as_bool(
                get_conf(entry, CONF_SELF_TELEMETRY_ENABLED), False
            ),
            self_telemetry_interval=_as_int(
                get_conf(entry, CONF_SELF_TELEMETRY_INTERVAL),
                DEFAULT_SELF_TELEMETRY_INTERVAL,
            ),
            self_diagnostics_enabled=_as_bool(
                get_conf(entry, CONF_SELF_DIAGNOSTICS_ENABLED), False
            ),
            self_diagnostics_interval=_as_int(
                get_conf(entry, CONF_SELF_DIAGNOSTICS_INTERVAL),
                DEFAULT_SELF_DIAGNOSTICS_INTERVAL,
            ),
            cli_console_enabled=_as_bool(get_conf(entry, CONF_CLI_CONSOLE_ENABLED), False),
            map_upload_enabled=_as_bool(get_conf(entry, CONF_MAP_UPLOAD_ENABLED), False),
            auto_cleanup_stale_contacts=_as_bool(
                get_conf(entry, CONF_AUTO_CLEANUP_STALE_CONTACTS), False
            ),
            stale_contact_days=_as_int(
                get_conf(entry, CONF_STALE_CONTACT_DAYS), DEFAULT_STALE_CONTACT_DAYS
            ),
            auto_cleanup_stale_neighbors=_as_bool(
                get_conf(entry, CONF_AUTO_CLEANUP_STALE_NEIGHBORS), False
            ),
            stale_neighbor_days=_as_int(
                get_conf(entry, CONF_STALE_NEIGHBOR_DAYS), DEFAULT_STALE_NEIGHBOR_DAYS
            ),
            consume_incoming_messages=_as_bool(
                get_conf(entry, CONF_CONSUME_INCOMING_MESSAGES), True
            ),
            adaptive_poll_wait=_as_bool(get_conf(entry, CONF_ADAPTIVE_POLL_WAIT), False),
            flood_scopes=_as_str(get_conf(entry, CONF_FLOOD_SCOPES), ""),
            traffic_policy=_as_str(
                get_conf(entry, CONF_TRAFFIC_POLICY), DEFAULT_TRAFFIC_POLICY
            ),
            mqtt_iata=get_conf(entry, CONF_MQTT_IATA, DEFAULT_MQTT_IATA),
            mqtt_decoder_cmd=get_conf(entry, CONF_MQTT_DECODER_CMD, DEFAULT_MQTT_DECODER_CMD),
            mqtt_token_ttl_seconds=get_conf(entry, CONF_MQTT_TOKEN_TTL_SECONDS),
            repeaters=tuple(
                RepeaterConfig.from_dict(raw)
                for raw in _records(get_conf(entry, CONF_REPEATER_SUBSCRIPTIONS, []))
            ),
            clients=tuple(
                ClientConfig.from_dict(raw)
                for raw in _records(get_conf(entry, CONF_TRACKED_CLIENTS, []))
            ),
            brokers={
                str(slot): BrokerConfig.from_dict(raw)
                for slot, raw in (brokers.items() if isinstance(brokers, Mapping) else ())
                if isinstance(raw, Mapping)
            },
        )

    @property
    def repeater_records(self) -> list[dict[str, Any]]:
        """Repeater subscriptions in the stored mapping shape."""
        return [repeater.to_dict() for repeater in self.repeaters]

    @property
    def client_records(self) -> list[dict[str, Any]]:
        """Tracked clients in the stored mapping shape."""
        return [client.to_dict() for client in self.clients]

    @property
    def broker_records(self) -> dict[str, dict[str, Any]]:
        """MQTT broker slots in the stored mapping shape."""
        return {slot: broker.to_dict() for slot, broker in self.brokers.items()}
