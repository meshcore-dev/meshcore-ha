"""Regression test for 0 mV ("battery measurement unsupported") handling.

In MeshCore firmware (`ESP32Board.h`), boards without a battery ADC pin (e.g.
mains/USB-powered gateways like the Elecrow ThinkNode G3) return 0 from
`getBattMilliVolts()`. Previously, `calculate_battery_percentage(0)`,
`MeshCoreSensor` (`battery_voltage` / `battery_percentage`), and
`MeshCoreRepeaterSensor` (`bat` / `battery_percentage`) treated `0 mV` as a
valid reading, reporting `0.0 V` and `0%` instead of `None` (`unknown`).
"""
import ast
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

for _mod in (
    "Crypto",
    "Crypto.Cipher",
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.slugify",
    "custom_components",
    "custom_components.meshcore",
    "custom_components.meshcore.const",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

sys.modules["homeassistant.util"].slugify = lambda x: x
sys.modules["custom_components.meshcore.const"].BAT_VMIN = 3000
sys.modules["custom_components.meshcore.const"].BAT_VMAX = 4200

_BASE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "custom_components", "meshcore"
)
_UTILS_PATH = os.path.join(_BASE, "utils.py")
_SENSOR_PY = os.path.join(_BASE, "sensor.py")

_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.utils", _UTILS_PATH
)
_utils_mod = importlib.util.module_from_spec(_spec)
_utils_mod.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_utils_mod)
calculate_battery_percentage = _utils_mod.calculate_battery_percentage


def _is_super_await(stmt: ast.stmt) -> bool:
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
    fn.body = [s for s in fn.body if not _is_super_await(s)]
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, _SENSOR_PY, "exec")
    namespace = {
        "Event": object,
        "EventType": types.SimpleNamespace(BATTERY="BATTERY"),
        "calculate_battery_percentage": calculate_battery_percentage,
    }
    exec(code, namespace)  # noqa: S102
    return namespace["async_added_to_hass"]


async_added_to_hass = _extract_async_added_to_hass()


def _run(coro):
    try:
        coro.send(None)
    except StopIteration:
        return
    raise AssertionError("coroutine unexpectedly suspended")


class _FakeSession:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, event_filter, callback, *, attribute_filters=None):
        self.subscriptions.append((event_filter, callback))
        return lambda: None


class _FakeBatterySensor:
    def __init__(self, key: str):
        self._native_value = None
        self.session = _FakeSession()
        self.coordinator = types.SimpleNamespace(api=self.session)
        self.entity_description = types.SimpleNamespace(key=key)

    def async_on_remove(self, unsubscribe):
        pass


def test_calculate_battery_percentage_zero_or_none_returns_none():
    assert calculate_battery_percentage(0) is None
    assert calculate_battery_percentage(-1) is None
    assert calculate_battery_percentage(None) is None


def test_calculate_battery_percentage_positive_mv():
    assert calculate_battery_percentage(3600) == 50.0
    assert calculate_battery_percentage(4200) == 100.0
    assert calculate_battery_percentage(3000) == 0.0


def test_companion_battery_voltage_zero_mv_sets_none():
    s = _FakeBatterySensor("battery_voltage")
    _run(async_added_to_hass(s))
    _, callback = s.session.subscriptions[0]

    callback(types.SimpleNamespace(payload={"level": 0}))
    assert s._native_value is None

    callback(types.SimpleNamespace(payload={"level": 3850}))
    assert s._native_value == 3.85


def test_companion_battery_percentage_zero_mv_sets_none():
    s = _FakeBatterySensor("battery_percentage")
    _run(async_added_to_hass(s))
    _, callback = s.session.subscriptions[0]

    callback(types.SimpleNamespace(payload={"level": 0}))
    assert s._native_value is None

    callback(types.SimpleNamespace(payload={"level": 3600}))
    assert s._native_value == 50.0
