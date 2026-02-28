"""API for communicating with MeshCore devices using the meshcore-py library."""
import logging
import asyncio
import time
from typing import Optional
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
    DOMAIN,
)

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
        
        # Periodic reconnect after SDK gives up
        self._reconnect_task = None
        
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
                    debug=False,
                    auto_reconnect=True,
                    max_reconnect_attempts=100
                )
                
            elif self.connection_type == CONNECTION_TYPE_BLE:
                _LOGGER.info(f"Using BLE connection with address {self.ble_address}")
                self._mesh_core = await MeshCore.create_ble(
                    self.ble_address if self.ble_address else "", 
                    debug=False,
                    auto_reconnect=True,
                    max_reconnect_attempts=100
                )
                
            elif self.connection_type == CONNECTION_TYPE_TCP and self.tcp_host:
                _LOGGER.info(f"Using TCP connection to {self.tcp_host}:{self.tcp_port}")
                self._mesh_core = await MeshCore.create_tcp(
                    self.tcp_host, 
                    self.tcp_port, 
                    debug=False,
                    auto_reconnect=True,
                    max_reconnect_attempts=100
                )
                
            else:
                _LOGGER.error("Invalid connection configuration")
                return False
                
            if not self._mesh_core:
                _LOGGER.error("Failed to create MeshCore instance")
                return False

            await asyncio.sleep(1)  # Small delay to ensure connection stability

            # Validate connection with appstart command
            try:
                _LOGGER.info("Validating connection with appstart command...")
                appstart_result = await self._mesh_core.commands.send_appstart()

                if appstart_result is None:
                    _LOGGER.error("Connection validation failed: appstart returned None")
                    self._connected = False
                    self._mesh_core = None
                    return False

                if appstart_result.type == EventType.ERROR:
                    _LOGGER.error(f"Connection validation failed: appstart returned error: {appstart_result.payload}")
                    self._connected = False
                    self._mesh_core = None
                    return False

                _LOGGER.info("Connection validated successfully: %s", appstart_result)
            except Exception as ex:
                _LOGGER.error(f"Connection validation failed (appstart exception): {ex}")
                self._connected = False
                self._mesh_core = None
                return False

            # Set up disconnect event handler for backup reconnect
            self._setup_disconnect_handler()

            # Sync time on connection
            try:
                _LOGGER.info("Syncing time with MeshCore device...")
                current_timestamp = int(time.time())
                await self._mesh_core.commands.set_time(current_timestamp)
                _LOGGER.info(f"Time sync completed: {current_timestamp}")
            except Exception as ex:
                _LOGGER.error(f"Failed to sync time on connection: {ex}")
            
            # Fire HA event for successful connection
            if self.hass:
                self.hass.bus.async_fire(f"{DOMAIN}_connected", {
                    "connection_type": self.connection_type
                })
                
            self._connected = True
            # Cancel any existing reconnect task since we're now connected
            if self._reconnect_task and not self._reconnect_task.done():
                self._reconnect_task.cancel()
            _LOGGER.info("Successfully connected to MeshCore device with auto-reconnect enabled")
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
            # Cancel reconnect task first if it exists
            if self._reconnect_task and not self._reconnect_task.done():
                _LOGGER.info("Cancelling reconnect task")
                self._reconnect_task.cancel()
                try:
                    await self._reconnect_task
                except asyncio.CancelledError:
                    pass
                self._reconnect_task = None

            # Trigger device disconnected event
            if self.hass:
                self.hass.bus.async_fire(f"{DOMAIN}_disconnected", {})

            # Properly disconnect using the MeshCore instance
            if self._mesh_core:
                try:
                    # Clean up event subscriptions BEFORE disconnecting
                    _LOGGER.info("Cleaning up event subscriptions")
                    if hasattr(self._mesh_core, "dispatcher") and hasattr(self._mesh_core.dispatcher, "subscriptions"):
                        subscription_count = len(self._mesh_core.dispatcher.subscriptions)
                        for subscription in list(self._mesh_core.dispatcher.subscriptions):
                            subscription.unsubscribe()
                        _LOGGER.info(f"Cleared {subscription_count} event subscriptions")
                except Exception as ex:
                    _LOGGER.error(f"Error cleaning up subscriptions: {ex}")

                # Now disconnect from the device
                try:
                    _LOGGER.info("Closing connection to MeshCore device")
                    await self._mesh_core.disconnect()
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
    
    def _setup_disconnect_handler(self) -> None:
        """Set up disconnect event handler."""
        if not self._mesh_core:
            return
            
        try:
            self._mesh_core.dispatcher.subscribe(
                EventType.DISCONNECTED,
                self._handle_disconnect_event
            )
            _LOGGER.info("Disconnect event handler registered")
        except Exception as ex:
            _LOGGER.error(f"Failed to set up disconnect handler: {ex}")
    
    def _handle_disconnect_event(self, event) -> None:
        """Handle disconnect events after SDK gives up trying to reconnect."""
        _LOGGER.warning("Device disconnected and SDK auto-reconnect has given up - starting periodic reconnect")
        self._connected = False
        
        if self.hass:
            self.hass.bus.async_fire(f"{DOMAIN}_disconnected", {
                "unexpected": True
            })
        
        # Start periodic reconnect task
        if not self._reconnect_task or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._periodic_reconnect())
    
    async def _periodic_reconnect(self) -> None:
        """Periodically try to reconnect after SDK gives up."""
        _LOGGER.info("Starting periodic reconnect task - will retry every minute")
        
        while not self._connected:
            try:
                await asyncio.sleep(60)  # Wait 1 minute
                
                if not self._connected:
                    _LOGGER.info("Attempting periodic reconnect...")
                    try:
                        await self._mesh_core.connect() # type: ignore
                        self._connected = True
                        
                        # Sync time after reconnection
                        try:
                            _LOGGER.info("Syncing time after reconnection...")
                            current_timestamp = int(time.time())
                            await self._mesh_core.commands.set_time(current_timestamp) # type: ignore
                            _LOGGER.info(f"Time sync after reconnection completed: {current_timestamp}")
                        except Exception as time_ex:
                            _LOGGER.error(f"Failed to sync time after reconnection: {time_ex}")
                        
                        _LOGGER.info("Periodic reconnect successful!")
                        break
                    except Exception as ex:
                        _LOGGER.debug(f"Periodic reconnect failed: {ex}, will retry in 1 minute")
                        
            except asyncio.CancelledError:
                _LOGGER.info("Periodic reconnect task cancelled")
                break
            except Exception as ex:
                _LOGGER.error(f"Error in periodic reconnect: {ex}")
                await asyncio.sleep(60)  # Wait before retrying
        
        _LOGGER.info("Periodic reconnect task ended")
