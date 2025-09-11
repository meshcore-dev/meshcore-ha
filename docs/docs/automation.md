---
sidebar_position: 6
title: Automation
---

# Automation

The Meshcore Home Assistant integration provides rich automation capabilities through events, services, and sensors.

## Message Automations

### Forward Messages to Push Notifications

Forward all Meshcore messages to your mobile device:

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

### Channel-Specific Notifications

Monitor only specific channels:

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

### Filter Messages by Sender

Get notifications only from specific nodes:

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

## Network Maintenance

### Scheduled Advertisement Broadcasting

Keep your node discoverable by sending periodic advertisements:

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
      kwargs: {}
mode: single
```

## Sensor Monitoring

### Temperature Alerts

Monitor environmental sensors from telemetry:

```yaml
alias: High Temperature Alert
trigger:
  - platform: numeric_state
    entity_id: sensor.meshcore_def456_sensor1_ch1_temperature
    above: 30
action:
  - service: notify.notify
    data:
      message: "Temperature alert: {{ states(trigger.entity_id) }}°C"
```

### Repeater Battery Monitoring

Monitor repeater stations for low battery:

```yaml
alias: Repeater Low Battery
trigger:
  - platform: numeric_state
    entity_id: sensor.meshcore_abc123_repeater1_battery_percentage
    below: 20
action:
  - service: notify.notify
    data:
      title: "Repeater Battery Low"
      message: "{{ state_attr(trigger.entity_id, 'friendly_name') }} at {{ states(trigger.entity_id) }}%"
```

## Connection Monitoring

### Node Offline Detection

Get notified when nodes stop responding:

```yaml
alias: Node Went Offline
trigger:
  - platform: state
    entity_id: sensor.meshcore_abc123_repeater1_uptime
    to: 'unavailable'
    for:
      minutes: 10
action:
  - service: notify.notify
    data:
      title: "Node Offline"
      message: "{{ state_attr(trigger.entity_id, 'friendly_name') }} is not responding"
```

### Connection Status Monitoring

Track when your Meshcore device connects or disconnects:

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

## Signal Quality

### Poor Signal Alert

Monitor signal quality and alert on degradation:

```yaml
alias: Poor Signal Alert
trigger:
  - platform: event
    event_type: meshcore_raw_event
    event_data:
      event_type: "EventType.CONTACT_MSG_RECV"
condition:
  - condition: template
    value_template: "{{ trigger.event.data.payload.SNR < 5 }}"
action:
  - service: notify.notify
    data:
      message: "Poor signal from {{ trigger.event.data.payload.name }}: SNR {{ trigger.event.data.payload.SNR }}"
```

## Raw Event Monitoring

### Battery Event Tracking

Monitor battery updates from the raw event stream:

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

## Message Logging

### Log Sent Messages

Keep track of all messages sent through the integration:

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

## Tips for Automations

1. **Use Event Filters**: Filter events in the trigger to reduce processing
2. **Check Conditions**: Use conditions to further refine when automations run
3. **Mode Selection**: Use `mode: single` to prevent duplicate executions
4. **Template Sensors**: Create template sensors for complex calculations
5. **Combine Triggers**: Use multiple triggers for related events

## Advanced Automation

For complex contact management and automation workflows, see the community example:
[Meshcore Contact Management in Home Assistant](https://github.com/WJ4IoT/Meshcore-Contact-Management-in-Home-Assistant)

## Related Documentation

- [Events](./events.md) - Complete event reference
- [Services](./services.md) - Available services for automations
- [Sensors](./sensors.md) - Sensor entities for triggers
- [Messaging](./messaging.md) - Message handling details