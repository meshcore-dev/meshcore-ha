"""Session stand-ins for tiers that cannot run the real RadioSession."""

from contextlib import asynccontextmanager
from inspect import signature
from typing import Any
from unittest.mock import MagicMock


async def passthrough_exchange(fn, /, *args: Any, deadline: float | None = None, **kwargs: Any):
    """Run one command with no gate, the way RadioSession.exchange would."""
    return await fn(*args, **kwargs)


@asynccontextmanager
async def open_transaction():
    """Stand in for RadioSession.transaction, which owns no state here."""
    yield


class StubSession:
    """Resolve the session's by-name command surface against scripted mocks.

    Tests drive the same call shape production uses -- a command name, not an
    SDK handle -- without a dispatcher, a link, or the gate's locks.
    """

    def __init__(self, commands: Any = None, **attributes: Any) -> None:
        """Build a session over a scripted command namespace."""
        self.commands = MagicMock() if commands is None else commands
        self.contacts: dict[str, dict] = {}
        self.self_info: dict[str, Any] = {}
        self.contacts_dirty = False
        self.contacts_reported_at = 0.0
        self.__dict__.update(attributes)

    def command_parameters(self, name: str) -> list[str] | None:
        """Return a scripted command's parameter names, None when it has none."""
        command = None if name.startswith("_") else getattr(self.commands, name, None)
        if not callable(command):
            return None
        try:
            return list(signature(command).parameters)
        except (TypeError, ValueError):
            return []

    async def exchange(
        self, command: Any, /, *args: Any, deadline: float | None = None, **kwargs: Any
    ) -> Any:
        """Run one command, named or callable, with no gate."""
        fn = getattr(self.commands, command) if isinstance(command, str) else command
        return await fn(*args, **kwargs)

    async def invoke(
        self, name: str, /, *args: Any, deadline: float | None = None, **kwargs: Any
    ) -> Any:
        """Run one named command the way the unlocked path would."""
        return await self.exchange(name, *args, **kwargs)

    @asynccontextmanager
    async def mesh_lease(self):
        """Stand in for the mesh lease, which owns no state here."""
        yield

    @asynccontextmanager
    async def transaction(self):
        """Stand in for RadioSession.transaction, which owns no state here."""
        yield

    def contact_by_prefix(self, prefix: str) -> dict | None:
        """Resolve a contact by public-key prefix from the scripted table."""
        return next((c for key, c in self.contacts.items() if key.startswith(prefix)), None)

    def contact_by_name(self, name: str) -> dict | None:
        """Resolve a contact by advertised name from the scripted table."""
        return next((c for c in self.contacts.values() if c.get("adv_name") == name), None)

    async def ensure_contacts(self, follow: bool = True) -> bool:
        """Report the contact table unchanged; nothing syncs here."""
        return False

    def mark_contacts_dirty(self) -> None:
        """Record that the next contact sync must refetch."""
        self.contacts_dirty = True

    def forget_contact(self, public_key: str) -> bool:
        """Drop one contact from the scripted table."""
        return self.contacts.pop(public_key, None) is not None

    def cache_self_info_event(self, event: Any) -> None:
        """Refresh cached identity the way the session does."""
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            self.self_info = dict(payload)


def stub_session(commands: Any = None, **attributes: Any) -> StubSession:
    """Build a session whose exchange just calls the command it is given."""
    return StubSession(commands, **attributes)
