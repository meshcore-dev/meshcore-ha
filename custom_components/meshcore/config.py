"""Typed configuration records and settings for one MeshCore config entry.

Every user-owned setting is read here and nowhere else. Reads are options
first by membership (an options value wins even when it is ``False``, ``0`` or
``""``), then the legacy copy in ``entry.data``, then the default below.
Connection identity (transport, path/address/host/port, name, pubkey) is not a
setting: it stays in ``entry.data`` and is read directly by its owners.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, Final, Literal

from .const import (
    CONF_ADAPTIVE_POLL_WAIT,
    CONF_AUTO_CLEANUP_STALE_CONTACTS,
    CONF_AUTO_CLEANUP_STALE_NEIGHBORS,
    CONF_BAUDRATE,
    CONF_BLE_ADDRESS,
    CONF_CLI_CONSOLE_ENABLED,
    CONF_CLIENT_DISABLE_PATH_RESET,
    CONF_CLIENT_UPDATE_INTERVAL,
    CONF_CONNECTION_TYPE,
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
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_TRACKED_CLIENTS,
    CONF_TRAFFIC_POLICY,
    CONF_USB_PATH,
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

_LOGGER = logging.getLogger(__name__)

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


def _as_given(value: Any, default: Any) -> Any:
    """Hand a setting through untouched, so its own owner can normalise it."""
    return default if value is None else value


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
class Connection:
    """How the entry reaches its radio; identity, read from entry data only."""

    connection_type: str = ""
    usb_path: Any = None
    baudrate: Any = None
    ble_address: Any = None
    tcp_host: Any = None
    tcp_port: Any = None

    @classmethod
    def from_entry(cls, entry: Any) -> Connection:
        """Read the transport identity of a config entry."""
        data = getattr(entry, "data", None)
        data = data if isinstance(data, Mapping) else {}
        return cls(
            connection_type=_as_str(data.get(CONF_CONNECTION_TYPE), ""),
            usb_path=data.get(CONF_USB_PATH),
            baudrate=data.get(CONF_BAUDRATE),
            ble_address=data.get(CONF_BLE_ADDRESS),
            tcp_host=data.get(CONF_TCP_HOST),
            tcp_port=data.get(CONF_TCP_PORT),
        )


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
    connection: Connection = field(default_factory=Connection)

    # Which entry key each setting reads, and how it is coerced; the default
    # is the field default above, so there is one place to change either.
    READERS: ClassVar[dict[str, tuple[str, Any]]] = {
        "messages_interval": (CONF_MESSAGES_INTERVAL, _as_int),
        "contact_discovery_mode": (CONF_CONTACT_DISCOVERY_MODE, _as_str),
        "limit_discovered_contacts": (CONF_LIMIT_DISCOVERED_CONTACTS, _as_bool),
        "max_discovered_contacts": (CONF_MAX_DISCOVERED_CONTACTS, _as_int),
        "self_telemetry_enabled": (CONF_SELF_TELEMETRY_ENABLED, _as_bool),
        "self_telemetry_interval": (CONF_SELF_TELEMETRY_INTERVAL, _as_int),
        "self_diagnostics_enabled": (CONF_SELF_DIAGNOSTICS_ENABLED, _as_bool),
        "self_diagnostics_interval": (CONF_SELF_DIAGNOSTICS_INTERVAL, _as_int),
        "cli_console_enabled": (CONF_CLI_CONSOLE_ENABLED, _as_bool),
        "map_upload_enabled": (CONF_MAP_UPLOAD_ENABLED, _as_bool),
        "auto_cleanup_stale_contacts": (CONF_AUTO_CLEANUP_STALE_CONTACTS, _as_bool),
        "stale_contact_days": (CONF_STALE_CONTACT_DAYS, _as_int),
        "auto_cleanup_stale_neighbors": (CONF_AUTO_CLEANUP_STALE_NEIGHBORS, _as_bool),
        "stale_neighbor_days": (CONF_STALE_NEIGHBOR_DAYS, _as_int),
        "consume_incoming_messages": (CONF_CONSUME_INCOMING_MESSAGES, _as_bool),
        "adaptive_poll_wait": (CONF_ADAPTIVE_POLL_WAIT, _as_bool),
        "flood_scopes": (CONF_FLOOD_SCOPES, _as_str),
        "traffic_policy": (CONF_TRAFFIC_POLICY, _as_str),
        # The uploader owns the normalisation of its own settings.
        "mqtt_iata": (CONF_MQTT_IATA, _as_given),
        "mqtt_decoder_cmd": (CONF_MQTT_DECODER_CMD, _as_given),
        "mqtt_token_ttl_seconds": (CONF_MQTT_TOKEN_TTL_SECONDS, _as_given),
    }

    @classmethod
    def from_entry(cls, entry: Any) -> Settings:
        """Build the effective settings of a config entry."""
        defaults = cls()
        brokers = get_conf(entry, CONF_MQTT_BROKERS, {})
        return cls(
            **{
                name: read(get_conf(entry, key), getattr(defaults, name))
                for name, (key, read) in cls.READERS.items()
            },
            repeaters=tuple(
                RepeaterConfig.from_dict(raw)
                for raw in _records(get_conf(entry, CONF_REPEATER_SUBSCRIPTIONS))
            ),
            clients=tuple(
                ClientConfig.from_dict(raw)
                for raw in _records(get_conf(entry, CONF_TRACKED_CLIENTS))
            ),
            brokers={
                str(slot): BrokerConfig.from_dict(raw)
                for slot, raw in (brokers.items() if isinstance(brokers, Mapping) else ())
                if isinstance(raw, Mapping)
            },
            connection=Connection.from_entry(entry),
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


ReloadScope = Literal["none", "apply", "reload"]
SCOPE_NONE: Final[ReloadScope] = "none"
SCOPE_APPLY: Final[ReloadScope] = "apply"
SCOPE_RELOAD: Final[ReloadScope] = "reload"

# Entry-wide toggles whose entities sit on the companion device and are only
# built at setup. Unlike a tracked node, they have no per-node builder to call
# on a loaded entry, so a change to these reloads it exactly as it always has.
ENTITY_SHAPING_FIELDS: Final = ("cli_console_enabled", "self_diagnostics_enabled")


def diff_settings(old: Settings, new: Settings) -> ReloadScope:
    """Classify a settings change: nothing to do, apply live, or reload.

    Two things still rebuild the entry, and nothing else does:

    - the radio's identity (``connection``), which is what the entry is
      built around;
    - ``ENTITY_SHAPING_FIELDS``, the two entry-wide toggles whose entities
      belong to the companion device and are only built at setup.

    Everything else is pushed into the running entry, tracked nodes included:
    adding, removing or editing one is the commonest edit there is, and a
    reload would reset the traffic budget and zero every other node's
    schedule to pay for it.
    """
    if old == new:
        return SCOPE_NONE
    if old.connection != new.connection:
        return SCOPE_RELOAD
    if any(getattr(old, name) != getattr(new, name) for name in ENTITY_SHAPING_FIELDS):
        return SCOPE_RELOAD
    return SCOPE_APPLY


async def apply_settings(hass: Any, entry: Any, coordinator: Any, new: Settings) -> None:
    """Push changed settings into a loaded entry without reloading the radio."""
    old = coordinator.settings
    coordinator.update_telemetry_settings(entry)

    if new.messages_interval != old.messages_interval:
        coordinator.update_interval = timedelta(seconds=new.messages_interval)
    if new.traffic_policy != old.traffic_policy:
        coordinator.apply_traffic_policy(new.traffic_policy)
    if new.contact_discovery_mode != old.contact_discovery_mode:
        await coordinator.async_reconcile_discovered_for_mode()
    if new.limit_discovered_contacts and (
        not old.limit_discovered_contacts
        or new.max_discovered_contacts < old.max_discovered_contacts
    ):
        await coordinator.async_evict_discovered_contacts(new.max_discovered_contacts)

    _apply_tracked_nodes(coordinator, old, new)
    await _apply_uploaders(hass, entry, coordinator, old, new)
    coordinator.async_update_listeners()


def _by_prefix(records: Sequence[Any]) -> dict[str, Any]:
    """Index tracked-node records by the prefix that identifies the node."""
    return {record.pubkey_prefix: record for record in records if record.pubkey_prefix}


def _apply_tracked_nodes(coordinator: Any, old: Settings, new: Settings) -> None:
    """Add, remove and edit tracked nodes on the loaded entry.

    Entities are built by the platforms that own them and handed to the
    add-entity callbacks they stored at setup, so a node added here is
    indistinguishable from one a fresh setup built.
    """
    for node_type, before, after in (
        ("repeater", old.repeaters, new.repeaters),
        ("client", old.clients, new.clients),
    ):
        was, now = _by_prefix(before), _by_prefix(after)
        for prefix in was.keys() - now.keys():
            coordinator.forget_tracked_node(prefix, node_type)
        for prefix in now.keys() - was.keys():
            coordinator.seed_tracked_node(prefix)
            _add_node_entities(coordinator, now[prefix].to_dict(), node_type)
        for prefix in now.keys() & was.keys():
            _apply_node_edit(coordinator, was[prefix], now[prefix])


def _add_entities(add: Any, entities: Sequence[Any]) -> None:
    """Hand new entities to a platform, skipping one that is not up yet."""
    if add is not None and entities:
        add(list(entities))


def _add_node_entities(coordinator: Any, record: dict[str, Any], node_type: str) -> None:
    """Create one tracked node's static entities through the platform builders."""
    from .binary_sensor import build_node_online_sensors
    from .button import build_repeater_buttons
    from .sensor import build_node_sensors

    _add_entities(
        getattr(coordinator, "sensor_add_entities", None),
        build_node_sensors(coordinator, record, node_type),
    )
    _add_entities(
        getattr(coordinator, "binary_sensor_async_add_entities", None),
        build_node_online_sensors(coordinator, record, node_type),
    )
    if node_type == "repeater":
        _add_entities(
            getattr(coordinator, "button_add_entities", None),
            build_repeater_buttons(coordinator, record),
        )


