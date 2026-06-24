"""Services for the MeshCore integration."""
import ast
import asyncio
import inspect
import logging
import random
import re
import shlex
import time
import uuid
import voluptuous as vol
from typing import Any, Dict, Optional, cast

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.const import MAJOR_VERSION
from meshcore.events import EventType

# Commands that modify values reported in SELF_INFO.
# After these succeed, send_appstart() refreshes cached self_info
# so sensors and name-change detection pick up the new values.
_SELF_INFO_COMMANDS = frozenset({
    "set_radio",
    "set_tx_power",
    "set_name",
    "set_coords",
    "set_multi_acks",
    "set_advert_loc_policy",
    "set_path_hash_mode",
    "set_telemetry_mode_base",
    "set_telemetry_mode_loc",
    "set_telemetry_mode_env",
    "set_manual_add_contacts",
    "import_private_key",
})

from .const import (
    ATTR_PUBKEY_PREFIX,
    MODE_DATA_ONLY,
    get_contact_discovery_mode,
    DOMAIN,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_CHANNEL_MESSAGE,
    SERVICE_EXECUTE_COMMAND,
    SERVICE_EXECUTE_COMMAND_UI,
    SERVICE_CLI_COMMAND,
    SERVICE_CLI_COMMAND_UI,
    SERVICE_CLI_CLEAR,
    EVENT_CLI_RESPONSE,
    SERVICE_MESSAGE_SCRIPT,
    SERVICE_ADD_SELECTED_CONTACT,
    SERVICE_REMOVE_SELECTED_CONTACT,
    SERVICE_REMOVE_DISCOVERED_CONTACT,
    SERVICE_CLEANUP_UNAVAILABLE_CONTACTS,
    SERVICE_CLEAR_DISCOVERED_CONTACTS,
    SERVICE_GET_CONTACTS,
    SERVICE_GET_DISCOVERED_CONTACT,
    SERVICE_GET_CHANNELS,
    SERVICE_TRACE,
    SELECT_NO_CONTACTS,
    SELECT_NO_DISCOVERED,
    SELECT_NO_ADDED,
    ATTR_NODE_ID,
    ATTR_CHANNEL_IDX,
    ATTR_MESSAGE,
    ATTR_COMMAND,
    ATTR_ENTRY_ID,
    ATTR_SCOPE,
)
from .utils import extract_pubkey_from_selection
from .binary_sensor import create_contact_sensor

_LOGGER = logging.getLogger(__name__)


# Schema for send_message service with either node_id or pubkey_prefix required
SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Exclusive(ATTR_NODE_ID, 'target'): cv.string,
        vol.Exclusive(ATTR_PUBKEY_PREFIX, 'target'): cv.string,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
).extend({vol.Required: vol.Any(ATTR_NODE_ID, ATTR_PUBKEY_PREFIX)})

# Schema for send_channel_message service
SEND_CHANNEL_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CHANNEL_IDX): cv.positive_int,
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_SCOPE): vol.Any(None, cv.string),
    }
)

# Schema for execute_command service
EXECUTE_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_COMMAND): cv.string,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

# Schema for UI message service (no parameters needed)
UI_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

def _parse_functional_command(command_str: str) -> tuple | None:
    """Parse 'cmd(arg1, kw=val)' format using ast. Returns (name, pos_args, kwargs) or None."""
    m = re.match(r"^(\w+)\s*\((.*)\)\s*$", command_str.strip(), re.DOTALL)
    if not m:
        return None
    cmd_name = m.group(1)
    body = m.group(2).strip()
    if not body:
        return cmd_name, [], {}
    try:
        node = ast.parse(f"_({body})", mode="eval")
        call_node = cast(ast.Call, node.body)
        pos_args = [ast.literal_eval(a) for a in call_node.args]
        kw_args = {kw.arg: ast.literal_eval(kw.value) for kw in call_node.keywords}
        return cmd_name, pos_args, kw_args
    except Exception:
        return None


def _ensure_contact_compat(contact: dict) -> dict:
    """Ensure contact dict has all fields required by the current meshcore SDK.

    Older stored contacts may be missing 'out_path_hash_mode' which was added
    in meshcore SDK ~2.2.20+. Default to -1 (flood) if out_path_len is -1,
    otherwise 0 (mode 0, 1-byte path hash).
    """
    if "out_path_hash_mode" not in contact:
        if contact.get("out_path_len", -1) == -1:
            contact["out_path_hash_mode"] = -1
        else:
            contact["out_path_hash_mode"] = 0
    return contact


def _resolve_contact(arg: str, command_name: str, api: Any, coordinator: Any) -> Any:
    """Look up a contact by pubkey prefix or name. Returns contact dict or None."""
    if len(arg) < 6:
        _LOGGER.error("Invalid pubkey prefix length: %s", arg)
        return None
    if not api or not api.mesh_core:
        _LOGGER.error("Device not connected - cannot resolve contact")
        return None
    contact = api.mesh_core.get_contact_by_key_prefix(arg)
    if not contact:
        contact = api.mesh_core.get_contact_by_name(arg)
    if not contact and command_name == "add_contact":
        for dc in coordinator._discovered_contacts.values():
            if dc.get("public_key", "").startswith(arg) or dc.get("adv_name") == arg:
                return _ensure_contact_compat(dc)
    if not contact:
        _LOGGER.error("Contact not found with key or name: %s", arg)
    if contact:
        return _ensure_contact_compat(contact)
    return contact


