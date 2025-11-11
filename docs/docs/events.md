---
sidebar_position: 4
title: Events
---

# Events

The Meshcore Home Assistant integration provides multiple layers of events, from raw SDK events to simplified message events designed for easy automation.

## Event Architecture

The integration provides three levels of events:

1. **First-Class Message Events** - Simplified events for common messaging use cases
2. **Raw SDK Events** - Direct access to all Meshcore SDK events
3. **Connection Events** - Integration status events

## First-Class Message Events

These events are designed for easy use in automations, with simplified field structures.

### meshcore_message
Fired when any message is received. Ideal for notifications and message logging.

**Channel Message Fields:**
- `message` - Message text
- `sender_name` - Name of sender
- `channel` - Channel type (e.g., "public")
- `channel_idx` - Channel number (0-255)
- `entity_id` - Related binary sensor entity
- `timestamp` - When received
- `message_type` - "channel"
- `pubkey_prefix` - Sender's public key prefix
- `rx_log_data` - (Optional) Array of radio reception details when message was received via multiple mesh paths:
  - `channel_idx` - Channel number
  - `channel_name` - Channel name
  - `timestamp` - Message timestamp
  - `text` - Decrypted message text
  - `snr` - Signal-to-noise ratio in dB
  - `rssi` - Received signal strength indicator
  - `path_len` - Number of hops
  - `path` - Hex-encoded path (node pubkey prefixes)
  - `channel_hash` - Channel identifier hash
  - `decrypted` - Whether decryption succeeded

**Direct Message Fields:**
- `message` - Message text
- `sender_name` - Name of sender
- `pubkey_prefix` - Sender's public key prefix
- `receiver_name` - Name of receiver
- `entity_id` - Related binary sensor entity
- `timestamp` - When received
- `message_type` - "direct"

**Example Automation:**
```yaml
alias: Forward All Messages
trigger:
  - platform: event
    event_type: meshcore_message
action:
  - service: notify.notify
    data:
      message: >
        {% if trigger.event.data.message_type == 'channel' %}
          Ch{{ trigger.event.data.channel_idx }}: {{ trigger.event.data.sender_name }}: {{ trigger.event.data.message }}
        {% else %}
          DM from {{ trigger.event.data.sender_name }}: {{ trigger.event.data.message }}
        {% endif %}
```

### meshcore_message_sent
Fired when a message is successfully sent via integration services.

**Channel Message Fields:**
- `message` - Message text sent
- `device` - Config entry ID
- `message_type` - "channel"
- `receiver` - Channel identifier (e.g., "channel_1")
- `timestamp` - Unix timestamp
- `channel_idx` - Channel number

**Direct Message Fields:**
- `message` - Message text sent
- `device` - Config entry ID
- `message_type` - "direct"
- `receiver` - Receiver name (may be null)
- `timestamp` - Unix timestamp
- `contact_public_key` - Full public key of recipient

**Example Automation:**
```yaml
alias: Log Sent Messages
trigger:
  - platform: event
    event_type: meshcore_message_sent
action:
  - service: logbook.log
    data:
      name: "Sent"
      message: "{{ trigger.event.data.message_type }}: {{ trigger.event.data.message }}"
```

## Raw SDK Events

All events from the Meshcore SDK are exposed as `meshcore_raw_event`. These provide complete access to all device data and events.

### Event Structure
Every raw event contains:
- `event_type` - The SDK event type string (e.g., "EventType.BATTERY")
- `payload` - Event-specific data structure
- `timestamp` - Unix timestamp when received

### Common Raw Event Types

#### Message Events
**EventType.CONTACT_MSG_RECV** - Direct message received
- `type` - Message type (PRIV)
- `SNR` - Signal-to-noise ratio in dB
- `pubkey_prefix` - Sender's public key prefix
- `text` - Message content
- `sender_timestamp` - When sent
- `path_len` - Routing path length

**EventType.CHANNEL_MSG_RECV** - Channel message received
- `type` - Message type (CHAN)
- `SNR` - Signal-to-noise ratio in dB
- `channel_idx` - Channel number
- `text` - Message content
- `sender_timestamp` - When sent

**EventType.MSG_SENT** - Message transmission confirmed

**EventType.RX_LOG_DATA** - Raw radio reception log
- `raw_hex` - Complete raw LoRa packet
- `snr` - Signal-to-noise ratio in dB
- `rssi` - Received signal strength indicator
- `payload` - Packet payload hex string
- `payload_length` - Length of payload
- `parsed` - Parsed packet structure:
  - `header` - Packet header byte
  - `path_len` - Number of hops
  - `path` - Routing path hex
  - `path_nodes` - Array of node pubkey prefixes
  - `channel_hash` - Channel identifier
