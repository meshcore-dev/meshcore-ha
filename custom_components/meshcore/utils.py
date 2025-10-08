"""Utility functions for the MeshCore integration."""

from __future__ import annotations

import logging
from typing import Any

from .const import BAT_VMAX, BAT_VMIN, CHANNEL_PREFIX, DOMAIN, MESSAGES_SUFFIX, NodeType

_LOGGER = logging.getLogger(__name__)


def get_node_type_str(node_type: str | None) -> str:
    """Convert NodeType to a human-readable string."""
    if node_type == NodeType.CLIENT:
        return "Client"
    elif node_type == NodeType.REPEATER:
        return "Repeater"
    elif node_type == NodeType.ROOM_SERVER:
        return "Room Server"
    elif node_type == NodeType.SENSOR:
        return "Sensor"
    else:
        return "Unknown"


def sanitize_name(name: str, replace_hyphens: bool = True) -> str:
    """Convert a name to a format safe for entity IDs.

    Converts to lowercase, replaces spaces with underscores,
    optionally replaces hyphens with underscores, and removes double underscores.
    """
    if not name:
        return ""

    safe_name = name.lower().replace(" ", "_")
    if replace_hyphens:
        safe_name = safe_name.replace("-", "_")
    return safe_name.replace("__", "_")


def format_entity_id(
    domain: str, device_name: str, entity_key: str, suffix: str = ""
) -> str:
    """Format a consistent entity ID.

    Args:
        domain: Entity domain (e.g., 'binary_sensor', 'sensor')
        device_name: Device name (already sanitized)
        entity_key: Entity-specific identifier
        suffix: Optional suffix for the entity ID

    Returns:
        Formatted entity ID with proper format: domain.name_parts
    """
    if not domain or not entity_key:
        _LOGGER.warning("Missing required parameters for entity ID formatting")
        return ""

    # Build the entity name parts (everything after the domain)
    # Filter out empty strings to prevent double underscores
    name_parts = [part for part in [DOMAIN, device_name, entity_key, suffix] if part]

    # Join parts with underscores and clean up any double underscores
    entity_name = "_".join(name_parts).replace("__", "_")

    # Format as domain.entity_name
    return f"{domain}.{entity_name}"


def get_channel_entity_id(
    domain: str, device_name: str, channel_idx: int, suffix: str = MESSAGES_SUFFIX
) -> str:
    """Create a consistent entity ID for channel entities."""
    safe_channel = f"{CHANNEL_PREFIX}{channel_idx}"
    return format_entity_id(domain, device_name, safe_channel, suffix)


def get_contact_entity_id(
    domain: str, device_name: str, pubkey: str, suffix: str = MESSAGES_SUFFIX
) -> str:
    """Create a consistent entity ID for contact entities."""
    return format_entity_id(domain, device_name, pubkey, suffix)


def extract_channel_idx(entity_key: str) -> int:
    """Extract channel index from an entity key."""
    try:
        if entity_key and entity_key.startswith(CHANNEL_PREFIX):
            channel_idx_str = entity_key.replace(CHANNEL_PREFIX, "")
            return int(channel_idx_str)
    except (ValueError, TypeError):
        _LOGGER.warning(f"Could not extract channel index from {entity_key}")

    return 0  # Default to channel 0 on error


def sanitize_event_data(data: Any) -> Any:
    """Make event data JSON serializable by converting bytes to hex strings.

    This function recursively processes dictionaries, lists and other data types
    to ensure they're safe for serialization in Home Assistant events.

    Args:
        data: The event data to sanitize

    Returns:
        JSON-serializable version of the data with bytes converted to hex strings
    """
    if isinstance(data, dict):
        return {k: sanitize_event_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_event_data(v) for v in data]
    elif isinstance(data, tuple):
        return tuple(sanitize_event_data(v) for v in data)
    elif isinstance(data, bytes):
        return data.hex()
    elif hasattr(data, "__dict__") and not isinstance(data, type):
        # For objects with __dict__, convert to a sanitized dict
        # Skip for class objects (they have __dict__ but we don't want to process them)
        return sanitize_event_data(vars(data))
    else:
        return data


def calculate_battery_percentage(voltage_mv: float) -> float:
    """Calculate battery percentage using generic battery discharge curve.

    Args:
        voltage_mv: Battery voltage in millivolts

    Returns:
        Battery percentage (0-100)
    """
    battery_percentage = (voltage_mv - BAT_VMIN) / (BAT_VMAX - BAT_VMIN) * 100
    if battery_percentage >= 100:
        battery_percentage = 100.0
    if battery_percentage < 0:
        battery_percentage = 0.0
    return round(battery_percentage, 2)

def build_device_name(name: str, pubkey_prefix: str, node_type: str = "unknown") -> str:
    """Build consistent device name based on node info.

    Args:
        name: Node name
        pubkey_prefix: Public key prefix (at least 6 chars)
        node_type: Type of node ("root", "repeater", "client", "contact", "unknown")

    Returns:
        Formatted device name
    """
    if not name:
        name = f"Node {pubkey_prefix[:6]}"

    pubkey_short = pubkey_prefix[:6] if pubkey_prefix else ""

    if node_type == "root":
        return f"MeshCore {name} ({pubkey_short})"
    elif node_type == "repeater":
        return f"MeshCore Repeater: {name} ({pubkey_short})"
    elif node_type == "client":
        return f"MeshCore Client: {name} ({pubkey_short})"
    else:
        return f"MeshCore Node: {name} ({pubkey_short})"


def get_device_model(node_type: str) -> str:
    """Get device model based on node type.

    Args:
        node_type: Type of node ("root", "repeater", "client", "contact", "unknown")

    Returns:
        Device model string
    """
    if node_type == "root":
        return "Mesh Radio"
    elif node_type == "repeater":
        return "Mesh Repeater"
    elif node_type == "client":
        return "Mesh Client"
    else:
        return "Mesh Node"


def build_device_id(
    entry_id: str, pubkey_prefix: str, node_type: str = "unknown"
) -> str:
    """Build consistent device ID based on node info.

    Args:
        entry_id: Config entry ID
        pubkey_prefix: Public key prefix
        node_type: Type of node ("root", "repeater", "client", "contact", "unknown")

    Returns:
        Device ID string
    """
    if node_type == "root":
        return entry_id
    elif node_type in ["repeater", "client"]:
        return f"{entry_id}_{node_type}_{pubkey_prefix}"
    else:
        return f"{entry_id}_{node_type}_{pubkey_prefix}"

