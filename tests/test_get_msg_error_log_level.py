"""Regression test for the benign ``no_event_received`` get_msg log level.

Two ``get_msg()`` loops in ``coordinator.py`` can return an ``EventType.ERROR``
result whose payload is ``{'reason': 'no_event_received'}`` -- a startup race
where the flush or poll fired before the radio link produced its first event.
It has no functional impact (the next cycle recovers), but both loops logged
every ``EventType.ERROR`` result at ERROR, so the benign race surfaced as
ERROR-tier noise: ``async_flush_messages`` -> "Error flushing messages" and the
``_async_update_data`` safety-net poll -> "Error retrieving messages" (the one
actually observed on the live host). Both now route through the shared
``_log_get_msg_error`` helper, which reason-gates that one payload to DEBUG and
keeps ERROR for every other failure.

``coordinator.py`` cannot be imported whole under the conftest stubs, so this
test AST-extracts the real module-level ``_log_get_msg_error`` and runs it with
its free names bound (the module logger, ``Any``). The assertions therefore
exercise production source. A second test guards against drift by asserting
both call sites route through the helper and no raw per-action ERROR log
survives.
"""
import ast
import logging
import os

from typing import Any

_BASE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "meshcore"
)
_COORDINATOR_PY = os.path.join(_BASE, "coordinator.py")

_LOGGER_NAME = "custom_components.meshcore.coordinator"
_LOGGER = logging.getLogger(_LOGGER_NAME)


def _read_source():
    with open(_COORDINATOR_PY, encoding="utf-8") as fh:
        return fh.read()


def _extract_log_get_msg_error():
    tree = ast.parse(_read_source())
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_log_get_msg_error"
    )
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, _COORDINATOR_PY, "exec")
    namespace = {"_LOGGER": _LOGGER, "Any": Any}
    exec(code, namespace)  # noqa: S102 -- executing our own production source
    return namespace["_log_get_msg_error"]


_log_get_msg_error = _extract_log_get_msg_error()


def _records(caplog):
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == _LOGGER_NAME
    ]


# --- benign startup race -> DEBUG -------------------------------------------

def test_no_event_received_flushing_is_debug_not_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_get_msg_error("flushing", {"reason": "no_event_received"})
    recs = _records(caplog)
    assert not [m for lvl, m in recs if lvl >= logging.ERROR], recs
    assert any(
        lvl == logging.DEBUG and "radio link still coming up" in m
        for lvl, m in recs
    ), recs


def test_no_event_received_retrieving_is_debug_not_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_get_msg_error("retrieving", {"reason": "no_event_received"})
    recs = _records(caplog)
    assert not [m for lvl, m in recs if lvl >= logging.ERROR], recs
    assert any(
        lvl == logging.DEBUG and "radio link still coming up" in m
        for lvl, m in recs
    ), recs


# --- real failures keep the original ERROR text -----------------------------

def test_other_reason_flushing_stays_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_get_msg_error("flushing", {"reason": "device_error"})
    errors = [m for lvl, m in _records(caplog) if lvl >= logging.ERROR]
    assert any("Error flushing messages" in m for m in errors), errors


def test_other_reason_retrieving_stays_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_get_msg_error("retrieving", {"reason": "device_error"})
    errors = [m for lvl, m in _records(caplog) if lvl >= logging.ERROR]
    assert any("Error retrieving messages" in m for m in errors), errors


def test_non_dict_payload_stays_error(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_get_msg_error("retrieving", "boom")
    errors = [m for lvl, m in _records(caplog) if lvl >= logging.ERROR]
    assert any("Error retrieving messages" in m for m in errors), errors


# --- drift guard: both call sites route through the helper ------------------

def test_both_call_sites_route_through_helper():
    src = _read_source()
    assert '_log_get_msg_error("flushing", result.payload)' in src, (
        "flush path no longer routes through _log_get_msg_error"
    )
    assert '_log_get_msg_error("retrieving", result.payload)' in src, (
        "poll path no longer routes through _log_get_msg_error"
    )
    # No raw per-action ERROR log should survive outside the helper.
    assert '"Error flushing messages: %s"' not in src, (
        "a raw 'Error flushing messages' log bypasses the helper"
    )
    assert '"Error retrieving messages: %s"' not in src, (
        "a raw 'Error retrieving messages' log bypasses the helper"
    )
