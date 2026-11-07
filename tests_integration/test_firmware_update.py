"""Real Home Assistant tests for the firmware update entities."""

from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore.config import Settings
from custom_components.meshcore.const import CONF_REPEATER_SUBSCRIPTIONS, DOMAIN
from custom_components.meshcore.firmware_release import (
    FIRMWARE_RELEASES_URL,
    FirmwareRelease,
)
from custom_components.meshcore.update import (
    FirmwareReleaseCoordinator,
    MeshCoreCompanionFirmwareUpdate,
    MeshCoreRepeaterFirmwareUpdate,
    async_setup_entry,
    build_repeater_updates,
)

REPEATER_PREFIX = "aabbccddeeff"
ROOM_PREFIX = "112233445566"

RELEASES = {
    "companion": FirmwareRelease(
        version="1.18.0",
        tag="companion-v1.18.0",
        url="https://example.com/companion-v1.18.0",
        notes="Companion notes",
    ),
    "repeater": FirmwareRelease(
        version="1.18.0",
        tag="repeater-v1.18.0",
        url="https://example.com/repeater-v1.18.0",
        notes="Repeater notes",
    ),
    "room-server": FirmwareRelease(
        version="1.17.1",
        tag="room-server-v1.17.1",
        url="https://example.com/room-server-v1.17.1",
        notes="R" * 400,
    ),
}


class _Api:
    def __init__(self, contacts):
        self.contacts = contacts

    def contact_by_prefix(self, prefix):
        return self.contacts.get(prefix)


def _coordinator(hass, *, companion_version="v1.17.0-abc1234", repeater_versions=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="hub",
        data={
            CONF_REPEATER_SUBSCRIPTIONS: [
                {"name": "Rep", "pubkey_prefix": REPEATER_PREFIX},
                {"name": "Room", "pubkey_prefix": ROOM_PREFIX},
            ]
        },
    )
    entry.add_to_hass(hass)
    versions = repeater_versions or {}

    coordinator = MagicMock()
    coordinator.config_entry = entry
    coordinator.settings = Settings.from_entry(entry)
    coordinator.pubkey = "cccccc" + "0" * 58
    coordinator.device_info = {"sw_version": companion_version}
    coordinator.api = _Api({REPEATER_PREFIX: {"type": 2}, ROOM_PREFIX: {"type": 3}})
    coordinator.repeater_firmware = versions.get
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return entry, coordinator


def _releases(hass, data):
    releases = FirmwareReleaseCoordinator(hass)
    releases.data = data
    return releases


async def test_release_check_parses_github(hass, aioclient_mock):
    """The shared coordinator keeps the newest stable release of each role."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        FIRMWARE_RELEASES_URL,
        json=[
            {"tag_name": "companion-v1.19.0", "prerelease": True},
            {
                "tag_name": "companion-v1.18.0",
                "html_url": "https://example.com/c",
                "body": "notes",
            },
            {"tag_name": "repeater-v1.18.0"},
        ],
    )

    releases = FirmwareReleaseCoordinator(hass)
    await releases.async_refresh()

    assert releases.last_update_success
    assert releases.data["companion"].version == "1.18.0"
    assert releases.data["companion"].url == "https://example.com/c"
    assert releases.data["repeater"].version == "1.18.0"
    assert aioclient_mock.mock_calls[0][1].query["per_page"] == "50"


async def test_release_check_failure_is_not_fatal(hass, aioclient_mock):
    """GitHub being down marks the poll failed instead of raising."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(FIRMWARE_RELEASES_URL, status=503)

    releases = FirmwareReleaseCoordinator(hass)
    await releases.async_refresh()

    assert not releases.last_update_success


