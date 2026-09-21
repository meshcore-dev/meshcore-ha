"""Temporary BlueZ pairing agent that answers passkey requests with a static PIN.

MeshCore companion radios configured with a Bluetooth PIN use passkey-entry
pairing. bleak's ``client.pair()`` initiates pairing but relies on a system
BlueZ agent to answer BlueZ's RequestPasskey/RequestPinCode call. The default
agent on Home Assistant OS is no-input/no-output ("just works") and cannot
supply a passkey, so pairing fails with ``org.bluez.Error.AuthenticationFailed``.

This module registers a short-lived ``org.bluez.Agent1`` over the system D-Bus
that returns the configured PIN, makes it the default agent for the duration of
a connection attempt, and unregisters it afterwards. Once the bond is stored,
later reconnects don't need the agent at all.

Linux/BlueZ only. ``dbus_fast`` (a transitive dependency of bleak/habluetooth
on the HA platform) is imported lazily so this module stays importable on
platforms — and in the test environment — where it isn't installed. Every entry
point is best-effort: failures are logged and degrade to the previous behaviour
rather than breaking the connection attempt.
"""
import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# Unique object path for our agent so it can't collide with any other agent.
AGENT_PATH = "/org/bluez/meshcore_ha_agent"

# IO capability advertised to BlueZ. "KeyboardDisplay" lets BlueZ pick the
# appropriate flow; for a radio holding a fixed passkey it results in a
# RequestPasskey/RequestPinCode call, which we answer with the configured PIN.
AGENT_CAPABILITY = "KeyboardDisplay"


async def register_pairing_agent(pin: str) -> Optional["PairingAgentRegistration"]:
    """Register a BlueZ agent that supplies ``pin`` during pairing.

    Returns a registration handle whose ``unregister()`` must be called when the
    connection attempt finishes, or ``None`` when an agent could not be
    registered (non-BlueZ platform, dbus_fast unavailable, no system bus, etc.).
    Never raises.
    """
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        from dbus_fast.service import ServiceInterface, method
    except Exception as ex:  # pragma: no cover - platform dependent
        _LOGGER.debug(
            "dbus_fast unavailable; cannot register BLE pairing agent: %s", ex
        )
        return None

    class _PairingAgent(ServiceInterface):
        """org.bluez.Agent1 implementation that returns a fixed PIN/passkey."""

        def __init__(self, pin: str) -> None:
            super().__init__("org.bluez.Agent1")
            self._pin = pin

        @method()
        def Release(self):  # noqa: N802 - D-Bus method name
            _LOGGER.debug("BLE pairing agent released")

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # type: ignore[name-defined]  # noqa: N802,F821
            _LOGGER.debug("BlueZ requested PIN code for %s", device)
            return str(self._pin)

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # type: ignore[name-defined]  # noqa: N802,F821
            _LOGGER.debug("BlueZ requested passkey for %s", device)
            try:
                return int(self._pin)
            except (TypeError, ValueError):
                _LOGGER.warning("BLE PIN %r is not numeric; passkey pairing will fail", self._pin)
                return 0

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # type: ignore[name-defined]  # noqa: N802,F821
            pass

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # type: ignore[name-defined]  # noqa: N802,F821
            pass

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # type: ignore[name-defined]  # noqa: N802,F821
            # Numeric-comparison / just-works: accept by returning normally.
            _LOGGER.debug("BlueZ requested confirmation for %s (accepting)", device)

        @method()
        def RequestAuthorization(self, device: "o"):  # type: ignore[name-defined]  # noqa: N802,F821
            _LOGGER.debug("BlueZ requested authorization for %s (accepting)", device)

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # type: ignore[name-defined]  # noqa: N802,F821
            pass

        @method()
        def Cancel(self):  # noqa: N802 - D-Bus method name
            _LOGGER.debug("BLE pairing request cancelled by BlueZ")

    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        agent = _PairingAgent(pin)
        bus.export(AGENT_PATH, agent)

        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        manager = obj.get_interface("org.bluez.AgentManager1")

        await manager.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)
        await manager.call_request_default_agent(AGENT_PATH)
        _LOGGER.info("Registered BlueZ pairing agent for BLE PIN authentication")
        return PairingAgentRegistration(bus, manager)
    except Exception as ex:
        _LOGGER.warning("Failed to register BlueZ pairing agent: %s", ex)
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:  # pragma: no cover - defensive
                pass
        return None


class PairingAgentRegistration:
    """Handle returned by :func:`register_pairing_agent` for later cleanup."""

    def __init__(self, bus: Any, manager: Any) -> None:
        self._bus = bus
        self._manager = manager

    async def unregister(self) -> None:
        """Unregister the agent and drop the bus connection. Never raises."""
        try:
            await self._manager.call_unregister_agent(AGENT_PATH)
        except Exception as ex:  # pragma: no cover - defensive
            _LOGGER.debug("Error unregistering BlueZ pairing agent: %s", ex)
        try:
            self._bus.disconnect()
        except Exception:  # pragma: no cover - defensive
            pass