- `decrypted` - (Optional) Decrypted GroupText payload:
  - `channel_idx` - Channel number
  - `channel_name` - Channel name
  - `timestamp` - Message timestamp
  - `text` - Decrypted message text
  - `decrypted` - Whether decryption succeeded
  - `path_len` - Number of hops
  - `path` - Routing path
  - `channel_hash` - Channel hash

:::info
RX_LOG events are automatically correlated with `meshcore_message` events. The integration decrypts GroupText payloads and attaches radio metrics (SNR, RSSI, path) to the corresponding message event as `rx_log_data`. This allows you to see which mesh routes your messages took and signal quality for each reception.
:::

#### Device Events
**EventType.BATTERY** - Battery status
- `level` - Battery level in millivolts
- `used_kb` - Memory used in KB
- `total_kb` - Total memory in KB

**EventType.DEVICE_INFO** - Device configuration
- Complete device capabilities and settings

**EventType.ERROR** - Error notifications
- Error messages and codes

#### Network Events
**EventType.CONTACTS** - Contact list updates
- Dictionary keyed by public key
- Each contact includes:
  - `type` - Node type (1=Client, 2=Repeater)
  - `adv_name` - Advertised name
  - `last_advert` - Last seen timestamp
  - `adv_lat`/`adv_lon` - GPS coordinates

**EventType.NODES** - Network topology updates

#### Telemetry Events
**EventType.TELEMETRY_RESPONSE** - Sensor data
- Cayenne LPP formatted telemetry

**EventType.STATUS_RESPONSE** - Repeater statistics
- Detailed operational metrics

### Using Raw Events
```yaml
alias: Battery Monitor
trigger:
  - platform: event
    event_type: meshcore_raw_event
    event_data:
      event_type: "EventType.BATTERY"
action:
  - service: notify.notify
    data:
      message: "Battery: {{ (trigger.event.data.payload.level / 1000) | round(2) }}V"
```

## Connection Events

### meshcore_connected
Fired when the Meshcore device connects.

### meshcore_disconnected  
Fired when the Meshcore device disconnects.

**Example:**
```yaml
alias: Connection Monitor
trigger:
  - platform: event
    event_type: meshcore_connected
  - platform: event
    event_type: meshcore_disconnected
action:
  - service: persistent_notification.create
    data:
      title: "Meshcore Status"
      message: "Device {{ 'connected' if trigger.event.event_type == 'meshcore_connected' else 'disconnected' }}"
```

## Common Automation Patterns

### Message Filtering by Channel
```yaml
alias: Channel 0 Messages Only
trigger:
  - platform: event
    event_type: meshcore_message
    event_data:
      message_type: "channel"
      channel_idx: 0
action:
  - service: notify.notify
    data:
      message: "Ch0: {{ trigger.event.data.message }}"
```

### Message Filtering by Sender
```yaml
alias: Messages from Specific Node
trigger:
  - platform: event
    event_type: meshcore_message
condition:
  - condition: template
    value_template: "{{ 'f293ac' in trigger.event.data.pubkey_prefix }}"
action:
  - service: notify.notify
    data:
      message: "{{ trigger.event.data.sender_name }}: {{ trigger.event.data.message }}"
```

### Signal Quality Monitoring
```yaml
alias: Poor Signal Alert
trigger:
  - platform: event
    event_type: meshcore_message
    event_data:
      message_type: "channel"
condition:
  - condition: template
    value_template: >
      {{ trigger.event.data.rx_log_data is defined and
         trigger.event.data.rx_log_data | selectattr('snr', 'lt', 5) | list | length > 0 }}
action:
  - service: notify.notify
    data:
      title: "Poor Signal Quality"
      message: >
        Message from {{ trigger.event.data.sender_name }} had poor signal:
        {% for rx in trigger.event.data.rx_log_data %}
        Path {{ rx.path_len }} hops: SNR {{ rx.snr }}dB, RSSI {{ rx.rssi }}
        {% endfor %}
```

### Mesh Path Monitoring
```yaml
alias: Multi-Path Message Detection
trigger:
  - platform: event
    event_type: meshcore_message
    event_data:
      message_type: "channel"
condition:
  - condition: template
    value_template: "{{ trigger.event.data.rx_log_data | length > 1 }}"
action:
  - service: logbook.log
    data:
      name: "Mesh Routing"
      message: >
        Message received via {{ trigger.event.data.rx_log_data | length }} paths:
        {% for rx in trigger.event.data.rx_log_data %}
        - {{ rx.path_len }} hops ({{ rx.path }}): SNR {{ rx.snr }}dB
        {% endfor %}
```

