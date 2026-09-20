"""The SDK stays behind the session: only radio.py may hold a MeshCore object.

Exposing the SDK instance let any caller reach ``commands`` and bypass the
command gate, so the escape hatch is closed structurally rather than by
convention. These checks read the package source, not its behaviour.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "meshcore"

# radio.py owns the instance; radio_commands.py is a mixin of the session.
OWNERS = frozenset({"radio.py", "radio_commands.py"})

# Enums, event and packet types are data the whole integration speaks in; the
# MeshCore instance, its dispatcher and its command surface are not.
SHARED_SDK_NAMES = frozenset(
    {"Event", "EventType", "BinaryReqType", "Subscription", "EventDispatcher"}
)

# ``_contacts`` is left out: the coordinator keeps its own dict under that name.
FORBIDDEN_ATTRIBUTES = frozenset(
    {"mesh_core", "commands", "dispatcher", "_contacts_dirty", "is_connected"}
)


def _modules() -> list[Path]:
    """Return every package module that must not touch the SDK instance."""
    return sorted(p for p in PACKAGE.glob("*.py") if p.name not in OWNERS)


def test_no_module_names_the_sdk_instance() -> None:
    """``mesh_core`` appears nowhere outside the modules that own the link."""
    offenders = [path.name for path in _modules() if "mesh_core" in path.read_text()]
    assert offenders == []


def test_no_module_reaches_through_to_the_sdk() -> None:
    """No module reads the SDK's own command, dispatch or contact internals."""
    offenders: list[str] = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
    assert offenders == []


def test_only_the_owner_imports_sdk_objects() -> None:
    """Other modules import SDK enums and types only, never the SDK itself."""
    offenders: list[str] = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{node.lineno} import {alias.name}"
                    for alias in node.names
                    if alias.name == "meshcore" or alias.name.startswith("meshcore.")
                ]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module != "meshcore" and not module.startswith("meshcore."):
                    continue
                offenders += [
                    f"{path.name}:{node.lineno} from {module} import {alias.name}"
                    for alias in node.names
                    if alias.name not in SHARED_SDK_NAMES
                ]
    assert offenders == []
