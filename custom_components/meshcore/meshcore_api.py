"""Facade over the radio session for the MeshCore integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import DEFAULT_BAUDRATE, DEFAULT_TCP_PORT
from .radio import RadioSession


class MeshCoreAPI:
    """Expose the radio session under the call surface the integration uses."""

    def __init__(
        self,
        hass: HomeAssistant,
        connection_type: str,
        usb_path: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        ble_address: str | None = None,
        tcp_host: str | None = None,
        tcp_port: int = DEFAULT_TCP_PORT,
    ) -> None:
        """Build the session that owns this entry's link."""
        self.hass = hass
        self.connection_type = connection_type
        self.session = RadioSession(
            hass,
            connection_type,
            usb_path=usb_path,
            baudrate=baudrate,
            ble_address=ble_address,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
        )

    @property
    def mesh_core(self):
        """Return the underlying MeshCore instance, or None when disconnected."""
        return self.session.mesh_core

    @property
    def connected(self) -> bool:
        """Return whether the device is connected."""
        return self.session.connected

    @property
    def node_name(self) -> str:
        """Return the latest known node name from SELF_INFO."""
        return str(self.session.self_info.get("name", "") or "").strip()

    @property
    def self_info(self) -> dict[str, Any]:
        """Return the latest known SELF_INFO payload."""
        return dict(self.session.self_info)

    def _cache_self_info_event(self, event: Any) -> None:
        """Cache a SELF_INFO payload for consumers that need identity details."""
        self.session.cache_self_info_event(event)

    async def connect(self) -> bool:
        """Connect to the MeshCore device using the configured transport."""
        return await self.session.start()

    async def disconnect(self) -> None:
        """Release the link and every resource the session holds."""
        await self.session.close()
