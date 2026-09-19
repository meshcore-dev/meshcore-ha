"""Tests for the stale-path recovery used by repeater/telemetry polling.

Mirrors MeshCoreDataUpdateCoordinator._call_with_path_recovery: on a timed-out
mesh request with a stored *direct* path, reset the path to flood and retry once
within the same poll. This is what lets a freshly-added (or moved) repeater go
green on its first poll instead of waiting MAX_FAILURES_BEFORE_PATH_RESET cycles.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


async def _call_with_path_recovery(
    coord, command_factory, contact, node_config, pubkey_prefix, label, timeout=0.05
):
    """Standalone copy of the coordinator method for testability."""
    node_name = node_config.get("name", "unknown")

    async def _bounded(c):
        try:
            return await asyncio.wait_for(command_factory(c), timeout=timeout)
        except TimeoutError:
            coord.logger.warning(
                f"{label} for {node_name} exceeded {timeout}s; treating as no response"
            )
            return None

    result = await _bounded(contact)
    if result or not contact or contact.get("out_path_len", -1) <= -1:
        return result, contact

    coord.logger.info(
        f"No response for {label} from {node_name}; resetting path to flood and retrying"
    )
    if not await coord._reset_node_path(contact, node_config):
        return result, contact

    await asyncio.sleep(0)
    contact = coord.api.mesh_core.get_contact_by_key_prefix(pubkey_prefix) or contact
    result = await _bounded(contact)
    return result, contact


def _make_coordinator(reset_ok=True, refreshed_contact=None):
    coord = MagicMock()
    coord.logger = MagicMock()
    coord._reset_node_path = AsyncMock(return_value=reset_ok)
    coord.api = MagicMock()
    coord.api.mesh_core.get_contact_by_key_prefix = MagicMock(return_value=refreshed_contact)
    return coord


@pytest.mark.asyncio
async def test_success_first_try_no_reset():
    """A successful request never touches the path."""
    coord = _make_coordinator()
    contact = {"out_path_len": 3}
    factory = AsyncMock(return_value={"uptime": 100})

    result, out_contact = await _call_with_path_recovery(
        coord, factory, contact, {"name": "rptr"}, "ab12", "status request"
    )

    assert result == {"uptime": 100}
    assert out_contact is contact
    coord._reset_node_path.assert_not_awaited()
    assert factory.await_count == 1


@pytest.mark.asyncio
async def test_timeout_with_direct_path_resets_and_retries():
    """A timeout with a stored direct path resets to flood and retries once."""
    refreshed = {"out_path_len": -1}
    coord = _make_coordinator(reset_ok=True, refreshed_contact=refreshed)
    contact = {"out_path_len": 5}
    factory = AsyncMock(side_effect=[None, {"uptime": 42}])

    result, out_contact = await _call_with_path_recovery(
        coord, factory, contact, {"name": "rptr"}, "ab12", "status request"
    )

    assert result == {"uptime": 42}
    assert out_contact is refreshed  # contact refreshed after reset
    coord._reset_node_path.assert_awaited_once()
    assert factory.await_count == 2


@pytest.mark.asyncio
async def test_timeout_while_flooding_does_not_reset():
    """No stored direct path (already flooding) → nothing to reset, no retry."""
    coord = _make_coordinator()
    contact = {"out_path_len": -1}
    factory = AsyncMock(return_value=None)

    result, out_contact = await _call_with_path_recovery(
        coord, factory, contact, {"name": "rptr"}, "ab12", "status request"
    )

    assert result is None
    assert out_contact is contact
    coord._reset_node_path.assert_not_awaited()
    assert factory.await_count == 1


@pytest.mark.asyncio
async def test_reset_disabled_skips_retry():
    """If the path reset is refused (disabled), don't retry the request."""
    coord = _make_coordinator(reset_ok=False)
    contact = {"out_path_len": 5}
    factory = AsyncMock(return_value=None)

    result, out_contact = await _call_with_path_recovery(
        coord, factory, contact, {"name": "rptr"}, "ab12", "status request"
    )

    assert result is None
    assert out_contact is contact
    coord._reset_node_path.assert_awaited_once()
    assert factory.await_count == 1  # no retry


@pytest.mark.asyncio
async def test_missing_contact_returns_without_reset():
    """A None contact short-circuits without attempting a reset."""
    coord = _make_coordinator()
    factory = AsyncMock(return_value=None)

    result, out_contact = await _call_with_path_recovery(
        coord, factory, None, {"name": "rptr"}, "ab12", "status request"
    )

    assert result is None
    assert out_contact is None
    coord._reset_node_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_hanging_command_is_treated_as_no_response_and_recovers():
    """A command that hangs past the timeout must be treated as no-response so a
    wedged link can't freeze the poll task — it then triggers path recovery."""
    refreshed = {"out_path_len": -1}
    coord = _make_coordinator(reset_ok=True, refreshed_contact=refreshed)
    contact = {"out_path_len": 5}

    calls = {"n": 0}

    async def factory(c):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(10)  # hang well past the timeout
            return {"uptime": 1}
        return {"uptime": 99}  # retry after path reset succeeds fast

    result, out_contact = await _call_with_path_recovery(
        coord, factory, contact, {"name": "rptr"}, "ab12", "status request", timeout=0.02
    )

    assert result == {"uptime": 99}
    assert out_contact is refreshed
    coord._reset_node_path.assert_awaited_once()
    coord.logger.warning.assert_called()  # logged the timeout


@pytest.mark.asyncio
async def test_retry_still_times_out_returns_none():
    """If the retry after reset also times out, the falsy result is returned."""
    refreshed = {"out_path_len": -1}
    coord = _make_coordinator(reset_ok=True, refreshed_contact=refreshed)
    contact = {"out_path_len": 5}
    factory = AsyncMock(side_effect=[None, None])

    result, out_contact = await _call_with_path_recovery(
        coord, factory, contact, {"name": "rptr"}, "ab12", "status request"
    )

    assert result is None
    assert out_contact is refreshed
    assert factory.await_count == 2
