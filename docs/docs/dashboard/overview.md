---
sidebar_position: 1
title: Overview
---

# Overview

The Meshcore Home Assistant integration provides UI components and helper entities for building custom dashboards.

## Helper Entities

The integration creates helper entities for UI interactions:

- `select.meshcore_recipient_type` - Choose between Channel or Contact
- `select.meshcore_channel` - Select channel (0-3)
- `select.meshcore_contact` - Select from available contacts
- `text.meshcore_message` - Message input field
- `text.meshcore_command` - Command input field

## Basic UI Components

### Messaging Card

A complete messaging interface for sending messages to channels or contacts:

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

### Command Interface

Execute Meshcore commands directly from the UI:

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

### Network Map

Display all Meshcore contacts on a map using their location data.

**Requirements:**
- Install [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) custom card

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

Features:
- Automatically shows contacts with GPS location
- Icons indicate node type (client, repeater, room server)
- Real-time location updates
- Adjustable zoom level

## Contact List Cards

### Simple Contact List

Display all contacts with their status:

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_contact
card:
  type: entities
  title: Mesh Contacts
```

### Contact Grid

Display contacts in a grid layout:

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_contact
card:
  type: grid
  columns: 3
  square: false
```

## Status Cards

### Device Status

Monitor your Meshcore device status:

```yaml
type: entities
title: Meshcore Status
entities:
  - entity: sensor.meshcore_abc123_battery_voltage_mynode
  - entity: sensor.meshcore_abc123_battery_percentage_mynode
  - entity: sensor.meshcore_abc123_node_count_mynode
  - entity: sensor.meshcore_abc123_tx_power_mynode
```

### Network Statistics

Track repeater network performance:

```yaml
type: entities
title: Network Stats
entities:
  - entity: sensor.meshcore_abc123_repeater1_messages_received
  - entity: sensor.meshcore_abc123_repeater1_messages_sent
  - entity: sensor.meshcore_abc123_repeater1_airtime_utilization
  - entity: sensor.meshcore_abc123_repeater1_noise_floor
```

## Message History

### Recent Messages Card

Display message history using the logbook:

```yaml
type: custom:auto-entities
card:
  type: logbook
filter:
  include:
    - entity_id: binary_sensor.meshcore_*_messages
  exclude: []
```

## Dashboard Examples

For complete dashboard configurations, see:

- [Basic Node](./basic-node) - Main node dashboard with messaging and monitoring
- [Basic Repeater](./basic-repeater) - Detailed repeater statistics and performance

## Tips for UI Development

1. **Use Conditional Cards**: Show/hide elements based on state
2. **Auto-entities**: Automatically discover and display Meshcore entities
3. **Custom Icons**: Use MDI icons for better visualization
4. **Grid Layouts**: Organize cards for different screen sizes
5. **Template Sensors**: Create custom sensors for complex data

## Mobile Optimization

For mobile-friendly dashboards:

```yaml
type: vertical-stack
cards:
  - type: custom:mushroom-chips-card
    chips:
      - type: entity
        entity: sensor.meshcore_contact_count
      - type: entity
        entity: sensor.meshcore_battery_percentage
  - type: custom:swipe-card
    cards:
      # Your message cards here
```

## Related Documentation

- [Services](../services.md) - Available services for UI actions
- [Sensors](../sensors.md) - Sensor entities for display
- [Events](../events.md) - Events for dynamic updates
- [Automation](../automation.md) - Automation examples