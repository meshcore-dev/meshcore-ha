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

        # AES plaintext: timestamp(4 bytes LE) + flags(1 byte: attempt+txt_type) + message text
        timestamp = int.from_bytes(decrypted[0:4], byteorder="little")
        message_text = decrypted[5:].decode("utf-8", errors="ignore").strip('\x00')

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
        if not isinstance(payload, dict):
            return result

        # Fast path: use pre-parsed fields set by the SDK's MeshcorePacketParser.
        # These are always correct for all route types, including TC_FLOOD (route_type 0)
        # and TC_DIRECT (route_type 3) which carry a 4-byte transport code between the
        # header and the path_byte — a detail the raw-parsing fallback previously missed,
        # causing region-scope (TC_FLOOD) messages to never correlate with RX_LOG data.
        if "payload_type" in payload:
            payload_type = payload.get("payload_type")
            path_len = payload.get("path_len", 0)
            path_hash_size = payload.get("path_hash_size", 1)
            path = payload.get("path", "")

            header_raw = payload.get("header")
            if isinstance(header_raw, int):
                result["header"] = f"{header_raw:02x}"
            elif isinstance(header_raw, str):
                result["header"] = header_raw

            result["path_len"] = path_len
            result["path_hash_size"] = path_hash_size
            result["payload_type"] = payload_type
            if path:
                result["path"] = path

            if payload_type != 5:  # 5 = GRP_TXT (GroupText channel message)
                return result

            chan_hash_hex = payload.get("chan_hash", "")
            cipher_mac_hex = payload.get("cipher_mac", "")
            crypted_hex = payload.get("crypted", "")

            if chan_hash_hex:
                result["channel_hash"] = chan_hash_hex

            if not (chan_hash_hex and cipher_mac_hex and crypted_hex):
                return result

            try:
                chan_hash_byte = int(chan_hash_hex, 16)
            except ValueError:
                return result

            for channel_idx, channel_info in channels_info.items():
                channel_secret = channel_info.get("channel_secret")
                if not channel_secret:
                    continue

                if isinstance(channel_secret, str):
                    channel_secret = bytes.fromhex(channel_secret)

                expected_hash_byte = hashlib.sha256(channel_secret).digest()[0]

                if expected_hash_byte == chan_hash_byte:
                    try:
                        ciphertext = bytes.fromhex(crypted_hex)
                        cipher_mac = bytes.fromhex(cipher_mac_hex)
                    except ValueError:
                        continue

                    timestamp, message_text = decrypt_channel_message(ciphertext, cipher_mac, channel_secret)

                    if timestamp is not None and message_text is not None:
                        result = {
                            "channel_idx": channel_idx,
                            "channel_name": channel_info.get("channel_name", f"Channel {channel_idx}"),
                            "timestamp": timestamp,
                            "text": message_text,
                            "decrypted": True,
                            "path_len": path_len,
                            "path": path,
                            "channel_hash": chan_hash_hex,
                            "path_hash_size": path_hash_size,
                        }
                        _LOGGER.debug(
                            "Decrypted RX_LOG via SDK fields for channel %d: %s",
                            channel_idx, message_text[:50],
                        )
                        break

            return result

        # Fallback: parse from raw payload bytes when SDK pre-parsed fields are absent.
        # TC_FLOOD (route_type 0) and TC_DIRECT (route_type 3) carry a 4-byte transport
        # code between the header byte and the path_byte; skip it before reading path info.
        hex_str = None
        if isinstance(payload, dict):
            hex_str = payload.get("payload") or payload.get("raw_hex")
        elif isinstance(payload, (str, bytes)):
            hex_str = payload

        if not hex_str:
            return result

        if isinstance(hex_str, bytes):
            hex_str = hex_str.hex()

        if isinstance(hex_str, str):
            packet_bytes = bytes.fromhex(hex_str.replace(" ", "").replace("\n", ""))
        else:
            packet_bytes = hex_str

        if len(packet_bytes) < 2:
            return result

        header = packet_bytes[0]
        route_type = header & 0x03
        payload_type = (header >> 2) & 0x0F

        result["header"] = f"{header:02x}"
        result["payload_type"] = payload_type

        # TC_FLOOD (0) and TC_DIRECT (3) have a 4-byte transport code before the path byte.
        transport_code_size = 4 if route_type in (0, 3) else 0
        path_byte_offset = 1 + transport_code_size

        if len(packet_bytes) <= path_byte_offset:
            return result

        path_byte = packet_bytes[path_byte_offset]
        path_hash_size = ((path_byte & 0xC0) >> 6) + 1
        hop_count = path_byte & 0x3F

        result["path_len"] = hop_count
        result["path_hash_size"] = path_hash_size

        if payload_type != 0x05:
            _LOGGER.debug(
                "RX_LOG payload type %02x is not GroupText, skipping decryption",
                payload_type,
            )
            return result

        path_start = path_byte_offset + 1
        path_end = path_start + hop_count * path_hash_size

        if len(packet_bytes) < path_end + 3:  # Need at least channel_hash + 2-byte MAC
            return result

        path_data = packet_bytes[path_start:path_end]
        result["path"] = path_data.hex()

        group_payload = packet_bytes[path_end:]
        channel_hash_byte = group_payload[0]
        result["channel_hash"] = f"{channel_hash_byte:02x}"

        if len(group_payload) < 3:
            return result

        cipher_mac = group_payload[1:3]
        ciphertext = group_payload[3:]

        for channel_idx, channel_info in channels_info.items():
            channel_secret = channel_info.get("channel_secret")
            if not channel_secret:
                continue

            if isinstance(channel_secret, str):
                channel_secret = bytes.fromhex(channel_secret)

            expected_hash_byte = hashlib.sha256(channel_secret).digest()[0]

            if expected_hash_byte == channel_hash_byte:
                timestamp, message_text = decrypt_channel_message(ciphertext, cipher_mac, channel_secret)

                if timestamp is not None and message_text is not None:
                    result = {
                        "channel_idx": channel_idx,
                        "channel_name": channel_info.get("channel_name", f"Channel {channel_idx}"),
                        "timestamp": timestamp,
                        "text": message_text,
                        "decrypted": True,
                        "path_len": hop_count,
                        "path": path_data.hex(),
                        "channel_hash": f"{channel_hash_byte:02x}",
                        "path_hash_size": path_hash_size,
                    }
                    _LOGGER.debug(
                        "Decrypted RX_LOG via raw parse for channel %d: %s",
                        channel_idx, message_text[:50],
                    )
                    break

    except Exception as ex:
        _LOGGER.debug("Error parsing/decrypting RX_LOG: %s", ex)

    return result


