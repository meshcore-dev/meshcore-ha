"""Services for the MeshCore integration."""
import logging
import time
import voluptuous as vol
import shlex
from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.const import MAJOR_VERSION
from meshcore.events import EventType

from .const import (
    ATTR_PUBKEY_PREFIX,
    DOMAIN,
    SERVICE_SEND_MESSAGE,
    SERVICE_SEND_CHANNEL_MESSAGE,
    SERVICE_EXECUTE_COMMAND,
    SERVICE_EXECUTE_COMMAND_UI,
    SERVICE_MESSAGE_SCRIPT,
    SERVICE_ADD_SELECTED_CONTACT,
    SERVICE_REMOVE_SELECTED_CONTACT,
    SERVICE_REMOVE_DISCOVERED_CONTACT,
    SERVICE_CLEANUP_UNAVAILABLE_CONTACTS,
    SELECT_NO_CONTACTS,
    SELECT_NO_DISCOVERED,
    SELECT_NO_ADDED,
    ATTR_NODE_ID,
    ATTR_CHANNEL_IDX,
    ATTR_MESSAGE,
    ATTR_COMMAND,
    ATTR_ENTRY_ID,
)
from .utils import extract_pubkey_from_selection

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
                        
                        # Create outgoing message event data
                        outgoing_msg = {
                            "message": message,
                            "device": config_entry_id,
                            "message_type": "direct",
                            "receiver": contact.get("name"),
                            "timestamp": int(time.time()),
                            "contact_public_key": pubkey
                        }
                        # Fire event for outgoing message to update message-related entities
                        hass.bus.async_fire(f"{DOMAIN}_message_sent", outgoing_msg)
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
                    
                    # Send the channel message using the new API
                    result = await api.mesh_core.commands.send_chan_msg(channel_idx, message)
                    
                    if result.type == EventType.ERROR:
                        _LOGGER.warning(
                            "Failed to send message to channel %s: %s", channel_idx, result.payload
                        )
                    else:
                        _LOGGER.info(
                            "Successfully sent message to channel %s", channel_idx
                        )
                        
                        # Create outgoing message event data
                        outgoing_msg = {
                            "message": message,
                            "device": config_entry_id,
                            "message_type": "channel",
                            "receiver": f"channel_{channel_idx}",
                            "timestamp": int(time.time()),
                            "channel_idx": channel_idx
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
        
        # Parse the command using shlex to handle quoted arguments properly
        try:
            parts = shlex.split(command_str)
        except Exception as ex:
            _LOGGER.error("Error parsing command: %s", ex)
            return
            
        if not parts:
            _LOGGER.error("No command specified")
            return
            
        # Extract the command name and arguments
        command_name = parts[0]
        arguments = parts[1:]
        
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
                        
                        # Messaging commands
                        "get_msg": ["float"],  # timeout (optional)
                        "send_login": ["contact", "str"],
                        "send_logout": ["contact"],
                        "send_statusreq": ["contact"],
                        "send_telemetry_req": ["contact"],
                        "send_msg": ["contact", "str", "int"],  # contact, message, timestamp (optional)
                        "send_chan_msg": ["int", "str", "int"],  # channel, message, timestamp
                        "send_cmd": ["contact", "str", "int"],  # contact, command, timestamp (optional)
                        "send_binary_req": ["contact", "bytes"],
                        "send_path_discovery": ["contact"],
                        "send_trace": ["int", "int", "int", "bytes"],  # hop_count, timestamp, flags, data
                        
                        # Binary commands
                        "req_binary": ["contact", "str", "int"],  # contact, request, timeout
                        "req_telemetry": ["contact", "int"],  # contact, timeout
                        "req_mma": ["contact", "int", "int", "int"],  # contact, start, end, timeout
                        "req_acl": ["contact", "int"],  # contact, timeout
                        
                        # Device configuration commands
                        "send_advert": ["bool"],
                        "set_name": ["str"],
                        "set_time": ["int"],
                        "set_tx_power": ["int"],
                        "set_devicepin": ["int"],
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
                        "get_channel": ["int"],
                        "set_channel": ["int", "str", "bytes"]
                    }
                    
                    # Get parameter types for this command
                    param_types = command_param_types.get(command_name, [])
                    
                    # Prepare arguments with proper types
                    prepared_args = []
                    
                    # Process each argument according to the expected parameter type
                    for i, arg in enumerate(arguments):
                        param_type = param_types[i] if i < len(param_types) else "str"
                        
                        if param_type == "contact":
                            # For contact params, try to get contact by key prefix
                            if len(arg) >= 6:  # Ensure it's a reasonable pubkey prefix
                                contact = api.mesh_core.get_contact_by_key_prefix(arg)
                                if contact:
                                    prepared_args.append(contact)
                                else:
                                    # If no contact found, try by name
                                    contact = api.mesh_core.get_contact_by_name(arg)
                                    if contact:
                                        prepared_args.append(contact)
                                    else:
                                        # For add_contact, also check discovered contacts
                                        if command_name == "add_contact":
                                            for discovered_contact in coordinator._discovered_contacts.values():
                                                if discovered_contact.get("public_key", "").startswith(arg) or discovered_contact.get("adv_name") == arg:
                                                    contact = discovered_contact
                                                    prepared_args.append(contact)
                                                    break

                                        if not contact:
                                            _LOGGER.error(f"Contact not found with key or name: {arg}")
                                            return
                            else:
                                _LOGGER.error(f"Invalid pubkey prefix length: {arg}")
                                return
                                
                        elif param_type == "int":
                            # Convert to integer
                            try:
                                prepared_args.append(int(arg))
                            except ValueError:
                                _LOGGER.error(f"Could not convert '{arg}' to integer")
                                return
                                
                        elif param_type == "float":
                            # Convert to float
                            try:
                                prepared_args.append(float(arg))
                            except ValueError:
                                _LOGGER.error(f"Could not convert '{arg}' to float")
                                return
                                
                        elif param_type == "bool":
                            # Handle boolean parameters
                            if arg.lower() in ('true', 'yes', 'y', '1'):
                                prepared_args.append(True)
                            elif arg.lower() in ('false', 'no', 'n', '0'):
                                prepared_args.append(False)
                            else:
                                _LOGGER.error(f"Could not convert '{arg}' to boolean")
                                return
                        
                        elif param_type == "bytes":
                            # Convert hex string to bytes
                            try:
                                prepared_args.append(bytes.fromhex(arg))
                            except ValueError:
                                _LOGGER.error(f"Could not convert '{arg}' to bytes - invalid hex string")
                                return
                        
                        else:
                            # For any other type, pass the argument as is
                            prepared_args.append(arg)
                    
                    # Execute the command with the converted arguments
                    _LOGGER.debug(f"Executing {command_name} with prepared arguments: {prepared_args}")
                    result = await command_method(*prepared_args)

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

                                updated_data = dict(coordinator.data) if coordinator.data else {}
                                updated_data["contacts"] = coordinator.get_all_contacts()
                                coordinator.async_set_updated_data(updated_data)

                    # Convert any binary data to hex strings for logging and events
                    if hasattr(result, 'payload') and isinstance(result.payload, dict):
                        # Create a JSON-serializable version of the payload
                        json_safe_payload = {}
                        for key, value in result.payload.items():
                            if isinstance(value, bytes):
                                json_safe_payload[key] = value.hex()
                            else:
                                json_safe_payload[key] = value
                        
                        # Log only the JSON-safe version
                        _LOGGER.info("Command result: %s with payload: %s", 
                                    result.type, json_safe_payload)
                    else:
                        _LOGGER.info("Command result: %s", result)
                    
                    return
                    
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
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE_COMMAND_UI,
        async_execute_command_ui_service,
        schema=UI_MESSAGE_SCHEMA,
    )
    
    # hass.services.async_register(
    #     DOMAIN,
    #     SERVICE_CLI_COMMAND,
    #     async_cli_command_service,
    #     schema=CLI_COMMAND_SCHEMA,
    # )
    
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
            registry = hass.helpers.entity_registry.async_get()
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
            registry = hass.helpers.entity_registry.async_get()
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
                registry = hass.helpers.entity_registry.async_get()
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

        # Remove the binary sensor entity for this contact
        entity_registry = er.async_get(hass)
        for entity in list(entity_registry.entities.values()):
            if entity.platform == DOMAIN and entity.domain == "binary_sensor":
                if entity.unique_id == pubkey_prefix:
                    _LOGGER.info(f"Removing binary sensor entity: {entity.entity_id}")
                    entity_registry.async_remove(entity.entity_id)
                    break

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

    # if hass.services.has_service(DOMAIN, SERVICE_CLI_COMMAND):
    #     hass.services.async_remove(DOMAIN, SERVICE_CLI_COMMAND)

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