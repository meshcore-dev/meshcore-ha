"""Config-flow tests exercising the real flow code under Home Assistant.

The unit tier cannot test this (it stubs Home Assistant), so the ~20 flow steps
in config_flow.py had no coverage before this tier. These drive the real flow
handler end to end and assert on the resulting entry.
"""
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.meshcore.const import (
    CONF_CONNECTION_TYPE,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONNECTION_TYPE_TCP,
    DOMAIN,
)


async def test_user_flow_tcp_creates_entry(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """User picks TCP, supplies host/port, and an entry is created.

    recorder_mock is required because the integration depends on logbook, which
    in turn depends on recorder; it must be ordered before hass.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tcp"

    # validate_tcp_input does real device I/O; stub it. async_setup_entry is
    # stubbed so creating the entry doesn't try to connect to a real node.
    with patch(
        "custom_components.meshcore.config_flow.validate_tcp_input",
        return_value={"title": "MeshCore TCP", "name": "MeshCore TCP", "pubkey": "deadbeef0123"},
    ), patch(
        "custom_components.meshcore.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TCP_HOST: "10.0.0.5", CONF_TCP_PORT: 5000},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CONNECTION_TYPE] == CONNECTION_TYPE_TCP
    assert result["data"][CONF_TCP_HOST] == "10.0.0.5"
    assert result["data"][CONF_TCP_PORT] == 5000


async def test_user_flow_cannot_connect(
    recorder_mock, enable_custom_integrations, hass: HomeAssistant
) -> None:
    """A failed TCP validation surfaces a cannot_connect error, not an entry."""
    from custom_components.meshcore.config_flow import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP}
    )

    with patch(
        "custom_components.meshcore.config_flow.validate_tcp_input",
        side_effect=CannotConnect,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_TCP_HOST: "10.0.0.5", CONF_TCP_PORT: 5000},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
