"""Radio session: the single owner of a config entry's MeshCore link."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
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
from .events import fire_connected, fire_disconnected
from .radio_commands import MeshCommands

_LOGGER = logging.getLogger(__name__)

CLOSE_DEADLINE: Final = 5.0
CONNECT_SETTLE_SECONDS: Final = 1.0
CREATE_TIMEOUT: Final = 30.0
LINK_ERRORS: Final = (OSError, EOFError)
RECONNECT_BACKOFF: Final[tuple[float, ...]] = (5.0, 10.0, 20.0, 40.0, 60.0)
RECONNECT_JITTER: Final = 0.2


class RadioUnavailable(RuntimeError):
    """Raised when a command cannot run because the link is down."""


@dataclass
class _Registration:
    """One integration subscription, replayed onto every new dispatcher."""

    event_type: EventType | None
    handler: Callable[[Event], Any]
    attribute_filters: dict[str, Any] | None
    live: Subscription | None = None


class RadioSession(MeshCommands):
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
        entry: ConfigEntry | None = None,
    ) -> None:
        """Record the transport configuration without touching hardware.

        ``entry`` is the config entry this link belongs to, so its lifecycle
        events can name the radio; a setup-time probe owns no entry yet.
        """
        self.hass = hass
        self.entry = entry
        self.connection_type = connection_type
        self.usb_path = usb_path
        self.baudrate = baudrate
        self.ble_address = ble_address
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.self_info: dict[str, Any] = {}

        self._mesh_core: MeshCore | None = None
        self._contacts_reported_at = 0.0
        self._connected = False
        self._closing = False
        self._started = False
        self._forwarding = True
        self._registrations: list[_Registration] = []
        self._connect_hooks: list[Callable[[], None]] = []
        self._reconnect_task: asyncio.Task | None = None
        self._exchange_lock = asyncio.Lock()
        self._exchange_owner: asyncio.Task | None = None
        self._mesh_lease = asyncio.Semaphore(1)

    @property
    def connected(self) -> bool:
        """Return whether the link is up and validated."""
        return self._connected

    @property
    def node_name(self) -> str:
        """Return the latest known node name from SELF_INFO."""
        return str(self.self_info.get("name", "") or "").strip()

    async def start(self) -> bool:
        """Open, validate and announce the link. False means nothing is open."""
        self._closing = False
        if not await self._open():
            return False
        return await self._on_connected()

    async def connect(self) -> bool:
        """Open the link under the name the integration's setup path uses."""
        return await self.start()

    async def disconnect(self) -> None:
        """Release the link under the name the integration's teardown uses."""
        await self.close()

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
        if not self._started:
            return
        fire_disconnected(self.hass, self.entry)
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

    async def exchange(
        self,
        command: str | Callable[..., Any],
        /,
        *args: Any,
        deadline: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run one immediate SDK command as the link's only speaker.

        ``command`` is an SDK command name, resolved on the live instance once
        the link is owned, or a callable the session itself already holds.
        ``deadline`` bounds the await here; every other keyword reaches the
        command, which is why it is not called ``timeout``.
        """
        if self._exchange_owner is asyncio.current_task():
            return await self._command(command, args, kwargs, deadline)
        async with self._exchange_lock:
            return await self._command(command, args, kwargs, deadline)

    async def invoke(
        self,
        name: str,
        /,
        *args: Any,
        deadline: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run one named command without the exchange lock.

        For the few SDK helpers that wait on a mesh round-trip: they hold the
        mesh lease instead, so a minutes-long request cannot stall local work.
        """
        return await self._command(name, args, kwargs, deadline)

    @contextlib.asynccontextmanager
    async def mesh_lease(self) -> AsyncIterator[None]:
        """Hold the radio's single firmware pending-flag slot."""
        async with self._mesh_lease:
            yield

    def command_parameters(self, name: str) -> list[str] | None:
        """Return a command's parameter names, or None when there is no such command."""
        try:
            command = self._resolve(name)
        except (AttributeError, RadioUnavailable):
            return None
        if not callable(command):
            return None
        try:
            return list(inspect.signature(command).parameters)
        except (TypeError, ValueError):
            return []

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Own the link across several immediate exchanges.

        Exchanges made by the owning task run re-entrantly. A transaction must
        never span a mesh reply, and a mesh request must never be started
        inside one: the lease and the exchange lock are always taken in that
        order.
        """
        async with self._exchange_lock:
            self._exchange_owner = asyncio.current_task()
            try:
                yield
            finally:
                self._exchange_owner = None

    async def wait_for(
        self, event_type: EventType, attribute_filters: dict[str, Any], timeout: float
    ) -> Event | None:
        """Await one filtered event through the session; None when it times out."""
        future: asyncio.Future[Event] = asyncio.get_running_loop().create_future()

        def resolve(event: Event) -> None:
            """Hand the first matching event to the waiter."""
            if not future.done():
                future.set_result(event)

        unsubscribe = self.subscribe(event_type, resolve, attribute_filters=attribute_filters)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            return None
        finally:
            unsubscribe()

    @property
    def contacts(self) -> Mapping[str, dict[str, Any]]:
        """Return a read-only view of the node's contact table."""
        mesh_core = self._live()
        return MappingProxyType(getattr(mesh_core, "contacts", None) or {})

    def contact_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        """Resolve a contact by public-key prefix; None when the link is down."""
        mesh_core = self._live()
        return mesh_core.get_contact_by_key_prefix(prefix) if mesh_core is not None else None

    def contact_by_name(self, name: str) -> dict[str, Any] | None:
        """Resolve a contact by advertised name; None when the link is down."""
        mesh_core = self._live()
        return mesh_core.get_contact_by_name(name) if mesh_core is not None else None

    async def ensure_contacts(self, follow: bool = True) -> bool:
        """Resync the contact table when the node reports it stale.

        True means a fetch was issued, not that the node answered it; compare
        ``contacts_reported_at`` across the call to tell those apart.
        """
        mesh_core = self._live()
        if mesh_core is None:
            raise RadioUnavailable("MeshCore device is not connected")
        return bool(await self.exchange(mesh_core.ensure_contacts, follow=follow))

    @property
    def contacts_reported_at(self) -> float:
        """Monotonic stamp of the last contact table the node actually sent."""
        return self._contacts_reported_at

    def mark_contacts_dirty(self) -> None:
        """Make the next contact sync fetch the table again."""
        mesh_core = self._live()
        if mesh_core is not None:
            mesh_core._contacts_dirty = True

    def forget_contact(self, public_key: str) -> bool:
        """Drop one contact from the cached table; True when it was there."""
        mesh_core = self._live()
        contacts = getattr(mesh_core, "_contacts", None)
        if not isinstance(contacts, dict) or public_key not in contacts:
            return False
        del contacts[public_key]
        return True

    def _live(self) -> MeshCore | None:
        """Return the SDK instance while the link is up, else None."""
        return self._mesh_core if self._connected else None

    def _commands(self) -> Any:
        """Return the live SDK command surface, or refuse when the link is down."""
        mesh_core = self._live()
        if mesh_core is None:
            raise RadioUnavailable("MeshCore device is not connected")
        return mesh_core.commands

    def _resolve(self, name: str) -> Any:
        """Look one command up by name, refusing private SDK attributes."""
        if name.startswith("_"):
            raise AttributeError(f"unknown command: {name}")
        return getattr(self._commands(), name)

    async def _command(
        self,
        command: str | Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        deadline: float | None,
    ) -> Any:
        """Await one SDK command, turning link failures into RadioUnavailable."""
        mesh_core = self._mesh_core
        if mesh_core is None or not self._connected:
            raise RadioUnavailable("MeshCore device is not connected")
        fn = self._resolve(command) if isinstance(command, str) else command
        try:
            call = fn(*args, **kwargs)
            result = await (call if deadline is None else asyncio.wait_for(call, deadline))
        except LINK_ERRORS as ex:
            self._link_lost(f"command failed: {ex}")
            raise RadioUnavailable(str(ex)) from ex
        if not mesh_core.is_connected:
            self._link_lost("command completed on a closed link")
            raise RadioUnavailable("MeshCore link is not available")
        return result

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
        self._started = True
        return True

    async def _create(self) -> MeshCore | None:
        """Build the SDK instance for the configured transport, bounded in time."""
        try:
            _LOGGER.info("Connecting to MeshCore device...")
            factory = self._factory()
            if factory is None:
                _LOGGER.error("Invalid connection configuration")
                return None
            return await asyncio.wait_for(factory, CREATE_TIMEOUT)
        except TimeoutError:
            _LOGGER.error(
                "Timed out after %.0fs opening the MeshCore connection", CREATE_TIMEOUT
            )
        except Exception as ex:
            _LOGGER.error("Error connecting to MeshCore device: %s", ex)
        return None

    def _factory(self) -> Coroutine[Any, Any, MeshCore] | None:
        """Return the unawaited SDK creation call for the configured transport."""
        if self.connection_type == CONNECTION_TYPE_USB and self.usb_path:
            _LOGGER.info(
                "Using USB connection at %s with baudrate %s", self.usb_path, self.baudrate
            )
            return MeshCore.create_serial(
                self.usb_path,
                self.baudrate,
                debug=False,
                auto_reconnect=False,
            )
        if self.connection_type == CONNECTION_TYPE_BLE:
            _LOGGER.info("Using BLE connection with address %s", self.ble_address)
            return MeshCore.create_ble(
                self.ble_address if self.ble_address else "",
                debug=False,
                auto_reconnect=False,
            )
        if self.connection_type == CONNECTION_TYPE_TCP and self.tcp_host:
            _LOGGER.info("Using TCP connection to %s:%s", self.tcp_host, self.tcp_port)
            return MeshCore.create_tcp(
                self.tcp_host,
                self.tcp_port,
                debug=False,
                auto_reconnect=False,
            )
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

    async def _on_connected(self) -> bool:
        """Replay subscriptions, sync time, run hooks and announce the edge.

        Returns False, with the instance released, if the link dropped before
        the edge could be declared; callers treat that as a failed attempt.
        """
        mesh_core = self._mesh_core
        if mesh_core is None:
            return False

        mesh_core.dispatcher.subscribe(EventType.DISCONNECTED, self._handle_sdk_disconnect)
        mesh_core.dispatcher.subscribe(EventType.CONTACTS, self._handle_contacts)
        for registration in self._registrations:
            self._attach(registration)

        try:
            _LOGGER.info("Syncing time with MeshCore device...")
            current_timestamp = int(time.time())
            await mesh_core.commands.set_time(current_timestamp)
            _LOGGER.info("Time sync completed: %s", current_timestamp)
        except Exception as ex:
            _LOGGER.error("Failed to sync time on connection: %s", ex)

        if not mesh_core.is_connected:
            _LOGGER.warning("MeshCore link dropped during connect; retrying")
            self._mesh_core = None
            self._detach_all()
            await self._shutdown_instance(mesh_core, CLOSE_DEADLINE)
            return False

        self._connected = True
        for hook in list(self._connect_hooks):
            try:
                hook()
            except Exception as ex:
                _LOGGER.error("Connect hook failed: %s", ex)

        fire_connected(self.hass, self.entry, connection_type=self.connection_type)
        _LOGGER.info("Successfully connected to MeshCore device")
        return True

    def _handle_sdk_disconnect(self, event: Event) -> None:
        """React to the SDK's link-loss event."""
        self._link_lost(getattr(event, "payload", None))

    def _handle_contacts(self, _event: Event) -> None:
        """Record that the node answered a contact-table fetch."""
        self._contacts_reported_at = time.monotonic()

    def _link_lost(self, reason: Any) -> None:
        """Cross the link-loss edge exactly once and start recovery."""
        if self._closing or not self._connected:
            return
        self._connected = False
        _LOGGER.warning("MeshCore link lost (%s); starting recovery", reason)
        fire_disconnected(self.hass, self.entry, unexpected=True)
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
            if await self._open() and await self._on_connected():
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
        if registration.live is not None:
            registration.live.unsubscribe()
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
