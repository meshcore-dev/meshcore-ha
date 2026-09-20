"""What the radio tells the integration is not automatically republished.

The raw-event forwarder feeds both the HA bus and raw-mode MQTT, so a secret
that survives it is a secret published twice. These tests drive the real setup
closure and read what each consumer received.
"""

from collections.abc import AsyncIterator
from typing import Any, Final
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import async_setup_entry
from custom_components.meshcore.const import CONF_EXPOSE_SECRETS, DOMAIN
from custom_components.meshcore.events import EVENT_RAW, REDACTED
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

SECRET: Final = "0badc0de"


class _Uploader:
    """Stand in for the MQTT uploader, keeping what it was asked to publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def async_publish_raw_event(self, event_type: str, payload: Any) -> None:
        """Record one publication instead of talking to a broker."""
        self.published.append((event_type, payload))


@pytest.fixture
async def radio() -> AsyncIterator[FakeRadio]:
    """Run one hardware-free radio for the duration of a test."""
    fake = FakeRadio()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


async def _forwarded(
    hass: HomeAssistant, radio: FakeRadio, *, expose: bool
) -> tuple[list[dict], _Uploader]:
    """Set an entry up, emit the two secret-bearing events, return what got out."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="redaction",
        data={"connection_type": "tcp", "tcp_host": "fixture.invalid", "name": "Hub"},
        options={CONF_EXPOSE_SECRETS: expose},
    )
    captured: list[dict] = []

    @callback
    def receive(event: Any) -> None:
        """Snapshot each raw event as its listeners see it."""
        captured.append(dict(event.data))

    hass.bus.async_listen(EVENT_RAW, receive)
    entry.add_to_hass(hass)
    radio.script[("send_appstart",)] = Event(EventType.SELF_INFO, {"name": "Hub"})
    hass.data["meshcore_static_path_registered"] = True
    session = RadioSession(
        hass=hass, connection_type="tcp", tcp_host="fixture.invalid", entry=entry
    )
    session._mesh_core = radio
    session._connected = True
    with (
        patch("custom_components.meshcore.RadioSession", return_value=session),
        patch.object(session, "connect", return_value=True),
        patch(
            "custom_components.meshcore.MeshCoreMqttUploader",
            side_effect=RuntimeError("disabled"),
        ),
        patch(
            "custom_components.meshcore.MeshCoreMapUploader",
            side_effect=RuntimeError("disabled"),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None),
    ):
        assert await async_setup_entry(hass, entry)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    uploader = _Uploader()
    coordinator.mqtt_uploader = uploader

    await radio.emit(EventType.PRIVATE_KEY, {"private_key": SECRET})
    await radio.emit(EventType.CHANNEL_INFO, {"channel_idx": 1, "channel_secret": SECRET})
    await hass.async_block_till_done()
    await coordinator.async_shutdown()
    await session.disconnect()
    return captured, uploader


async def test_secrets_are_withheld_by_default(
    hass: HomeAssistant, radio: FakeRadio
) -> None:
    """The key export never fires, and the channel secret is replaced."""
    captured, uploader = await _forwarded(hass, radio, expose=False)

    assert [event["event_type"] for event in captured] == ["EventType.CHANNEL_INFO"]
    assert captured[0]["payload"] == {"channel_idx": 1, "channel_secret": REDACTED}
    assert [event_type for event_type, _ in uploader.published] == ["EventType.CHANNEL_INFO"]
    assert uploader.published[0][1]["channel_secret"] == REDACTED
    assert SECRET not in str(captured) + str(uploader.published)


async def test_opting_in_forwards_both(hass: HomeAssistant, radio: FakeRadio) -> None:
    """With expose_secrets set, the entry publishes what the radio said."""
    captured, uploader = await _forwarded(hass, radio, expose=True)

    assert [event["event_type"] for event in captured] == [
        "EventType.PRIVATE_KEY",
        "EventType.CHANNEL_INFO",
    ]
    assert captured[0]["payload"] == {"private_key": SECRET}
    assert captured[1]["payload"] == {"channel_idx": 1, "channel_secret": SECRET}
    assert [payload for _, payload in uploader.published] == [
        {"private_key": SECRET},
        {"channel_idx": 1, "channel_secret": SECRET},
    ]
