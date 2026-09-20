"""Session stand-ins for tiers that cannot run the real RadioSession."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any


async def passthrough_exchange(fn, /, *args: Any, timeout: float | None = None, **kwargs: Any):
    """Run one command with no gate, the way RadioSession.exchange would."""
    return await fn(*args, **kwargs)


@asynccontextmanager
async def open_transaction():
    """Stand in for RadioSession.transaction, which owns no state here."""
    yield


def stub_session(**attributes: Any) -> SimpleNamespace:
    """Build a session whose exchange just calls the command it is given."""
    return SimpleNamespace(
        exchange=passthrough_exchange, transaction=open_transaction, **attributes
    )
