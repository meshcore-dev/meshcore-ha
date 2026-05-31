"""Tests for select.py entity definitions.

select.py defines entity classes that subclass Home Assistant base classes,
and this suite mocks Home Assistant (see tests/conftest.py) so helpers can be
imported standalone. The select entities are therefore asserted at the AST
level rather than instantiated, consistent with the standalone-helper approach
used across this suite.
"""
import ast
import os


def _load_select_ast() -> ast.Module:
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "custom_components", "meshcore", "select.py",
    )
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _class_def(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == name
        ),
        None,
    )


def test_discovered_contact_select_marks_options_unrecorded() -> None:
    """MeshCoreDiscoveredContactSelect must exclude its ``options`` list from the
    recorder via ``_unrecorded_attributes = frozenset({"options"})``.

    The options list grows with the discovered-contact set and can exceed the
    recorder's 16 KiB per-state attribute cap on dense meshes; excluding it
    keeps the state recordable at any contact count. This guards the annotation
    against accidental removal or a typo.
    """
    cls = _class_def(_load_select_ast(), "MeshCoreDiscoveredContactSelect")
    assert cls is not None, "MeshCoreDiscoveredContactSelect class not found"

    assignments = [
        node.value
        for node in cls.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_unrecorded_attributes"
            for t in node.targets
        )
    ]
    assert assignments, "_unrecorded_attributes assignment not found in class body"

    value = assignments[0]
    assert (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and value.args
    ), f"expected frozenset(...), got {ast.dump(value)}"
    assert ast.literal_eval(value.args[0]) == {"options"}, (
        f"unexpected _unrecorded_attributes value: {ast.dump(value)}"
    )
