"""Scripted radio with the installed SDK's actual dispatcher and subscriptions."""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Final

from meshcore.events import Event, EventDispatcher, EventType, Subscription

SCRIPTABLE_PRIVATE: Final = frozenset({"_send_login_raw"})


class FakeRadio:
    """Replace radio I/O while retaining SDK dispatch and reconnect semantics.

    Script keys are ``(method, *args)`` with a trailing sorted keyword tuple
    when keywords are supplied; contact dictionaries become sorted item tuples.
    Responses are Events, exceptions, awaitables, or callables returning one of
    those, which is how a command answers differently on each invocation.
    Unscripted commands fail immediately so accidental I/O cannot be hidden.
    """

    def __init__(self) -> None:
        """Initialize a disconnected radio without spawning tasks."""
        self.dispatcher = EventDispatcher()
        self._dispatchers = [self.dispatcher]
        self.connected = False
        self.commands = self
        self.script: dict[tuple, Any] = {}
        self.calls: list[tuple] = []
        self._contacts: dict[str, dict] = {}
        self._contacts_dirty = False
        self.transport_closed = False
        self.connection_manager = SimpleNamespace(
            connection=SimpleNamespace(disconnect=self.close_transport)
        )

    async def close_transport(self) -> None:
        """Record the explicit handle closure an owner must always perform."""
        self.transport_closed = True
        self.connected = False

    def stop(self) -> None:
        """Cancel the dispatcher worker exactly as MeshCore.stop() does."""
        if self.dispatcher._task and not self.dispatcher._task.done():
            self.dispatcher.running = False
            self.dispatcher._task.cancel()

    def key(self, method: str, *args: Any, **kwargs: Any) -> tuple:
        """Build the script key for one invocation, as callers must script it."""
        key = (
            method,
            *(tuple(sorted(arg.items())) if isinstance(arg, dict) else arg for arg in args),
        )
        if kwargs:
            key += (tuple(sorted(kwargs.items())),)
        return key

    def __getattr__(self, method: str) -> Callable:
        """Expose scripted SDK command methods without mocking dispatch."""
        if method.startswith("_") and method not in SCRIPTABLE_PRIVATE:
            raise AttributeError(method)

        async def command(*args: Any, **kwargs: Any) -> Any:
            """Return the scripted response for this exact invocation."""
            if not self.connected:
                raise ConnectionError("FakeRadio link is down")
            key = self.key(method, *args, **kwargs)
            self.calls.append(key)
            response = self.script[key]
            if callable(response):
                response = response()
            if isinstance(response, BaseException):
                raise response
            if hasattr(response, "__await__"):
                response = await response
            if isinstance(response, Event):
                await self.emit(response.type, response.payload, response.attributes)
            return response

        return command

    async def start(self) -> None:
        """Start the real dispatcher on the running loop."""
        await self.dispatcher.start()
        self.connected = True

    @property
    def contacts(self) -> dict[str, dict]:
        """Mirror the SDK's contact-table property over the same store."""
        return self._contacts

    @contacts.setter
    def contacts(self, table: dict[str, dict]) -> None:
        """Install a contact table the way a CONTACTS frame fills one."""
        self._contacts = table

    @property
    def contacts_dirty(self) -> bool:
        """Mirror the SDK flag that decides whether a resync is due."""
        return self._contacts_dirty

    def get_contact_by_key_prefix(self, prefix: str) -> dict | None:
        """Resolve a contact using the SDK's public lookup surface."""
        return next((c for key, c in self.contacts.items() if key.startswith(prefix)), None)

    def get_contact_by_name(self, name: str) -> dict | None:
        """Resolve an advertised contact name."""
        return next((c for c in self.contacts.values() if c.get("adv_name") == name), None)

    async def ensure_contacts(self, follow: bool = False) -> bool:
        """Expose fixture contacts without issuing a radio request."""
        return False

    def subscribe(
        self,
        event_type: EventType | None,
        handler: Callable,
        attribute_filters: dict | None = None,
    ) -> Subscription:
        """Subscribe directly to the current SDK dispatcher."""
        return self.dispatcher.subscribe(event_type, handler, attribute_filters)

    async def emit(
        self,
        event_type: EventType,
        payload: Any,
        attributes: dict | None = None,
    ) -> None:
        """Deliver an event and settle its SDK callbacks before returning."""
        await self.dispatcher.dispatch(Event(event_type, payload, attributes))
        await self.dispatcher.queue.join()
        while pending := [
            task
            for task in self.dispatcher._background_tasks
            if not task.done() and task is not asyncio.current_task()
        ]:
            await asyncio.gather(*pending)

    @property
    def is_connected(self) -> bool:
        """Mirror the SDK's link-state property."""
        return self.connected

    async def drop_link(self) -> None:
        """Publish the SDK's link-loss event and reject subsequent commands."""
        self.connected = False
        await self.emit(EventType.DISCONNECTED, {})

    async def restore_link(self) -> None:
        """Replace the dispatcher, intentionally orphaning old subscriptions."""
        await self.close()
        self.dispatcher = EventDispatcher()
        self._dispatchers.append(self.dispatcher)
        await self.start()
        await self.emit(EventType.CONNECTED, {})

    async def close(self) -> None:
        """Drain before SDK stop to avoid its queued-event shutdown deadlock."""
        self.connected = False
        for dispatcher in self._dispatchers:
            if dispatcher.running:
                await dispatcher.queue.join()
                await dispatcher.stop()

    async def disconnect(self) -> None:
        """Provide the SDK disconnect surface for API lifecycle tests."""
        await self.close()

    def assert_no_leaked_tasks(self) -> None:
        """Check all dispatcher generations, including retired callbacks."""
        for dispatcher in self._dispatchers:
            assert dispatcher._task is None or dispatcher._task.done()
            assert not any(not task.done() for task in dispatcher._background_tasks)