def create_message_correlation_key(channel_idx: int, timestamp: int) -> str:
    """Create a correlation hash key for matching RX_LOG to channel messages.

    Uses only channel index and timestamp for correlation. Text is excluded
    because the HA config name may differ from the on-device advertised name,
    making text-based matching unreliable.

    Note: Because the timestamp has 1-second granularity, two messages sent on
    the same channel within the same second will produce the same correlation
    key. In practice this is unlikely given mesh radio TX times, but it is a
    known limitation.

    Args:
        channel_idx: Channel index
        timestamp: Sender's timestamp (unix time)

    Returns:
        16-character hex string hash
    """
    correlation_key = f"{channel_idx}:{timestamp}"
    hash_key = hashlib.sha256(correlation_key.encode()).hexdigest()[:16]
    return hash_key


def _normalize_flood_scope_name(name: str) -> str:
    """Prepend '#' to a region name if not already present."""
    name = name.strip()
    if name and not name.startswith("#"):
        return "#" + name
    return name


def load_flood_scope_keys(scopes_str: str) -> dict[str, bytes]:
    """Parse a comma-separated flood_scopes string into a name→16-byte-key dict.

    Key derivation mirrors the firmware's auto-key for #hashtag regions:
    SHA256(scope_name_bytes)[:16].  Names are normalized to have a '#' prefix.
    """
    result: dict[str, bytes] = {}
    if not scopes_str:
        return result
    for entry in scopes_str.split(","):
        name = _normalize_flood_scope_name(entry)
        if name and name not in ("*", "#"):
            result[name] = hashlib.sha256(name.encode()).digest()[:16]
    return result


def match_flood_scope(
    transport_code: int,
    payload_type: int,
    pkt_payload: bytes,
    scope_keys: dict[str, bytes],
) -> str | None:
    """Return the scope name whose HMAC matches transport_code, or None.

    Mirrors TransportKey::calcTransportCode from the firmware:
    HMAC-SHA256(scope_key, [payload_type_byte] + pkt_payload)[0:2] as uint16 LE.
    """
    if not scope_keys or not pkt_payload:
        return None
    check_data = bytes([payload_type]) + pkt_payload
    for name, key in scope_keys.items():
        digest = hmac.new(key, check_data, hashlib.sha256).digest()
        computed = int.from_bytes(digest[:2], "little")
        if computed == 0:
            computed = 1
        elif computed == 0xFFFF:
            computed = 0xFFFE
        if computed == transport_code:
            return name
    return None