def _node_has_tracked_subscription(coordinator, pubkey_prefix: str) -> bool:
    """True when the node has a repeater/client tracking subscription.

    Subscription-backed telemetry/GPS entities recreate dynamically, so
    demote-cleanup must not sweep them; their lifecycle follows the
    subscription. Bidirectional startswith matches the convention used
    for subscription lookups elsewhere (varying prefix lengths).
    """
    for cfg in list(coordinator._tracked_repeaters or []) + list(coordinator._tracked_clients or []):
        cp = cfg.get("pubkey_prefix", "")
        if cp and (pubkey_prefix.startswith(cp) or cp.startswith(pubkey_prefix)):
            return True
    return False


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for MeshCore integration."""
    
    async def async_send_message_service(call: ServiceCall) -> None:
        """Handle sending a message service call."""
        message = call.data[ATTR_MESSAGE]
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Check which target identifier was provided
        if ATTR_NODE_ID in call.data:
            # Sending by node_id (friendly name)
            node_id = call.data[ATTR_NODE_ID]
            pubkey_prefix = None
            target_identifier = f"node_id '{node_id}'"
        else:
            # Sending by public key
            node_id = None
            pubkey_prefix = call.data[ATTR_PUBKEY_PREFIX]
            target_identifier = f"public key '{pubkey_prefix}'"
        
        # Iterate through all registered config entries
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            # Skip non-coordinator entries (like event listener flags)
            if not hasattr(coordinator, 'api'):
                continue
                
            _LOGGER.debug("Entry ID: %s, coordinator: %s", config_entry_id, coordinator)
            # If entry_id is specified, only use the matching entry
            if entry_id and entry_id != config_entry_id:
                continue
                
            # Get the API from coordinator
            api = coordinator.api
            if api and api.connected:
                try:
                    _LOGGER.debug(
                        "Sending message to %s: %s", target_identifier, message
                    )
                    
                    # Get the contact object
                    contact = None
                    contact_name = None
                    if node_id is not None:
                        # Find contact by name
                        contact = api.mesh_core.get_contact_by_name(node_id)
                        if not contact:
                            _LOGGER.error(f"Contact with name '{node_id}' not found")
                            continue
                    else:
                        # Find contact by pubkey prefix
                        contact = api.mesh_core.get_contact_by_key_prefix(pubkey_prefix)
                        if not contact:
                            _LOGGER.error(f"Contact with pubkey prefix '{pubkey_prefix}' not found")
                            continue
                    
                    # Send the message using the new API
                    result = await api.mesh_core.commands.send_msg(contact, message)

                    if result.type == EventType.ERROR:
                        _LOGGER.warning(
                            "Failed to send message to %s: %s", target_identifier, result.payload
                        )
                    else:
                        # Use the actual contact name for logging when available
                        display_name = contact_name if contact_name else target_identifier
                        pubkey = contact.get("public_key", "Unknown")
                        _LOGGER.info("Successfully sent message to %s, pubkey: %s", display_name, pubkey)

                        # Fire-and-forget: wait for ACK in background so the
                        # service call returns immediately after sending.
                        send_id = uuid.uuid4().hex[:8]

                        async def _wait_for_ack_and_notify():
                            """Background: wait for ACK then fire delivery event."""
                            ack_received = False
                            try:
                                expected_ack = result.payload.get("expected_ack")
                                suggested_timeout = result.payload.get("suggested_timeout", 10000)
                                if expected_ack:
                                    ack_code = expected_ack.hex() if isinstance(expected_ack, bytes) else str(expected_ack)
                                    ack_timeout = (suggested_timeout / 1000) * 1.2
                                    _LOGGER.debug(
                                        "Waiting for ACK (code=%s, timeout=%.1fs) for message to %s",
                                        ack_code[:8], ack_timeout, display_name
                                    )
                                    ack_event = await api.mesh_core.dispatcher.wait_for_event(
                                        EventType.ACK,
                                        attribute_filters={"code": ack_code},
                                        timeout=ack_timeout
                                    )
                                    ack_received = ack_event is not None
                                    if ack_received:
                                        _LOGGER.info("ACK received for message to %s", display_name)
                                    else:
                                        _LOGGER.info("ACK timeout for message to %s", display_name)
                            except Exception as ack_ex:
                                _LOGGER.debug("Error waiting for ACK: %s", ack_ex)

                            outgoing_msg = {
                                "message": message,
                                "device": config_entry_id,
                                "message_type": "direct",
                                "receiver": contact.get("adv_name") or contact.get("name"),
                                "timestamp": int(time.time()),
                                "contact_public_key": pubkey,
                                "ack_received": ack_received,
                                "send_id": send_id,
                            }
                            hass.bus.async_fire(f"{DOMAIN}_message_sent", outgoing_msg)

                        asyncio.create_task(_wait_for_ack_and_notify())
                except Exception as ex:
                    _LOGGER.error(
                        "Error sending message to %s: %s", target_identifier, ex
                    )
                # Only attempt with the first available API if no entry_id specified
                if not entry_id:
                    return
    
    async def async_send_channel_message_service(call: ServiceCall) -> None:
        """Handle sending a channel message service call."""
        channel_idx = call.data[ATTR_CHANNEL_IDX]
        message = call.data[ATTR_MESSAGE]
        entry_id = call.data.get(ATTR_ENTRY_ID)
        scope = call.data.get(ATTR_SCOPE)

        # Iterate through all registered config entries
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            # Skip non-coordinator entries (like event listener flags)
            if not hasattr(coordinator, 'api'):
                continue

            _LOGGER.debug("Entry ID: %s, coordinator: %s", config_entry_id, coordinator.name)
            # If entry_id is specified, only use the matching entry
            if entry_id and entry_id != config_entry_id:
                continue

            # Get the API from coordinator
            api = coordinator.api
            if api and api.connected:
                try:
                    _LOGGER.debug(
                        "Sending message to channel %s: %s", channel_idx, message
                    )

                    # Set flood scope before sending if requested, then always reset.
                    if scope is not None:
                        _LOGGER.debug("Setting flood scope to: %s", scope)
                        scope_result = await api.mesh_core.commands.set_flood_scope(scope)
                        if scope_result.type == EventType.ERROR:
                            _LOGGER.warning(
                                "Failed to set flood scope %s: %s", scope, scope_result.payload
                            )

                    # Capture a fallback timestamp before sending.
                    # The actual device timestamp may differ from the HA server clock.
                    fallback_timestamp = int(time.time())

                    try:
                        result = await api.mesh_core.commands.send_chan_msg(channel_idx, message, timestamp=fallback_timestamp)
                    finally:
                        if scope is not None:
                            _LOGGER.debug("Resetting flood scope after send")
                            await api.mesh_core.commands.set_flood_scope(None)

                    if result.type == EventType.ERROR:
                        _LOGGER.warning(
                            "Failed to send message to channel %s: %s", channel_idx, result.payload
                        )
                    else:
                        _LOGGER.info(
                            "Successfully sent message to channel %s", channel_idx
                        )

                        # Use the actual timestamp from the device response if available,
                        # otherwise fall back to the server-side timestamp we passed in.
                        # This avoids clock drift between HA and the device breaking correlation.
                        send_timestamp = fallback_timestamp
                        if hasattr(result, 'payload') and isinstance(result.payload, dict):
                            device_ts = result.payload.get("timestamp")
                            if device_ts and isinstance(device_ts, (int, float)):
                                send_timestamp = int(device_ts)
                                if send_timestamp != fallback_timestamp:
                                    _LOGGER.debug(
                                        "Using device timestamp %s instead of server timestamp %s",
                                        send_timestamp, fallback_timestamp
                                    )

                        # Create outgoing message event data
                        outgoing_msg = {
                            "message": message,
                            "device": config_entry_id,
                            "message_type": "channel",
                            "receiver": f"channel_{channel_idx}",
                            "timestamp": int(time.time()),
                            "channel_idx": channel_idx,
                            "send_timestamp": send_timestamp,
                            "send_id": uuid.uuid4().hex[:8],
                            "scope": scope,
                        }
                        # Fire event for outgoing message to update message-related entities
                        hass.bus.async_fire(f"{DOMAIN}_message_sent", outgoing_msg)
                except Exception as ex:
                    _LOGGER.error(
                        "Error sending message to channel %s: %s", channel_idx, ex
                    )
                # Only attempt with the first available API if no entry_id specified
                if not entry_id:
                    return

    # Create combined message script service
    async def async_message_script_service(call: ServiceCall) -> None:
        """Handle the combined messaging script service that works with UI helpers."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Get state from helper entities
        recipient_type = hass.states.get("select.meshcore_recipient_type")
        
        if not recipient_type:
            _LOGGER.error("Recipient type helper not found: select.meshcore_recipient_type")
            return
            
        # Get recipient type value
        recipient_type_value = recipient_type.state
        
        # Get message from text entity
        message_entity = hass.states.get("text.meshcore_message")
        if not message_entity:
            _LOGGER.error("Message input helper not found: text.meshcore_message")
            return
            
        message = message_entity.state
        
        if not message:
            _LOGGER.warning("No message to send - message input is empty")
            return
            
        # Handle based on recipient type
        if recipient_type_value == "Channel":
            # Get channel selection
            channel_entity = hass.states.get("select.meshcore_channel")
            if not channel_entity:
                _LOGGER.error("Channel helper not found: select.meshcore_channel")
                return

            # Get the channel_idx from attributes
            channel_idx = channel_entity.attributes.get("channel_idx")
            if channel_idx is None:
                _LOGGER.error("Channel index not found in channel attributes")
                return

            # Create channel message service call
            channel_call = create_service_call(
                DOMAIN, 
                SERVICE_SEND_CHANNEL_MESSAGE, 
                {"channel_idx": channel_idx, "message": message, "entry_id": entry_id}
            )
            
            # Send the channel message
            await async_send_channel_message_service(channel_call)
            
        elif recipient_type_value == "Contact":
            # Get contact selection
            contact_entity = hass.states.get("select.meshcore_contact")
            if not contact_entity:
                _LOGGER.error("Contact helper not found: select.meshcore_contact")
                return
                
            # Get the public key from attributes
            pubkey_prefix = contact_entity.attributes.get("public_key_prefix")
            if not pubkey_prefix:
                _LOGGER.error("Public key not found in contact attributes")
                return
                
            # Create contact message service call
            contact_call = create_service_call(
                DOMAIN, 
                SERVICE_SEND_MESSAGE, 
                {"pubkey_prefix": pubkey_prefix, "message": message, "entry_id": entry_id}
            )
            
            # Send the direct message
            await async_send_message_service(contact_call)
        else:
            _LOGGER.error(f"Unknown recipient type: {recipient_type_value}")
            
        # Clear the message input after sending
        try:
            await hass.services.async_call(
                "text", 
                "set_value", 
                {"entity_id": "text.meshcore_message", "value": ""},
                blocking=False
            )
        except Exception as ex:
            _LOGGER.warning(f"Could not clear message input: {ex}")
    
    async def async_execute_command_service(call: ServiceCall) -> None:
        """Handle execute command service call."""
        command_str = call.data[ATTR_COMMAND]
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Support both functional: cmd(arg1, kw=val) and positional: cmd arg1 arg2
        functional = _parse_functional_command(command_str)
        if functional:
            command_name, pos_literals, kw_literals = functional
            arguments = None
        else:
            try:
                parts = shlex.split(command_str)
            except Exception as ex:
                _LOGGER.error("Error parsing command: %s", ex)
                return
            if not parts:
                _LOGGER.error("No command specified")
                return
            command_name = parts[0]
            arguments = parts[1:]
            pos_literals = None
            kw_literals = {}
        
        _LOGGER.debug("Executing command: %s with arguments: %s", command_name, arguments)
        
        # Iterate through all registered config entries
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            # Skip non-coordinator entries (like event listener flags)
            if not hasattr(coordinator, 'api'):
                continue
                
            # If entry_id is specified, only use the matching entry
            if entry_id and entry_id != config_entry_id:
                continue
                
            # Get the API from coordinator
            api = coordinator.api
            if api and api.connected:
                try:
                    # Get the command method from the commands object
                    command_method = getattr(api.mesh_core.commands, command_name, None)
                    
                    if not command_method:
                        _LOGGER.error("Command not found: %s", command_name)
                        continue
                    
                    # Define known command parameter types
                    # Format: {command_name: [param1_type, param2_type, ...]}
                    command_param_types = {
                        # Device commands with no parameters
                        "send_appstart": [],
                        "send_device_query": [],
                        "reboot": [],
                        "get_bat": [],
                        "get_time": [],
                        "get_self_telemetry": [],
                        "get_custom_vars": [],
                        "export_private_key": [],
                        "sign_start": [],
                        "sign_finish": [],
                        "get_stats_core": [],
                        "get_stats_radio": [],
                        "get_stats_packets": [],
                        "get_allowed_repeat_freq": [],
                        "get_path_hash_mode": [],

                        # Contact commands
                        "get_contacts": ["int"],  # lastmod parameter (optional, defaults to 0)
                        "reset_path": ["contact"],
                        "share_contact": ["contact"],
                        "export_contact": ["contact"],
                        "remove_contact": ["contact"],
                        "import_contact": ["bytes"],
                        "update_contact": ["contact", "str", "str"],  # contact, path, flags
                        "add_contact": ["contact"],
                        "change_contact_path": ["contact", "int"],
                        "change_contact_flags": ["contact", "int"],
                        "set_autoadd_config": ["int"],
                        "get_autoadd_config": [],

                        # Messaging commands
                        "get_msg": ["float"],  # timeout (optional)
                        "send_login": ["contact", "str"],
                        "send_logout": ["contact"],
                        "send_statusreq": ["contact"],
                        "send_telemetry_req": ["contact"],
                        "send_msg": ["contact", "str", "int"],  # contact, message, timestamp (optional)
                        "send_msg_with_retry": ["contact", "str"],  # contact, message (many optional params)
                        "send_chan_msg": ["int", "str", "int"],  # channel, message, timestamp
                        "send_cmd": ["contact", "str", "int"],  # contact, command, timestamp (optional)
                        "send_binary_req": ["contact", "int"],  # contact, BinaryReqType (int enum)
                        "send_path_discovery": ["contact"],
                        "send_trace": ["int", "int", "int", "bytes"],  # auth_code, tag, flags, path
                        "set_flood_scope": ["str"],

                        # Binary commands
                        "req_telemetry": ["contact", "int"],  # contact, timeout
                        "req_telemetry_sync": ["contact", "int"],
                        "req_mma": ["contact", "int", "int"],  # contact, timeout, min_timeout
                        "req_mma_sync": ["contact", "int", "int", "int"],  # contact, start, end, timeout
                        "req_acl": ["contact", "int"],  # contact, timeout
                        "req_acl_sync": ["contact", "int"],
                        "req_status": ["contact"],
                        "req_status_sync": ["contact"],
                        "req_neighbours_async": ["contact"],
                        "req_neighbours_sync": ["contact"],
                        "fetch_all_neighbours": ["contact"],
                        "req_regions_async": ["contact"],
                        "req_regions_sync": ["contact"],
                        "req_owner_async": ["contact"],
                        "req_owner_sync": ["contact"],
                        "req_basic_async": ["contact"],
                        "req_basic_sync": ["contact"],

                        # Control data commands
                        "send_control_data": ["int", "bytes"],  # control_type, payload
                        "send_node_discover_req": ["int", "bool"],  # filter, prefix_only (tag/since optional)

                        # Device configuration commands
                        "send_advert": ["bool"],
                        "set_name": ["str"],
                        "set_time": ["int"],
                        "set_tx_power": ["int"],
                        "set_devicepin": ["int"],
                        "set_multi_acks": ["int"],
                        "set_coords": ["float", "float"],
                        "set_radio": ["float", "float", "int", "int"],
                        "set_tuning": ["int", "int"],
                        "set_telemetry_mode_base": ["int"],
                        "set_telemetry_mode_loc": ["int"],
                        "set_telemetry_mode_env": ["int"],
                        "set_manual_add_contacts": ["bool"],
                        "set_advert_loc_policy": ["int"],
                        "set_other_params": ["bool", "int", "int", "int", "int"],  # 5 parameters
                        "set_custom_var": ["str", "str"],  # key, value
                        "set_path_hash_mode": ["int"],
                        "import_private_key": ["bytes"],
                        "sign_data": ["bytes"],
                        "sign": ["bytes", "int"],  # data, chunk_size (timeout optional)
                        "get_channel": ["int"],
                        "set_channel": ["int", "str", "bytes"],
                    }
                    
                    param_types = command_param_types.get(command_name, [])
                    prepared_args = []
                    prepared_kwargs = {}

                    if pos_literals is not None:
                        # Functional format: values are already-typed Python literals
                        for i, val in enumerate(pos_literals):
                            ptype = param_types[i] if i < len(param_types) else None
                            if ptype == "contact":
                                contact = _resolve_contact(str(val), command_name, api, coordinator)
                                if contact is None:
                                    return
                                prepared_args.append(contact)
                            else:
                                prepared_args.append(val)
                        if kw_literals:
                            sig_params = list(inspect.signature(command_method).parameters.keys())
                            for kw_name, kw_val in kw_literals.items():
                                if kw_name not in sig_params:
                                    _LOGGER.error("Unknown keyword '%s' for command '%s'", kw_name, command_name)
                                    return
                                idx = sig_params.index(kw_name)
                                ptype = param_types[idx] if idx < len(param_types) else None
                                if ptype == "contact":
                                    kw_val = _resolve_contact(str(kw_val), command_name, api, coordinator)
                                    if kw_val is None:
                                        return
                                prepared_kwargs[kw_name] = kw_val
                    else:
                        # Space-separated format: convert string arguments by declared type
                        for i, arg in enumerate(arguments or []):
                            param_type = param_types[i] if i < len(param_types) else "str"
                            if param_type == "contact":
                                contact = _resolve_contact(arg, command_name, api, coordinator)
                                if contact is None:
                                    return
                                prepared_args.append(contact)
                            elif param_type == "int":
                                try:
                                    prepared_args.append(int(arg))
                                except ValueError:
                                    _LOGGER.error("Could not convert '%s' to integer", arg)
                                    return
                            elif param_type == "float":
                                try:
                                    prepared_args.append(float(arg))
                                except ValueError:
                                    _LOGGER.error("Could not convert '%s' to float", arg)
                                    return
                            elif param_type == "bool":
                                if arg.lower() in ("true", "yes", "y", "1"):
                                    prepared_args.append(True)
                                elif arg.lower() in ("false", "no", "n", "0"):
                                    prepared_args.append(False)
                                else:
                                    _LOGGER.error("Could not convert '%s' to boolean", arg)
                                    return
                            elif param_type == "bytes":
                                try:
                                    prepared_args.append(bytes.fromhex(arg))
                                except ValueError:
                                    _LOGGER.error("Could not convert '%s' to bytes - invalid hex string", arg)
                                    return
                            else:
                                prepared_args.append(arg)

                    _LOGGER.debug("Executing %s args=%s kwargs=%s", command_name, prepared_args, prepared_kwargs)
                    result = await command_method(*prepared_args, **prepared_kwargs)

                    # Refresh SELF_INFO after commands that modify config values
                    # so HA sensors immediately reflect the new state.
                    if command_name in _SELF_INFO_COMMANDS and result.type != EventType.ERROR:
                        try:
                            appstart_result = await api.mesh_core.commands.send_appstart()
                            api._cache_self_info_event(appstart_result)
                        except Exception as ex:
                            _LOGGER.warning(
                                "Failed to refresh SELF_INFO after %s: %s",
                                command_name, ex,
                            )

                    # Update coordinator channel info after set_channel
                    if command_name == "set_channel" and result.type != EventType.ERROR:
                        channel_idx = prepared_args[0]
                        # Fetch updated channel info
                        channel_info_result = await api.mesh_core.commands.get_channel(channel_idx)
                        if channel_info_result.type != EventType.ERROR:
                            coordinator._channel_info[channel_idx] = channel_info_result.payload
                            _LOGGER.info(f"Updated channel {channel_idx} info: {channel_info_result.payload}")
                            # Trigger coordinator update to refresh select entities
                            coordinator.async_set_updated_data(coordinator.data)

                    # Mark contacts as dirty after add_contact or remove_contact so next ensure_contacts() will sync
                    if command_name == "add_contact" and result.type != EventType.ERROR:
                        api.mesh_core._contacts_dirty = True
                        # Also add to coordinator and trigger immediate update
                        contact_to_add = prepared_args[0]
                        if contact_to_add and isinstance(contact_to_add, dict):
                            pubkey = contact_to_add.get("public_key")
                            if pubkey:
                                # Mark as added to node
                                contact_to_add["added_to_node"] = True

                                # Add to coordinator if not already present
                                prefix = pubkey[:12]
                                if prefix not in coordinator._contacts:
                                    coordinator._contacts[prefix] = contact_to_add

                                # Mark contact as dirty so binary sensors update
                                coordinator.mark_contact_dirty(prefix)

                                # Create binary sensor entity if one doesn't exist yet
                                try:
                                    add_entities_cb = getattr(coordinator, "binary_sensor_async_add_entities", None)
                                    if add_entities_cb:
                                        sensor = create_contact_sensor(coordinator, contact_to_add)
                                        if sensor:
                                            add_entities_cb([sensor])
                                except Exception as sensor_ex:
                                    _LOGGER.warning("Failed to create binary sensor for contact %s: %s", prefix, sensor_ex)

                                # Trigger immediate update
                                updated_data = dict(coordinator.data) if coordinator.data else {}
                                updated_data["contacts"] = coordinator.get_all_contacts()
                                coordinator.async_set_updated_data(updated_data)
                    elif command_name == "remove_contact" and result.type != EventType.ERROR:
                        api.mesh_core._contacts_dirty = True
                        # Also remove from SDK's internal contacts dict and coordinator
                        contact_to_remove = prepared_args[0]
                        if contact_to_remove and isinstance(contact_to_remove, dict):
                            pubkey = contact_to_remove.get("public_key")
                            if pubkey:
                                # Remove from SDK
                                if pubkey in api.mesh_core._contacts:
                                    del api.mesh_core._contacts[pubkey]

                                # Remove from coordinator and trigger immediate update
                                prefix = pubkey[:12]
                                if prefix in coordinator._contacts:
                                    del coordinator._contacts[prefix]

                                # Mark contact as dirty so binary sensors update
                                coordinator.mark_contact_dirty(prefix)

                                # Data-only mode: a demoted contact (added ->
                                # discovered) becomes data-only, so its per-contact
                                # binary_sensor must be removed. In data-only mode
                                # nothing else deletes it -- the discovered-cleanup
                                # paths only find an entity when one exists, and a
                                # demoted contact's entity must go immediately rather
                                # than wait for eviction/stale-cleanup. In full mode
                                # the contact stays a valid discovered entity, so this
                                # block is gated off and the entity is left untouched.
                                # NOTE: this gate is the INVERSE of the discovered-
                                # cleanup paths; data-only mode is exactly when the
                                # entity must go. Discard the FULL public_key (the key
                                # create_contact_sensor added), not the 12-hex prefix.
                                if (
                                    get_contact_discovery_mode(coordinator.config_entry)
                                    == MODE_DATA_ONLY
                                ):
                                    coordinator.tracked_diagnostic_binary_contacts.discard(pubkey)
                                    entity_registry = er.async_get(hass)
                                    unique_id = f"{coordinator.config_entry.entry_id}_contact_{prefix}"
                                    entity_id = entity_registry.async_get_entity_id(
                                        "binary_sensor", DOMAIN, unique_id
                                    )
                                    if entity_id:
                                        _LOGGER.info(
                                            "Data-only mode: removing entity for demoted contact %s",
                                            entity_id,
                                        )
                                        entity_registry.async_remove(entity_id)

                                    # Telemetry sensors and the GPS tracker are created
                                    # dynamically while a contact is added and have no
                                    # other demote teardown. Sweep them too -- except
                                    # for nodes with a tracked-device subscription,
                                    # whose entities are subscription-backed and would
                                    # recreate on the next response.
                                    # Allowlist by unique_id SHAPE (prefix + suffix):
                                    # a bare "contains pubkey" match would wrongly hit
                                    # repeater-neighbor sensors (they embed OTHER
                                    # nodes' pubkeys) and tracked-client entities.
                                    if not _node_has_tracked_subscription(coordinator, prefix):
                                        uid_prefix = (
                                            f"{coordinator.config_entry.entry_id}_{prefix}_"
                                        )
                                        to_remove = [
                                            e.entity_id
                                            for e in er.async_entries_for_config_entry(
                                                entity_registry,
                                                coordinator.config_entry.entry_id,
                                            )
                                            if (e.unique_id or "").startswith(uid_prefix)
                                            and (
                                                e.unique_id.endswith("_telemetry")
                                                or e.unique_id.endswith("_gps_tracker")
                                            )
                                        ]
                                        for stale_entity_id in to_remove:
                                            _LOGGER.info(
                                                "Data-only mode: removing telemetry/GPS entity for demoted contact %s",
                                                stale_entity_id,
                                            )
                                            entity_registry.async_remove(stale_entity_id)

                                        # In-memory dedup maps: without these discards
                                        # the managers keep updating deregistered
                                        # entities and a same-session re-add will not
                                        # recreate the sensors (same desync class as
                                        # the tracked-set discard above).
                                        tm = getattr(coordinator, "telemetry_manager", None)
                                        if tm is not None:
                                            for key in [
                                                k for k in tm.discovered_sensors if k.startswith(prefix)
                                            ]:
                                                del tm.discovered_sensors[key]
                                        dtm = getattr(coordinator, "device_tracker_manager", None)
                                        if dtm is not None:
                                            for key in [
                                                k for k in dtm.discovered_trackers if k.startswith(prefix)
                                            ]:
                                                del dtm.discovered_trackers[key]

                                updated_data = dict(coordinator.data) if coordinator.data else {}
                                updated_data["contacts"] = coordinator.get_all_contacts()
                                coordinator.async_set_updated_data(updated_data)

                    # Normalize the SDK return value into a JSON-safe response.
                    # Possible shapes:
                    #   * Event with .payload dict — send_* / set_* commands
                    #   * Plain dict — req_*_sync (awaited response payload)
                    #   * list / scalar / str — wrapped as {"result": <value>}
                    #   * None — req_*_sync on timeout / no response
                    if hasattr(result, "payload") and isinstance(result.payload, dict):
                        response = {
                            k: (v.hex() if isinstance(v, bytes) else v)
                            for k, v in result.payload.items()
                        }
                        _LOGGER.info(
                            "Command result: %s with payload: %s",
                            result.type, response,
                        )
                        if response:
                            return response
                        return
                    if isinstance(result, dict):
                        response = {
                            k: (v.hex() if isinstance(v, bytes) else v)
                            for k, v in result.items()
                        }
                        _LOGGER.info("Command result: %s", response)
                        return response
                    if result is None:
                        _LOGGER.info(
                            "Command %s returned no response", command_name,
                        )
                        return {"error": "no_response", "command": command_name}
                    # Any other non-None shape (list / scalar / string) returned by
                    # req_*_sync helpers — e.g. req_telemetry_sync (lpp list),
                    # req_mma_sync / req_acl_sync, req_regions_sync (str). These are
                    # primitives-only by construction, so wrap as-is for the caller.
                    _LOGGER.info("Command result: %s", result)
                    return {"result": result}

                except Exception as ex:
                    _LOGGER.error("Error executing command %s: %s", command_name, ex)

                # Only attempt with the first available API if no entry_id specified
                if not entry_id:
                    return

        _LOGGER.error("Failed to execute command on any device: %s", command_name)
    
    async def async_execute_command_ui_service(call: ServiceCall) -> None:
        """Execute command from the text helper entity."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        
        # Get command from command text entity
        command_entity = hass.states.get("text.meshcore_command")
        if not command_entity:
            _LOGGER.error("Command input helper not found: text.meshcore_command")
            return
            
        command = command_entity.state
        
        if not command:
            _LOGGER.warning("No command to execute - command input is empty")
            return
        
        # Create command service call
        command_call = create_service_call(
            DOMAIN, 
            SERVICE_EXECUTE_COMMAND, 
            {"command": command, "entry_id": entry_id}
        )
        
        # Execute the command
        await async_execute_command_service(command_call)
        
        # Clear the command input after execution
        try:
            await hass.services.async_call(
                "text",
                "set_value",
                {"entity_id": "text.meshcore_command", "value": ""},
                blocking=False
            )
        except Exception as ex:
            _LOGGER.warning(f"Could not clear command input: {ex}")

    def _resolve_console_coordinator(entry_id: "str | None") -> Any:
        """Pick the coordinator a CLI console command should record against.

        Mirrors execute_command's target selection: the entry_id coordinator
        when specified, otherwise the first connected one. Returns None when no
        suitable coordinator is found.
        """
        first_connected = None
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            if not hasattr(coordinator, "api"):
                continue
            if entry_id and entry_id != config_entry_id:
                continue
            if entry_id:
                return coordinator
            api = coordinator.api
            if first_connected is None and api and api.connected:
                first_connected = coordinator
        return first_connected

    async def async_cli_command_service(call: ServiceCall):
        """Run a CLI command and record its output to the console transcript.

        Thin wrapper over execute_command: it reuses the exact command parsing
        and execution path, then records the command/response pair to the
        console sensor (when CONF_CLI_CONSOLE_ENABLED) and fires the
        EVENT_CLI_RESPONSE event so the result is visible in the UI and
        available to automations. Returns the same response as execute_command.
        """
        command_str = call.data[ATTR_COMMAND]
        entry_id = call.data.get(ATTR_ENTRY_ID)

        response = await async_execute_command_service(call)

        # execute_command returns None on total failure (no connected device /
        # unknown command) and an {"error": ...} dict for explicit no-response.
        is_error = response is None or (
            isinstance(response, dict) and "error" in response
        )

        coordinator = _resolve_console_coordinator(entry_id)
        if coordinator is not None:
            coordinator.record_cli_console(command_str, response, is_error)

        hass.bus.async_fire(EVENT_CLI_RESPONSE, {
            "command": command_str,
            "response": response,
            "is_error": is_error,
            "entry_id": entry_id,
            "timestamp": int(time.time()),
        })

        return response

    async def async_cli_command_ui_service(call: ServiceCall):
        """Run the command from text.meshcore_command via the CLI console.

        Like execute_command_ui, but routes through cli_command so the response
        is captured in the console transcript instead of being discarded. The
        command input is cleared after execution.
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)

        command_entity = hass.states.get("text.meshcore_command")
        if not command_entity:
            _LOGGER.error("Command input helper not found: text.meshcore_command")
            return
        command = command_entity.state
        if not command:
            _LOGGER.warning("No command to execute - command input is empty")
            return

        command_call = create_service_call(
            DOMAIN,
            SERVICE_CLI_COMMAND,
            {"command": command, "entry_id": entry_id},
        )
        response = await async_cli_command_service(command_call)

        try:
            await hass.services.async_call(
                "text",
                "set_value",
                {"entity_id": "text.meshcore_command", "value": ""},
                blocking=False,
            )
        except Exception as ex:
            _LOGGER.warning(f"Could not clear command input: {ex}")

        return response

    async def async_cli_clear_service(call: ServiceCall) -> None:
        """Clear the CLI console transcript.

        Clears the resolved coordinator when an entry_id is given, otherwise
        clears every configured coordinator's console.
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)
        for config_entry_id, coordinator in hass.data[DOMAIN].items():
            if not hasattr(coordinator, "clear_cli_console"):
                continue
            if entry_id and entry_id != config_entry_id:
                continue
            coordinator.clear_cli_console()

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        async_send_message_service,
        schema=SEND_MESSAGE_SCHEMA,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_CHANNEL_MESSAGE,
        async_send_channel_message_service,
        schema=SEND_CHANNEL_MESSAGE_SCHEMA,
    )
    
    # Register the execute command services
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE_COMMAND,
        async_execute_command_service,
        schema=EXECUTE_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE_COMMAND_UI,
        async_execute_command_ui_service,
        schema=UI_MESSAGE_SCHEMA,
    )
    
    # Register the CLI console services. cli_command mirrors execute_command
    # but records the command/response pair into the CLI console transcript
    # sensor so the output is visible in the UI; cli_command_ui drives it from
    # the text.meshcore_command input helper.
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLI_COMMAND,
        async_cli_command_service,
        schema=EXECUTE_COMMAND_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLI_COMMAND_UI,
        async_cli_command_ui_service,
        schema=UI_MESSAGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLI_CLEAR,
        async_cli_clear_service,
        schema=UI_MESSAGE_SCHEMA,
    )

    # Register the combined UI message service
    hass.services.async_register(
        DOMAIN,
        SERVICE_MESSAGE_SCRIPT,
        async_message_script_service,
        schema=UI_MESSAGE_SCHEMA,
    )

    async def async_add_selected_contact_service(call: ServiceCall) -> None:
        """Add the contact selected in the discovered contact select entity."""
        entry_id = call.data.get(ATTR_ENTRY_ID)

        # Find the discovered contact select entity for this entry_id
        # If entry_id not specified, look for first available
        select_entity_id = None
        if entry_id:
            # Look for entity with matching unique_id
            registry = er.async_get(hass)
            for entity in registry.entities.values():
                if entity.unique_id == f"{entry_id}_discovered_contact_select":
                    select_entity_id = entity.entity_id
                    break
        else:
            # Find first discovered contact select entity
            for state in hass.states.async_all():
                if state.entity_id.startswith("select.") and "discovered_contact" in state.entity_id:
                    select_entity_id = state.entity_id
                    break

        if not select_entity_id:
            _LOGGER.error("Discovered contact select entity not found")
            return

        select_entity = hass.states.get(select_entity_id)
        if not select_entity:
            _LOGGER.error(f"Could not get state for {select_entity_id}")
            return

        selected_option = select_entity.state
        if not selected_option or selected_option in [SELECT_NO_CONTACTS, SELECT_NO_DISCOVERED]:
            _LOGGER.error("No contact selected")
            return

        pubkey_prefix = extract_pubkey_from_selection(selected_option)
        if not pubkey_prefix:
            _LOGGER.error(f"Could not parse pubkey from selection: {selected_option}")
            return

        # Call execute_command with add_contact
        command_call = create_service_call(
            DOMAIN,
            SERVICE_EXECUTE_COMMAND,
            {"command": f"add_contact {pubkey_prefix}", "entry_id": entry_id},
            hass
        )
        await async_execute_command_service(command_call)

    async def async_remove_selected_contact_service(call: ServiceCall) -> None:
        """Remove the contact selected in the added contact select entity."""
        entry_id = call.data.get(ATTR_ENTRY_ID)

        # Find the added contact select entity for this entry_id
        # If entry_id not specified, look for first available
        select_entity_id = None
        if entry_id:
            # Look for entity with matching unique_id
            registry = er.async_get(hass)
            for entity in registry.entities.values():
                if entity.unique_id == f"{entry_id}_added_contact_select":
                    select_entity_id = entity.entity_id
                    break
        else:
            # Find first added contact select entity
            for state in hass.states.async_all():
                if state.entity_id.startswith("select.") and "added_contact" in state.entity_id:
                    select_entity_id = state.entity_id
                    break

        if not select_entity_id:
            _LOGGER.error("Added contact select entity not found")
            return

        select_entity = hass.states.get(select_entity_id)
        if not select_entity:
            _LOGGER.error(f"Could not get state for {select_entity_id}")
            return

        selected_option = select_entity.state
        if not selected_option or selected_option in [SELECT_NO_CONTACTS, SELECT_NO_ADDED]:
            _LOGGER.error("No contact selected")
            return

        pubkey_prefix = extract_pubkey_from_selection(selected_option)
        if not pubkey_prefix:
            _LOGGER.error(f"Could not parse pubkey from selection: {selected_option}")
            return

        # Call execute_command with remove_contact
        command_call = create_service_call(
            DOMAIN,
            SERVICE_EXECUTE_COMMAND,
            {"command": f"remove_contact {pubkey_prefix}", "entry_id": entry_id},
            hass
        )
        await async_execute_command_service(command_call)

    async def async_remove_discovered_contact_service(call: ServiceCall) -> None:
        """Remove a discovered contact from the discovered contacts list."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        pubkey_prefix = call.data.get(ATTR_PUBKEY_PREFIX)

        # If pubkey_prefix not provided, get from discovered contact select entity
        if not pubkey_prefix:
            select_entity_id = None
            if entry_id:
                registry = er.async_get(hass)
                for entity in registry.entities.values():
                    if entity.unique_id == f"{entry_id}_discovered_contact_select":
                        select_entity_id = entity.entity_id
                        break
            else:
                for state in hass.states.async_all():
                    if state.entity_id.startswith("select.") and "discovered_contact" in state.entity_id:
                        select_entity_id = state.entity_id
                        break

            if not select_entity_id:
                _LOGGER.error("Discovered contact select entity not found and no pubkey_prefix provided")
                return

            select_entity = hass.states.get(select_entity_id)
            if not select_entity:
                _LOGGER.error(f"Could not get state for {select_entity_id}")
                return

            selected_option = select_entity.state
            if not selected_option or selected_option in [SELECT_NO_CONTACTS, SELECT_NO_DISCOVERED]:
                _LOGGER.error("No contact selected")
                return

            pubkey_prefix = extract_pubkey_from_selection(selected_option)
            if not pubkey_prefix:
                _LOGGER.error(f"Could not parse pubkey from selection: {selected_option}")
                return

        # Find coordinator for this entry_id or use first available
        coordinator = None
        if entry_id:
            coordinator = hass.data[DOMAIN].get(entry_id)
        else:
            for config_entry_id, coord in hass.data[DOMAIN].items():
                if hasattr(coord, 'api'):
                    coordinator = coord
                    break

        if not coordinator:
            _LOGGER.error("Could not find coordinator")
            return

        # Find the full public key from discovered contacts
        full_pubkey = None
        for pubkey, contact in coordinator._discovered_contacts.items():
            if pubkey.startswith(pubkey_prefix):
                full_pubkey = pubkey
                break

        if not full_pubkey:
            _LOGGER.error(f"Discovered contact not found with prefix: {pubkey_prefix}")
            return

        # Remove from discovered contacts
        contact_name = coordinator._discovered_contacts[full_pubkey].get("adv_name", "Unknown")
        del coordinator._discovered_contacts[full_pubkey]
        _LOGGER.info(f"Removed discovered contact: {contact_name} ({pubkey_prefix})")

        # Save to storage
        try:
            await coordinator._store.async_save(coordinator._discovered_contacts)
        except Exception as ex:
            _LOGGER.error(f"Error saving discovered contacts: {ex}")

        # Mark contact as dirty so binary sensors update
        coordinator.mark_contact_dirty(pubkey_prefix)

        # Trigger coordinator update
        updated_data = dict(coordinator.data) if coordinator.data else {}
        updated_data["contacts"] = coordinator.get_all_contacts()
        coordinator.async_set_updated_data(updated_data)

        # Remove the binary sensor entity for this contact. Removal runs
        # unconditionally: in data-only/off modes discovered contacts have no
        # entity, so async_get_entity_id returns None and this is a no-op (and
        # also clears any entity orphaned by a prior mode switch).
        # Post-PR-#236 contact unique_ids are scoped by entry_id; the migration
        # at __init__.py:_migrate_unique_ids_scope_contact_diagnostics guarantees
        # every existing entity uses this format.
        entity_registry = er.async_get(hass)
        unique_id = f"{coordinator.config_entry.entry_id}_contact_{pubkey_prefix}"
        entity_id = entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, unique_id
        )
        if entity_id:
            _LOGGER.info(f"Removing binary sensor entity: {entity_id}")
            entity_registry.async_remove(entity_id)

    # Register the contact management services
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_SELECTED_CONTACT,
        async_add_selected_contact_service,
        schema=UI_MESSAGE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_SELECTED_CONTACT,
        async_remove_selected_contact_service,
        schema=UI_MESSAGE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_DISCOVERED_CONTACT,
        async_remove_discovered_contact_service,
        schema=vol.Schema({
            vol.Optional(ATTR_ENTRY_ID): cv.string,
            vol.Optional(ATTR_PUBKEY_PREFIX): cv.string,
        }),
    )

    async def async_cleanup_unavailable_contacts_service(call: ServiceCall) -> None:
        """Remove all unavailable MeshCore contact binary sensors."""
        entry_id = call.data.get(ATTR_ENTRY_ID)

        entity_registry = er.async_get(hass)
        removed_count = 0

        for entity in list(entity_registry.entities.values()):
            if entity.platform == DOMAIN and entity.domain == "binary_sensor":
                # If entry_id specified, only clean for that device
                if entry_id and not entity.unique_id.startswith(entry_id):
                    continue

                # Check if entity is unavailable
                state = hass.states.get(entity.entity_id)
                if state and state.state == "unavailable":
                    _LOGGER.info(f"Removing unavailable entity: {entity.entity_id}")
                    entity_registry.async_remove(entity.entity_id)
                    removed_count += 1

        _LOGGER.info(f"Removed {removed_count} unavailable MeshCore contact sensors")

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEANUP_UNAVAILABLE_CONTACTS,
        async_cleanup_unavailable_contacts_service,
        schema=UI_MESSAGE_SCHEMA,
    )

    async def async_clear_discovered_contacts_service(call: ServiceCall) -> None:
        """Remove discovered contacts, optionally filtered by age.

        When days_threshold is provided, only contacts whose lastmod is older
        than the threshold are removed and contacts with added_to_node=True
        are preserved. When omitted, all discovered contacts are removed
        (original behavior).
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)

        coordinator = None
        if entry_id:
            coordinator = hass.data[DOMAIN].get(entry_id)
        else:
            for config_entry_id, coord in hass.data[DOMAIN].items():
                if hasattr(coord, "api"):
                    coordinator = coord
                    break

        if not coordinator:
            _LOGGER.error("Could not find coordinator")
            return

        if not coordinator._discovered_contacts:
            _LOGGER.info("No discovered contacts to clear")
            return

        days_threshold = call.data.get("days_threshold")
        if days_threshold:
            # Threshold-based cleanup: only remove stale contacts
            await coordinator._cleanup_stale_discovered_contacts(days_threshold)
        else:
            # Original behavior: clear all discovered contacts
            entity_registry = er.async_get(hass)
            removed_count = len(coordinator._discovered_contacts)

            # Removal runs unconditionally. In data-only/off modes discovered
            # contacts have no per-contact entity, so async_get_entity_id
            # returns None and the removal is a no-op; the dict clear below is
            # the whole operation. Running it in every mode also clears any
            # entity orphaned by a prior mode switch.
            for public_key in list(coordinator._discovered_contacts.keys()):
                pubkey_prefix = public_key[:12]
                coordinator.tracked_diagnostic_binary_contacts.discard(public_key)

                # Post-PR-#236 contact unique_ids are scoped by entry_id; the
                # migration at __init__.py:_migrate_unique_ids_scope_contact_diagnostics
                # guarantees every existing entity uses this format.
                unique_id = f"{coordinator.config_entry.entry_id}_contact_{pubkey_prefix}"
                entity_id = entity_registry.async_get_entity_id(
                    "binary_sensor", DOMAIN, unique_id
                )
                if entity_id:
                    entity_registry.async_remove(entity_id)

            coordinator._discovered_contacts.clear()

            try:
                await coordinator._store.async_save(coordinator._discovered_contacts)
            except Exception as ex:
                _LOGGER.error(f"Error saving discovered contacts: {ex}")

            updated_data = dict(coordinator.data) if coordinator.data else {}
            updated_data["contacts"] = coordinator.get_all_contacts()
            coordinator.async_set_updated_data(updated_data)

            _LOGGER.info(f"Cleared {removed_count} discovered contacts")

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_DISCOVERED_CONTACTS,
        async_clear_discovered_contacts_service,
        schema=vol.Schema({
            vol.Optional(ATTR_ENTRY_ID): cv.string,
            vol.Optional("days_threshold"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=365)
            ),
        }),
    )

    # ── Structured query services (get_contacts, get_channels, trace) ──
    # These services return structured responses (SupportsResponse.ONLY) so
    # companion integrations don't have to string-scrape execute_command output.
    # See docs/docs/companion-integration-api.md for the published surface.

    def _resolve_coordinator(entry_id: Optional[str]) -> Any:
        """Locate a MeshCore coordinator by entry_id, or the first available one."""
        if entry_id:
            coord = hass.data[DOMAIN].get(entry_id)
            if coord is not None and hasattr(coord, "api"):
                return coord
            return None
        for _eid, coord in hass.data[DOMAIN].items():
            if hasattr(coord, "api"):
                return coord
        return None

    async def async_get_contacts_service(call: ServiceCall) -> dict:
        """Return the device's known contacts as a structured list.

        Delegates to ``coordinator.get_all_contacts()`` so the result includes
        both contacts saved to the device and those only discovered via
        advertisements. ``added_to_node`` and ``pubkey_prefix`` are set by
        the coordinator; ``out_path_hash_mode`` is backfilled for older
        records via ``_ensure_contact_compat``.
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)
        coordinator = _resolve_coordinator(entry_id)
        if coordinator is None:
            return {"contacts": [], "error": "no_coordinator"}

        try:
            raw_contacts = coordinator.get_all_contacts() or []
        except Exception as ex:
            _LOGGER.error("get_contacts: coordinator access failed: %s", ex)
            return {"contacts": [], "error": "coordinator_error"}

        contacts_out = []
        for contact in raw_contacts:
            if not isinstance(contact, dict):
                continue
            c = _ensure_contact_compat(dict(contact))
            # get_all_contacts populates pubkey_prefix; double-check for
            # older records that somehow made it through without one.
            if "pubkey_prefix" not in c:
                pk = c.get("public_key") or ""
                if pk:
                    c["pubkey_prefix"] = pk[:12]
            contacts_out.append(c)

        return {"contacts": contacts_out}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CONTACTS,
        async_get_contacts_service,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    async def async_get_discovered_contact_service(call: ServiceCall) -> dict:
        """Return the full data dict for a single discovered contact.

        In data-only mode discovered contacts have no per-contact entity, so
        this is the supported way to inspect one. ``pubkey_prefix`` matches any
        discovered contact whose full public key starts with the given value
        (the 12-char prefix shown in the discovered-contact dropdown works, as
        does a full key). The returned dict carries only data already exposed
        via ``get_contacts`` and the dropdown -- pubkeys are mesh-advertised,
        not secret -- so this opens no new data-exposure surface.
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)
        pubkey_prefix = call.data.get(ATTR_PUBKEY_PREFIX)
        # Defense-in-depth for direct / non-schema callers: an empty or 1-char
        # prefix would match an arbitrary contact (str.startswith("") is always
        # True). The vol.Schema min-length below is the primary boundary check;
        # this inline guard protects callers that bypass schema validation.
        if not pubkey_prefix or len(pubkey_prefix) < 2:
            return {"contact": None, "error": "invalid_prefix"}
        coordinator = _resolve_coordinator(entry_id)
        if coordinator is None:
            return {"contact": None, "error": "no_coordinator"}

        match = None
        try:
            for pubkey, contact in coordinator._discovered_contacts.items():
                if pubkey.startswith(pubkey_prefix):
                    match = contact
                    break
        except Exception as ex:
            _LOGGER.error("get_discovered_contact: coordinator access failed: %s", ex)
            return {"contact": None, "error": "coordinator_error"}

        if match is None:
            return {"contact": None, "error": "not_found", "pubkey_prefix": pubkey_prefix}

        c = _ensure_contact_compat(dict(match))
        pk = c.get("public_key") or ""
        if pk and "pubkey_prefix" not in c:
            c["pubkey_prefix"] = pk[:12]
        return {"contact": c}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_DISCOVERED_CONTACT,
        async_get_discovered_contact_service,
        schema=vol.Schema({
            # Min length 2 is the canonical input-validation boundary (security
            # rubric S-04): rejects the empty/1-char prefix that would match an
            # arbitrary discovered contact via str.startswith.
            vol.Required(ATTR_PUBKEY_PREFIX): vol.All(cv.string, vol.Length(min=2)),
            vol.Optional(ATTR_ENTRY_ID): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )

    async def async_get_channels_service(call: ServiceCall) -> dict:
        """Return the device's configured channels as a structured list.

        Reads cached info from ``coordinator._channel_info`` (same source the
        sidebar-panel's ``ws_get_channels`` uses) so this is a pure query
        that never issues an on-device request. Unused channel slots
        (empty name or ``(unused)``) are filtered out. The shared secret is
        never returned — only its presence via ``shared_secret_present``.
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)
        coordinator = _resolve_coordinator(entry_id)
        if coordinator is None:
            return {"channels": [], "error": "no_coordinator"}

        try:
            max_ch = int(getattr(coordinator, "max_channels", 0) or 0)
        except Exception:
            max_ch = 0

        channel_info_map = getattr(coordinator, "_channel_info", {}) or {}

        channels_out = []
        for channel_idx in range(max_ch):
            info = channel_info_map.get(channel_idx) or {}
            if not info:
                continue
            channel_name = info.get("channel_name", "") or ""
            # Skip unused/unconfigured channel slots so companions only see
            # real channels the user could actually send to.
            if not channel_name or channel_name == "(unused)":
                continue
            entry = {
                "channel_idx": channel_idx,
                "channel_name": channel_name,
                # Don't leak the shared secret; only surface presence.
                "shared_secret_present": bool(info.get("channel_secret")),
            }
            channels_out.append(entry)

        return {"channels": channels_out}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CHANNELS,
        async_get_channels_service,
        schema=vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    async def async_trace_service(call: ServiceCall) -> dict:
        """Trace the route to a contact and return hop/path/round-trip info.

        Ported from the sidebar-panel's ``ws_trace`` so the service honours
        the same validated behavior:

          * Only contacts saved to the device can be traced — firmware's
            path-discovery handler rejects discovered-only contacts with
            ERR_CODE_NOT_FOUND, so fail fast with ``contact_not_on_device``.
          * Flood contacts (``out_path_len == -1``) run a path-discovery
            pass first. The PATH_RESPONSE listener is registered before the
            PATH_REQ is sent to close the race where the response could
            arrive before the subscription was live. Firmware rejection
            (ERROR) and timeout surface as distinct error strings.
          * The trace packet is built as outbound_hops + target + reverse
            (outbound_hops) with 1-byte per-hop hashes (flags=0). The
            round-trip hash list is required by the firmware TRACE
            handler (Mesh.cpp:41-66) for the sender to receive the echo,
            and 1-byte hashes are empirically the only width that
            completes reliably in production meshes.
          * Every failure mode returns a structured ``{"trace": null,
            "error": "..."}`` dict so automations never see an exception.
        """
        entry_id = call.data.get(ATTR_ENTRY_ID)
        pubkey_prefix = call.data[ATTR_PUBKEY_PREFIX]
        requested_timeout_s = float(call.data.get("timeout", 15))

        coordinator = _resolve_coordinator(entry_id)
        if coordinator is None:
            return {"trace": None, "error": "no_coordinator"}

        api = coordinator.api
        if not api or not api.connected or not api.mesh_core:
            return {"trace": None, "error": "not_connected"}

        # Prefer coordinator.get_contact_by_prefix (searches added +
        # discovered), fall back to the SDK lookup used elsewhere. This
        # matches the sidebar-panel's behavior so discovered-only contacts
        # can be recognised and rejected with a clear error.
        contact = None
        get_by_prefix = getattr(coordinator, "get_contact_by_prefix", None)
        if callable(get_by_prefix):
            contact = get_by_prefix(pubkey_prefix) or None
        if not contact:
            contact = _resolve_contact(pubkey_prefix, "trace", api, coordinator)
        if not contact:
            return {"trace": None, "error": "contact_not_found"}

        # Firmware CMD_SEND_PATH_DISCOVERY_REQ memcmps the target pubkey
        # against the on-device contact table; discovered-only contacts are
        # rejected with ERR_CODE_NOT_FOUND. Fail fast with an actionable
        # error instead of paying for the firmware round-trip.
        if not contact.get("added_to_node"):
            return {"trace": None, "error": "contact_not_on_device"}

        public_key = contact.get("public_key") or ""
        if not public_key:
            return {"trace": None, "error": "contact_missing_pubkey"}

        mesh_core = api.mesh_core
        tag = random.randint(0, 0xFFFFFFFF)

        out_path_len = contact.get("out_path_len", -1)
        out_path_hash_mode = contact.get("out_path_hash_mode", 0)
        out_path_hex = contact.get("out_path", "") or ""

        # ── Flood contact: run path discovery first ──
        if out_path_len == -1:
            try:
                dst_bytes = bytes.fromhex(public_key)
            except (ValueError, TypeError) as ex:
                _LOGGER.error("trace: bad pubkey hex for path discovery: %s", ex)
                return {"trace": None, "error": "contact_missing_pubkey"}

            # Pre-register the PATH_RESPONSE listener so the response can't
            # arrive and be dispatched before our subscription is live.
            # Filter by pubkey_pre so concurrent path-discovery traffic
            # for other contacts can't satisfy this wait. Mirrors
            # Remote-Terminal-for-MeshCore's approach.
            path_response_task = asyncio.create_task(
                mesh_core.dispatcher.wait_for_event(
                    EventType.PATH_RESPONSE,
                    attribute_filters={"pubkey_pre": pubkey_prefix},
                    timeout=30.0,  # outer safety; real bound applied below
                )
            )

            pd_data = b"\x34\x00" + dst_bytes
            try:
                send_result = await mesh_core.commands.send(
                    pd_data,
                    [EventType.MSG_SENT, EventType.ERROR],
                )
            except Exception as ex:
                path_response_task.cancel()
                _LOGGER.error("trace: path discovery send raised: %s", ex)
                return {"trace": None, "error": "path_discovery_failed"}

            if send_result is None:
                path_response_task.cancel()
                return {
                    "trace": None,
                    "error": "path_discovery_failed",
                    "reason": "no_firmware_ack",
                }

            if getattr(send_result, "type", None) == EventType.ERROR:
                path_response_task.cancel()
                # Firmware PacketType.ERROR carries {"error_code",
                # "code_string"} when mapped, or {"reason"} for reader
                # parse-failures. Accept either shape.
                reason = "unknown"
                if isinstance(send_result.payload, dict):
                    p = send_result.payload
                    reason = (
                        p.get("code_string")
                        or p.get("reason")
                        or (f"error_code={p['error_code']}" if "error_code" in p else "unknown")
                    )
                elif send_result.payload is not None:
                    reason = repr(send_result.payload)
                return {
                    "trace": None,
                    "error": "path_discovery_rejected",
                    "reason": reason,
                }

            # MSG_SENT — firmware accepted and broadcast the request.
            # Apply a 15s floor on the PATH_RESPONSE wait — two-hop flood
            # round-trips routinely run 5-12s under real LoRa conditions,
            # and a shorter timeout gives up before the mesh has had time
            # to answer. Honour firmware's suggested_timeout if it ever
            # exceeds 15s.
            suggested_ms = 0
            if isinstance(send_result.payload, dict):
                suggested_ms = send_result.payload.get("suggested_timeout", 0) or 0
            pd_timeout = max(suggested_ms / 800.0, 15.0)

            try:
                path_event = await asyncio.wait_for(
                    path_response_task, timeout=pd_timeout,
                )
            except asyncio.TimeoutError:
                path_response_task.cancel()
                path_event = None
            except Exception as ex:
                path_response_task.cancel()
                _LOGGER.error("trace: PATH_RESPONSE wait raised: %s", ex)
                return {"trace": None, "error": "path_discovery_failed"}

            if path_event is None:
                return {"trace": None, "error": "path_discovery_timeout"}

            discovered = path_event.payload or {}
            out_path_len = discovered.get("out_path_len", -1)
            if out_path_len < 0:
                return {
                    "trace": None,
                    "error": "path_discovery_failed",
                    "reason": "malformed_path_response",
                }

            out_path_hash_len = discovered.get("out_path_hash_len", 1)
            out_path_hash_mode = {1: 0, 2: 1, 4: 2}.get(out_path_hash_len, 0)
            out_path_hex = discovered.get("out_path", "") or ""

        # ── Build the round-trip 1-byte-hash path ──
        # Force flags=0 (1-byte hashes) regardless of the contact's cached
        # hash mode: 2-byte traces empirically fail to complete round-trip
        # in production meshes. Truncate each stored hop to its first byte
        # to match.
        flags = 0
        target_hash_hex = public_key[:2]  # first 1 byte
        if not target_hash_hex:
            return {"trace": None, "error": "contact_missing_pubkey"}

        stored_hop_width = {0: 2, 1: 4, 2: 8}.get(out_path_hash_mode, 2)
        outbound_hops = []
        for i in range(out_path_len):
            start = i * stored_hop_width
            stored_hop = out_path_hex[start : start + stored_hop_width]
            if len(stored_hop) >= 2:
                outbound_hops.append(stored_hop[:2])
        return_hops = list(reversed(outbound_hops))
        full_path_hex = (
            "".join(outbound_hops) + target_hash_hex + "".join(return_hops)
        )

        try:
            trace_path_bytes = bytes.fromhex(full_path_hex)
        except ValueError as ex:
            _LOGGER.error("trace: bad hex in path construction: %s", ex)
            return {"trace": None, "error": "internal_error"}

        _LOGGER.debug(
            "trace: sending tag=%08x flags=%d path=%s (hops=%d, target=%s)",
            tag, flags, full_path_hex, len(outbound_hops), target_hash_hex,
        )

        # ── Send trace and await TRACE_DATA with our tag ──
        start_time = time.monotonic()
        try:
            send_result = await mesh_core.commands.send_trace(
                0, tag, flags, trace_path_bytes
            )
        except Exception as ex:
            _LOGGER.error("trace: send_trace raised: %s", ex)
            return {"trace": None, "error": "send_failed"}

        if send_result is None or getattr(send_result, "type", None) == EventType.ERROR:
            reason = "no_response"
            if send_result is not None and isinstance(send_result.payload, dict):
                reason = send_result.payload.get("reason", "unknown")
            return {"trace": None, "error": reason}

        # Bound the TRACE_DATA wait using (in order of preference) the
        # user's requested timeout, the device's self-reported suggested
        # timeout, and sensible floor/ceiling. Use firmware-suggested *1.2
        # like ws_trace so near-timeout responses aren't cut off.
        self_info = getattr(api, "self_info", None) or {}
        fw_suggested_ms = self_info.get("suggested_timeout", 15000) if isinstance(self_info, dict) else 15000
        try:
            fw_suggested_s = float(fw_suggested_ms) / 1000.0 * 1.2
        except Exception:
            fw_suggested_s = 18.0
        effective_timeout = min(max(requested_timeout_s, fw_suggested_s, 5.0), 60.0)

        try:
            trace_event = await mesh_core.dispatcher.wait_for_event(
                EventType.TRACE_DATA,
                attribute_filters={"tag": tag},
                timeout=effective_timeout,
            )
        except Exception as ex:
            _LOGGER.error("trace: awaiting TRACE_DATA raised: %s", ex)
            return {"trace": None, "error": "await_failed"}

        rtt_ms = int((time.monotonic() - start_time) * 1000)
        if trace_event is None:
            return {"trace": None, "error": "timeout", "round_trip_ms": rtt_ms}

        payload = trace_event.payload or {}
        path_nodes = payload.get("path") or []
        # Final path entry is the local device on receiving the echo; its
        # SNR tells callers how strong the return leg was.
        final_snr = None
        if path_nodes and isinstance(path_nodes[-1], dict) and "snr" in path_nodes[-1]:
            final_snr = path_nodes[-1]["snr"]

        return {
            "trace": {
                "hops": payload.get("path_len", 0),
                "path": path_nodes,
                "round_trip_ms": rtt_ms,
                "final_snr": final_snr,
                "tag": payload.get("tag"),
            }
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_TRACE,
        async_trace_service,
        schema=vol.Schema({
            vol.Required(ATTR_PUBKEY_PREFIX): cv.string,
            vol.Optional(ATTR_ENTRY_ID): cv.string,
            vol.Optional("timeout", default=15): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=120)
            ),
        }),
        supports_response=SupportsResponse.ONLY,
    )

    # Create CLI command execution service from UI helper
    # async def async_execute_cli_command_ui(call: ServiceCall) -> None:
    #     """Execute CLI command from the text helper entity."""
    #     entry_id = call.data.get(ATTR_ENTRY_ID)
        
    #     # Get command from CLI command text entity
    #     cli_command_entity = hass.states.get("text.meshcore_cli_command")
    #     if not cli_command_entity:
    #         _LOGGER.error("CLI command input helper not found: text.meshcore_cli_command")
    #         return
            
    #     command = cli_command_entity.state
        
    #     if not command:
    #         _LOGGER.warning("No command to execute - CLI input is empty")
    #         return
        
    #     # Create CLI command service call
    #     cli_call = create_service_call(
    #         DOMAIN, 
    #         SERVICE_CLI_COMMAND, 
    #         {"command": command, "entry_id": entry_id}
    #     )
        
    #     # Execute the CLI command
    #     await async_cli_command_service(cli_call)
        
    #     # Clear the command input after execution
    #     try:
    #         await hass.services.async_call(
    #             "text", 
    #             "set_value", 
    #             {"entity_id": "text.meshcore_cli_command", "value": ""},
    #             blocking=False
    #         )
    #     except Exception as ex:
    #         _LOGGER.warning(f"Could not clear CLI command input: {ex}")
    
    # Register the CLI command execution service
    # hass.services.async_register(
    #     DOMAIN,
    #     SERVICE_EXECUTE_CLI_COMMAND_UI,
    #     async_execute_cli_command_ui,
    #     schema=UI_MESSAGE_SCHEMA,
    # )

