---
sidebar_position: 14
title: Automation
---

# Automation

The examples are valid for Home Assistant 2025.6 and later. For the event fields, see [Events](events.md). For the service fields, see [Services](services.md).

Replace these example values with your values:

| Example value | Meaning |
|---|---|
| `abc123` | The first 6 hex characters of the companion public key |
| `def456abc0` | The first 10 hex characters of the public key of a tracked node |
| `def456abc012` | The 12-character public key prefix of a contact |
| `mynode`, `myrepeater`, `myclient` | The name of the companion or the tracked node in the entity ID |
| `YOUR_ENTRY_ID`, `YOUR_DEVICE_ID` | The IDs of a companion. See [Select the entry](services.md#select-the-entry). |
| `notify.notify` | Your notify action, for example `notify.mobile_app_myphone` |

To find your entity IDs, open **Developer Tools > States** and filter on `meshcore`.

## Ignore your own messages

`meshcore_message` also fires for the messages that the send services send, with `outgoing: true`. An automation that forwards or answers messages must ignore them. If not, a reply automation answers itself. Use this condition:

```yaml
conditions:
  - condition: template
    value_template: "{{ not (trigger.event.data.outgoing | default(false)) }}"
```

## Message automations

### Notify on an incoming direct message

```yaml
alias: MeshCore direct message notification
triggers:
  - trigger: event
    event_type: meshcore_message
    event_data:
      message_type: direct
conditions:
  - condition: template
    value_template: "{{ not (trigger.event.data.outgoing | default(false)) }}"
actions:
  - action: notify.notify
    data:
      title: >-
        MeshCore message from
        {{ trigger.event.data.sender_name or trigger.event.data.pubkey_prefix }}
      message: "{{ trigger.event.data.message }}"
mode: queued
```

`sender_name` is `null` when the sender is not a contact on the companion. The title then shows the public key prefix.

### Forward all received messages

```yaml
alias: MeshCore forward to notification
description: Sends each received MeshCore message to a notification
triggers:
  - trigger: event
    event_type: meshcore_message
conditions:
  - condition: template
    value_template: "{{ not (trigger.event.data.outgoing | default(false)) }}"
actions:
  - action: notify.notify
    data:
      message: >-
        {% set sender = trigger.event.data.sender_name or 'Unknown' %}
        {% if trigger.event.data.message_type == 'channel' %}
          {{ trigger.event.data.channel }}: {{ sender }}: {{ trigger.event.data.message }}
        {% else %}
          {{ sender }}: {{ trigger.event.data.message }}
        {% endif %}
mode: queued
```

For a channel message, `sender_name` is the name before the first `: `, also when the sender is not a contact.

### Filter messages

Add one of these filters to the forward example above.

Messages on channel 1 (trigger):

```yaml
    event_data:
      message_type: channel
      channel_idx: 1
```

Messages on one companion (trigger). To filter on the device, use `device_id: YOUR_DEVICE_ID`.

```yaml
    event_data:
      entry_id: YOUR_ENTRY_ID
```

Messages from one node (condition). A channel message has `pubkey_prefix` only when the sender is a contact on the companion.

```yaml
  - condition: template
    value_template: >-
      {{ (trigger.event.data.pubkey_prefix | default('', true)).startswith('def456') }}
```

### Reply to a status request

The sender must be a contact on the companion. If not, the reply fails with `contact_not_found`. The reply goes out from the companion that received the request.

```yaml
alias: MeshCore reply to status requests
triggers:
  - trigger: event
    event_type: meshcore_message
    event_data:
      message_type: direct
conditions:
  - condition: template
    value_template: "{{ not (trigger.event.data.outgoing | default(false)) }}"
  - condition: template
    value_template: "{{ trigger.event.data.message | trim | lower == 'status' }}"
actions:
  - action: meshcore.send_message
    data:
      entry_id: "{{ trigger.event.data.entry_id }}"
      pubkey_prefix: "{{ trigger.event.data.pubkey_prefix }}"
      message: >-
        Battery {{ states('sensor.meshcore_abc123_battery_percentage_mynode') }}%,
        {{ states('sensor.meshcore_abc123_node_count_mynode') }} nodes
mode: queued
max: 5
```

## Send automations

### Alert when a send fails

Most send failures do not raise an error. They fire `meshcore_message_send_failed`.

```yaml
alias: MeshCore send failed
triggers:
  - trigger: event
    event_type: meshcore_message_send_failed
actions:
  - action: notify.notify
    data:
      title: MeshCore send failed
      message: >-
        {{ trigger.event.data.message_type }} message not sent:
        {{ trigger.event.data.reason }}
        {{ trigger.event.data.detail | default('') }}
mode: queued
```

### Continue when the traffic policy refuses a send

Under the Governed traffic policy, a send fails when the messages lane is empty, and the error stops the automation. Set `continue_on_error: true` to continue. See [Mesh Traffic Policy](traffic-policy.md).

```yaml
alias: MeshCore hourly status message
triggers:
  - trigger: time_pattern
    minutes: "0"
actions:
  - action: meshcore.send_message
    continue_on_error: true
    data:
      pubkey_prefix: "def456abc012"
      message: "Hourly status check"
  - action: logbook.log
    data:
      name: MeshCore
      message: Hourly status action completed
mode: single
```

### Log final delivery results

Each outgoing message gets one final `meshcore_delivery_update` with `progressive: false`.

```yaml
alias: MeshCore log final delivery results
triggers:
  - trigger: event
    event_type: meshcore_delivery_update
    event_data:
      outgoing: true
      progressive: false
actions:
  - action: logbook.log
    data:
      name: MeshCore delivery
      message: >-
        {% set d = trigger.event.data %}
        {% if d.message_type == 'direct' %}
          Direct message to {{ d.receiver_name or d.pubkey_prefix }}:
          {{ 'ACK received' if d.ack_received else 'no ACK' }}
        {% elif d.repeats_observable %}
          Channel {{ d.channel }}: {{ d.repeater_count }} relayed copies heard
        {% else %}
          Channel {{ d.channel }}: message too long to count relayed copies
        {% endif %}
mode: queued
```

`repeater_count` is the number of relayed copies that the companion heard. It does not confirm that other nodes received the message. See [meshcore_delivery_update](events.md#meshcore_delivery_update).

### Notify on a CLI error

```yaml
alias: MeshCore CLI error
triggers:
  - trigger: event
    event_type: meshcore_cli_response
    event_data:
      is_error: true
actions:
  - action: persistent_notification.create
    data:
      title: MeshCore CLI error
      message: "Command '{{ trigger.event.data.command }}' failed: {{ trigger.event.data.response }}"
mode: queued
```

The event fires only for commands that run with `record_to_console: true`. A firmware error has `is_error: true`. See [Response shapes](services.md#response-shapes).

## Network maintenance

### Send an advert on a schedule

This example sends a flood advert every 6 hours. Under Governed, each advert takes one credit from the flood lane. Do not send adverts often, because every repeater retransmits a flood advert.

```yaml
alias: MeshCore scheduled advert
triggers:
  - trigger: time_pattern
    hours: "/6"
    minutes: "0"
actions:
  - action: meshcore.execute_command
    data:
      command: "send_advert true"
mode: single
```

## Connection automations

### Connection lost and restored

```yaml
alias: MeshCore connection lost or restored
triggers:
  - trigger: event
    event_type: meshcore_disconnected
    event_data:
      unexpected: true
    id: lost
  - trigger: event
    event_type: meshcore_connected
    id: restored
actions:
  - action: persistent_notification.create
    data:
      title: MeshCore
      notification_id: meshcore_connection
      message: >-
        {% if trigger.id == 'lost' %}
          The link to the companion is lost. The integration tries to reconnect.
        {% else %}
          The companion is connected over {{ trigger.event.data.connection_type }}.
        {% endif %}
mode: queued
```

A reload or a shutdown fires `meshcore_disconnected` without `unexpected`, so this automation ignores it. Use these events, not the sensors, to detect a lost link: the sensors become `unavailable` when the companion disconnects.

## Sensor automations

### Companion battery low

```yaml
alias: MeshCore companion battery low
triggers:
  - trigger: numeric_state
    entity_id: sensor.meshcore_abc123_battery_percentage_mynode
    below: 20
    for:
      minutes: 10
actions:
  - action: notify.notify
    data:
      title: MeshCore companion battery low
      message: "{{ trigger.to_state.name }} is at {{ trigger.to_state.state }} %"
mode: single
```

#### Raw event variant (experimental)

The raw `BATTERY` event is experimental: the payload can change with meshcore-py releases. `level` is in mV.

```yaml
alias: MeshCore companion battery low (raw event)
triggers:
  - trigger: event
    event_type: meshcore_raw_event
    event_data:
      event_type: EventType.BATTERY
conditions:
  - condition: template
    value_template: >-
      {{ trigger.event.data.payload is mapping
         and trigger.event.data.payload.level is defined
         and trigger.event.data.payload.level < 3500 }}
actions:
  - action: notify.notify
    data:
      message: "Companion battery: {{ (trigger.event.data.payload.level / 1000) | round(2) }} V"
mode: single
```

### Tracked node stops answering

The Online sensor changes to `off` when the node does not answer for 2.5 times its update interval. See [Online sensors](sensors.md#online-sensors).

```yaml
alias: MeshCore repeater offline
triggers:
  - trigger: state
    entity_id: binary_sensor.meshcore_def456abc0_online_myrepeater
    to: "off"
    for:
      minutes: 10
actions:
  - action: notify.notify
    data:
      title: MeshCore node offline
      message: "{{ trigger.to_state.name }} does not answer"
mode: single
```

### Low battery on any tracked node

The pattern `meshcore_[0-9a-f]{10}_` selects the battery sensors of tracked nodes and other nodes that send battery telemetry. It excludes the companion.

```yaml
alias: MeshCore low battery on a tracked node
triggers:
  - trigger: template
    value_template: >-
      {% set ns = namespace(count=0) %}
      {% for id in integration_entities('meshcore')
           | select('match', 'sensor[.]meshcore_[0-9a-f]{10}_') %}
        {% if state_attr(id, 'device_class') == 'battery'
              and states(id) | is_number
              and states(id) | float < 20 %}
          {% set ns.count = ns.count + 1 %}
        {% endif %}
      {% endfor %}
      {{ ns.count > 0 }}
actions:
  - variables:
      low_nodes: >-
        {% set ns = namespace(names=[]) %}
        {% for id in integration_entities('meshcore')
             | select('match', 'sensor[.]meshcore_[0-9a-f]{10}_') %}
          {% if state_attr(id, 'device_class') == 'battery'
                and states(id) | is_number
                and states(id) | float < 20 %}
            {% set ns.names = ns.names + [state_attr(id, 'friendly_name') ~ ': ' ~ states(id) ~ ' %'] %}
          {% endif %}
        {% endfor %}
        {{ ns.names | join(', ') }}
  - action: notify.notify
    data:
      title: MeshCore low battery
      message: "{{ low_nodes }}"
mode: single
```

A template trigger fires only when its result changes from false to true. A second low node does not fire it again while the first node is still low.

### Remote temperature

```yaml
alias: MeshCore high temperature
triggers:
  - trigger: numeric_state
    entity_id: sensor.meshcore_def456abc0_ch1_temperature_myclient
    above: 30
actions:
  - action: notify.notify
    data:
      message: "Temperature on myclient: {{ trigger.to_state.state }} °C"
mode: single
```

The telemetry entity ID format is `sensor.meshcore_<pk10>_ch<n>_<type>_<name>`. See [Sensors](sensors.md).

## Signal automations

A received channel message can have `rx_log_data`: one item for each copy that the companion heard, with `snr`, `rssi`, `path_len` and `path`. See [rx_log_data entries](events.md#rx_log_data-entries).

### Weak signal alert

```yaml
alias: MeshCore weak signal
triggers:
  - trigger: event
    event_type: meshcore_message
    event_data:
      message_type: channel
conditions:
  - condition: template
    value_template: "{{ not (trigger.event.data.outgoing | default(false)) }}"
  - condition: template
    value_template: >-
      {{ trigger.event.data.rx_log_data | default([])
         | rejectattr('snr', 'none') | selectattr('snr', 'lt', 5)
         | list | count > 0 }}
actions:
  - action: notify.notify
    data:
      title: Weak MeshCore signal
      message: >-
        Message from {{ trigger.event.data.sender_name }}:
        {% for rx in trigger.event.data.rx_log_data %}
        {{ rx.path_len }} hops, SNR {{ rx.snr }} dB, RSSI {{ rx.rssi }} dBm
        {% endfor %}
mode: queued
```

### Other signal conditions

Replace the second condition of the weak signal alert with one of these.

Message heard on more than one route:

```yaml
  - condition: template
    value_template: "{{ trigger.event.data.rx_log_data | default([]) | count > 1 }}"
```

Message heard with no hops:

```yaml
  - condition: template
    value_template: >-
      {{ trigger.event.data.rx_log_data | default([])
         | selectattr('path_len', 'eq', 0) | list | count > 0 }}
```

## Tips

- Filter in the trigger with `event_data` when you can.
- Use `mode: queued` for message automations. `mode: single` drops a message that arrives while the automation runs.
- For a community example of contact management, see [MeshCore Contact Management in Home Assistant](https://github.com/WJ4IoT/Meshcore-Contact-Management-in-Home-Assistant).