def parse_rx_log_data(payload: Any) -> dict[str, Any]:
    """Parse RX_LOG event payload to extract LoRa packet details.

    Args:
        payload: Raw event payload (dict with SDK pre-parsed fields, or 'payload'/'raw_hex',
                 or direct hex string)

    Returns:
        Dict with parsed fields: header, path_len, path_hash_size, path, path_nodes,
        channel_hash. Returns empty dict on parsing errors (fault tolerant).
    """
    result = {}

    try:
        if isinstance(payload, dict):
            # Fast path: use pre-parsed fields set by the SDK's MeshcorePacketParser.
            # These are always correct for all route types including TC_FLOOD (route_type 0)
            # and TC_DIRECT (route_type 3) which carry a 4-byte transport code that the
            # raw-parsing fallback previously handled incorrectly.
            if "payload_type" in payload:
                path_len = payload.get("path_len", 0)
                path_hash_size = payload.get("path_hash_size", 1)
                path_hex = payload.get("path", "")
                chan_hash = payload.get("chan_hash")

                header_raw = payload.get("header")
                if isinstance(header_raw, int):
                    result["header"] = f"{header_raw:02x}"
                elif isinstance(header_raw, str):
                    result["header"] = header_raw

                result["path_len"] = path_len
                result["path_hash_size"] = path_hash_size

                if path_hex:
                    result["path"] = path_hex
                    step = path_hash_size * 2
                    result["path_nodes"] = [
                        path_hex[i:i + step]
                        for i in range(0, len(path_hex), step)
                        if path_hex[i:i + step]
                    ]

                if chan_hash:
                    result["channel_hash"] = chan_hash

                return result

            # Fallback: extract raw hex and parse manually.
            hex_str = payload.get("payload") or payload.get("raw_hex")
        elif isinstance(payload, (str, bytes)):
            hex_str = payload
        else:
            return result

        if not hex_str:
            _LOGGER.debug("No hex data found in RX_LOG payload")
            return result

        if isinstance(hex_str, bytes):
            hex_str = hex_str.hex()

        hex_str = str(hex_str).lower().replace(" ", "").replace("\n", "").replace("\r", "")

        if len(hex_str) < 4:
            _LOGGER.debug("RX_LOG hex too short: %d chars", len(hex_str))
            return result

        result["header"] = hex_str[0:2]

        header = int(hex_str[0:2], 16)
        route_type = header & 0x03

        # TC_FLOOD (0) and TC_DIRECT (3) have a 4-byte transport code before the path byte;
        # skip it (8 hex chars) before reading path info.
        transport_nibbles = 8 if route_type in (0, 3) else 0
        path_byte_start = 2 + transport_nibbles

        if len(hex_str) < path_byte_start + 2:
            return result

        try:
            path_byte = int(hex_str[path_byte_start:path_byte_start + 2], 16)
        except ValueError:
            _LOGGER.debug(
                "Could not parse path byte from: %s",
                hex_str[path_byte_start:path_byte_start + 2],
            )
            return result

        path_hash_size = ((path_byte & 0xC0) >> 6) + 1
        hop_count = path_byte & 0x3F
        result["path_len"] = hop_count
        result["path_hash_size"] = path_hash_size

        path_start = path_byte_start + 2
        path_end = path_start + hop_count * path_hash_size * 2

        if len(hex_str) < path_end:
            _LOGGER.debug(
                "RX_LOG hex too short for path data: expected at least %d, got %d",
                path_end,
                len(hex_str),
            )
            return result

        path_hex = hex_str[path_start:path_end]
        result["path"] = path_hex

        step = path_hash_size * 2
        result["path_nodes"] = [path_hex[i:i + step] for i in range(0, len(path_hex), step)]

        if len(hex_str) >= path_end + 2:
            result["channel_hash"] = hex_str[path_end:path_end + 2]

        _LOGGER.debug(
            "Parsed RX_LOG: header=%s, path_len=%d, path=%s, channel_hash=%s",
            result.get("header"),
            result.get("path_len"),
            result.get("path"),
            result.get("channel_hash"),
        )

    except Exception as ex:
        _LOGGER.debug("Error parsing RX_LOG data: %s", ex)

    return result

