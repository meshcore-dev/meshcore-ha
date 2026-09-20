"""Import the complete integration with real Home Assistant and SDK modules."""

import importlib
from pathlib import Path
from typing import Final

import pytest

PACKAGE: Final = Path(__file__).parents[1] / "custom_components" / "meshcore"
MODULES: Final = sorted(
    ".".join(path.relative_to(PACKAGE.parent.parent).with_suffix("").parts).removesuffix(
        ".__init__"
    )
    for path in PACKAGE.rglob("*.py")
)


@pytest.mark.parametrize("module", MODULES)
def test_import_every_module(module: str) -> None:
    """Catch invalid imports that the unit tier's module mocks conceal."""
    importlib.import_module(module)
