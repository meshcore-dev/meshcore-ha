"""Pytest configuration: mock HA and third-party modules so helpers can be imported standalone."""
import sys
from unittest.mock import MagicMock

_MOCKS = [
    # Home Assistant
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.http",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.update_coordinator",
    # Third-party deps not installed in test venv
    "voluptuous",
    "meshcore",
    "meshcore.events",
    # Integration internal modules (relative imports become absolute when loaded via spec)
    "custom_components",
    "custom_components.meshcore",
    "custom_components.meshcore.const",
    "custom_components.meshcore.coordinator",
    "custom_components.meshcore.meshcore_api",
    "custom_components.meshcore.utils",
    "custom_components.meshcore.mqtt_uploader",
]

for _mod in _MOCKS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
