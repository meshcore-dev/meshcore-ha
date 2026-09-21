"""Telemetry sensors under real Home Assistant: LPP coverage and attribution.

The sensor table used to be a hand-kept copy of the decoder's, so the types
whose field names it guessed wrong created nothing, the types the decoder does
not name produced list-valued states Home Assistant rejects, and a telemetry
frame the node pushed was filed under the companion instead of its sender.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from meshcore.lpp_json_encoder import my_lpp_types
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.telemetry_sensor import (
    GPS_LPP_TYPE,
    LPP_TYPE_MAPPINGS,
    TelemetrySensorManager,
)

REMOTE_PREFIX = "a1b2c3d4e5f6"
ROOT_PUBKEY = "cc" * 32


def _manager(hass: HomeAssistant) -> TelemetrySensorManager:
    """A telemetry manager wired to a real entity platform."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    platform = MockEntityPlatform(hass, domain="sensor", platform_name=DOMAIN)
    platform.config_entry = config_entry

    coordinator = MagicMock()
    coordinator.config_entry = config_entry
    coordinator.pubkey = ROOT_PUBKEY
    coordinator.name = "Hub"
    coordinator.data = {"contacts": []}
    coordinator.get_device_update_interval.return_value = 60
    return TelemetrySensorManager(coordinator, platform._async_schedule_add_entities)


def _event(lpp: list[dict[str, Any]], payload: dict | None = None, **attributes: Any):
    """Build a TELEMETRY_RESPONSE-shaped event."""
    event = MagicMock()
    event.payload = {**(payload or {}), "lpp": lpp}
    event.attributes = attributes
    return event


async def _emit(
    hass: HomeAssistant, manager: TelemetrySensorManager, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """Deliver one telemetry event and return the sensors it produced."""
    await manager._handle_telemetry_event(_event(*args, **kwargs))
    await hass.async_block_till_done()
    return dict(manager.discovered_sensors)


@pytest.mark.parametrize("code", sorted(my_lpp_types))
def test_every_decoded_type_has_a_sensor_mapping(code: int) -> None:
    """H-16: the table covers each type the decoder emits, GPS excepted."""
    name = my_lpp_types[code][0]
    if code == GPS_LPP_TYPE:
        assert code not in LPP_TYPE_MAPPINGS  # the device_tracker owns it
        return
    for key in (code, name):
        config = LPP_TYPE_MAPPINGS[key]
        assert config["name"]
        assert config["icon"]


@pytest.mark.parametrize("code", sorted(my_lpp_types))
def test_multi_value_fields_match_the_library(code: int) -> None:
    """H-16: named multi-value fields are the library's, not a guess."""
    if code == GPS_LPP_TYPE:
        return
    fields = my_lpp_types[code][1]
    config = LPP_TYPE_MAPPINGS[code]
    if not fields:
        return
    assert [entry["field"] for entry in config["multi_fields"]] == fields


async def test_accelerometer_creates_one_sensor_per_axis(hass: HomeAssistant) -> None:
    """H-16: the library names accelerometer axes acc_x/acc_y/acc_z."""
    manager = _manager(hass)
    sensors = await _emit(
        hass,
        manager,
        [
            {
                "channel": 1,
                "type": "accelerometer",
                "value": {"acc_x": 0.125, "acc_y": -0.25, "acc_z": 9.81},
            }
        ],
        {"pubkey_prefix": REMOTE_PREFIX},
    )

    assert sorted(sensors) == [
        f"{REMOTE_PREFIX}_1_accelerometer_acc_x",
        f"{REMOTE_PREFIX}_1_accelerometer_acc_y",
        f"{REMOTE_PREFIX}_1_accelerometer_acc_z",
    ]
    values = {key[-1]: sensor.native_value for key, sensor in sensors.items()}
    assert values == {"x": 0.125, "y": -0.25, "z": 9.81}


async def test_colour_creates_one_sensor_per_component(hass: HomeAssistant) -> None:
    """H-16: colour components are red/green/blue in the payload."""
    manager = _manager(hass)
    sensors = await _emit(
        hass,
        manager,
        [
            {
                "channel": 2,
                "type": "colour",
                "value": {"red": 10, "green": 20, "blue": 30},
            }
        ],
        {"pubkey_prefix": REMOTE_PREFIX},
    )

    assert sorted(sensors) == [
        f"{REMOTE_PREFIX}_2_colour_blue",
        f"{REMOTE_PREFIX}_2_colour_green",
        f"{REMOTE_PREFIX}_2_colour_red",
    ]
    assert sensors[f"{REMOTE_PREFIX}_2_colour_green"].native_value == 20


async def test_gyrometer_list_becomes_per_axis_scalars(hass: HomeAssistant) -> None:
    """H-16: the decoder leaves gyrometer unnamed, so its axes are positional."""
    manager = _manager(hass)
    sensors = await _emit(
        hass,
        manager,
        [{"channel": 3, "type": "gyrometer", "value": [1.5, -2.5, 0.25]}],
        {"pubkey_prefix": REMOTE_PREFIX},
    )

    assert sorted(sensors) == [
        f"{REMOTE_PREFIX}_3_gyrometer_x",
        f"{REMOTE_PREFIX}_3_gyrometer_y",
        f"{REMOTE_PREFIX}_3_gyrometer_z",
    ]
    for sensor in sensors.values():
        assert isinstance(sensor.native_value, float)


async def test_direction_is_a_scalar_not_a_list(hass: HomeAssistant) -> None:
    """H-16: a one-dimensional unnamed type must not report a list state."""
    manager = _manager(hass)
    sensors = await _emit(
        hass,
        manager,
        [{"channel": 4, "type": "direction", "value": [271.0]}],
        {"pubkey_prefix": REMOTE_PREFIX},
    )

    sensor = sensors[f"{REMOTE_PREFIX}_4_direction"]
    assert sensor.native_value == 271.0
    assert hass.states.get(sensor.entity_id).state == "271.0"


async def test_a_pushed_frame_is_attributed_to_its_sender(hass: HomeAssistant) -> None:
    """H-13: a push frame names its sender in the attributes only."""
    manager = _manager(hass)
    sensors = await _emit(
        hass,
        manager,
        [{"channel": 1, "type": "temperature", "value": 31.0}],
        {"pubkey_pre": REMOTE_PREFIX},
        pubkey_prefix=REMOTE_PREFIX,
        raw="deadbeef",
    )

    assert list(sensors) == [f"{REMOTE_PREFIX}_1_temperature"]
    assert next(iter(sensors.values())).pubkey_prefix == REMOTE_PREFIX


async def test_self_telemetry_still_belongs_to_the_companion(
    hass: HomeAssistant,
) -> None:
    """A frame that names no sender is the companion's own telemetry."""
    manager = _manager(hass)
    sensors = await _emit(
        hass, manager, [{"channel": 1, "type": "temperature", "value": 20.0}]
    )

    assert list(sensors) == [f"{ROOT_PUBKEY[:12]}_1_temperature"]


async def test_gps_never_becomes_a_sensor(hass: HomeAssistant) -> None:
    """The device_tracker platform owns GPS; no sensor is created for it."""
    manager = _manager(hass)
    sensors = await _emit(
        hass,
        manager,
        [
            {
                "channel": 5,
                "type": "gps",
                "value": {"latitude": 1.0, "longitude": 2.0, "altitude": 3.0},
            }
        ],
        {"pubkey_prefix": REMOTE_PREFIX},
    )

    assert sensors == {}
