"""Utility functions for the MeshCore integration."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

from Crypto.Cipher import AES
from homeassistant.util import slugify

from .const import BAT_VMAX, BAT_VMIN, CHANNEL_PREFIX, DOMAIN, MESSAGES_SUFFIX, NodeType

_LOGGER = logging.getLogger(__name__)


def extract_pubkey_from_selection(selection: str) -> str | None:
    """Extract pubkey from selection format 'Name (pubkey)'.

    Handles names with parentheses by extracting only the last parentheses.
    Example: 'Queen Anne (Soon) (abc123)' returns 'abc123'
    """
    match = re.search(r'\(([^)]+)\)$', selection)
    return match.group(1) if match else None


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


def sanitize_name(name: str) -> str:
    """Convert a name to a format safe for entity IDs.

    Converts to lowercase, replaces spaces with underscores,
    optionally replaces hyphens with underscores, and removes double underscores.
    """
    return slugify(name.lower() if name else "")



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
    return f"{domain}.{sanitize_name(entity_name)}"


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
    return round(max(0, min(100, battery_percentage)), 2)

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


def decrypt_channel_message(ciphertext: bytes, cipher_mac: bytes, channel_secret: bytes) -> tuple[int | None, str | None]:
    """Decrypt a GroupText channel message using AES-128-ECB.

    Args:
        ciphertext: Encrypted message bytes
        cipher_mac: 2-byte HMAC-SHA256 truncated MAC
        channel_secret: Channel secret key (16 bytes for AES-128)

    Returns:
        Tuple of (timestamp, message_text) or (None, None) on failure
    """
    try:
        # Verify HMAC (optional but recommended)
        expected_mac = hmac.new(channel_secret, ciphertext, hashlib.sha256).digest()[:2]
        if expected_mac != cipher_mac:
            _LOGGER.debug("HMAC verification failed for channel message")
            # Continue anyway - some implementations may not verify

        # Decrypt using AES-128 ECB
        cipher = AES.new(channel_secret, AES.MODE_ECB)
        decrypted = cipher.decrypt(ciphertext)

        # Parse structure: timestamp(4 bytes, little endian) + message text
        timestamp = int.from_bytes(decrypted[0:4], byteorder="little")
        message_text = decrypted[4:].decode("utf-8", errors="ignore").strip('\x00')

        return timestamp, message_text

    except Exception as ex:
        _LOGGER.debug(f"Error decrypting channel message: {ex}")
        return None, None


def parse_and_decrypt_rx_log(payload: Any, channels_info: dict[int, dict]) -> dict[str, Any]:
    """Parse RX_LOG packet and attempt to decrypt GroupText payload.

    Args:
        payload: Raw RX_LOG event payload
        channels_info: Dict of channel_idx -> channel info (with 'channel_secret')

    Returns:
        Dict with decryption-specific fields on success (channel_idx, channel_name,
        timestamp, text, decrypted), or parsed fields on failure (header, path_len,
        payload_type, path, channel_hash). Empty dict if not GroupText.
    """
    result = {}

    try:
        # Extract hex string from payload
        hex_str = None
        if isinstance(payload, dict):
            hex_str = payload.get("payload") or payload.get("raw_hex")
        elif isinstance(payload, (str, bytes)):
            hex_str = payload

        if not hex_str:
            return result

        # Convert to bytes
        if isinstance(hex_str, str):
            packet_bytes = bytes.fromhex(hex_str.replace(" ", "").replace("\n", ""))
        else:
            packet_bytes = hex_str

        if len(packet_bytes) < 2:
            return result

        # Parse header and path
        header = packet_bytes[0]
        path_len = packet_bytes[1]

        # Extract payload type from header (bits 2-5)
        payload_type = (header >> 2) & 0x0F

        result["header"] = f"{header:02x}"
        result["path_len"] = path_len
        result["payload_type"] = payload_type

        # Check if this is GroupText (0x05)
        if payload_type != 0x05:
            _LOGGER.debug(f"RX_LOG payload type {payload_type:02x} is not GroupText, skipping decryption")
            return result

        # Validate packet length
        path_end = 2 + path_len
        if len(packet_bytes) < path_end + 3:  # Need at least channel_hash + 2-byte MAC
            return result

        # Extract path data
        path_data = packet_bytes[2:path_end]
        result["path"] = path_data.hex()

        # Parse GroupText payload
        group_payload = packet_bytes[path_end:]
        channel_hash_byte = group_payload[0]
        result["channel_hash"] = f"{channel_hash_byte:02x}"

        if len(group_payload) < 3:
            return result

        cipher_mac = group_payload[1:3]
        ciphertext = group_payload[3:]

        # Try to match channel hash and decrypt
        for channel_idx, channel_info in channels_info.items():
            channel_secret = channel_info.get("channel_secret")
            if not channel_secret:
                continue

            # Calculate channel hash (first byte of SHA256)
            if isinstance(channel_secret, str):
                channel_secret = bytes.fromhex(channel_secret)

            expected_hash_byte = hashlib.sha256(channel_secret).digest()[0]

            if expected_hash_byte == channel_hash_byte:
                # Found matching channel, try to decrypt
                timestamp, message_text = decrypt_channel_message(ciphertext, cipher_mac, channel_secret)

                if timestamp is not None and message_text is not None:
                    # Keep essential parsed fields, add decryption-specific fields
                    result = {
                        "channel_idx": channel_idx,
                        "channel_name": channel_info.get("channel_name", f"Channel {channel_idx}"),
                        "timestamp": timestamp,
                        "text": message_text,
                        "decrypted": True,
                        "path_len": path_len,
                        "path": path_data.hex(),
                        "channel_hash": f"{channel_hash_byte:02x}"
                    }

                    _LOGGER.debug(f"Successfully decrypted RX_LOG for channel {channel_idx}: {message_text[:50]}")
                    break

    except Exception as ex:
        _LOGGER.debug(f"Error parsing/decrypting RX_LOG: {ex}")

    return result


def create_message_correlation_key(channel_idx: int, timestamp: int, text: str) -> str:
    """Create a correlation hash key for matching RX_LOG to channel messages.

    Args:
        channel_idx: Channel index
        timestamp: Sender's timestamp (unix time)
        text: Message text content

    Returns:
        16-character hex string hash
    """
    correlation_key = f"{channel_idx}:{timestamp}:{text}"
    hash_key = hashlib.sha256(correlation_key.encode()).hexdigest()[:16]
    return hash_key


def parse_rx_log_data(payload: Any) -> dict[str, Any]:
    """Parse RX_LOG event payload to extract LoRa packet details.

    RX_LOG_DATA events contain raw LoRa payload with header, path, and channel hash.
    Format:
    - Bytes 0-1: Header/identifier
    - Bytes 2-3: Hop count (path_len)
    - Bytes 4+: Path data (2 hex chars per node)
    - After path: Channel hash

    Args:
        payload: Raw event payload (dict with 'payload' or 'raw_hex', or direct hex string)

    Returns:
        Dict with parsed fields: header, path_len, path, channel_hash
        Returns empty dict on parsing errors (fault tolerant)
    """
    result = {}

    try:
        # Extract hex string from payload
        hex_str = None

        if isinstance(payload, dict):
            # Try payload.payload first, then raw_hex
            hex_str = payload.get("payload") or payload.get("raw_hex")
        elif isinstance(payload, (str, bytes)):
            # Direct string or bytes
            hex_str = payload

        if not hex_str:
            _LOGGER.debug("No hex data found in RX_LOG payload")
            return result

        # Convert bytes to hex string if needed
        if isinstance(hex_str, bytes):
            hex_str = hex_str.hex()

        # Normalize: lowercase, remove spaces and newlines
        hex_str = str(hex_str).lower().replace(" ", "").replace("\n", "").replace("\r", "")

        # Validate minimum length (at least header + path_len)
        if len(hex_str) < 4:
            _LOGGER.debug(f"RX_LOG hex too short: {len(hex_str)} chars")
            return result

        # Parse header (bytes 0-1)
        result["header"] = hex_str[0:2]

        # Parse path_len (bytes 2-3)
        try:
            path_len = int(hex_str[2:4], 16)
            result["path_len"] = path_len
        except ValueError:
            _LOGGER.debug(f"Could not parse path_len from: {hex_str[2:4]}")
            return result

        # Calculate expected positions
        path_start = 4
        path_end = path_start + (path_len * 2)  # Each node is 2 hex chars

        # Validate length for path data
        if len(hex_str) < path_end:
            _LOGGER.debug(f"RX_LOG hex too short for path data: expected {path_end}, got {len(hex_str)}")
            return result

        # Extract path data
        path_hex = hex_str[path_start:path_end]
        result["path"] = path_hex

        # Parse individual nodes in path (2 chars each)
        path_nodes = []
        for i in range(0, len(path_hex), 2):
            node_hex = path_hex[i:i+2]
            path_nodes.append(node_hex)
        result["path_nodes"] = path_nodes

        # Extract channel hash if available
        if len(hex_str) > path_end:
            # Channel hash is the next 2 characters after path
            if len(hex_str) >= path_end + 2:
                result["channel_hash"] = hex_str[path_end:path_end+2]

        _LOGGER.debug(f"Parsed RX_LOG: header={result.get('header')}, path_len={result.get('path_len')}, "
                     f"path={result.get('path')}, channel_hash={result.get('channel_hash')}")

    except Exception as ex:
        _LOGGER.debug(f"Error parsing RX_LOG data: {ex}")

    return result