### Contact Discovery
```yaml
alias: New Contact Alert
trigger:
  - platform: event
    event_type: meshcore_raw_event
    event_data:
      event_type: "EventType.CONTACTS"
action:
  - service: persistent_notification.create
    data:
      title: "Contacts Updated"
      message: "Network has {{ trigger.event.data.payload | length }} contacts"
```

## Event Data Examples

### First-Class Events

#### Received Channel Message
```yaml
event_type: meshcore_message
data:
  message: "Testing channel 0"
  sender_name: "🦄"
  channel: "public"
  channel_idx: 0
  entity_id: binary_sensor.meshcore_a305ca_ch_0_messages
  timestamp: "2025-09-11T18:08:47.722967"
  message_type: "channel"
  pubkey_prefix: "f293ac8c4a71"
  rx_log_data:
    - channel_idx: 0
      channel_name: "public"
      timestamp: 1762838456
      text: "🦄: Testing channel 0"
      snr: 12.0
      rssi: -70
      path_len: 0
      path: ""
      channel_hash: "11"
      decrypted: true
    - channel_idx: 0
      channel_name: "public"
      timestamp: 1762838456
      text: "🦄: Testing channel 0"
      snr: 5.5
      rssi: -50
      path_len: 1
      path: "cf"
      channel_hash: "11"
      decrypted: true
```

#### Received Direct Message
```yaml
event_type: meshcore_message
data:
  message: "Hello there!"
  sender_name: "🦄"
  pubkey_prefix: "f293ac8c4a71"
  receiver_name: "meshcore"
  entity_id: binary_sensor.meshcore_a305ca_f293ac_messages
  timestamp: "2025-09-11T18:09:27.722298"
  message_type: "direct"
```

### Raw SDK Events

#### Battery Status
```yaml
event_type: meshcore_raw_event
data:
  event_type: EventType.BATTERY
  payload:
    level: 4069
    used_kb: 167
    total_kb: 1404
  timestamp: 1757613857.7153687
```

#### Message Received (Raw)
```yaml
event_type: meshcore_raw_event
data:
  event_type: EventType.CONTACT_MSG_RECV
  payload:
    type: PRIV
    SNR: 11.5
    pubkey_prefix: f293ac8c4a71
    path_len: 255
    txt_type: 0
    sender_timestamp: 1757613902
    text: "Test message"
  timestamp: 1757613903.7221627
```

#### Radio Reception Log
```yaml
event_type: meshcore_raw_event
data:
  event_type: EventType.RX_LOG_DATA
  payload:
    raw_hex: 32ce1501cf11e351c12442cbb78bab821ae4ab935d741e58
    snr: 12.5
    rssi: -50
    payload: 1501cf11e351c12442cbb78bab821ae4ab935d741e58
    payload_length: 22
    parsed:
      header: "15"
      path_len: 1
      path: cf
      path_nodes:
        - cf
      channel_hash: "11"
    decrypted:
      channel_idx: 0
      channel_name: Public
      timestamp: 1762838525
      text: "🦄: Test message"
      decrypted: true
      path_len: 1
      path: cf
      channel_hash: "11"
  timestamp: 1762838527.1688693
```

#### Contacts Update
```yaml
event_type: meshcore_raw_event
data:
  event_type: EventType.CONTACTS
  payload:
    f293ac8c4a712ce1a82f06aad4c40e9bc38a0860fc789c7a2f9ce106bdaff710:
      public_key: f293ac8c4a712ce1a82f06aad4c40e9bc38a0860fc789c7a2f9ce106bdaff710
      type: 1
      adv_name: "Weather Station"
      last_advert: 1757574270
      adv_lat: 45.427231
      adv_lon: -122.795721
```

## When to Use Which Event Type

### Use First-Class Message Events When:
- Building simple message notifications
- Creating message logging automations
- Filtering messages by type (channel vs direct)
- You need clean, simplified data structures

### Use Raw SDK Events When:
- Monitoring battery or device status
- Tracking network topology changes
- Accessing signal quality metrics (SNR)
- Building advanced telemetry automations
- You need complete event data

## Performance Considerations

- First-class events have simplified payloads for better performance
- Use event_data filters in triggers to reduce processing
- Consider using `mode: single` or `mode: queued` for message handlers
- Raw events contain complete SDK data - extract only needed fields

## SDK Event Reference

For a complete list of all SDK event types and their payloads, see the [Meshcore Python SDK Events documentation](https://github.com/meshcore-dev/meshcore_py/blob/main/src/meshcore/events.py).