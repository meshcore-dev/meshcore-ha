"""Comparing produced events with the reviewed golden fixtures.

The fixtures hold what a listener written against an older release sees, so
they never gain a field. Every event now also carries the identity of the entry
that produced it, which is what ``with_identity`` adds to an expectation.
"""

from typing import Any

IDENTITY_FIELDS = ("entry_id", "device_id")


def with_identity(
    events: list[dict[str, Any]],
    *,
    entry_id: str | None = None,
    device_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return legacy fixture events with the entry identity stamped on.

    Producer-set keys win, which is how the CLI event keeps ``entry_id``
    meaning the entry the caller named.
    """
    identity = {"entry_id": entry_id, "device_id": device_id}
    return [{**identity, **event} for event in events]
