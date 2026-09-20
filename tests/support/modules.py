"""Load integration modules for real in the unit tier.

``tests/conftest.py`` MagicMock-stubs the package so helpers can be imported
without Home Assistant. Modules that are pure Python (constants, the token
bucket, the traffic policy) are better exercised as themselves, so this loads
them from source under their real dotted names — which also lets their
relative imports resolve to each other rather than to a mock.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PACKAGE = "custom_components.meshcore"
SOURCE = Path(__file__).resolve().parents[2] / "custom_components" / "meshcore"


def load_module(name: str) -> ModuleType:
    """Import one integration module from source and register it by name."""
    full_name = f"{PACKAGE}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, SOURCE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PACKAGE
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module