async def test_setup_builds_companion_and_repeater_entities(hass):
    """One entity for the companion and one per tracked repeater."""
    entry, coordinator = _coordinator(hass)
    entities = []

    await async_setup_entry(hass, entry, entities.extend)

    assert [type(entity) for entity in entities] == [
        MeshCoreCompanionFirmwareUpdate,
        MeshCoreRepeaterFirmwareUpdate,
        MeshCoreRepeaterFirmwareUpdate,
    ]
    assert entities[0].unique_id == "hub_firmware_update"
    assert entities[0].device_info["identifiers"] == {(DOMAIN, "hub")}
    assert entities[1].unique_id == f"hub_repeater_{REPEATER_PREFIX}_firmware_update"
    assert entities[1].device_info["identifiers"] == {(DOMAIN, f"hub_repeater_{REPEATER_PREFIX}")}
    # A repeater added to the loaded entry reuses the shared release poll.
    assert coordinator.update_add_entities == entities.extend
    added = build_repeater_updates(coordinator, {"pubkey_prefix": "ffeeddccbbaa"})
    assert len(added) == 1
    await hass.async_block_till_done()


async def test_companion_update_available(hass):
    """An older companion reports the newer release as available."""
    _, coordinator = _coordinator(hass, companion_version="v1.17.0-abc1234")
    entity = MeshCoreCompanionFirmwareUpdate(coordinator, _releases(hass, RELEASES))

    assert entity.installed_version == "1.17.0"
    assert entity.latest_version == "1.18.0"
    assert entity.state == "on"
    assert entity.release_url == "https://example.com/companion-v1.18.0"
    assert entity.release_summary == "Companion notes"
    assert await entity.async_release_notes() == "Companion notes"
    assert entity.extra_state_attributes == {
        "firmware_role": "companion",
        "reported_version": "v1.17.0-abc1234",
        "release_tag": "companion-v1.18.0",
    }


async def test_companion_up_to_date(hass):
    _, coordinator = _coordinator(hass, companion_version="v1.18.0-81dbae5")
    entity = MeshCoreCompanionFirmwareUpdate(coordinator, _releases(hass, RELEASES))

    assert entity.installed_version == "1.18.0"
    assert entity.state == "off"


async def test_unknown_installed_version_never_claims_an_update(hass):
    """Before the device query answers, the entity shows nothing to install."""
    _, coordinator = _coordinator(hass, companion_version="Unknown")
    entity = MeshCoreCompanionFirmwareUpdate(coordinator, _releases(hass, RELEASES))

    assert entity.installed_version is None
    assert entity.latest_version is None
    assert entity.state is None


async def test_no_release_data_yet(hass):
    _, coordinator = _coordinator(hass)
    entity = MeshCoreCompanionFirmwareUpdate(coordinator, _releases(hass, None))

    assert entity.latest_version is None
    assert entity.release_url is None
    assert entity.release_summary is None
    assert await entity.async_release_notes() is None


async def test_repeater_and_room_server_use_their_own_release_line(hass):
    """A room server is compared with room-server releases, not repeater ones."""
    _, coordinator = _coordinator(
        hass,
        repeater_versions={
            REPEATER_PREFIX: "v1.18.0-81dbae5 (Build: 15-Oct-2026)",
            ROOM_PREFIX: "v1.16.0-07a3ca9 (Build: 06-Jun-2026)",
        },
    )
    releases = _releases(hass, RELEASES)
    repeater = MeshCoreRepeaterFirmwareUpdate(
        coordinator, releases, {"pubkey_prefix": REPEATER_PREFIX}
    )
    room = MeshCoreRepeaterFirmwareUpdate(coordinator, releases, {"pubkey_prefix": ROOM_PREFIX})

    assert repeater.extra_state_attributes["firmware_role"] == "repeater"
    assert repeater.state == "off"
    assert room.extra_state_attributes["firmware_role"] == "room-server"
    assert room.installed_version == "1.16.0"
    assert room.latest_version == "1.17.1"
    assert room.state == "on"
    # The summary respects Home Assistant's 255 character limit.
    assert len(room.release_summary) == 255
    assert room.release_summary.endswith("…")


async def test_repeater_without_reported_version(hass):
    """A repeater that never answered `ver` shows no update."""
    _, coordinator = _coordinator(hass)
    entity = MeshCoreRepeaterFirmwareUpdate(
        coordinator, _releases(hass, RELEASES), {"pubkey_prefix": REPEATER_PREFIX}
    )

    assert entity.installed_version is None
    assert entity.state is None
