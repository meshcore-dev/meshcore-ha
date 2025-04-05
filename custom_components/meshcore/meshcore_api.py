"""API for communicating with MeshCore devices using the meshcore-py library."""
import logging
import asyncio
from sched import Event
from typing import Any, Dict, List, Optional
from asyncio import Lock

from meshcore import MeshCore
from meshcore.events import EventType

from homeassistant.core import HomeAssistant

from .const import (
    CONNECTION_TYPE_USB,
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    NodeType,
    DOMAIN,
)
from .utils import get_node_type_str

_LOGGER = logging.getLogger(__name__)

class MeshCoreAPI:
    """API for interacting with MeshCore devices using the event-driven meshcore-py library."""

    def __init__(
        self,
        hass: HomeAssistant,
        connection_type: str,
        usb_path: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        ble_address: Optional[str] = None,
        tcp_host: Optional[str] = None,
        tcp_port: int = DEFAULT_TCP_PORT,
    ) -> None:
        """Initialize the API."""
        self.hass = hass
        self.connection_type = connection_type
        self.usb_path = usb_path
        self.baudrate = baudrate
        self.ble_address = ble_address
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        
        self._connected = False
        self._connection = None
        self._mesh_core = None
        self._node_info = {}
        self._cached_contacts = {}
        self._cached_messages = []
        
        # Add a lock to prevent concurrent access to the device
        self._device_lock = Lock()
        
    @property
    def mesh_core(self) -> MeshCore:
        """Get the underlying MeshCore instance for direct event subscription."""
        if not self._mesh_core:
            _LOGGER.error("MeshCore instance is not initialized")
            raise RuntimeError("MeshCore instance is not initialized")
        return self._mesh_core
        
    @property
    def connected(self) -> bool:
        """Return whether the device is connected."""
        return self._connected
        
    async def connect(self) -> bool:
        """Connect to the MeshCore device using the appropriate connection type."""
        try:
            # Reset state first
            self._connected = False
            self._mesh_core = None
            
            _LOGGER.info("Connecting to MeshCore device...")
            
            # Create the MeshCore instance using the factory methods based on connection type
            if self.connection_type == CONNECTION_TYPE_USB and self.usb_path:
                _LOGGER.info(f"Using USB connection at {self.usb_path} with baudrate {self.baudrate}")
                self._mesh_core = await MeshCore.create_serial(
                    self.usb_path, 
                    self.baudrate, 
                    debug=False
                )
                
            elif self.connection_type == CONNECTION_TYPE_BLE:
                _LOGGER.info(f"Using BLE connection with address {self.ble_address}")
                self._mesh_core = await MeshCore.create_ble(
                    self.ble_address if self.ble_address else "", 
                    debug=False
                )
                
            elif self.connection_type == CONNECTION_TYPE_TCP and self.tcp_host:
                _LOGGER.info(f"Using TCP connection to {self.tcp_host}:{self.tcp_port}")
                self._mesh_core = await MeshCore.create_tcp(
                    self.tcp_host, 
                    self.tcp_port, 
                    debug=False
                )
                
            else:
                _LOGGER.error("Invalid connection configuration")
                return False
                
            if not self._mesh_core:
                _LOGGER.error("Failed to create MeshCore instance")
                return False
                
            # Load contacts
            _LOGGER.info("Loading contacts...")
            await self._mesh_core.ensure_contacts()
            
            # Fire HA event for successful connection
            if self.hass:
                self.hass.bus.async_fire(f"{DOMAIN}_connected", {
                    "connection_type": self.connection_type
                })
                
            self._connected = True
            _LOGGER.info("Successfully connected to MeshCore device")
            return True
            
        except Exception as ex:
            _LOGGER.error("Error connecting to MeshCore device: %s", ex)
            self._connected = False
            self._mesh_core = None
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from the MeshCore device."""
        _LOGGER.debug("Disconnecting from MeshCore device... (HA)")
        try:
            # Trigger device disconnected event
            if self.hass:
                self.hass.bus.async_fire(f"{DOMAIN}_disconnected", {})
                
            # Properly disconnect using the MeshCore instance
            if self._mesh_core:
                self._mesh_core.cx.transport.close()
                try:
                    _LOGGER.info("Cleaning up event subscriptions")
                    if hasattr(self._mesh_core, "dispatcher") and hasattr(self._mesh_core.dispatcher, "subscriptions"):
                        subscription_count = len(self._mesh_core.dispatcher.subscriptions)
                        for subscription in list(self._mesh_core.dispatcher.subscriptions):
                            subscription.unsubscribe()
                        _LOGGER.info(f"Cleared {subscription_count} event subscriptions")
                    # Close the connection
                    _LOGGER.info("Closing connection to MeshCore device")
                except Exception as ex:
                    _LOGGER.error(f"Error during MeshCore disconnect: {ex}")
            
        except Exception as ex:
            _LOGGER.error(f"Error during disconnect: {ex}")
        finally:
            # Always reset these values
            self._connected = False
            self._mesh_core = None
            _LOGGER.info("Disconnection complete")
        return