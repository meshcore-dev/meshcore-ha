"""Integration-tier regression for telemetry/GPS dedup-map eviction.

When Home Assistant removes a telemetry sensor or GPS tracker entity, the
managing ``TelemetrySensorManager`` / ``DeviceTrackerManager`` must drop the
entity's key from its in-memory dedup map (``discovered_sensors`` /
``discovered_trackers``). Without the eviction the manager keeps the
deregistered entity object and a later telemetry event updates the dead entity
instead of recreating it -- the desync flagged during PR #247 review and already
patched in the untracked-contact-discard and data-only-demote flows.

These tests drive the *real* manager handlers under *real* Home Assistant: the
entity is created and added through the production ``AddEntitiesCallback``
(``EntityPlatform._async_schedule_add_entities``, eager-start, so ``entity.hass``
is set synchronously the way it is in production), then removed via the entity
registry. That exercises the real ``async_on_remove`` ->
``_call_on_remove_callbacks`` lifecycle. Teeth-check: without the eviction
registration the key persists after removal and these tests fail.
"""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.device_tracker import DeviceTrackerManager
from custom_components.meshcore.telemetry_sensor import TelemetrySensorManager

# A pubkey prefix that matches no configured repeater/client, no root pubkey, and
# no live contact, so _get_node_info resolves the node as "unknown" -- the
# discovered-node case the eviction protects.
PUBKEY_PREFIX = "a1b2c3d4e5f6"


def _coordinator(config_entry):
    """A coordinator stub exposing only the attributes the handlers read.

    get_device_update_interval returns a real number because the entities'
    ``available`` property does arithmetic with it during the initial state
    write.
    """
    coordinator = MagicMock()
    coordinator.config_entry = config_entry
    coordinator.pubkey = ""
    coordinator.name = "Test Root"
    coordinator.data = {"contacts": []}
    coordinator.get_device_update_interval.return_value = 60
    return coordinator


def _platform(hass, config_entry, domain):
    """A real EntityPlatform bound to a real config entry so added entities are
    registered in the entity registry (and so the registry-remove cascade can
    reach them)."""
    platform = MockEntityPlatform(hass, domain=domain, platform_name=DOMAIN)
    platform.config_entry = config_entry
    return platform


async def test_telemetry_sensor_evicted_on_entity_removal(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    platform = _platform(hass, config_entry, "sensor")
    manager = TelemetrySensorManager(
        _coordinator(config_entry), platform._async_schedule_add_entities
    )

    event = MagicMock()
    event.payload = {
        "pubkey_prefix": PUBKEY_PREFIX,
        "lpp": [{"channel": 1, "type": 103, "value": 21.5}],  # 103 = temperature
    }
    await manager._handle_telemetry_event(event)
    await hass.async_block_till_done()

    # Exactly one sensor discovered and added to HA.
    assert len(manager.discovered_sensors) == 1
    sensor_key, sensor = next(iter(manager.discovered_sensors.items()))
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(sensor.entity_id) is not None

    # Remove the entity via the entity registry -> real async_on_remove cascade.
    ent_reg.async_remove(sensor.entity_id)
    await hass.async_block_till_done()

    # The removal cascade actually ran (state machine no longer has the entity),
    assert hass.states.get(sensor.entity_id) is None
    # and eviction popped the key, so a later telemetry event recreates the
    # sensor instead of updating a deregistered entity.
    assert sensor_key not in manager.discovered_sensors


async def test_gps_tracker_evicted_on_entity_removal(hass: HomeAssistant):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    platform = _platform(hass, config_entry, "device_tracker")
    manager = DeviceTrackerManager(
        _coordinator(config_entry), platform._async_schedule_add_entities
    )

    event = MagicMock()
    event.payload = {
        "pubkey_prefix": PUBKEY_PREFIX,
        "lpp": [
            {
                "channel": 5,
                "type": "gps",
                "value": {"latitude": 1.0, "longitude": 2.0},
            }
        ],
    }
    await manager._handle_gps_telemetry_event(event)
    await hass.async_block_till_done()

    assert len(manager.discovered_trackers) == 1
    tracker_key, tracker = next(iter(manager.discovered_trackers.items()))
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(tracker.entity_id) is not None

    ent_reg.async_remove(tracker.entity_id)
    await hass.async_block_till_done()

    assert hass.states.get(tracker.entity_id) is None
    assert tracker_key not in manager.discovered_trackers
