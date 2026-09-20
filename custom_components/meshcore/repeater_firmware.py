"""Helpers for querying and persisting repeater firmware versions."""

from __future__ import annotations

import asyncio
import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from meshcore.events import EventType

from .config import Settings
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
# CommonCLI formats `ver` as "<firmware> (Build: <date>)".
_VERSION_REPLY_PATTERN = re.compile(r"^.+ \(Build: .+\)$")
_ACTIVE_REFRESHES: set[tuple[str, str]] = set()


class RepeaterFirmwareRefreshError(Exception):
    """Raised when a repeater firmware version cannot be refreshed."""


async def async_query_repeater_firmware(
    session,
    pubkey_prefix: str,
    *,
    password: str | None = None,
    timeout: float = 15,
) -> str:
    """Query one repeater's firmware version."""
    if not session.connected:
        raise RepeaterFirmwareRefreshError("MeshCore device is not connected")

    contact = session.contact_by_prefix(pubkey_prefix)
    if not contact:
        raise RepeaterFirmwareRefreshError("repeater contact was not found")

    public_key = contact.get("public_key") or ""
    if not public_key:
        raise RepeaterFirmwareRefreshError("repeater contact has no public key")

    if password is not None:
        try:
            login_result = await session.login(contact, password)
        except Exception as ex:
            raise RepeaterFirmwareRefreshError("failed to log in to repeater") from ex
        if not login_result:
            raise RepeaterFirmwareRefreshError("failed to log in to repeater")

    target_prefix = public_key[:12]
    response_future = asyncio.get_running_loop().create_future()

    def _handle_response(event) -> None:
        if response_future.done():
            return
        text = str((event.payload or {}).get("text") or "").strip()
        if text.casefold().startswith(("error", "err:", "failed")):
            response_future.set_result(event)
        elif _VERSION_REPLY_PATTERN.fullmatch(text):
            response_future.set_result(event)

    unsubscribe = session.subscribe(
        EventType.CONTACT_MSG_RECV,
        _handle_response,
        attribute_filters={"pubkey_prefix": target_prefix},
    )
    try:
        try:
            send_result = await session.exchange("send_cmd", contact, "ver")
        except Exception as ex:
            raise RepeaterFirmwareRefreshError("failed to send version command") from ex

        if send_result is None or getattr(send_result, "type", None) == EventType.ERROR:
            raise RepeaterFirmwareRefreshError("version command was rejected")

        try:
            message = await asyncio.wait_for(response_future, timeout)
        except TimeoutError as ex:
            raise RepeaterFirmwareRefreshError("timed out waiting for version reply") from ex
    finally:
        unsubscribe()

    if message is None or getattr(message, "type", None) != EventType.CONTACT_MSG_RECV:
        raise RepeaterFirmwareRefreshError("timed out waiting for version reply")

    version = str((message.payload or {}).get("text") or "").strip()
    if not version:
        raise RepeaterFirmwareRefreshError("version reply was empty")
    if version.casefold().startswith(("error", "err:", "failed")):
        raise RepeaterFirmwareRefreshError("repeater returned an error reply")
    return version


def async_save_repeater_firmware_version(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    pubkey_prefix: str,
    version: str,
) -> bool:
    """Record a repeater version in runtime state and its device registry entry.

    The version is an observation of the mesh, so it is never written into the
    config entry: doing that used to reload the whole entry and reset the
    traffic budget and every node's schedule.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if coordinator is None:
        return False
    if not any(
        repeater.pubkey_prefix == pubkey_prefix
        for repeater in Settings.from_entry(config_entry).repeaters
    ):
        return False

    device_registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{config_entry.entry_id}_repeater_{pubkey_prefix}")
    device = device_registry.async_get_device(identifiers={identifier})
    if not device:
        raise RepeaterFirmwareRefreshError(
            "repeater device-registry entry was not found"
        )

    coordinator.set_repeater_firmware(pubkey_prefix, version)
    device_registry.async_update_device(device.id, sw_version=version)

    return True


async def async_refresh_repeater_firmware(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    session,
    pubkey_prefix: str,
    *,
    timeout: float = 15,
) -> str:
    """Query and persist one configured repeater's firmware version."""
    configured = Settings.from_entry(config_entry).repeaters
    repeater = next(
        (item for item in configured if item.pubkey_prefix == pubkey_prefix), None
    )
    if repeater is None:
        raise RepeaterFirmwareRefreshError("repeater is no longer configured")

    refresh_key = (config_entry.entry_id, pubkey_prefix)
    if refresh_key in _ACTIVE_REFRESHES:
        raise RepeaterFirmwareRefreshError("firmware refresh is already in progress")

    _ACTIVE_REFRESHES.add(refresh_key)
    try:
        version = await async_query_repeater_firmware(
            session,
            pubkey_prefix,
            password=repeater.password,
            timeout=timeout,
        )
        if not async_save_repeater_firmware_version(
            hass, config_entry, pubkey_prefix, version
        ):
            raise RepeaterFirmwareRefreshError("repeater is no longer configured")
        return version
    finally:
        _ACTIVE_REFRESHES.discard(refresh_key)
