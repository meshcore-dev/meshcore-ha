"""Every ``meshcore_*`` event is built and fired in one module.

Sixteen inline ``hass.bus.async_fire`` literals is how events drifted apart and
how entry identity went missing from all of them. The rule is structural: only
``events.py`` fires, so a new producer cannot skip what listeners rely on.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "meshcore"
EMITTER = "events.py"


def _fire_calls(path: Path) -> list[str]:
    """Return every ``*.bus.async_fire(...)`` call site in one module."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "async_fire"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "bus"
        ):
            found.append(f"{path.name}:{node.lineno}")
    return found


def test_only_the_emitter_fires_events() -> None:
    """No module outside events.py puts anything on the HA bus."""
    offenders = [
        site
        for path in sorted(PACKAGE.glob("*.py"))
        if path.name != EMITTER
        for site in _fire_calls(path)
    ]
    assert offenders == []


def test_the_emitter_still_fires() -> None:
    """The check above stays honest: events.py is where firing happens."""
    assert _fire_calls(PACKAGE / EMITTER)
