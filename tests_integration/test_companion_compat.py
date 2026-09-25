"""2.x internals that companion integrations read still resolve in 3.0, with a warning.

meshcore-ha-chat reads ``coordinator._repeater_stats``, ``api.mesh_core`` and
``api._cache_self_info_event`` directly. They are kept as shims until those
integrations move off them; each access logs once and names the caller.
"""

import logging
from datetime import timedelta
from typing import Any, Final

import pytest
from homeassistant.core import HomeAssistant
from meshcore.events import Event, EventType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshcore import utils
from custom_components.meshcore.const import DOMAIN
from custom_components.meshcore.coordinator import MeshCoreDataUpdateCoordinator
from custom_components.meshcore.radio import RadioSession
from tests.support.fake_radio import FakeRadio

COMPANION_FILE: Final = "/config/custom_components/meshcore_chat/ws_api.py"


def _as_companion(expression: str, **names: Any) -> Any:
    """Evaluate an expression as though another custom integration ran it."""
    return eval(compile(expression, COMPANION_FILE, "eval"), {}, names)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture(autouse=True)
def fresh_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utils, "_REPORTED_DEPRECATIONS", set())


async def test_mesh_core_returns_the_live_sdk_and_warns_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    radio = FakeRadio()
    session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
    session._mesh_core = radio

    assert _as_companion("api.mesh_core", api=session) is None
    session._connected = True
    assert _as_companion("api.mesh_core", api=session) is radio

    messages = _warnings(caplog)
    assert len(messages) == 1
    assert "api.mesh_core" in messages[0]
    assert "meshcore_chat/ws_api.py:1" in messages[0]


async def test_old_self_info_cache_name_still_updates(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    session = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
    event = Event(EventType.SELF_INFO, {"name": "Hub"})

    _as_companion("api._cache_self_info_event(event)", api=session, event=event)

    assert session.self_info == {"name": "Hub"}
    assert "api._cache_self_info_event" in _warnings(caplog)[0]


async def test_repeater_stats_reads_as_empty(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"connection_type": "tcp", "tcp_host": "fixture.invalid", "name": "Hub"},
    )
    entry.add_to_hass(hass)
    api = RadioSession(hass, "tcp", tcp_host="fixture.invalid")
    coordinator = MeshCoreDataUpdateCoordinator(
        hass, logging.getLogger(__name__), DOMAIN, timedelta(seconds=5), api, entry
    )

    stats = _as_companion("coordinator._repeater_stats.get('aabbccddeeff', {})",
                          coordinator=coordinator)

    assert stats == {}
    assert "coordinator._repeater_stats" in _warnings(caplog)[0]
