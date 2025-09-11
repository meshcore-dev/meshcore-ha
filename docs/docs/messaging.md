---
sidebar_position: 5
title: Messaging
---

# Messaging

The Meshcore Home Assistant integration provides comprehensive messaging capabilities for your mesh network, including sending, receiving, and logging messages.

## Message Flow

### Sending Messages

Messages can be sent using the integration's [services](./services.md#send-message):

1. **Direct Messages** - Send to specific nodes by name or public key
2. **Channel Messages** - Broadcast to all nodes on a channel

When you send a message:
1. The service validates the recipient and message
2. The message is transmitted via the Meshcore device
3. A `meshcore_message_sent` event is fired
4. The message appears in the Home Assistant logbook

### Receiving Messages

When messages are received:
1. Raw SDK events (`EventType.CONTACT_MSG_RECV` or `EventType.CHANNEL_MSG_RECV`) are processed
2. Contact information is resolved (name lookup from public key)
3. A `meshcore_message` event is fired with simplified data
4. The message is logged to the Home Assistant logbook
5. Binary sensor entities track message activity

## Logbook Integration

All messages automatically appear in the Home Assistant logbook with appropriate formatting and icons.

### Message Format in Logbook

#### Channel Messages
Display with channel prefix and sender name:
- **Format**: `<channel> Sender: Message`
- **Icon**: `mdi:message-bulleted`
- **Examples**:
  - `<public> PonyBot: back at you`
  - `<public> 🦄: Ignore this testing 2`
  - `<public> Iris03: Good morning Tigard.`

Channel 0 displays as `<public>`, other channels show as `<1>`, `<2>`, etc.

#### Direct Messages
Display as simple sender and message:
- **Format**: `Sender: Message`
- **Icon**: `mdi:message-text`
- **Examples**:
  - `PonyBot: test`
  - `🦄: Test 2`
  - `Weather Station: Temperature 72°F`

### Outgoing Messages
When your node (e.g., "PonyBot") sends messages, they appear in the logbook with your node name as the sender:
- Channel: `<public> PonyBot: Your message here`
- Direct: `PonyBot: Your reply here`

### Logbook Features

- **Automatic Sender Resolution** - Public keys are resolved to friendly names
- **Emoji Support** - Full support for emoji in node names and messages
- **Channel Identification** - Channel 0 shows as "public"
- **Message Truncation** - Long messages are truncated with "..." in debug logs
- **Timestamp Tracking** - Shows relative time (e.g., "1 minute ago", "3 hours ago")
- **Date Grouping** - Messages grouped by date in the logbook
- **Entity Linking** - Messages link to their binary sensor entities

## Message Events

The integration provides two types of message events for automations:

