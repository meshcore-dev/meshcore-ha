"""Diagnostics download for a MeshCore companion entry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .const import (
    CONF_CLIENT_DISABLE_PATH_RESET,
    CONF_CONNECTION_TYPE,
    CONF_DEVICE_DISABLED,
    CONF_REPEATER_NEIGHBORS_ENABLED,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_REPEATER_UPDATE_INTERVAL,
    DOMAIN,
)
from .traffic import iso_timestamp

# Key names that carry credentials or personal data. Any key containing one of
# these words is removed, except the few below that only name a setting.
_SECRET_WORDS = ("password", "secret", "token", "key", "username", "email")
_KEEP_KEYS = frozenset({"pubkey_prefix", "use_auth_token", "mqtt_token_ttl_seconds"})


def _secret_keys(data: Any, found: set[str] | None = None) -> set[str]:
    """Collect every key name in nested data that looks like a secret."""
    found = set() if found is None else found
    if isinstance(data, Mapping):
        for key, value in data.items():
            name = str(key).lower()
            if name not in _KEEP_KEYS and any(word in name for word in _SECRET_WORDS):
                found.add(key)
            _secret_keys(value, found)
    elif isinstance(data, (list, tuple)):
        for item in data:
            _secret_keys(item, found)
    return found


def _redact(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of data with secret-looking keys redacted."""
    return async_redact_data(dict(data), _secret_keys(data))


def _when(epoch_seconds: Any) -> str | None:
    """Format a stored epoch second, or None when it was never set."""
    if isinstance(epoch_seconds, (int, float)) and epoch_seconds > 0:
        return iso_timestamp(epoch_seconds)
    return None


def _size(value: Any) -> int | None:
    """Return the length of a container, or None when it is absent."""
    try:
        return len(value)
    except TypeError:
        return None


def _connection(entry: ConfigEntry, coordinator: Any) -> dict[str, Any]:
    """Describe the companion link and the identity it reported."""
    api = getattr(coordinator, "api", None)
    pubkey = getattr(coordinator, "pubkey", None) or ""
    return {
        "type": entry.data.get(CONF_CONNECTION_TYPE),
        "connected": bool(getattr(api, "connected", False)),
        "node_name": getattr(api, "node_name", None) or getattr(coordinator, "name", None),
        "pubkey_prefix": pubkey[:12] or None,
        "firmware": getattr(coordinator, "_firmware_version", None),
        "model": getattr(coordinator, "_hardware_model", None),
        "max_channels": getattr(coordinator, "_max_channels", None),
    }


def _lookup(coordinator: Any, table: str, prefix: str, default: Any = None) -> Any:
    """Read one node's value from a coordinator table keyed by pubkey prefix."""
    return (getattr(coordinator, table, None) or {}).get(prefix, default)


def _route(coordinator: Any, prefix: str) -> dict[str, Any] | None:
    """Return the route the companion holds for a node, None without a contact."""
    api = getattr(coordinator, "api", None)
    try:
        contact = api.contact_by_prefix(prefix) if api is not None else None
    except Exception:
        contact = None
    if not contact:
        return None
    path_len = contact.get("out_path_len", -1)
    if path_len is None or path_len < 0:
        return {"type": "flood"}
    return {"type": "direct", "path_len": path_len}