async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload MeshCore services."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_MESSAGE)

    if hass.services.has_service(DOMAIN, SERVICE_SEND_CHANNEL_MESSAGE):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_CHANNEL_MESSAGE)

    if hass.services.has_service(DOMAIN, SERVICE_CLI_COMMAND):
        hass.services.async_remove(DOMAIN, SERVICE_CLI_COMMAND)

    if hass.services.has_service(DOMAIN, SERVICE_CLI_COMMAND_UI):
        hass.services.async_remove(DOMAIN, SERVICE_CLI_COMMAND_UI)

    if hass.services.has_service(DOMAIN, SERVICE_CLI_CLEAR):
        hass.services.async_remove(DOMAIN, SERVICE_CLI_CLEAR)

    if hass.services.has_service(DOMAIN, SERVICE_MESSAGE_SCRIPT):
        hass.services.async_remove(DOMAIN, SERVICE_MESSAGE_SCRIPT)

    if hass.services.has_service(DOMAIN, SERVICE_EXECUTE_COMMAND):
        hass.services.async_remove(DOMAIN, SERVICE_EXECUTE_COMMAND)

    if hass.services.has_service(DOMAIN, SERVICE_EXECUTE_COMMAND_UI):
        hass.services.async_remove(DOMAIN, SERVICE_EXECUTE_COMMAND_UI)

    if hass.services.has_service(DOMAIN, SERVICE_ADD_SELECTED_CONTACT):
        hass.services.async_remove(DOMAIN, SERVICE_ADD_SELECTED_CONTACT)

    if hass.services.has_service(DOMAIN, SERVICE_REMOVE_SELECTED_CONTACT):
        hass.services.async_remove(DOMAIN, SERVICE_REMOVE_SELECTED_CONTACT)

    if hass.services.has_service(DOMAIN, SERVICE_REMOVE_DISCOVERED_CONTACT):
        hass.services.async_remove(DOMAIN, SERVICE_REMOVE_DISCOVERED_CONTACT)

    if hass.services.has_service(DOMAIN, SERVICE_CLEANUP_UNAVAILABLE_CONTACTS):
        hass.services.async_remove(DOMAIN, SERVICE_CLEANUP_UNAVAILABLE_CONTACTS)

    if hass.services.has_service(DOMAIN, SERVICE_CLEAR_DISCOVERED_CONTACTS):
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_DISCOVERED_CONTACTS)

    if hass.services.has_service(DOMAIN, SERVICE_GET_CONTACTS):
        hass.services.async_remove(DOMAIN, SERVICE_GET_CONTACTS)

    if hass.services.has_service(DOMAIN, SERVICE_GET_DISCOVERED_CONTACT):
        hass.services.async_remove(DOMAIN, SERVICE_GET_DISCOVERED_CONTACT)

    if hass.services.has_service(DOMAIN, SERVICE_GET_CHANNELS):
        hass.services.async_remove(DOMAIN, SERVICE_GET_CHANNELS)

    if hass.services.has_service(DOMAIN, SERVICE_TRACE):
        hass.services.async_remove(DOMAIN, SERVICE_TRACE)


