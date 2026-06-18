"""Regression test for the ``node_status`` sensor state push + initial seed.

After an integration restart the header connection indicator
(``sensor.*_node_status_*``) showed Disconnected while the API was already
connected, until the next dispatcher event arrived. Two causes in the
``node_status`` branch of ``MeshCoreSensor.async_added_to_hass``: the
``update_status`` callback set the native value but never called
``async_write_ha_state()`` (so the change was not pushed -- every sibling
callback in the method does call it), and no initial value was seeded at setup.

``sensor.py`` cannot be imported whole under the conftest stubs (its entity
classes subclass mocked HA bases, and conftest replaces the package with a
``MagicMock``), so this test takes the same AST-extract approach as
``test_create_contact_sensor_real.py``: it parses ``sensor.py``, pulls out the
real ``MeshCoreSensor.async_added_to_hass`` coroutine, drops only the leading
``await super().async_added_to_hass()`` line (the base-class hook is HA
machinery that needs a real class to resolve and does not touch this branch),
and runs the real ``node_status`` branch. The assertions therefore exercise
production source -- dropping either the state push or the seed flips them red.
"""
import ast
import os
import types

_BASE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "meshcore"
)
_SENSOR_PY = os.path.join(_BASE, "sensor.py")


def _is_super_await(stmt: ast.stmt) -> bool:
    """True for an ``await super().<...>()`` expression statement."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Await)
        and any(
            isinstance(n, ast.Name) and n.id == "super"
            for n in ast.walk(stmt.value)
        )
    )


def _extract_async_added_to_hass():
    with open(_SENSOR_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "MeshCoreSensor"
    )
    fn = next(
        n for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_added_to_hass"
    )
    kept = [s for s in fn.body if not _is_super_await(s)]
    assert len(fn.body) - len(kept) == 1, (
        "expected exactly one `await super().async_added_to_hass()` statement "
        "to strip; the method shape changed -- re-check the extraction"
    )
    fn.body = kept
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, _SENSOR_PY, "exec")
    # `Event` is the annotation on the inner callback, evaluated at def-time.
    namespace = {"Event": object}
    exec(code, namespace)  # noqa: S102 -- executing our own production source
    return namespace["async_added_to_hass"]


async_added_to_hass = _extract_async_added_to_hass()


def _run(coro):
    """Drive a coroutine that has no real awaits left to completion."""
    try:
        coro.send(None)
    except StopIteration:
        return
    raise AssertionError("coroutine unexpectedly suspended (an await survived)")


# --- Fakes -------------------------------------------------------------------

class _FakeDispatcher:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, event_filter, callback):
        self.subscriptions.append((event_filter, callback))


class _FakeSensor:
    """Minimal stand-in carrying only what the node_status branch touches."""

    def __init__(self, connected: bool):
        self._native_value = None
        self.write_calls = 0
        self.dispatcher = _FakeDispatcher()
        self.coordinator = types.SimpleNamespace(
            api=types.SimpleNamespace(
                connected=connected,
                mesh_core=types.SimpleNamespace(dispatcher=self.dispatcher),
            )
        )
        self.entity_description = types.SimpleNamespace(key="node_status")

    def async_write_ha_state(self):
        self.write_calls += 1


# --- Tests -------------------------------------------------------------------

def test_initial_value_seeded_online_when_connected():
    """Setup seeds "online" when the API is already connected (post-restart)."""
    s = _FakeSensor(connected=True)
    _run(async_added_to_hass(s))
    assert s._native_value == "online"


def test_initial_value_seeded_offline_when_disconnected():
    """Setup seeds "offline" when the API is not connected."""
    s = _FakeSensor(connected=False)
    _run(async_added_to_hass(s))
    assert s._native_value == "offline"


def test_update_callback_pushes_state():
    """The subscribed callback writes HA state so the change is pushed."""
    s = _FakeSensor(connected=True)
    _run(async_added_to_hass(s))

    assert len(s.dispatcher.subscriptions) == 1
    event_filter, callback = s.dispatcher.subscriptions[0]
    assert event_filter is None  # node_status subscribes to all events

    # Simulate the connection dropping, then an event arriving.
    s.coordinator.api.connected = False
    writes_before = s.write_calls
    callback(object())  # the Event argument is unused by update_status

    assert s._native_value == "offline"
    assert s.write_calls == writes_before + 1, (
        "update_status must call async_write_ha_state() to push the change"
    )
