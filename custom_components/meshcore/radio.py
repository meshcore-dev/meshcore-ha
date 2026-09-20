"""Radio session: the single owner of a config entry's MeshCore link."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.core import HomeAssistant

from meshcore import MeshCore
from meshcore.events import Event, EventType, Subscription

from .const import (
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_TCP,
    CONNECTION_TYPE_USB,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CLOSE_DEADLINE: Final = 5.0
CONNECT_SETTLE_SECONDS: Final = 1.0
RECONNECT_BACKOFF: Final[tuple[float, ...]] = (5.0, 10.0, 20.0, 40.0, 60.0)
RECONNECT_JITTER: Final = 0.2


@dataclass
class _Registration:
    """One integration subscription, replayed onto every new dispatcher."""

    event_type: EventType | None
    handler: Callable[[Event], Any]
    attribute_filters: dict[str, Any] | None
    live: Subscription | None = None


class RadioSession:
    """Own one config entry's MeshCore link: connect, recover, subscribe, close.

    Subscriptions registered here outlive the SDK instance they run on: the
    session replays them onto whichever dispatcher is current, so no caller
    keeps a reference to an SDK object that a reconnect may replace.
    """

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
        """Record the transport configuration without touching hardware."""
        self.hass = hass
        self.connection_type = connection_type
        self.usb_path = usb_path
        self.baudrate = baudrate
        self.ble_address = ble_address
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.self_info: dict[str, Any] = {}

        self._mesh_core: MeshCore | None = None
        self._connected = False
        self._closing = False
        self._forwarding = True
        self._registrations: list[_Registration] = []
        self._connect_hooks: list[Callable[[], None]] = []
        self._reconnect_task: asyncio.Task | None = None

    @property
    def mesh_core(self) -> MeshCore | None:
        """Return the live SDK instance, or None whenever the link is down."""
        return self._mesh_core if self._connected else None

    @property
    def connected(self) -> bool:
        """Return whether the link is up and validated."""
        return self._connected

    async def start(self) -> bool:
        """Open, validate and announce the link. False means nothing is open."""
        self._closing = False
        if not await self._open():
            return False
        await self._on_connected()
        return True

    async def close(self, deadline: float = CLOSE_DEADLINE) -> None:
        """Stop recovery and release the link; safe to call more than once."""
        if self._closing:
            return
        self._closing = True
        self._connected = False

        task, self._reconnect_task = self._reconnect_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        mesh_core, self._mesh_core = self._mesh_core, None
        self._detach_all()
        self.hass.bus.async_fire(f"{DOMAIN}_disconnected", {})
        if mesh_core is not None:
            await self._shutdown_instance(mesh_core, deadline)
        _LOGGER.info("Disconnection complete")

    def subscribe(
        self,
        event_type: EventType | None,
        handler: Callable[[Event], Any],
        *,
        attribute_filters: dict[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Register a handler that survives reconnects; returns its remover."""
        registration = _Registration(event_type, self._guard(handler), attribute_filters)
        self._registrations.append(registration)
        self._attach(registration)

        def unsubscribe() -> None:
            """Drop the registration and any live dispatcher subscription."""
            if registration in self._registrations:
                self._registrations.remove(registration)
            if registration.live is not None:
                registration.live.unsubscribe()
                registration.live = None

        return unsubscribe

    def add_connect_hook(self, hook: Callable[[], None]) -> None:
        """Register a callback run after every successful (re)connect."""
        self._connect_hooks.append(hook)

    def pause_forwarding(self) -> None:
        """Silence registered handlers without dropping their registrations."""
        self._forwarding = False

    def resume_forwarding(self) -> None:
        """Let registered handlers run again after a refused unload."""
        self._forwarding = True

    def cache_self_info_event(self, event: Any) -> None:
        """Refresh the cached SELF_INFO payload from a command result or event."""
        if event is None:
            return
        event_type = getattr(event, "type", None)
        if event_type != EventType.SELF_INFO and "SELF_INFO" not in str(event_type):
            return
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            self.self_info = dict(payload)

    async def _open(self) -> bool:
        """Create and validate an SDK instance, leaving no handle on failure."""
        mesh_core = await self._create()
        if mesh_core is None:
            return False

        await asyncio.sleep(CONNECT_SETTLE_SECONDS)

        if not await self._validate(mesh_core):
            await self._shutdown_instance(mesh_core, CLOSE_DEADLINE)
            return False

        self._mesh_core = mesh_core
        return True

    async def _create(self) -> MeshCore | None:
        """Build the SDK instance for the configured transport."""
        try:
            _LOGGER.info("Connecting to MeshCore device...")
            if self.connection_type == CONNECTION_TYPE_USB and self.usb_path:
                _LOGGER.info(
                    "Using USB connection at %s with baudrate %s", self.usb_path, self.baudrate
                )
                return await MeshCore.create_serial(
                    self.usb_path,
                    self.baudrate,
                    debug=False,
                    auto_reconnect=False,
                )
            if self.connection_type == CONNECTION_TYPE_BLE:
                _LOGGER.info("Using BLE connection with address %s", self.ble_address)
                return await MeshCore.create_ble(
                    self.ble_address if self.ble_address else "",
                    debug=False,
                    auto_reconnect=False,
                )
            if self.connection_type == CONNECTION_TYPE_TCP and self.tcp_host:
                _LOGGER.info("Using TCP connection to %s:%s", self.tcp_host, self.tcp_port)
                return await MeshCore.create_tcp(
                    self.tcp_host,
                    self.tcp_port,
                    debug=False,
                    auto_reconnect=False,
                )
            _LOGGER.error("Invalid connection configuration")
        except Exception as ex:
            _LOGGER.error("Error connecting to MeshCore device: %s", ex)
        return None

    async def _validate(self, mesh_core: MeshCore) -> bool:
        """Run the appstart handshake and cache the identity it reports."""
        try:
            _LOGGER.info("Validating connection with appstart command...")
            result = await mesh_core.commands.send_appstart()
        except Exception as ex:
            _LOGGER.error("Connection validation failed (appstart exception): %s", ex)
            return False

        if result is None:
            _LOGGER.error("Connection validation failed: appstart returned None")
            return False
        if result.type == EventType.ERROR:
            _LOGGER.error(
                "Connection validation failed: appstart returned error: %s", result.payload
            )
            return False

        self.cache_self_info_event(result)
        _LOGGER.info("Connection validated successfully: %s", result)
        return True

    async def _on_connected(self) -> None:
        """Replay subscriptions, sync time, run hooks and announce the edge."""
        mesh_core = self._mesh_core
        if mesh_core is None:
            return

        mesh_core.dispatcher.subscribe(EventType.DISCONNECTED, self._handle_sdk_disconnect)
        for registration in self._registrations:
            self._attach(registration)

        try:
            _LOGGER.info("Syncing time with MeshCore device...")
            current_timestamp = int(time.time())
            await mesh_core.commands.set_time(current_timestamp)
            _LOGGER.info("Time sync completed: %s", current_timestamp)
        except Exception as ex:
            _LOGGER.error("Failed to sync time on connection: %s", ex)

        self._connected = True
        for hook in list(self._connect_hooks):
            try:
                hook()
            except Exception as ex:
                _LOGGER.error("Connect hook failed: %s", ex)

        self.hass.bus.async_fire(
            f"{DOMAIN}_connected", {"connection_type": self.connection_type}
        )
        _LOGGER.info("Successfully connected to MeshCore device")

    def _handle_sdk_disconnect(self, event: Event) -> None:
        """React to the SDK's link-loss event exactly once per edge."""
        if self._closing or not self._connected:
            return
        self._connected = False
        _LOGGER.warning(
            "MeshCore link lost (%s); starting recovery", getattr(event, "payload", None)
        )
        self.hass.bus.async_fire(f"{DOMAIN}_disconnected", {"unexpected": True})
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = self.hass.async_create_background_task(
                self._reconnect_loop(), f"{DOMAIN}_reconnect", eager_start=False
            )

    async def _reconnect_loop(self) -> None:
        """Release the dead instance, then retry with capped jittered backoff."""
        mesh_core, self._mesh_core = self._mesh_core, None
        self._detach_all()
        if mesh_core is not None:
            await self._shutdown_instance(mesh_core, CLOSE_DEADLINE)

        attempt = 0
        while not self._closing:
            base = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            await asyncio.sleep(base * (1 + random.uniform(-RECONNECT_JITTER, RECONNECT_JITTER)))
            if self._closing:
                return
            if await self._open():
                await self._on_connected()
                return
            attempt += 1

    async def _shutdown_instance(self, mesh_core: MeshCore, deadline: float) -> None:
        """Close the retained transport first, then bound the dispatcher stop."""
        connection = getattr(getattr(mesh_core, "connection_manager", None), "connection", None)
        if connection is not None:
            try:
                await connection.disconnect()
            except Exception as ex:
                _LOGGER.error("Error closing MeshCore transport: %s", ex)

        dispatcher = getattr(mesh_core, "dispatcher", None)
        if dispatcher is None:
            return
        worker = getattr(dispatcher, "_task", None)
        try:
            await asyncio.wait_for(dispatcher.stop(), deadline)
        except TimeoutError:
            _LOGGER.warning(
                "MeshCore dispatcher did not drain within %.1fs; cancelling its worker", deadline
            )
        except Exception as ex:
            _LOGGER.error("Error stopping MeshCore dispatcher: %s", ex)
        if worker is not None and not worker.done():
            mesh_core.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    def _attach(self, registration: _Registration) -> None:
        """Bind one registration to the current dispatcher, if there is one."""
        if self._mesh_core is None:
            return
        registration.live = self._mesh_core.dispatcher.subscribe(
            registration.event_type, registration.handler, registration.attribute_filters
        )

    def _detach_all(self) -> None:
        """Forget dispatcher subscriptions; the registrations themselves stay."""
        for registration in self._registrations:
            registration.live = None

    def _guard(self, handler: Callable[[Event], Any]) -> Callable[[Event], Any]:
        """Wrap a handler so paused forwarding silences it without unsubscribing."""
        if asyncio.iscoroutinefunction(handler):

            async def async_guarded(event: Event) -> None:
                """Deliver to an async handler unless forwarding is paused."""
                if self._forwarding:
                    await handler(event)

            return async_guarded

        def guarded(event: Event) -> None:
            """Deliver to a sync handler unless forwarding is paused."""
            if self._forwarding:
                handler(event)

        return guarded