def create_service_call(
    domain: str,
    service: str,
    data: Optional[Dict[str, Any]] = None,
    hass: Optional[HomeAssistant] = None
) -> ServiceCall:
    """Returns a ServiceCall instance compatible with the current Home Assistant version.
    
    In Home Assistant 2025.x.x and newer, ServiceCall requires a 'hass' parameter
    as the first argument, which was a breaking change from 2024.x.x. This factory
    function creates the appropriate instance based on the detected version.
    
    Args:
        domain: Service domain name
        service: Service name to call
        data: Dictionary containing service call parameters
        hass: HomeAssistant instance, required for 2025.x.x+ but ignored in 2024.x.x
        
    Returns:
        ServiceCall: Properly configured service call instance for the current HA version
    
    Example:
        # Create a service call that works across HA versions
        service_call = create_service_call(
            domain="light",
            service="turn_on",
            data={"entity_id": "light.living_room", "brightness": 255},
            hass=hass  # Always pass this, will be used only when needed
        )
    """
    # Create the service call instance based on the detected HA version
    if MAJOR_VERSION >= 2025:
        _LOGGER.debug("Creating ServiceCall with hass parameter (2025.x.x+ format)")
        return ServiceCall(
            hass=hass,
            domain=domain,
            service=service,
            data=data or {}
        )
    else:
        _LOGGER.debug("Creating ServiceCall without hass parameter (2024.x.x format)")
        return ServiceCall(
            domain=domain,
            service=service,
            data=data or {}
        )