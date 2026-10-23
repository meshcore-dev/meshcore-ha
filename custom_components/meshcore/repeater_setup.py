"""Route text used by the repeater setup form."""

import re
from typing import Any


def normalize_path(value: str) -> str:
    """Return explicit SDK path syntax; empty is flood, direct is zero hops.

    The companion allocates 64 route bytes; the packed hop count has six bits.
    Modes 0/1/2 mean one/two/three bytes per hop respectively.
    """
    value = value.strip().lower()
    if not value:
        return ""
    if value == "direct":
        return ":0"
    if value == ":0":
        return value
    match = re.fullmatch(r"([0-9a-f]+)(?::([012]))?", value)
    if not match:
        raise ValueError("Expected a hex route with optional :0, :1 or :2")
    route, mode = match.groups()
    mode = int(mode or 0)
    if len(route) % (2 * (mode + 1)) or len(route) > 128:
        raise ValueError("Route must contain complete hop hashes within 64 bytes")
    if len(route) // (2 * (mode + 1)) > 63:
        raise ValueError("Route exceeds 63 hops")
    return f"{route}:{mode}"


def contact_path(contact: dict[str, Any]) -> str:
    """Format the saved route without confusing a direct link with flooding."""
    hops = contact.get("out_path_len", -1)
    if hops < 0:
        return ""
    if hops == 0:
        return "direct"
    mode = max(0, contact.get("out_path_hash_mode", 0))
    route = contact.get("out_path", "")[: hops * (mode + 1) * 2].upper()
    return route if mode == 0 else f"{route}:{mode}"
