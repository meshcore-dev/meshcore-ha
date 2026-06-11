"""Tests for MQTT timestamp formatting."""
import importlib.util
import os
import sys
from unittest.mock import MagicMock


class _EventType:
    PRIVATE_KEY = "private_key"


sys.modules.setdefault("paho", MagicMock())
sys.modules.setdefault("paho.mqtt", MagicMock())
sys.modules.setdefault("paho.mqtt.client", MagicMock())
sys.modules.setdefault("nacl", MagicMock())
sys.modules.setdefault("nacl.bindings", MagicMock())

_mc_events = sys.modules.get("meshcore.events")
if _mc_events is not None:
    _mc_events.EventType = _EventType

_MQTT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "mqtt_uploader.py",
)
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.mqtt_uploader", _MQTT_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)


def _uploader():
    entry = MagicMock()
    entry.data = {}
    return _module.MeshCoreMqttUploader(MagicMock(), MagicMock(), entry)


def _assert_utc_aware(timestamp: str):
    assert timestamp.endswith("+00:00") or timestamp.endswith("Z")


def test_status_payload_uses_utc_aware_timestamp():
    payload = _uploader()._build_status_payload("online")

    _assert_utc_aware(payload["timestamp"])


def test_raw_event_payload_uses_utc_aware_timestamp():
    payload = _uploader()._build_raw_event_payload("RX_LOG_DATA", {})

    _assert_utc_aware(payload["timestamp"])


def test_packet_payload_uses_utc_aware_timestamp():
    packet = _uploader()._normalize_packet_event("RX_LOG_DATA", {"payload": "00"})

    assert packet is not None
    _assert_utc_aware(packet["timestamp"])
