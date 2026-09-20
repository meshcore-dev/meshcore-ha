"""Node secrets are hidden from forwarded event payloads unless opted in."""

import sys

_events = sys.modules["custom_components.meshcore.events"]

REDACTED = _events.REDACTED
is_secret_event = _events.is_secret_event
sanitize_event_data = _events.sanitize_event_data


def test_channel_secret_is_replaced_by_default() -> None:
    """A channel's shared secret never leaves the integration verbatim."""
    payload = {"channel_idx": 1, "channel_name": "Private", "channel_secret": b"\xab\xcd"}

    assert sanitize_event_data(payload) == {
        "channel_idx": 1,
        "channel_name": "Private",
        "channel_secret": REDACTED,
    }


def test_nested_and_listed_secrets_are_replaced() -> None:
    """Redaction follows the payload, not just its top level."""
    payload = {"channels": [{"secret": "aabb"}, {"nested": {"channel_secret": "ccdd"}}]}

    assert sanitize_event_data(payload) == {
        "channels": [{"secret": REDACTED}, {"nested": {"channel_secret": REDACTED}}]
    }


def test_opting_in_forwards_the_secret() -> None:
    """With expose_secrets set, the payload is only made serializable."""
    payload = {"channel_secret": b"\xab\xcd", "other": b"\x01"}

    assert sanitize_event_data(payload, redact=False) == {
        "channel_secret": "abcd",
        "other": "01",
    }


def test_serialization_is_unchanged() -> None:
    """Bytes, tuples and objects still sanitize the way they always have."""

    class Payload:
        def __init__(self) -> None:
            self.raw = b"\x0a"

    assert sanitize_event_data({"a": (b"\x01", [b"\x02"]), "b": Payload()}) == {
        "a": ("01", ["02"]),
        "b": {"raw": "0a"},
    }


def test_private_key_events_are_recognised() -> None:
    """The whole-payload secret is identified by its SDK event type."""
    assert is_secret_event("EventType.PRIVATE_KEY")
    assert not is_secret_event("EventType.BATTERY")
