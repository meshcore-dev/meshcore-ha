"""Tests for how many MQTT broker slots the integration supports."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.support.modules import load_module

sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("nacl", MagicMock())
sys.modules.setdefault("nacl.bindings", MagicMock())

CONST = load_module("const")
CONFIG = load_module("config")
UPLOADER = load_module("mqtt_uploader")


def _brokers(count: int) -> dict[str, dict]:
    return {
        str(idx): {"enabled": True, "server": f"broker{idx}.example", "iata": "EAT"}
        for idx in range(1, count + 1)
    }


def test_six_broker_slots_are_supported():
    assert CONFIG.MAX_MQTT_BROKERS == 6


def test_uploader_loads_every_configured_slot():
    entry = SimpleNamespace(data={CONST.CONF_MQTT_BROKERS: _brokers(6)}, options={})

    uploader = UPLOADER.MeshCoreMqttUploader(MagicMock(), MagicMock(), entry)

    assert [broker.number for broker in uploader.get_brokers()] == [1, 2, 3, 4, 5, 6]
