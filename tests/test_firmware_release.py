"""Tests for picking the latest MeshCore firmware release per node role."""

import pytest

from tests.support.modules import load_module

_MODULE = load_module("firmware_release")

normalize = _MODULE.normalize_firmware_version
latest_releases = _MODULE.latest_releases
role_for_contact_type = _MODULE.role_for_contact_type


def _release(tag, *, prerelease=False, draft=False, body="notes", url=None):
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "body": body,
        "html_url": url or f"https://example.com/releases/{tag}",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v1.18.0-81dbae5", "1.18.0"),
        ("v1.18.0-81dbae5 (Build: 15-Oct-2026)", "1.18.0"),
        ("1.17.1", "1.17.1"),
        ("companion-v1.18.0", "1.18.0"),
        ("v1.9", "1.9"),
        ("MeshCore 1.14.2 (Build: Sep 18 2026)", "1.14.2"),
        ("Unknown", None),
        ("", None),
        (None, None),
        (118, None),
        ("Error: unknown command", None),
    ],
)
def test_normalize_firmware_version(raw, expected):
    assert normalize(raw) == expected


def test_latest_release_per_role():
    releases = [
        _release("room-server-v1.18.0"),
        _release("repeater-v1.18.0"),
        _release("companion-v1.18.0", body="companion notes"),
        _release("companion-v1.17.1"),
        _release("repeater-v1.17.1"),
    ]

    latest = latest_releases(releases)

    assert set(latest) == {"companion", "repeater", "room-server"}
    assert latest["companion"].version == "1.18.0"
    assert latest["companion"].tag == "companion-v1.18.0"
    assert latest["companion"].notes == "companion notes"
    assert latest["companion"].url == "https://example.com/releases/companion-v1.18.0"
    assert latest["repeater"].version == "1.18.0"
    assert latest["room-server"].version == "1.18.0"


def test_prereleases_and_drafts_are_ignored():
    releases = [
        _release("companion-v1.19.0", prerelease=True),
        _release("companion-v1.19.1", draft=True),
        _release("companion-v1.18.0"),
    ]

    assert latest_releases(releases)["companion"].version == "1.18.0"


def test_newest_version_wins_regardless_of_order():
    releases = [
        _release("companion-v1.9.0"),
        _release("companion-v1.10.0"),
        _release("companion-v1.2.3"),
    ]

    assert latest_releases(releases)["companion"].version == "1.10.0"


def test_room_server_is_not_mistaken_for_repeater():
    latest = latest_releases([_release("room-server-v1.18.0")])

    assert "repeater" not in latest
    assert latest["room-server"].version == "1.18.0"


def test_unrelated_and_malformed_entries_are_skipped():
    releases = [
        _release("v1.0.0"),
        _release("sensor-v1.18.0"),
        _release("companion-vnext"),
        {"tag_name": None},
        "not a release",
        _release("companion-v1.18.0"),
    ]

    latest = latest_releases(releases)

    assert set(latest) == {"companion"}
    assert latest["companion"].version == "1.18.0"


def test_missing_body_and_url_become_empty_strings():
    release = {"tag_name": "repeater-v1.18.0", "prerelease": False, "draft": False}

    latest = latest_releases([release])["repeater"]

    assert latest.notes == ""
    assert latest.url == ""


@pytest.mark.parametrize(
    ("contact_type", "role"),
    [(2, "repeater"), (3, "room-server"), (None, "repeater"), (1, "repeater")],
)
def test_role_for_contact_type(contact_type, role):
    assert role_for_contact_type(contact_type) == role
