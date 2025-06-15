![MeshCore Banner](images/meshcore-bg.png)

# MeshCore for Home Assistant

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshcore)
[![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=awolden&repository=meshcore-ha&category=integration)

This is a custom Home Assistant integration for MeshCore mesh radio nodes. It allows you to monitor and control MeshCore nodes via USB, BLE, or TCP connections.

> :warning: **Work in Progress**: This integration is under active development. BLE connection method hasn't been thoroughly tested yet.

Core integration is powered by [meshcore-py](https://github.com/fdlamotte/meshcore_py).

## Features

- Connect to MeshCore nodes via USB, BLE, or TCP
- Event-driven architecture for efficient updates and low overhead
- Monitor node status, signal strength, battery levels, and more
- View messages received by the mesh network
- Send messages to other nodes in the network
- Automatically discover nodes in the mesh network and create sensors for them
- Track and monitor repeater nodes with detailed statistics 
- Support for Room Server nodes allowing group chat functionality
- Direct access to all meshcore-py commands and events
- Configurable update intervals for different data types (messages, device info, repeaters)

## Installation

### HACS Installation (Recommended)

1. Make sure you have [HACS](https://hacs.xyz/) installed
2. Add this repository as a custom repository in HACS:
   - Go to HACS > Integrations
   - Click on the three dots in the top right corner
   - Select "Custom repositories"
   - Add the URL of this repository
   - Select "Integration" as the category
3. Click "Install" on the MeshCore integration

### Manual Installation

1. Copy the `custom_components/meshcore` directory to your Home Assistant `custom_components` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services**
2. Click **+ Add Integration** and search for "MeshCore"
3. Follow the setup wizard:
   - Select the connection type (USB, BLE, or TCP)
   - For USB: Select the USB port and set the baud rate (default: 115200)
   - For BLE: Select your MeshCore device from the discovered devices or enter the address manually
   - For TCP: Enter the hostname/IP and port of your MeshCore device
   - Configure update intervals for different data types:
     - Messages interval: How often to poll for new messages (default: 10 seconds)
     - Device info interval: How often to update device statistics (default: 60 seconds)
     - Repeater update interval: How often to poll repeater nodes (default: 300 seconds)

## Available Sensors

For the local node:
- **Node Status**: Shows if the node is online or offline
- **Battery Voltage**: Battery voltage in volts
- **Battery Percentage**: Battery level (percentage)
- **Node Count**: Number of nodes in the mesh network (including the local node)
- **TX Power**: Transmission power in dBm
- **Latitude/Longitude**: Node location (if available)
- **Frequency**: Radio frequency in MHz
- **Bandwidth**: Radio bandwidth in kHz
- **Spreading Factor**: Radio spreading factor

For remote nodes (automatically created for each node in the network):
- **MeshCore Contacts**: Diagnostic sensor showing all contacts with their details
- **Contact Status**: Status sensor for each contact ("fresh" or "stale" based on last seen time)
- Contact details are included as attributes (name, type, public key, last seen, etc.)

For message tracking:
- **Channel Messages**: Binary sensors for tracking messages on channels 0-3
- **Contact Messages**: Binary sensors for tracking messages from specific contacts

For repeater nodes:
- **Battery Voltage**: Battery voltage in volts
- **Battery Percentage**: Estimated battery level percentage
- **Uptime**: How long the repeater has been running (in minutes)
- **Airtime**: Total radio airtime used by the repeater (in minutes)
- **Messages Sent/Received**: Count of messages handled by the repeater
- **TX Queue Length**: Number of messages in transmission queue
- **Free Queue Length**: Number of free slots in queue
- **Sent/Received Flood Messages**: Count of broadcast messages
- **Sent/Received Direct Messages**: Count of direct messages
- **Full Events**: Count of queue full events
- **Direct/Flood Duplicates**: Count of duplicate messages

## Services

The integration provides the following services to interact with MeshCore devices:

### Send Message

Send a message to a specific node in the mesh network. You can identify the node by either its name or public key.

Service: `meshcore.send_message`

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `node_id` | string | One of node_id or pubkey_prefix required | The name of the node to send the message to |
| `pubkey_prefix` | string | One of node_id or pubkey_prefix required | The public key prefix (at least 6 characters) |
| `message` | string | Yes | The message text to send |
| `entry_id` | string | No | The config entry ID if you have multiple MeshCore devices |

Example using node name:
```yaml
service: meshcore.send_message
data:
  node_id: "NodeAlpha"
  message: "Hello from Home Assistant!"
```

Example using public key:
```yaml
service: meshcore.send_message
data:
  pubkey_prefix: "f293ac"
  message: "Hello using public key!"
```

### Send Channel Message

Send a message to a specific channel on the mesh network.

Service: `meshcore.send_channel_message`

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `channel_idx` | integer | Yes | The channel index to send to (usually 0-3) |
| `message` | string | Yes | The message text to send |
| `entry_id` | string | No | The config entry ID if you have multiple MeshCore devices |

Example:
```yaml
service: meshcore.send_channel_message
data:
  channel_idx: 0
  message: "Broadcast to everyone on channel 0!"
```

### Execute Command (Advanced)

Execute MeshCore SDK commands directly. This service provides access to the underlying meshcore-py library methods, enabling automation of advanced features.

> ⚠️ **Advanced Feature**: This service directly exposes the meshcore-py library methods and is intended for advanced users.

Service: `meshcore.execute_command`

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `command` | string | Yes | The command with parameters to execute (e.g., "get_bat", "set_tx_power 10") |
| `entry_id` | string | No | The config entry ID if you have multiple MeshCore devices |

Example with arguments:
```yaml
service: meshcore.execute_command
data:
  command: "set_tx_power 15"
```

Example sending messages:
```yaml
service: meshcore.execute_command
data:
  command: 'send_msg "Repeater1" "Hello from Home Assistant!"'
```

```yaml
service: meshcore.execute_command
data:
  command: "send_advert true"
```

Available commands include:
- `get_bat` - Get battery level
- `send_device_query` - Query device information
- `reboot` - Reboot the node
- `send_advert` - Send an advertisement (add true for flood)
- `set_tx_power` - Set transmit power (e.g., `set_tx_power 10`)
- `set_radio` - Set radio parameters (e.g., `set_radio 868.0 250.0 7 8`)
- `set_name` - Set node name (e.g., `set_name "My Node"`)
- `send_msg` - Send message to contact (e.g., `send_msg "NodeName" "Hello!"`)
- `send_chan_msg` - Send channel message (e.g., `send_chan_msg 0 "Hello channel 0!"`)
- `get_contacts` - Get list of contacts
- And many more advanced commands from the meshcore-py library

### UI Message Service

This service is designed to work with the UI messaging card and simplifies sending messages through the UI.

Service: `meshcore.send_ui_message`

This service automatically pulls values from the helper entities (`select.meshcore_recipient_type`, `select.meshcore_channel`, `select.meshcore_contact`, and `text.meshcore_message`), so you don't need to specify any parameters other than entry_id.

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `entry_id` | string | No | The config entry ID if you have multiple MeshCore devices |

### Execute Command UI

This service executes commands entered through the UI text input.

Service: `meshcore.execute_command_ui`

This service automatically pulls the command from the `text.meshcore_command` helper entity.

| Field | Type | Required | Description |
| ----- | ---- | -------- | ----------- |
| `entry_id` | string | No | The config entry ID if you have multiple MeshCore devices |

> For more detailed service definitions, see the [services.yaml](custom_components/meshcore/services.yaml) file.

## Event System

The MeshCore integration now uses an event-driven architecture, exposing all events from the meshcore-py library to Home Assistant. This allows for more flexible automations and integrations.

### Raw Events

All events from the MeshCore device are forwarded to the Home Assistant event bus as `meshcore_raw_event`. This allows you to listen for specific events and create automations based on them.

The event data contains:
- `event_type`: The type of event as a string, e.g., "EventType.BATTERY"
- `payload`: The payload of the event (contains the specific data for that event type)
- `timestamp`: When the event was received

Example of listening to raw events in an automation:

```yaml
trigger:
  - platform: event
    event_type: meshcore_raw_event
    event_data:
      event_type: "EventType.BATTERY"
condition: []
action:
  - service: notify.notify
    data:
      title: "Battery Update"
      message: "Battery status updated: {{ trigger.event.data.payload.voltage }}V ({{ trigger.event.data.payload.percent }}%)"
```

Common event types include:
- `EventType.BATTERY` - Battery status updates
- `EventType.DEVICE_INFO` - Device information updates
- `EventType.CONTACTS` - Contact list updates 
- `EventType.CONTACT_MSG_RECV` - Direct message received
- `EventType.CHANNEL_MSG_RECV` - Channel message received
- `EventType.STATUS_RESPONSE` - Repeater status updates
- `EventType.MSG_SENT` - Message sent confirmation

### Specific Message Events

In addition to raw events, the integration fires specific events for common operations:

- `meshcore_message`: Fired when a message is received (either channel or direct)
- `meshcore_message_sent`: Fired when a message is sent through the integration services

## Automation Examples

Below are examples of automations that utilize the MeshCore services.

### Forward New Messages to Push Notifications
```yaml
alias: Meshcore Forward to Push
description: "Forwards all MeshCore messages to a push notification"
triggers:
  - trigger: event
    event_type: meshcore_message
actions:
  - action: notify.notify
    data:
      message: >-
        {% if trigger.event.data.channel is defined %}
          Channel {{ trigger.event.data.channel }}: {{ trigger.event.data.sender_name }}: {{ trigger.event.data.message }}
        {% else %}
          {{ trigger.event.data.sender_name }}: {{ trigger.event.data.message }}
        {% endif %}
mode: single
```

### Scheduled Advertisement Broadcasting

This automation sends an advertisement broadcast every 15 minutes to help maintain network connectivity and make your node more discoverable to other nodes in the mesh network.

```yaml
alias: MeshCore Scheduled Advertisement
description: "Sends a MeshCore advertisement broadcast every 15 minutes"
trigger:
  - platform: time_pattern
    minutes: "/15"  # Every 15 minutes
action:
  - service: meshcore.execute_command
    data:
      command: "send_advert"
mode: single
```

## Room Servers

Room Servers are a special type of node in the MeshCore network that provide group chat functionality. Unlike regular nodes, Room Servers:

- Appear as normal contacts in your chat interface
- Allow multiple users to join and communicate in a shared chat room
- Require login before you can engage with them

### Connecting to a Room Server

To use a Room Server, you need to:

1. Add it as a repeater in the MeshCore configuration (even though it's not technically a repeater)
2. This login step is required before you can engage with the Room Server
3. Once added, the Room Server will appear as a normal contact in your mesh network
4. You can then send direct messages to the Room Server, which will be broadcast to all users in the room

When configured properly, Room Server nodes will be displayed with a distinct icon in the UI to help identify them.

## UI Components

The integration provides helper UI components that can be used to create messaging dashboards in Home Assistant.

### Messaging System Card

This card allows sending messages to either channels or specific contacts:

```yaml
type: vertical-stack
cards:
  - type: entities
    title: MeshCore Messaging
    entities:
      - entity: select.meshcore_recipient_type
        name: Send To
  - type: conditional
    conditions:
      - entity: select.meshcore_recipient_type
        state: Channel
    card:
      type: entities
      entities:
        - entity: select.meshcore_channel
          name: Channel
  - type: conditional
    conditions:
      - entity: select.meshcore_recipient_type
        state: Contact
    card:
      type: entities
      entities:
        - entity: select.meshcore_contact
          name: Contact
  - type: entities
    entities:
      - entity: text.meshcore_message
        name: Message
  - show_name: true
    show_icon: true
    type: button
    name: Send Message
    icon: mdi:send
    tap_action:
      action: call-service
      service: meshcore.send_ui_message
    icon_height: 24px
```

### Command Card

This card provides a simple interface for executing MeshCore commands:

```yaml
type: vertical-stack
cards:
  - type: entities
    entities:
      - entity: text.meshcore_command
        name: MeshCore Command
  - show_name: true
    show_icon: true
    type: button
    tap_action:
      action: call-service
      service: meshcore.execute_command_ui
    name: Execute Command
    icon: mdi:console
    icon_height: 24px
```

### MeshCore Network Map

This card displays all MeshCore contacts on a map using their location data. It requires the [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) custom card:

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_contact
      options:
        label_mod: icon
card:
  type: map
  default_zoom: 15
  label_mode: icon
```

This map will automatically display any MeshCore contacts that have location data (latitude/longitude) available. Contacts will be displayed using their appropriate icons (client, repeater, or room server), making it easy to visualize your mesh network's geographic distribution.

## Troubleshooting

### Connection Issues

- **USB Connection**: Make sure the device is properly connected and the correct port is selected. Try a different baud rate if the default doesn't work.
- **BLE Connection**: Ensure Bluetooth is enabled on your Home Assistant host. Try moving closer to the device if signal strength is low. **Note: BLE pairing over Home Assistant Bluetooth proxy is not currently working until MeshCore supports disabling the PIN requirement.**
- **TCP Connection**: Verify the hostname/IP and port are correct and that there are no firewalls blocking the connection.

### Repeater and Room Server Issues

- If repeaters or room servers aren't appearing, check that your node has correct time synchronization
- Verify the public key used for repeater/room server login is correct
- Try increasing the repeater update interval if connections are unreliable
- For room servers, make sure you've added them as repeaters first to establish the connection
- Room server messages are broadcast to all connected clients - they'll appear as direct messages from the room server
- Check the Home Assistant logs for detailed error messages related to repeater connections

### Integration Not Working

- Check the Home Assistant logs for error messages related to the MeshCore integration
- Verify that your MeshCore device is working correctly (try using the MeshCore CLI directly)
- Make sure you have the required permissions to access the device (especially for USB devices)
- Try adjusting the update intervals if you're experiencing performance issues

## Support and Development

- Report issues on GitHub
- Contributions are welcome via pull requests

## Requirements

- Home Assistant (version 2023.8.0 or newer)
- MeshCore node with firmware that supports API commands
- For BLE: Bluetooth adapter on the Home Assistant host (direct connection only; proxy connections don't work with PIN pairing)
- For USB: USB port on the Home Assistant host

## License

This project is licensed under the MIT License - see the LICENSE file for details.
