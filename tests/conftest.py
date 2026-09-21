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
    "homeassistant.exceptions",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.service",
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
    "custom_components.meshcore.radio",
    "custom_components.meshcore.utils",
    "custom_components.meshcore.mqtt_uploader",
    "custom_components.meshcore.binary_sensor",
    "custom_components.meshcore.traffic",
]

for _mod in _MOCKS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


# Production code catches this, so it must be a real exception class rather
# than the MagicMock attribute the stub would otherwise hand out.
sys.modules["homeassistant.exceptions"].HomeAssistantError = type(
    "HomeAssistantError", (Exception,), {}
)


def _async_register_admin_service(
    hass, domain, service, handler, schema=None, supports_response=None, **kwargs
):
    """Register admin services in unit tests without requiring Home Assistant."""
    hass.services.async_register(
        domain,
        service,
        handler,
        schema=schema,
        supports_response=supports_response,
        **kwargs,
    )


sys.modules[
    "homeassistant.helpers.service"
].async_register_admin_service = _async_register_admin_service


# events.py is the only module that puts anything on the HA bus, so it is loaded
# for real: a MagicMock stub would swallow the events these tests assert on.
from tests.support.modules import load_module  # noqa: E402

load_module("events")
