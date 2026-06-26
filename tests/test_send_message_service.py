"""Regression test for the outgoing-DM stale ``device`` fix in services.py.

A direct message sent via ``meshcore.send_message`` *with* an ``entry_id``
argument used to fire the ``meshcore_message_sent`` event with a stale
``device`` field. The deferred ACK-wait task read the loop variable
``config_entry_id`` at execution time, by which point the send-loop had
advanced past the sending coordinator to a trailing non-coordinator key, so
``device`` no longer identified the sender and the downstream handler dropped
the event. The fix binds the loop-scoped values by value at task-creation
time (default arguments on the nested coroutine).

This test drives the real ``async_send_message_service`` against a multi-key
``hass.data[DOMAIN]`` (a coordinator followed by a trailing bookkeeping key)
and runs the deferred coroutine only after the send-loop has fully advanced,
asserting the fired event carries the *sending* coordinator's entry id.
"""
import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock

# Load services.py directly (same pattern as test_services_parsing.py) so the
# relative imports resolve against the mocked sys.modules entries in conftest.
_SERVICES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "services.py",
)
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.services", _SERVICES_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)


async def _extract_send_message_handler(hass):
    """Run async_setup_services and return the registered send_message handler."""
    await _module.async_setup_services(hass)
    for call in hass.services.async_register.call_args_list:
        # async_register(DOMAIN, SERVICE_NAME, handler, schema=...)
        handler = call.args[2]
        if getattr(handler, "__name__", "") == "async_send_message_service":
            return handler
    raise AssertionError("async_send_message_service was not registered")


def _make_coordinator(contact):
    """A coordinator that sends successfully and reports no expected_ack."""
    coordinator = MagicMock()
    result = MagicMock()
    result.type = "SUCCESS"            # != EventType.ERROR (a distinct mock)
    result.payload = {}                # no expected_ack -> ACK wait is skipped
    coordinator.api.connected = True
    coordinator.api.mesh_core.get_contact_by_key_prefix.return_value = contact
    coordinator.api.mesh_core.commands.send_msg = AsyncMock(return_value=result)
    return coordinator


async def test_outgoing_dm_fires_event_with_sending_entry_id():
    """device == the sending coordinator's entry id, not a trailing key."""
    contact = {
        "public_key": "abcdef1234567890",
        "adv_name": "PeerNode",
        "name": "PeerNode",
    }
    coordinator = _make_coordinator(contact)
    sender_entry_id = "coordinator_entry_aaaa"
    # A trailing, non-coordinator key that sorts AFTER the coordinator in
    # insertion order and lacks an ``api`` attribute (the listener-unsubscribe
    # bookkeeping handle that triggered issue #20). The buggy code left
    # config_entry_id pointing here once the loop finished.
    trailing_key = "zzzz_message_sent_listener"

    def _unsub():  # a callable bookkeeping value with no .api attribute
        return None

    hass = MagicMock()
    # Configure hass.data AFTER setup so any setup-time bookkeeping cannot
    # clobber it; the handler reads hass.data only when it is called.
    handler = await _extract_send_message_handler(hass)
    hass.data = {
        _module.DOMAIN: {
            sender_entry_id: coordinator,
            trailing_key: _unsub,
        }
    }
    # Capture the deferred coroutine instead of letting a real loop run it.
    hass.async_create_background_task = MagicMock()
    hass.bus.async_fire = MagicMock()

    call = MagicMock()
    call.data = {
        _module.ATTR_MESSAGE: "hello there",
        _module.ATTR_PUBKEY_PREFIX: "abcdef123456",
        _module.ATTR_ENTRY_ID: sender_entry_id,
    }

    await handler(call)

    # The fix schedules the ACK-wait via async_create_background_task with a
    # retained (named) reference, not the old untracked asyncio.create_task.
    assert hass.async_create_background_task.called, (
        "deferred ACK-wait task was not scheduled"
    )
    bg_call = hass.async_create_background_task.call_args
    assert bg_call.kwargs.get("name"), "background task was not given a retained name"

    # Run the deferred coroutine AFTER the send-loop has fully advanced past the
    # coordinator to the trailing key -- the timing that exposed the bug.
    coro = bg_call.args[0]
    await coro

    hass.bus.async_fire.assert_called_once()
    event_payload = hass.bus.async_fire.call_args.args[1]
    assert event_payload["device"] == sender_entry_id, (
        f"device should be the sending entry id {sender_entry_id!r}, "
        f"got {event_payload['device']!r}"
    )
    assert event_payload["device"] != trailing_key
    assert event_payload["message_type"] == "direct"
