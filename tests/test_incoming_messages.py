"""Execute production queue paths without requiring a running HA instance."""

import ast
import asyncio
import logging
import runpy
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.support.modules import load_module
from tests.support.session import stub_session

BASE = Path(__file__).resolve().parents[1] / "custom_components" / "meshcore"
load_module("const")
load_module("rate_limiter")
CONFIG = load_module("config")
TRAFFIC = load_module("traffic")


def coordinator_class():
    """Extract complete production methods, bypassing the mocked HA base class."""
    tree = ast.parse((BASE / "coordinator.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    cls.bases = []
    cls.body = [
        n
        for n in cls.body
        if getattr(n, "name", None)
        in (
            "consume_incoming_messages",
            "async_flush_messages",
            "_async_update_data",
            "_sync_contacts",
            "_defer_contact_sync",
        )
    ]
    namespace = runpy.run_path(str(BASE / "const.py"))
    namespace.update(
        asyncio=asyncio,
        time=time,
        Dict=dict,
        Any=object,
        _LOGGER=logging.getLogger(__name__),
        MSG_SAFETY_NET_INTERVAL=60,
        CONTACT_SYNC_BACKOFF_MIN=5,
        CONTACT_SYNC_BACKOFF_MAX=60,
        EventType=SimpleNamespace(
            NO_MORE_MSGS="empty", ERROR="error", TELEMETRY_RESPONSE="telemetry"
        ),
        auto_disable_applies=TRAFFIC.auto_disable_applies,
        get_conf=CONFIG.get_conf,
        NODE_CLIENT=TRAFFIC.NODE_CLIENT,
        NODE_REPEATER=TRAFFIC.NODE_REPEATER,
    )
    exec(
        compile(
            ast.Module(body=[cls], type_ignores=[]),
            str(BASE / "coordinator.py"),
            "exec",
        ),
        namespace,
    )
    return namespace[cls.name]


def make_coordinator(enabled):
    coord = coordinator_class()()
    coord.config_entry = SimpleNamespace(
        data={} if enabled is None else {"consume_incoming_messages": enabled},
        async_create_background_task=lambda _hass, target, name, eager_start=True: (
            asyncio.create_task(target, name=name)
        ),
    )
    coord.hass = MagicMock()
    coord._traffic_policy = TRAFFIC.POLICY_LEGACY
    coord._message_lock = asyncio.Lock()
    commands = SimpleNamespace(
        get_msg=AsyncMock(return_value=SimpleNamespace(type="empty")),
        get_bat=AsyncMock(),
        get_self_telemetry=AsyncMock(
            return_value=SimpleNamespace(type="telemetry", payload={})
        ),
    )
    coord.api = stub_session(
        commands, connected=True, ensure_contacts=AsyncMock(return_value=False)
    )
    coord.data = {"contacts": []}
    coord.logger = logging.getLogger(__name__)
    coord._current_time = lambda: 1000
    coord._next_contact_sync = 0.0
    coord._contact_sync_backoff = 5
    coord.invalidate_contacts = lambda: None
    coord._next_repeater_update_times = {}
    coord._next_telemetry_update_times = {}
    coord._repeater_consecutive_failures = {}
    coord._manual_mode_initialized = coord._device_info_initialized = True
    coord.get_all_contacts = lambda: [{"public_key": "test"}]
    coord._auto_cleanup_stale_contacts = coord._auto_cleanup_stale_neighbors = False
    coord._self_diagnostics_enabled = False
    coord._self_telemetry_enabled = True
    coord._last_self_telemetry_update = 0
    coord._self_telemetry_interval = 300
    coord._initial_drain_done = False
    coord._last_msg_activity = 0
    coord._tracked_repeaters = coord._tracked_clients = []
    coord._active_repeater_tasks = {}
    coord._active_telemetry_tasks = {}
    return coord


@pytest.mark.parametrize("enabled", [None, True, False])
async def test_notification_flush(enabled):
    coord = make_coordinator(enabled)
    await coord.async_flush_messages()
    assert coord.api.commands.get_msg.await_count == (enabled is not False)


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("initial_done", [False, True])
async def test_startup_and_safety_poll_keep_other_updates(enabled, initial_done):
    coord = make_coordinator(enabled)
    coord._initial_drain_done = initial_done
    result = await coord._async_update_data()
    assert coord.api.commands.get_msg.await_count == (enabled is not False)
    coord.api.commands.get_bat.assert_awaited_once()
    coord.api.ensure_contacts.assert_awaited_once_with(follow=True)
    coord.api.commands.get_self_telemetry.assert_awaited_once()
    assert result["contacts"] == [{"public_key": "test"}]


async def test_disable_during_flush_stops_next_fetch():
    coord = make_coordinator(True)

    async def receive_one():
        coord.config_entry.data["consume_incoming_messages"] = False
        return SimpleNamespace(type="message")

    coord.api.commands.get_msg.side_effect = receive_one
    await coord.async_flush_messages()
    coord.api.commands.get_msg.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
async def test_messages_waiting_schedules_only_when_enabled(enabled):
    tree = ast.parse((BASE / "__init__.py").read_text())
    handler = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_messages_waiting"
    )
    coord = SimpleNamespace(
        consume_incoming_messages=enabled, async_flush_messages=MagicMock()
    )
    scheduler = MagicMock()
    namespace = {
        "coordinator": coord,
        "asyncio": scheduler,
        "_LOGGER": logging.getLogger(__name__),
    }
    exec(
        compile(ast.Module(body=[handler], type_ignores=[]), "handler", "exec"),
        namespace,
    )
    await namespace[handler.name](None)
    assert scheduler.create_task.call_count == enabled


async def test_repeater_polling_and_telemetry_continue_when_disabled():
    coord = make_coordinator(False)
    repeater = {"name": "station", "pubkey_prefix": "abcdef", "telemetry_enabled": True}
    coord._tracked_repeaters = [repeater]
    coord._auto_disabled_devices = set()
    coord._last_successful_request = {}
    coord._coordinator_start_time = 1000
    coord._active_repeater_tasks = {}
    coord._active_telemetry_tasks = {}
    coord.api.contacts = {"abcdef": {"public_key": "abcdef"}}
    coord._update_repeater = AsyncMock()
    coord._update_node_telemetry = AsyncMock()
    await coord._async_update_data()
    await asyncio.gather(
        *coord._active_repeater_tasks.values(), *coord._active_telemetry_tasks.values()
    )
    coord._update_repeater.assert_awaited_once_with(repeater)
    coord._update_node_telemetry.assert_awaited_once()
    coord.api.commands.get_msg.assert_not_awaited()
