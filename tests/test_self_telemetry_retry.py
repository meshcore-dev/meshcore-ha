"""Regression tests for the self-telemetry retry storm and its log level.

``get_self_telemetry()`` sends the four-byte "self" form of
``CMD_SEND_TELEMETRY_REQ``. Firmware answers it; a non-firmware companion need
not. An openHop virtual companion up to and including 1.1.1 requires the
36-byte contact form and rejects the short frame with ``ERR_CODE_ILLEGAL_ARG``
(``openhop_core/companion/frame_server.py``: ``if len(data) < 35``).

Two bugs compounded that on a live instance:

1. ``_last_self_telemetry_update`` advanced only in the success branch, so a
   node that always fails never re-armed the interval gate. The configured
   300 s interval collapsed to the coordinator tick -- a measured ~357 requests
   per 30 minutes (~5 s apart), half of every companion frame the integration
   sent, none of which could succeed.
2. Every failure logged at ERROR, so that storm surfaced as roughly 17k
   ERROR-tier lines a day.

``coordinator.py`` cannot be imported whole under the conftest stubs, so these
tests AST-extract the real module-level helper and run it with its free names
bound, exercising production source. A second test guards the ordering fix
structurally: the attempt must be recorded before the request is issued, not
inside the ``try`` that only records it on success.
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


def _extract_log_self_telemetry_error():
    """Compile the production helper plus the constant it closes over."""
    tree = ast.parse(_read_source())
    wanted = ("_TELEMETRY_UNSUPPORTED_CODES", "_log_self_telemetry_error")
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in wanted)
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id in wanted for t in node.targets
            )
        )
    ]
    assert len(body) == 2, f"expected both {wanted} at module level, got {len(body)}"
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, _COORDINATOR_PY, "exec")
    namespace = {"_LOGGER": _LOGGER, "Any": Any, "frozenset": frozenset}
    exec(code, namespace)  # noqa: S102 -- executing our own production source
    return namespace["_log_self_telemetry_error"]


_log_self_telemetry_error = _extract_log_self_telemetry_error()


def _records(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records]


def test_unsupported_code_warns_once_then_debugs(caplog):
    """A node that cannot answer is named once, then demoted."""
    payload = {"error_code": 6, "code_string": "ERR_CODE_ILLEGAL_ARG"}

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_self_telemetry_error(payload, already_reported=False)
    levels = [level for level, _ in _records(caplog)]
    assert levels == [logging.WARNING]
    assert "ERR_CODE_ILLEGAL_ARG" in caplog.records[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_self_telemetry_error(payload, already_reported=True)
    assert [level for level, _ in _records(caplog)] == [logging.DEBUG]


def test_unsupported_cmd_code_is_also_demoted(caplog):
    """openHop >= 1.1.2 answers the self form and returns UNSUPPORTED_CMD for
    other malformed lengths; treat it the same way."""
    payload = {"error_code": 2, "code_string": "ERR_CODE_UNSUPPORTED_CMD"}
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        _log_self_telemetry_error(payload, already_reported=True)
    assert [level for level, _ in _records(caplog)] == [logging.DEBUG]


def test_other_failures_keep_error(caplog):
    """Transient or unknown failures stay at ERROR on every occurrence."""
    for payload in (
        {"error_code": 4, "code_string": "ERR_CODE_TABLE_FULL"},
        {"reason": "no_event_received"},
        None,
    ):
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
            _log_self_telemetry_error(payload, already_reported=True)
        assert [level for level, _ in _records(caplog)] == [logging.ERROR], payload


def _self_telemetry_gate():
    """Return the ``if`` node holding the self-telemetry attempt."""
    for node in ast.walk(ast.parse(_read_source())):
        if not isinstance(node, ast.Try):
            continue
        calls = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and n.attr == "get_self_telemetry"
        ]
        if calls:
            return node
    raise AssertionError("no try block issuing get_self_telemetry() found")


def _assignments_to(node, name):
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == name for t in n.targets
        )
    ]


def test_attempt_recorded_before_request_not_only_on_success():
    """The interval gate must re-arm on failure, not just on success.

    Structural guard against the original bug returning: no assignment to
    ``_last_self_telemetry_update`` may live inside the ``try`` that issues the
    request, because every path in there other than success would skip it.
    """
    try_node = _self_telemetry_gate()
    inside = _assignments_to(try_node, "_last_self_telemetry_update")
    assert not inside, (
        "_last_self_telemetry_update is assigned inside the get_self_telemetry() "
        "try block; a failing node would never re-arm the interval and would be "
        "retried on every coordinator tick"
    )


def test_error_reported_flag_is_cleared_on_success():
    """A success must reset the flag so a later regression is reported again."""
    try_node = _self_telemetry_gate()
    assigns = _assignments_to(try_node, "_self_telemetry_error_reported")
    values = {
        n.value.value for n in assigns if isinstance(n.value, ast.Constant)
    }
    assert values == {True, False}, (
        "expected _self_telemetry_error_reported to be set True on failure and "
        f"cleared False on success, found {values}"
    )