def _node(coordinator: Any, record: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """Describe one tracked node's schedule, failures and route."""
    prefix = record.get("pubkey_prefix", "")
    stats = getattr(coordinator, "_reliability_stats", {})
    node = {
        "kind": kind,
        "pubkey_prefix": prefix,
        "name": record.get("name"),
        "update_interval": record.get(CONF_REPEATER_UPDATE_INTERVAL),
        "disabled": record.get(CONF_DEVICE_DISABLED, False),
        "disable_path_reset": record.get(CONF_CLIENT_DISABLE_PATH_RESET, False),
        "auto_disabled": prefix in getattr(coordinator, "_auto_disabled_devices", set()),
        "next_telemetry": _when(_lookup(coordinator, "_next_telemetry_update_times", prefix)),
        "telemetry_failures": _lookup(coordinator, "_telemetry_consecutive_failures", prefix, 0),
        "last_success": _when(_lookup(coordinator, "_last_successful_request", prefix)),
        "request_successes": stats.get(f"{prefix}_request_successes", 0),
        "request_failures": stats.get(f"{prefix}_request_failures", 0),
        "route": _route(coordinator, prefix),
    }
    if kind == "repeater":
        node.update({
            "telemetry_enabled": record.get(CONF_REPEATER_TELEMETRY_ENABLED, False),
            "neighbors_enabled": record.get(CONF_REPEATER_NEIGHBORS_ENABLED, False),
            "next_status": _when(_lookup(coordinator, "_next_repeater_update_times", prefix)),
            "status_failures": _lookup(coordinator, "_repeater_consecutive_failures", prefix, 0),
            "last_login": _when(_lookup(coordinator, "_repeater_login_times", prefix)),
        })
    return node


def _traffic(coordinator: Any) -> dict[str, Any]:
    """Return the policy, the lane credits and the nodes waiting on a lane."""
    traffic: dict[str, Any] = {"policy": getattr(coordinator, "traffic_policy", None)}
    try:
        traffic["lanes"] = coordinator.traffic_attributes()
        if traffic["lanes"] is None:
            traffic["legacy_tokens"] = coordinator._rate_limiter.get_tokens()
    except Exception:
        traffic.setdefault("lanes", None)
    return traffic


def _sizes(coordinator: Any) -> dict[str, int | None]:
    """Return the size of each runtime store that grows with the mesh."""
    neighbors = getattr(coordinator, "_repeater_neighbors", {}) or {}
    try:
        companion_contacts = getattr(coordinator.api, "contacts", None)
    except Exception:
        companion_contacts = None
    stores = {
        "contacts_on_companion": companion_contacts,
        "contacts_cached": getattr(coordinator, "_contacts", None),
        "discovered_contacts": getattr(coordinator, "_discovered_contacts", None),
        "advert_paths": getattr(coordinator, "_advert_paths", None),
        "pending_rx_logs": getattr(coordinator, "_pending_rx_logs", None),
        "outgoing_correlation_keys": getattr(coordinator, "_outgoing_correlation_keys", None),
        "channel_info": getattr(coordinator, "_channel_info", None),
        "tracked_message_contacts": getattr(coordinator, "tracked_contacts", None),
        "neighbor_sensors": getattr(coordinator, "_created_neighbor_sensors", None),
        "cli_console_lines": getattr(coordinator, "cli_console_history", None),
        "deferred_nodes": getattr(coordinator, "_deferred_nodes", None),
    }
    sizes = {name: _size(value) for name, value in stores.items()}
    sizes["neighbors"] = sum(_size(table) or 0 for table in neighbors.values())
    for owner, attribute, name in (
        ("telemetry_manager", "discovered_sensors", "telemetry_sensors"),
        ("mqtt_uploader", "_publish_queue", "mqtt_publish_queue"),
        ("map_uploader", "_seen_adverts", "map_seen_adverts"),
    ):
        sizes[name] = _size(getattr(getattr(coordinator, owner, None), attribute, None))
    return sizes


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted snapshot of one companion entry for a bug report."""
    try:
        version = (await async_get_integration(hass, DOMAIN)).version
    except IntegrationNotFound:
        version = None
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    diagnostics: dict[str, Any] = {
        "integration_version": str(version) if version else None,
        "home_assistant_version": HA_VERSION,
        "entry": {
            "version": entry.version,
            "minor_version": getattr(entry, "minor_version", None),
            "state": str(entry.state),
            "data": _redact(entry.data),
            "options": _redact(entry.options),
        },
        "registry": {
            "entities": len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)),
            "devices": len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)),
        },
    }
    if coordinator is None:
        diagnostics["coordinator"] = None
        return diagnostics

    settings = getattr(coordinator, "settings", None)
    nodes = [
        _node(coordinator, record, "repeater")
        for record in getattr(settings, "repeater_records", [])
    ] + [
        _node(coordinator, record, "client")
        for record in getattr(settings, "client_records", [])
    ]
    diagnostics.update({
        "connection": _connection(entry, coordinator),
        "traffic": _traffic(coordinator),
        "nodes": nodes,
        "sizes": _sizes(coordinator),
    })
    return diagnostics
