"""Latest MeshCore firmware releases, for the firmware update entities.

The firmware repository publishes one release line per node role, each tagged
``<role>-v<version>`` (``companion-v1.18.0``, ``repeater-v1.18.0``,
``room-server-v1.18.0``). Nodes report their version as ``v1.18.0-81dbae5``
(companion device query) or ``v1.18.0-81dbae5 (Build: 15-Oct-2026)`` (the
``ver`` CLI reply), so both sides are reduced to the bare ``1.18.0`` before
they are compared.

Everything here is pure so it can be tested without Home Assistant; the
polling coordinator lives with the entities in update.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

FIRMWARE_RELEASES_URL = "https://api.github.com/repos/meshcore-dev/MeshCore/releases"
FIRMWARE_RELEASES_PAGE_SIZE = 50
FIRMWARE_RELEASE_CHECK_INTERVAL = timedelta(hours=12)

ROLE_COMPANION = "companion"
ROLE_REPEATER = "repeater"
ROLE_ROOM_SERVER = "room-server"
FIRMWARE_ROLES = (ROLE_COMPANION, ROLE_REPEATER, ROLE_ROOM_SERVER)

# Contact types as the firmware advertises them (ADV_TYPE_*).
_CONTACT_TYPE_ROOM_SERVER = 3

_VERSION_PATTERN = re.compile(r"v?(\d+\.\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class FirmwareRelease:
    """The newest stable release of one firmware role."""

    version: str
    tag: str
    url: str
    notes: str


def normalize_firmware_version(raw: Any) -> str | None:
    """Reduce a reported firmware version or release tag to ``X.Y.Z``.

    Returns None for anything that carries no version (``None``, ``Unknown``,
    an error reply), so a node that has not reported yet shows as unknown
    rather than as outdated.
    """
    if not isinstance(raw, str):
        return None
    match = _VERSION_PATTERN.search(raw.strip())
    if match is None:
        return None
    return match.group(1)


def role_for_contact_type(contact_type: Any) -> str:
    """Map a tracked node's advertised contact type to its firmware role."""
    if contact_type == _CONTACT_TYPE_ROOM_SERVER:
        return ROLE_ROOM_SERVER
    return ROLE_REPEATER


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def latest_releases(releases: list[dict[str, Any]]) -> dict[str, FirmwareRelease]:
    """Pick the newest stable release of each role from a GitHub release list.

    Drafts and pre-releases are skipped; the newest version wins regardless of
    the order the API returned them in.
    """
    latest: dict[str, FirmwareRelease] = {}
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        for role in FIRMWARE_ROLES:
            prefix = f"{role}-"
            if not tag.startswith(prefix):
                continue
            version = normalize_firmware_version(tag[len(prefix) :])
            if version is None:
                break
            current = latest.get(role)
            if current is None or _version_key(version) > _version_key(current.version):
                latest[role] = FirmwareRelease(
                    version=version,
                    tag=tag,
                    url=str(release.get("html_url") or ""),
                    notes=str(release.get("body") or ""),
                )
            break
    return latest