def _apply_node_edit(coordinator: Any, old: Any, new: Any) -> None:
    """Follow one tracked node's own edits on the loaded entry.

    Interval, telemetry, path-reset and disabled are read off the replaced
    record on the next tick, so only the neighbours toggle needs doing here:
    switching it on creates the counter a fresh setup would have built,
    switching it off tears the neighbour sensors down.
    """
    from .sensor import build_neighbor_count_sensors

    if old == new or not isinstance(new, RepeaterConfig):
        return
    if new.neighbors_enabled and not old.neighbors_enabled:
        _add_entities(
            getattr(coordinator, "sensor_add_entities", None),
            build_neighbor_count_sensors(coordinator, new.to_dict()),
        )
    elif old.neighbors_enabled and not new.neighbors_enabled:
        coordinator.cleanup_neighbor_entities(new.pubkey_prefix)


async def _apply_uploaders(
    hass: Any, entry: Any, coordinator: Any, old: Settings, new: Settings
) -> None:
    """Reconfigure the uploaders in place; a broker edit never touches the radio."""
    if coordinator.map_uploader is not None:
        coordinator.map_uploader.enabled = new.map_upload_enabled

    if (
        old.brokers == new.brokers
        and old.mqtt_iata == new.mqtt_iata
        and old.mqtt_decoder_cmd == new.mqtt_decoder_cmd
        and old.mqtt_token_ttl_seconds == new.mqtt_token_ttl_seconds
    ):
        return

    from .mqtt_uploader import MeshCoreMqttUploader

    previous = coordinator.mqtt_uploader
    version = getattr(previous, "integration_version", "unknown")
    if previous is not None:
        try:
            await previous.async_stop()
        except Exception as ex:
            _LOGGER.warning("MQTT uploader did not stop cleanly: %s", ex)
    coordinator.mqtt_uploader = None

    try:
        uploader = MeshCoreMqttUploader(
            hass, coordinator.logger, entry, api=coordinator.api,
            integration_version=version,
        )
        await uploader.async_start()
        coordinator.mqtt_uploader = uploader
    except Exception as ex:
        _LOGGER.warning("MQTT uploader failed to restart: %s - continuing without it", ex)
