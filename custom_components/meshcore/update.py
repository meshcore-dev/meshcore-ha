"""Firmware update entities for the companion and its tracked repeaters.

Each entity compares the firmware a node reported with the newest stable
MeshCore release of the node's role. The entities only announce updates:
MeshCore nodes are flashed over USB or the vendor web flasher, so there is no
install action. Release notes link to the GitHub release.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, ENTITY_DOMAIN_UPDATE
from .firmware_release import (
    FIRMWARE_RELEASE_CHECK_INTERVAL,
    FIRMWARE_RELEASES_PAGE_SIZE,
    FIRMWARE_RELEASES_URL,
    ROLE_COMPANION,
    FirmwareRelease,
    latest_releases,
    normalize_firmware_version,
    role_for_contact_type,
)
from .utils import format_entity_id

_LOGGER = logging.getLogger(__name__)

# UpdateEntity.release_summary is capped at 255 characters.
_RELEASE_SUMMARY_MAX = 255
_RELEASE_DATA_KEY = f"{DOMAIN}_firmware_releases"


class FirmwareReleaseCoordinator(DataUpdateCoordinator[dict[str, FirmwareRelease]]):
    """Polls the firmware releases twice a day, shared by every config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} firmware releases",
            update_interval=FIRMWARE_RELEASE_CHECK_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, FirmwareRelease]:
        session = async_get_clientsession(self.hass)
        try:
            response = await session.get(
                FIRMWARE_RELEASES_URL,
                params={"per_page": FIRMWARE_RELEASES_PAGE_SIZE},
                headers={"Accept": "application/vnd.github+json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
            response.raise_for_status()
            releases = await response.json()
        except Exception as ex:
            raise UpdateFailed(f"Could not fetch MeshCore firmware releases: {ex}") from ex
        if not isinstance(releases, list):
            raise UpdateFailed("Unexpected MeshCore firmware release list")
        return latest_releases(releases)


def async_get_release_coordinator(hass: HomeAssistant) -> FirmwareReleaseCoordinator:
    """Return the shared release coordinator, creating it once.

    The first fetch runs in the background so a slow or unreachable GitHub
    never delays the entry setup; until it succeeds the update entities show
    the latest version as unknown.
    """
    coordinator: FirmwareReleaseCoordinator | None = hass.data.get(_RELEASE_DATA_KEY)
    if coordinator is None:
        coordinator = FirmwareReleaseCoordinator(hass)
        hass.data[_RELEASE_DATA_KEY] = coordinator
        hass.async_create_background_task(
            coordinator.async_refresh(), f"{DOMAIN} firmware release check"
        )
    return coordinator


def build_repeater_updates(coordinator, repeater: dict) -> list[UpdateEntity]:
    """Build a tracked repeater's update entity, for setup and for a live add."""
    releases = getattr(coordinator, "firmware_releases", None)
    if releases is None:
        return []
    return [MeshCoreRepeaterFirmwareUpdate(coordinator, releases, repeater)]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up MeshCore firmware update entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    releases = async_get_release_coordinator(hass)

    # Kept so a repeater added to a loaded entry gets its entity without a reload.
    coordinator.firmware_releases = releases
    coordinator.update_add_entities = async_add_entities

    entities: list[UpdateEntity] = [MeshCoreCompanionFirmwareUpdate(coordinator, releases)]
    for repeater in coordinator.settings.repeater_records:
        entities.extend(build_repeater_updates(coordinator, repeater))
    async_add_entities(entities)


class _MeshCoreFirmwareUpdate(CoordinatorEntity, UpdateEntity):
    """Shared behaviour: installed version from the node, latest from GitHub."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Firmware"

    def __init__(self, coordinator, releases: FirmwareReleaseCoordinator) -> None:
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._releases = releases

    async def async_added_to_hass(self) -> None:
        """Also refresh when the release check completes."""
        await super().async_added_to_hass()
        self.async_on_remove(self._releases.async_add_listener(self.async_write_ha_state))

    @property
    def _role(self) -> str:
        raise NotImplementedError

    @property
    def _raw_installed_version(self) -> Any:
        raise NotImplementedError

    @property
    def _release(self) -> FirmwareRelease | None:
        return (self._releases.data or {}).get(self._role)

    @property
    def installed_version(self) -> str | None:
        return normalize_firmware_version(self._raw_installed_version)

    @property
    def latest_version(self) -> str | None:
        release = self._release
        if release is None:
            return None
        # An unknown installed version reports no update rather than a false one.
        if self.installed_version is None:
            return None
        return release.version

    @property
    def release_url(self) -> str | None:
        release = self._release
        return (release.url or None) if release else None

    @property
    def release_summary(self) -> str | None:
        release = self._release
        if release is None or not release.notes:
            return None
        summary = release.notes.strip()
        if len(summary) > _RELEASE_SUMMARY_MAX:
            summary = summary[: _RELEASE_SUMMARY_MAX - 1].rstrip() + "…"
        return summary

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        release = self._release
        return {
            "firmware_role": self._role,
            "reported_version": self._raw_installed_version,
            "release_tag": release.tag if release else None,
        }

    async def async_release_notes(self) -> str | None:
        release = self._release
        return release.notes if release else None


class MeshCoreCompanionFirmwareUpdate(_MeshCoreFirmwareUpdate):
    """Firmware update for the companion radio this entry connects to."""

    def __init__(self, coordinator, releases: FirmwareReleaseCoordinator) -> None:
        super().__init__(coordinator, releases)
        public_key_short = coordinator.pubkey[:6] if coordinator.pubkey else ""
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_firmware_update"
        self.entity_id = format_entity_id(ENTITY_DOMAIN_UPDATE, public_key_short, "firmware")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)}
        )

    @property
    def _role(self) -> str:
        return ROLE_COMPANION

    @property
    def _raw_installed_version(self) -> Any:
        return (self.coordinator.device_info or {}).get("sw_version")


class MeshCoreRepeaterFirmwareUpdate(_MeshCoreFirmwareUpdate):
    """Firmware update for a tracked repeater or room server.

    The installed version is the one the repeater last reported, through the
    firmware refresh button or a version restored from the device registry.
    """

    def __init__(self, coordinator, releases: FirmwareReleaseCoordinator, repeater: dict) -> None:
        super().__init__(coordinator, releases)
        self.pubkey_prefix = repeater.get("pubkey_prefix", "")
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_repeater_{self.pubkey_prefix}_firmware_update"
        )
        self.entity_id = format_entity_id(ENTITY_DOMAIN_UPDATE, self.pubkey_prefix[:10], "firmware")
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{coordinator.config_entry.entry_id}_repeater_{self.pubkey_prefix}",
                )
            }
        )

    @property
    def _role(self) -> str:
        contact = None
        api = getattr(self.coordinator, "api", None)
        if api is not None:
            try:
                contact = api.contact_by_prefix(self.pubkey_prefix)
            except Exception:  # noqa: BLE001 - a missing contact is not an error here
                contact = None
        return role_for_contact_type((contact or {}).get("type"))

    @property
    def _raw_installed_version(self) -> Any:
        return self.coordinator.repeater_firmware(self.pubkey_prefix)