### meshcore_message Event
Fired when any message is received. See [Events documentation](./events.md#meshcore_message) for field details.

**Key Fields:**
- `message` - The message text
- `sender_name` - Resolved sender name (e.g., "🦄", "PonyBot")
- `message_type` - "channel" or "direct"
- `entity_id` - Related binary sensor

### meshcore_message_sent Event
Fired when a message is sent. See [Events documentation](./events.md#meshcore_message_sent) for field details.

**Key Fields:**
- `message` - The sent message
- `receiver` - Recipient name or channel
- `message_type` - "channel" or "direct"

## Binary Sensor Entities

Message activity creates binary sensor entities that track communication:

### Channel Message Sensors
- **Entity ID**: `binary_sensor.meshcore_<device_pubkey>_ch_<number>_messages`
- **Example**: `binary_sensor.meshcore_a305ca_ch_0_messages`
- **Created**: On first message in channel
- **State**: Always "Active" when messages exist
- **Attributes**: Channel index

### Contact Message Sensors
- **Entity ID**: `binary_sensor.meshcore_<device_pubkey>_<contact_pubkey>_messages`
- **Example**: `binary_sensor.meshcore_a305ca_f293ac_messages`
- **Created**: On first message from contact
- **State**: Always "Active" when messages exist
- **Attributes**: Public key

## Message Services

Send messages using these services:

### Send Direct Message
```yaml
service: meshcore.send_message
data:
  node_id: "🦄"
  message: "Hello from PonyBot!"
```

### Send Channel Message
```yaml
service: meshcore.send_channel_message
data:
  channel_idx: 0  # Public channel
  message: "Good morning mesh!"
```

See [Services documentation](./services.md) for complete service details.

## Automation Examples

### Forward Messages to Notifications
```yaml
alias: Mesh Message Notifications
trigger:
  - platform: event
    event_type: meshcore_message
action:
  - service: notify.mobile_app
    data:
      title: >
        {% if trigger.event.data.message_type == 'channel' %}
          Mesh Channel {{ trigger.event.data.channel_idx }}
        {% else %}
          DM from {{ trigger.event.data.sender_name }}
        {% endif %}
      message: "{{ trigger.event.data.message }}"
```

### Auto-Reply to Direct Messages
```yaml
alias: Auto Reply to Status Requests
trigger:
  - platform: event
    event_type: meshcore_message
    event_data:
      message_type: "direct"
condition:
  - condition: template
    value_template: "{{ 'status' in trigger.event.data.message.lower() }}"
action:
  - service: meshcore.send_message
    data:
      pubkey_prefix: "{{ trigger.event.data.pubkey_prefix }}"
      message: "PonyBot Status: Online, Battery: 95%, Temp: 72°F"
```

### Morning Greeting
```yaml
alias: Morning Mesh Greeting
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: meshcore.send_channel_message
    data:
      channel_idx: 0
      message: "Good morning mesh! ☀️"
```

### Message Rate Limiting
```yaml
alias: Hourly Status Broadcast
trigger:
  - platform: time_pattern
    hours: "*"
    minutes: "0"
action:
  - service: meshcore.send_channel_message
    data:
      channel_idx: 0
      message: >
        PonyBot Status: {{ states('sensor.meshcore_battery_percentage') }}% battery,
        {{ states('sensor.meshcore_node_count') }} nodes online
```

## Message Filtering

### By Specific Sender
```yaml
trigger:
  - platform: event
    event_type: meshcore_message
condition:
  - condition: template
    value_template: "{{ trigger.event.data.sender_name == '🦄' }}"
```

### By Channel
```yaml
trigger:
  - platform: event
    event_type: meshcore_message
    event_data:
      message_type: "channel"
      channel_idx: 0  # Public channel
```

### By Message Content
```yaml
trigger:
  - platform: event
    event_type: meshcore_message
condition:
  - condition: template
    value_template: "{{ 'test' in trigger.event.data.message.lower() }}"
```

### Exclude Own Messages
```yaml
trigger:
  - platform: event
    event_type: meshcore_message
condition:
  - condition: template
    value_template: "{{ trigger.event.data.sender_name != 'PonyBot' }}"
```

## Message History

### Viewing in Logbook
1. Navigate to **History** in Home Assistant
2. Select the **Logbook** tab
3. Filter by "Meshcore" domain to see only mesh messages
4. Messages show with relative timestamps and are grouped by date

### Recent Messages Example
```
<public> PonyBot: back at you
11:17:11 AM - 1 minute ago

<public> 🦄: Ignore this testing 2
11:08:47 AM - 9 minutes ago

<public> Roamer 2: Ack
10:17:55 AM - 1 hour ago

<public> Iris03: Good morning Tigard.
8:31:03 AM - 3 hours ago
```

### Querying via Templates
```yaml
# Count today's messages
{{ states.binary_sensor 
   | selectattr('entity_id', 'match', 'binary_sensor.meshcore.*messages')
   | list | length }}

# Check if specific contact sent messages
{{ states('binary_sensor.meshcore_abc123_messages') }}
```

## Performance Considerations

### Message Processing
- Messages are processed asynchronously to avoid blocking
- Sender name resolution is cached for performance
- Long messages (>50 chars) are truncated in debug logs only

### Event Handling
- Use event filters to reduce automation triggers
- Consider using `mode: queued` for message handlers
- Batch message processing when handling multiple messages

### Binary Sensors
- Created dynamically on first message
- Minimal state changes (always "Active")
- Use attributes for additional data without state changes

## Troubleshooting

### Messages Not Appearing in Logbook
- Verify the Meshcore device is connected
- Check that the sender exists in contacts
- Review debug logs for processing errors

### Missing Sender Names
- Sender must be in the contact list for name resolution
- Unknown senders show as "Unknown (pubkey)"
- Channel messages extract sender from "Name: Message" format

### Binary Sensors Not Created
- Sensors are created on first message only
- Check entity registry for existing sensors
- Verify entity naming follows the pattern

### Own Messages Not Showing
- Ensure your node name is configured correctly
- Check that message send services complete successfully
- Verify `meshcore_message_sent` events are firing

## Related Documentation

- [Services](./services.md) - Sending messages
- [Events](./events.md) - Message event details
- [Automation](./automation.md) - Message automation examples