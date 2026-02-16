"""Config flow for MeshCore integration."""
import asyncio
import copy
import logging
from typing import Any, Dict, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from bleak import BleakScanner
from meshcore.events import EventType

from .const import (
    CONF_NAME,
    CONF_PUBKEY,
    DOMAIN,
    CONF_CONNECTION_TYPE,
    CONF_USB_PATH,
    CONF_BLE_ADDRESS,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_BAUDRATE,
    CONNECTION_TYPE_USB,
    CONNECTION_TYPE_BLE,
    CONNECTION_TYPE_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    CONNECTION_TIMEOUT,
    CONF_REPEATER_SUBSCRIPTIONS,
    CONF_REPEATER_NAME,
    CONF_REPEATER_PASSWORD,
    CONF_REPEATER_UPDATE_INTERVAL,
    CONF_REPEATER_TELEMETRY_ENABLED,
    CONF_REPEATER_DISABLE_PATH_RESET,
    DEFAULT_REPEATER_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    CONF_TRACKED_CLIENTS,
    CONF_CLIENT_NAME,
    CONF_CLIENT_UPDATE_INTERVAL,
    CONF_CLIENT_DISABLE_PATH_RESET,
    DEFAULT_CLIENT_UPDATE_INTERVAL,
    CONF_DEVICE_DISABLED,
    CONF_DISABLE_CONTACT_DISCOVERY,
    CONF_SELF_TELEMETRY_ENABLED,
    CONF_SELF_TELEMETRY_INTERVAL,
    DEFAULT_SELF_TELEMETRY_INTERVAL,
    CONF_MQTT_IATA,
    CONF_MQTT_DECODER_CMD,
    CONF_MQTT_PRIVATE_KEY,
    CONF_MQTT_TOKEN_TTL_SECONDS,
    CONF_MQTT_PUBLISH_ALL_EVENTS,
    CONF_MQTT_BROKERS,
    NodeType,
)
from .meshcore_api import MeshCoreAPI

_LOGGER = logging.getLogger(__name__)

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


DEFAULT_MQTT_TOPIC_STATUS = "meshcore/{IATA}/{PUBLIC_KEY}/status"
DEFAULT_MQTT_TOPIC_EVENTS = "meshcore/{IATA}/{PUBLIC_KEY}/packets"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONNECTION_TYPE): vol.In(
            [CONNECTION_TYPE_USB, CONNECTION_TYPE_BLE, CONNECTION_TYPE_TCP]
        ),
    }
)

USB_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USB_PATH): str,
        vol.Optional(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): cv.positive_int,
        vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
        vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600))
    }
)

BLE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BLE_ADDRESS): str,
        vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
        vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600))
    }
)

TCP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TCP_HOST): str,
        vol.Optional(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): cv.port,
        vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
        vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600))
    }
)

async def validate_common(api: MeshCoreAPI) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the USB device."""
    try: 
        # Try to connect with timeout
        connect_success = await asyncio.wait_for(api.connect(), timeout=CONNECTION_TIMEOUT)
        
        # Check if connection was successful
        if not connect_success or not api._mesh_core:
            _LOGGER.error("Failed to connect to device - connect() returned False")
            raise CannotConnect("Device connection failed")
            
        # Get node info to verify communication
        node_info = await api._mesh_core.commands.send_appstart()
        
        # Validate we got meaningful info back
        if node_info.type == EventType.ERROR:
            _LOGGER.error("Failed to get node info - received error: %s", node_info.payload)
            raise CannotConnect("Failed to get node info")
            
        # Disconnect when done
        await api.disconnect()
        
        # Extract and log the device information
        device_name = node_info.payload.get('name', 'Unknown')
        public_key = node_info.payload.get('public_key', '')
        
        # Log the values we're extracting
        _LOGGER.info(f"Validating device - Name: {device_name}, Public Key: {public_key[:10]}")
        
        # If we get here, the connection was successful and we got valid info
        return {"title": f"MeshCore Node {device_name}", "name": device_name, "pubkey": public_key}
    except asyncio.TimeoutError:
        raise CannotConnect("Connection timed out")
    except Exception as ex:
        _LOGGER.error("Validation error: %s", ex)
        raise CannotConnect(f"Failed to connect: {str(ex)}")

async def validate_usb_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the USB device."""
    api = MeshCoreAPI(
        hass=hass,
        connection_type=CONNECTION_TYPE_USB,
        usb_path=data[CONF_USB_PATH],
        baudrate=data[CONF_BAUDRATE],
    )
    return await validate_common(api)


async def validate_ble_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the BLE device."""
    api = MeshCoreAPI(
        hass=hass,
        connection_type=CONNECTION_TYPE_BLE,
        ble_address=data[CONF_BLE_ADDRESS],
    ) 
    return await validate_common(api)


async def validate_tcp_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect to the TCP device."""
    api = MeshCoreAPI(
        hass=hass,
        connection_type=CONNECTION_TYPE_TCP,
        tcp_host=data[CONF_TCP_HOST],
        tcp_port=data[CONF_TCP_PORT],
    )
    return await validate_common(api)


class MeshCoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN): # type: ignore
    """Handle a config flow for MeshCore."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize flow."""
        self.connection_type: Optional[str] = None
        self.discovery_info: Optional[Dict[str, Any]] = None
        
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle the initial step."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            self.connection_type = user_input[CONF_CONNECTION_TYPE]
            
            if self.connection_type == CONNECTION_TYPE_USB:
                return await self.async_step_usb()
            if self.connection_type == CONNECTION_TYPE_BLE:
                return await self.async_step_ble()
            if self.connection_type == CONNECTION_TYPE_TCP:
                return await self.async_step_tcp()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_usb(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle USB configuration."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_usb_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_USB,
                    CONF_USB_PATH: user_input[CONF_USB_PATH],
                    CONF_BAUDRATE: user_input[CONF_BAUDRATE],
                    CONF_SELF_TELEMETRY_ENABLED: user_input.get(CONF_SELF_TELEMETRY_ENABLED, False),
                    CONF_SELF_TELEMETRY_INTERVAL: user_input.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL),
                    CONF_NAME: info.get("name"),
                    CONF_PUBKEY: info.get("pubkey"),
                    CONF_REPEATER_SUBSCRIPTIONS: [],
                    CONF_TRACKED_CLIENTS: [],
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Always allow manual entry for USB path
        # Skip trying to detect ports completely
        return self.async_show_form(
            step_id="usb",
            data_schema=vol.Schema({
                vol.Required(CONF_USB_PATH): str,
                vol.Optional(CONF_BAUDRATE, default=DEFAULT_BAUDRATE): cv.positive_int,
                vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
                vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600)),
            }),
            errors=errors
        )

    async def async_step_ble(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle BLE configuration."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_ble_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_BLE,
                    CONF_BLE_ADDRESS: user_input[CONF_BLE_ADDRESS],
                    CONF_SELF_TELEMETRY_ENABLED: user_input.get(CONF_SELF_TELEMETRY_ENABLED, False),
                    CONF_SELF_TELEMETRY_INTERVAL: user_input.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL),
                    CONF_NAME: info.get("name"),
                    CONF_PUBKEY: info.get("pubkey"),
                    CONF_REPEATER_SUBSCRIPTIONS: [],
                    CONF_TRACKED_CLIENTS: [],
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Scan for BLE devices
        devices = {}
        try:
            scanner = BleakScanner()
            discovered_devices = await scanner.discover(timeout=5.0)
            for device in discovered_devices:
                if device.name and "MeshCore" in device.name:
                    devices[device.address] = f"{device.name} ({device.address})"
        except Exception as ex:
            _LOGGER.warning("Failed to scan for BLE devices: %s", ex)

        # If we have discovered devices, show them in a dropdown
        if devices:
            schema = vol.Schema(
                {
                    vol.Required(CONF_BLE_ADDRESS): vol.In(devices),
                    vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
                    vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600)),
                }
            )
        else:
            # Otherwise, allow manual entry, but with simplified schema
            schema = vol.Schema({
                vol.Required(CONF_BLE_ADDRESS): str,
                vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
                vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600)),
            })

        return self.async_show_form(
            step_id="ble", data_schema=schema, errors=errors
        )

    async def async_step_tcp(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Handle TCP configuration."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_tcp_input(self.hass, user_input)
                return self.async_create_entry(title=info["title"], data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP,
                    CONF_TCP_HOST: user_input[CONF_TCP_HOST],
                    CONF_TCP_PORT: user_input[CONF_TCP_PORT],
                    CONF_SELF_TELEMETRY_ENABLED: user_input.get(CONF_SELF_TELEMETRY_ENABLED, False),
                    CONF_SELF_TELEMETRY_INTERVAL: user_input.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL),
                    CONF_NAME: info.get("name"),
                    CONF_PUBKEY: info.get("pubkey"),
                    CONF_REPEATER_SUBSCRIPTIONS: [],
                    CONF_TRACKED_CLIENTS: [],
                })
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="tcp",
            data_schema=vol.Schema({
                vol.Required(CONF_TCP_HOST): str,
                vol.Optional(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): cv.port,
                vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=False): cv.boolean,
                vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=DEFAULT_SELF_TELEMETRY_INTERVAL): vol.All(cv.positive_int, vol.Range(min=60, max=3600)),
            }),
            errors=errors
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for MeshCore."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.repeater_subscriptions = copy.deepcopy(config_entry.data.get(CONF_REPEATER_SUBSCRIPTIONS, []))
        self.tracked_clients = copy.deepcopy(config_entry.data.get(CONF_TRACKED_CLIENTS, []))
        self.hass = None

    async def async_step_init(self, user_input=None):
        """Handle options flow main menu."""
        if user_input is not None:
            action = user_input.get("action")
            
            if action == "add_repeater":
                return await self.async_step_add_repeater()
            elif action == "add_client":
                return await self.async_step_add_client()
            elif action == "manage_devices":
                return await self.async_step_manage_devices()
            elif action == "global_settings":
                return await self.async_step_global_settings()
            elif action == "mqtt_global":
                return await self.async_step_mqtt_global()
            elif action == "mqtt_brokers":
                return await self.async_step_mqtt_brokers()
            else:
                return self.async_create_entry(title="", data={})

        # Get device counts for display
        repeater_count = len(self.repeater_subscriptions)
        client_count = len(self.tracked_clients)
        
        # Build device status display
        device_status_lines = []
        connection_type = self.config_entry.data.get(CONF_CONNECTION_TYPE, "unknown")
        primary_node_name = self.config_entry.data.get(CONF_NAME, "Unknown")
        device_status_lines.append(f"Primary Node: {primary_node_name} ({connection_type})")
        device_status_lines.append(f"Monitored Devices: {repeater_count + client_count} active")
        
        # Add device list if any exist
        if repeater_count > 0:
            device_status_lines.append(f"Repeaters: {repeater_count} configured")
        if client_count > 0:
            device_status_lines.append(f"Tracked Clients: {client_count} configured")
            
        device_status = "\n".join(device_status_lines)
        
        schema = vol.Schema({
            vol.Required("action"): vol.In({
                "add_repeater": "Add Repeater Station",
                "add_client": "Add Tracked Client", 
                "manage_devices": "Manage Monitored Devices",
                "global_settings": "Global Settings",
                "mqtt_global": "MQTT Global Settings",
                "mqtt_brokers": "MQTT Broker Settings",
            })
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "device_status": device_status
            },
        )
        
        
    def _get_repeater_contacts(self):
        """Get repeater contacts from coordinator's cached data."""
        repeater_contacts = []
        for contact in self._iter_known_contacts():
            contact_type = self._normalize_contact_type(contact)
            contact_name = self._contact_name(contact)
            public_key = contact.get("public_key", "")
            pubkey_prefix = public_key[:12] if public_key else ""

            is_repeater_like = contact_type in {NodeType.REPEATER, NodeType.ROOM_SERVER, NodeType.SENSOR}
            if not is_repeater_like and isinstance(contact_name, str):
                name_lower = contact_name.lower()
                is_repeater_like = any(tag in name_lower for tag in ("repeater", "roomserver", "room server", "sensor"))

            if is_repeater_like and pubkey_prefix and contact_name:
                repeater_contacts.append((pubkey_prefix, contact_name))

        return repeater_contacts
        
    def _show_add_repeater_form(self, repeater_dict, errors=None, user_input=None):
        """Helper to show repeater form with current values preserved."""
        if errors is None:
            errors = {}
            
        # Get values from user_input or use defaults
        default_password = ""
        default_interval = DEFAULT_REPEATER_UPDATE_INTERVAL
        default_telemetry = False
        default_disable_path_reset = False
        
        if user_input:
            default_password = user_input.get(CONF_REPEATER_PASSWORD, "")
            default_interval = user_input.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
            default_telemetry = user_input.get(CONF_REPEATER_TELEMETRY_ENABLED, False)
            default_disable_path_reset = user_input.get(CONF_REPEATER_DISABLE_PATH_RESET, False)
            
        return self.async_show_form(
            step_id="add_repeater",
            data_schema=vol.Schema({
                vol.Required(CONF_REPEATER_NAME): vol.In(repeater_dict.keys()),
                vol.Optional(CONF_REPEATER_PASSWORD, default=default_password): str,
                vol.Optional(CONF_REPEATER_TELEMETRY_ENABLED, default=default_telemetry): bool,
                vol.Optional(CONF_REPEATER_UPDATE_INTERVAL, default=default_interval): vol.All(cv.positive_int, vol.Range(min=MIN_UPDATE_INTERVAL)),
                vol.Optional(CONF_REPEATER_DISABLE_PATH_RESET, default=default_disable_path_reset): bool,
            }),
            errors=errors,
        )
        
    async def async_step_add_repeater(self, user_input=None):
        """Handle adding a new repeater subscription."""
        errors = {}
        
        # Get repeater contacts
        repeater_contacts = self._get_repeater_contacts()
        
        # Show the form with repeater selection
        if not repeater_contacts:
            # No repeaters found
            return self.async_show_form(
                step_id="add_repeater",
                data_schema=vol.Schema({
                    vol.Required("no_repeaters", default="No repeaters found in contacts. Please ensure your device has repeaters in its contacts list."): str,
                }),
                errors=errors,
            )

        # Create a dictionary with name as key and (prefix, name) tuple as value
        # Sort contacts alphabetically by name
        sorted_contacts = sorted(repeater_contacts, key=lambda x: x[1].lower())  # Sort by name (case-insensitive)
        repeater_dict = {}
        for prefix, name in sorted_contacts:
            display_name = f"{name} ({prefix})"
            repeater_dict[display_name] = (prefix, name)
            
        if user_input is None:
            # First time showing form
            return self._show_add_repeater_form(repeater_dict)
            
        selected_repeater = user_input.get(CONF_REPEATER_NAME)
        password = user_input.get(CONF_REPEATER_PASSWORD, "")
        update_interval = user_input.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)
        telemetry_enabled = user_input.get(CONF_REPEATER_TELEMETRY_ENABLED, True)
        disable_path_reset = user_input.get(CONF_REPEATER_DISABLE_PATH_RESET, False)

        # The selected_repeater has format: "Name (prefix)"
        selected_str = selected_repeater
        # Extract the pubkey from between parentheses
        start = selected_str.rfind("(") + 1
        end = selected_str.rfind(")")
        pubkey_prefix = selected_str[start:end]
        # Extract name (everything before the open parenthesis)
        repeater_name = selected_str[:start-1].strip()

        # Check if this repeater is already in the subscriptions by prefix
        existing_prefixes = [r.get("pubkey_prefix") for r in self.repeater_subscriptions]
        if pubkey_prefix in existing_prefixes:
            errors["base"] = "Repeater is already configured"
            return self._show_add_repeater_form(repeater_dict, errors, user_input)

        coordinator = self.hass.data[DOMAIN].get(self.config_entry.entry_id) # type: ignore
        meshcore = coordinator.api.mesh_core # type: ignore

        # validate the repeater can be logged into
        contact = meshcore.get_contact_by_key_prefix(pubkey_prefix)
        if not contact:
            _LOGGER.error(f"Contact not found with public key prefix: {pubkey_prefix}")
            errors["base"] = "Contact not found"
            return self._show_add_repeater_form(repeater_dict, errors, user_input)
            
        # Try to login
        send_result = await meshcore.commands.send_login(contact, password)
        
        if send_result.type == EventType.ERROR:
            error_message = send_result.payload
            _LOGGER.error("Failed to login to repeater - received error: %s", error_message)
            errors["base"] = "Failed to log in to repeater. Check password and try again."
            return self._show_add_repeater_form(repeater_dict, errors, user_input)
        
        result = await meshcore.wait_for_event(EventType.LOGIN_SUCCESS, timeout=10)
        if not result:
            _LOGGER.error("Timed out waiting for login success")
            errors["base"] = "Timed out waiting for login response"
            return self._show_add_repeater_form(repeater_dict, errors, user_input)
        
        if result.type == EventType.ERROR:
            error_message = result.payload if hasattr(result, 'payload') else "Unknown error"
            _LOGGER.error("Failed to login to repeater - received error: %s", error_message)
            errors["base"] = "Failed to log in to repeater. Check password and try again."
            return self._show_add_repeater_form(repeater_dict, errors, user_input)
            
            
        # Login successful, now optionally check for version
        send_result = await meshcore.commands.send_cmd(contact, "ver")
        
        if send_result.type == EventType.ERROR:
            _LOGGER.error("Failed to get repeater version - received error: %s", send_result.payload)
            
        filter = { "pubkey_prefix": contact.get("public_key")[:12] }

        msg = await meshcore.wait_for_event(EventType.CONTACT_MSG_RECV, filter, timeout=15)
        _LOGGER.debug("Received ver message: %s", msg)
        ver = "Unknown"
        if not msg or msg.type == EventType.ERROR:
            _LOGGER.error("Failed to get repeater version")
        elif msg.type == EventType.CONTACT_MSG_RECV:
            ver = msg.payload.get("text")
            _LOGGER.info("Repeater version: %s", ver)
        
        # Add the new repeater subscription with pubkey_prefix
        self.repeater_subscriptions.append({
            "name": repeater_name,
            "pubkey_prefix": pubkey_prefix,
            "firmware_version": ver,
            CONF_REPEATER_PASSWORD: password,
            CONF_REPEATER_TELEMETRY_ENABLED: telemetry_enabled,
            CONF_REPEATER_UPDATE_INTERVAL: update_interval,
            CONF_REPEATER_DISABLE_PATH_RESET: disable_path_reset,
        })

        # Update the config entry data
        new_data = copy.deepcopy(dict(self.config_entry.data))
        new_data[CONF_REPEATER_SUBSCRIPTIONS] = copy.deepcopy(self.repeater_subscriptions)
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore

        # Return to the init step
        return await self.async_step_init() # type: ignore
        
    async def async_step_add_client(self, user_input=None):
        """Handle adding a tracked client."""
        errors = {}
        
        # Get client contacts
        client_contacts = self._get_client_contacts()
        
        if not client_contacts:
            return self.async_show_form(
                step_id="add_client",
                data_schema=vol.Schema({
                    vol.Required("no_clients", default="No client devices found in contacts. Please ensure your device has client devices in its contacts list."): str,
                }),
                errors=errors,
            )

        # Create a dictionary with name as key and (prefix, name) tuple as value
        # Sort contacts alphabetically by name
        sorted_contacts = sorted(client_contacts, key=lambda x: x[1].lower())  # Sort by name (case-insensitive)
        client_dict = {}
        for prefix, name in sorted_contacts:
            display_name = f"{name} ({prefix})"
            client_dict[display_name] = (prefix, name)
            
        if user_input is None:
            return self.async_show_form(
                step_id="add_client",
                data_schema=vol.Schema({
                    vol.Required(CONF_CLIENT_NAME): vol.In(client_dict.keys()),
                    vol.Optional(CONF_CLIENT_UPDATE_INTERVAL, default=DEFAULT_CLIENT_UPDATE_INTERVAL): vol.All(cv.positive_int, vol.Range(min=MIN_UPDATE_INTERVAL)),
                    vol.Optional(CONF_CLIENT_DISABLE_PATH_RESET, default=False): bool,
                }),
                errors=errors,
            )
            
        selected_client = user_input.get(CONF_CLIENT_NAME)
        update_interval = user_input.get(CONF_CLIENT_UPDATE_INTERVAL, DEFAULT_CLIENT_UPDATE_INTERVAL)
        disable_path_reset = user_input.get(CONF_CLIENT_DISABLE_PATH_RESET, False)

        # Extract pubkey prefix and name from selection
        start = selected_client.rfind("(") + 1
        end = selected_client.rfind(")")
        pubkey_prefix = selected_client[start:end]
        client_name = selected_client[:start-1].strip()

        # Check if this client is already tracked
        existing_prefixes = [c.get("pubkey_prefix") for c in self.tracked_clients]
        if pubkey_prefix in existing_prefixes:
            errors["base"] = "Client is already being tracked"
            return self.async_show_form(
                step_id="add_client",
                data_schema=vol.Schema({
                    vol.Required(CONF_CLIENT_NAME): vol.In(client_dict.keys()),
                    vol.Optional(CONF_CLIENT_UPDATE_INTERVAL, default=DEFAULT_CLIENT_UPDATE_INTERVAL): vol.All(cv.positive_int, vol.Range(min=MIN_UPDATE_INTERVAL)),
                    vol.Optional(CONF_CLIENT_DISABLE_PATH_RESET, default=False): bool,
                }),
                errors=errors,
            )

        # Add the new client tracking
        self.tracked_clients.append({
            "name": client_name,
            "pubkey_prefix": pubkey_prefix,
            "update_interval": update_interval,
            "disable_path_reset": disable_path_reset,
        })

        # Update the config entry data
        new_data = copy.deepcopy(dict(self.config_entry.data))
        new_data[CONF_TRACKED_CLIENTS] = copy.deepcopy(self.tracked_clients)
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore

        # Return to the init step
        return await self.async_step_init() # type: ignore
        
    async def async_step_manage_devices(self, user_input=None):
        """Handle device management."""
        if user_input is not None:
            action = user_input.get("device_action")
            device_id = user_input.get("selected_device")
            
            if action == "edit" and device_id:
                # Store device info for edit form
                self._edit_device_id = device_id
                if device_id.startswith("repeater_"):
                    return await self.async_step_edit_repeater()
                elif device_id.startswith("client_"):
                    return await self.async_step_edit_client()
            
            elif action == "remove" and device_id:
                # Remove the selected device
                if device_id.startswith("repeater_"):
                    prefix = device_id[9:]  # Remove "repeater_" prefix
                    self.repeater_subscriptions = [
                        r for r in self.repeater_subscriptions
                        if r.get("pubkey_prefix") != prefix
                    ]
                elif device_id.startswith("client_"):
                    prefix = device_id[7:]  # Remove "client_" prefix
                    self.tracked_clients = [
                        c for c in self.tracked_clients
                        if c.get("pubkey_prefix") != prefix
                    ]
                
                # Update config entry
                new_data = copy.deepcopy(dict(self.config_entry.data))
                new_data[CONF_REPEATER_SUBSCRIPTIONS] = copy.deepcopy(self.repeater_subscriptions)
                new_data[CONF_TRACKED_CLIENTS] = copy.deepcopy(self.tracked_clients)
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore
                
                return await self.async_step_manage_devices()
            
            else:
                return await self.async_step_init()
        
        # Build device list for management
        device_options = {}
        device_list = []
        
        for r in self.repeater_subscriptions:
            name = r.get("name", "")
            prefix = r.get("pubkey_prefix", "")
            telemetry = r.get("telemetry_enabled", False)
            telem_status = "📊 Telemetry ON" if telemetry else "📊 Telemetry OFF"
            display = f"📡 {name} ({telem_status})"
            device_options[f"repeater_{prefix}"] = display
            device_list.append(display)
            
        for c in self.tracked_clients:
            name = c.get("name", "")
            prefix = c.get("pubkey_prefix", "")
            display = f"📱 {name} (Tracking)"
            device_options[f"client_{prefix}"] = display
            device_list.append(display)
        
        if not device_options:
            return self.async_show_form(
                step_id="manage_devices",
                data_schema=vol.Schema({
                    vol.Required("no_devices", default="No devices configured yet."): str,
                }),
            )
        
        device_status = "\n".join([f"• {item}" for item in device_list])
        
        return self.async_show_form(
            step_id="manage_devices",
            data_schema=vol.Schema({
                vol.Required("selected_device"): vol.In(device_options),
                vol.Required("device_action"): vol.In({
                    "edit": "Edit Device Settings",
                    "remove": "Remove Device",
                }),
            }),
            description_placeholders={
                "device_list": device_status
            },
        )
        
    async def async_step_global_settings(self, user_input=None):
        """Handle global settings."""
        if user_input is not None:
            # Update global settings in config entry
            new_data = copy.deepcopy(dict(self.config_entry.data))
            new_data[CONF_DISABLE_CONTACT_DISCOVERY] = user_input[CONF_DISABLE_CONTACT_DISCOVERY]
            new_data[CONF_SELF_TELEMETRY_ENABLED] = user_input[CONF_SELF_TELEMETRY_ENABLED]
            new_data[CONF_SELF_TELEMETRY_INTERVAL] = user_input[CONF_SELF_TELEMETRY_INTERVAL]
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore

            return await self.async_step_init()

        # Get current values
        current_disable_discovery = self.config_entry.data.get(CONF_DISABLE_CONTACT_DISCOVERY, False)
        current_telemetry_enabled = self.config_entry.data.get(CONF_SELF_TELEMETRY_ENABLED, False)
        current_telemetry_interval = self.config_entry.data.get(CONF_SELF_TELEMETRY_INTERVAL, DEFAULT_SELF_TELEMETRY_INTERVAL)

        return self.async_show_form(
            step_id="global_settings",
            data_schema=vol.Schema({
                vol.Optional(CONF_DISABLE_CONTACT_DISCOVERY, default=current_disable_discovery): cv.boolean,
                vol.Optional(CONF_SELF_TELEMETRY_ENABLED, default=current_telemetry_enabled): cv.boolean,
                vol.Optional(CONF_SELF_TELEMETRY_INTERVAL, default=current_telemetry_interval): vol.All(cv.positive_int, vol.Range(min=60, max=3600)),
            }),
        )

    def _get_mqtt_brokers_data(self) -> Dict[str, Dict[str, Any]]:
        """Get MQTT broker settings from config entry data."""
        brokers = self.config_entry.data.get(CONF_MQTT_BROKERS, {})
        if isinstance(brokers, dict):
            return copy.deepcopy(brokers)
        return {}

    async def async_step_mqtt_global(self, user_input=None):
        """Handle MQTT global settings."""
        if user_input is not None:
            new_data = copy.deepcopy(dict(self.config_entry.data))
            new_data[CONF_MQTT_IATA] = user_input.get(CONF_MQTT_IATA, "LOC").upper()
            new_data[CONF_MQTT_PRIVATE_KEY] = user_input.get(CONF_MQTT_PRIVATE_KEY, "")
            new_data[CONF_MQTT_TOKEN_TTL_SECONDS] = user_input.get(CONF_MQTT_TOKEN_TTL_SECONDS, 3600)
            new_data[CONF_MQTT_PUBLISH_ALL_EVENTS] = user_input.get(CONF_MQTT_PUBLISH_ALL_EVENTS, False)
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore
            return await self.async_step_init()

        current_iata = self.config_entry.data.get(CONF_MQTT_IATA, "LOC")
        current_private_key = self.config_entry.data.get(CONF_MQTT_PRIVATE_KEY, "")
        current_ttl = self.config_entry.data.get(CONF_MQTT_TOKEN_TTL_SECONDS, 3600)
        current_publish_all = self.config_entry.data.get(CONF_MQTT_PUBLISH_ALL_EVENTS, False)

        return self.async_show_form(
            step_id="mqtt_global",
            data_schema=vol.Schema({
                vol.Optional(CONF_MQTT_IATA, default=current_iata): str,
                vol.Optional(CONF_MQTT_PRIVATE_KEY, default=current_private_key): str,
                vol.Optional(CONF_MQTT_TOKEN_TTL_SECONDS, default=current_ttl): vol.All(cv.positive_int, vol.Range(min=60, max=86400)),
                vol.Optional(CONF_MQTT_PUBLISH_ALL_EVENTS, default=current_publish_all): cv.boolean,
            }),
        )

    async def async_step_mqtt_brokers(self, user_input=None):
        """Select which MQTT broker to configure."""
        if user_input is not None:
            broker_id = user_input.get("broker_id")
            self._editing_mqtt_broker = int(broker_id)
            return await self.async_step_mqtt_broker()

        options = {
            "1": "Broker 1",
            "2": "Broker 2",
            "3": "Broker 3",
            "4": "Broker 4",
        }
        return self.async_show_form(
            step_id="mqtt_brokers",
            data_schema=vol.Schema({
                vol.Required("broker_id"): vol.In(options),
            }),
        )

    async def async_step_mqtt_broker(self, user_input=None):
        """Edit one MQTT broker settings."""
        broker_num = getattr(self, "_editing_mqtt_broker", 1)
        broker_key = str(broker_num)
        brokers = self._get_mqtt_brokers_data()
        broker = brokers.get(broker_key, {})

        if user_input is not None:
            brokers[broker_key] = {
                "enabled": user_input.get("enabled", False),
                "server": user_input.get("server", ""),
                "port": user_input.get("port", 1883),
                "transport": user_input.get("transport", "tcp"),
                "use_tls": user_input.get("use_tls", False),
                "tls_verify": user_input.get("tls_verify", True),
                "keepalive": user_input.get("keepalive", 60),
                "username": user_input.get("username", ""),
                "password": user_input.get("password", ""),
                "use_auth_token": user_input.get("use_auth_token", False),
                "token_audience": user_input.get("token_audience", ""),
                "topic_status": user_input.get("topic_status", DEFAULT_MQTT_TOPIC_STATUS),
                "topic_events": user_input.get("topic_events", DEFAULT_MQTT_TOPIC_EVENTS),
                "iata": user_input.get("iata", ""),
            }
            new_data = copy.deepcopy(dict(self.config_entry.data))
            new_data[CONF_MQTT_BROKERS] = brokers
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore
            return await self.async_step_mqtt_brokers()

        schema = vol.Schema({
            vol.Optional("enabled", default=broker.get("enabled", False)): cv.boolean,
            vol.Optional("server", default=broker.get("server", "")): str,
            vol.Optional("port", default=broker.get("port", 1883)): cv.port,
            vol.Optional("transport", default=broker.get("transport", "tcp")): vol.In(["tcp", "websockets"]),
            vol.Optional("use_tls", default=broker.get("use_tls", False)): cv.boolean,
            vol.Optional("tls_verify", default=broker.get("tls_verify", True)): cv.boolean,
            vol.Optional("keepalive", default=broker.get("keepalive", 60)): vol.All(cv.positive_int, vol.Range(min=15, max=300)),
            vol.Optional("username", default=broker.get("username", "")): str,
            vol.Optional("password", default=broker.get("password", "")): str,
            vol.Optional("use_auth_token", default=broker.get("use_auth_token", False)): cv.boolean,
            vol.Optional("token_audience", default=broker.get("token_audience", "")): str,
            vol.Optional("topic_status", default=broker.get("topic_status", DEFAULT_MQTT_TOPIC_STATUS)): str,
            vol.Optional("topic_events", default=broker.get("topic_events", DEFAULT_MQTT_TOPIC_EVENTS)): str,
            vol.Optional("iata", default=broker.get("iata", "")): str,
        })

        return self.async_show_form(
            step_id="mqtt_broker",
            data_schema=schema,
            description_placeholders={"broker_number": str(broker_num)},
        )
        
    def _get_client_contacts(self):
        """Get client contacts from coordinator's cached data."""
        client_contacts = []
        for contact in self._iter_known_contacts():
            contact_type = self._normalize_contact_type(contact)
            if contact_type != NodeType.CLIENT:
                continue

            contact_name = self._contact_name(contact)
            public_key = contact.get("public_key", "")
            pubkey_prefix = public_key[:12] if public_key else ""

            if pubkey_prefix and contact_name:
                client_contacts.append((pubkey_prefix, contact_name))

        return client_contacts

    def _iter_known_contacts(self) -> list[Dict[str, Any]]:
        """Return merged contacts from coordinator (added + discovered)."""
        if not self.hass or DOMAIN not in self.hass.data:
            return []
        coordinator = self.hass.data[DOMAIN].get(self.config_entry.entry_id) # type: ignore
        if not coordinator:
            return []
        if hasattr(coordinator, "get_all_contacts"):
            try:
                contacts = coordinator.get_all_contacts()
                if isinstance(contacts, list):
                    return [c for c in contacts if isinstance(c, dict)]
            except Exception:
                pass
        if hasattr(coordinator, "_contacts"):
            return [c for c in coordinator._contacts.values() if isinstance(c, dict)] # type: ignore
        return []

    @staticmethod
    def _contact_name(contact: Dict[str, Any]) -> str:
        """Get best available contact display name."""
        return (
            contact.get("adv_name")
            or contact.get("name")
            or contact.get("display_name")
            or ""
        )

    @staticmethod
    def _normalize_contact_type(contact: Dict[str, Any]):
        """Normalize contact type from various formats to NodeType/int when possible."""
        raw = contact.get("type", contact.get("node_type"))
        if isinstance(raw, NodeType):
            return raw
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            value = raw.strip().lower()
            if value.isdigit():
                return int(value)
            mapping = {
                "client": NodeType.CLIENT,
                "repeater": NodeType.REPEATER,
                "room_server": NodeType.ROOM_SERVER,
                "roomserver": NodeType.ROOM_SERVER,
                "sensor": NodeType.SENSOR,
            }
            return mapping.get(value, raw)
        return raw
        
    async def async_step_edit_repeater(self, user_input=None):
        """Handle editing a repeater."""
        prefix = self._edit_device_id[9:]  # Remove "repeater_" prefix
        
        # Find the repeater to edit
        repeater = None
        for r in self.repeater_subscriptions:
            if r.get("pubkey_prefix") == prefix:
                repeater = r
                break
        
        if not repeater:
            _LOGGER.error("Repeater not found for editing: %s", prefix)
            return await self.async_step_manage_devices()
        
        _LOGGER.debug("User_input for editing repeater: %s", user_input)
        if user_input is not None:
            # Update repeater settings
            repeater[CONF_REPEATER_PASSWORD] = user_input.get(CONF_REPEATER_PASSWORD, "")
            repeater[CONF_REPEATER_TELEMETRY_ENABLED] = user_input[CONF_REPEATER_TELEMETRY_ENABLED]
            repeater[CONF_REPEATER_UPDATE_INTERVAL] = user_input[CONF_REPEATER_UPDATE_INTERVAL]
            repeater[CONF_REPEATER_DISABLE_PATH_RESET] = user_input[CONF_REPEATER_DISABLE_PATH_RESET]
            repeater[CONF_DEVICE_DISABLED] = user_input[CONF_DEVICE_DISABLED]

            # Update config entry - deep copy entire data to ensure HA detects changes
            new_data = copy.deepcopy(dict(self.config_entry.data))
            new_data[CONF_REPEATER_SUBSCRIPTIONS] = copy.deepcopy(self.repeater_subscriptions)
            _LOGGER.debug("Updating repeater subscriptions: %s", new_data[CONF_REPEATER_SUBSCRIPTIONS])
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore

            return await self.async_step_init()

        # Show current settings
        return self.async_show_form(
            step_id="edit_repeater",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_REPEATER_PASSWORD,
                    description={"suggested_value": repeater.get(CONF_REPEATER_PASSWORD, "")}
                ): str,
                vol.Optional(CONF_REPEATER_TELEMETRY_ENABLED, default=repeater.get(CONF_REPEATER_TELEMETRY_ENABLED, False)): bool,
                vol.Optional(CONF_REPEATER_UPDATE_INTERVAL, default=repeater.get(CONF_REPEATER_UPDATE_INTERVAL, DEFAULT_REPEATER_UPDATE_INTERVAL)): vol.All(cv.positive_int, vol.Range(min=MIN_UPDATE_INTERVAL)),
                vol.Optional(CONF_REPEATER_DISABLE_PATH_RESET, default=repeater.get(CONF_REPEATER_DISABLE_PATH_RESET, False)): bool,
                vol.Optional(CONF_DEVICE_DISABLED, default=repeater.get(CONF_DEVICE_DISABLED, False)): bool,
            }),
            description_placeholders={
                "device_name": repeater.get("name", "Unknown")
            },
        )
        
    async def async_step_edit_client(self, user_input=None):
        """Handle editing a tracked client."""
        prefix = self._edit_device_id[7:]  # Remove "client_" prefix
        
        # Find the client to edit
        client = None
        for c in self.tracked_clients:
            if c.get("pubkey_prefix") == prefix:
                client = c
                break
        
        if not client:
            return await self.async_step_manage_devices()
        
        if user_input is not None:
            # Update client settings
            client[CONF_CLIENT_UPDATE_INTERVAL] = user_input[CONF_CLIENT_UPDATE_INTERVAL]
            client[CONF_CLIENT_DISABLE_PATH_RESET] = user_input[CONF_CLIENT_DISABLE_PATH_RESET]
            client[CONF_DEVICE_DISABLED] = user_input[CONF_DEVICE_DISABLED]

            # Update config entry
            new_data = copy.deepcopy(dict(self.config_entry.data))
            new_data[CONF_TRACKED_CLIENTS] = copy.deepcopy(self.tracked_clients)
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data) # type: ignore

            return await self.async_step_init()

        # Show current settings
        return self.async_show_form(
            step_id="edit_client",
            data_schema=vol.Schema({
                vol.Optional(CONF_CLIENT_UPDATE_INTERVAL, default=client.get(CONF_CLIENT_UPDATE_INTERVAL, DEFAULT_CLIENT_UPDATE_INTERVAL)): vol.All(cv.positive_int, vol.Range(min=MIN_UPDATE_INTERVAL)),
                vol.Optional(CONF_CLIENT_DISABLE_PATH_RESET, default=client.get(CONF_CLIENT_DISABLE_PATH_RESET, False)): bool,
                vol.Optional(CONF_DEVICE_DISABLED, default=client.get(CONF_DEVICE_DISABLED, False)): bool,
            }),
            description_placeholders={
                "device_name": client.get("name", "Unknown")
            },
        )
        
