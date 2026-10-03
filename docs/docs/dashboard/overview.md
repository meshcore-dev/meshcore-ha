---
sidebar_position: 1
title: Dashboard cards
---

# Dashboard cards

This page gives small cards. For full dashboards, see [Basic Node](basic-node.md) and [Basic Repeater](basic-repeater.md). Replace `abc123`, `def456abc0`, `mynode` and `myrepeater` with your values from **Developer Tools > States**. For all entity ID formats, see [Sensors](../sensors.md). The `custom:auto-entities` cards need the [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) custom card from HACS.

## Helper entities

These helpers exist for each entry. They are hidden by default and are always available.

| Entity | Use |
|---|---|
| `select.meshcore_recipient_type` | Select `Channel` or `Contact` |
| `select.meshcore_channel` | Select a channel, as `<channel name> (<index>)` |
| `select.meshcore_contact` | Select an added contact that is not a repeater |
| `select.meshcore_discovered_contact` | Select a discovered contact to add |
| `select.meshcore_added_contact` | Select an added contact to remove |
| `text.meshcore_message` | Message text (200 characters maximum) |
| `text.meshcore_command` | Command text (255 characters maximum) |

### Two or more entries

The helpers of the second entry get a suffix such as `_2`, for example `select.meshcore_channel_2`. Each card must use the helpers and the `entry_id` of its own entry.

- Without `entry_id`, `send_ui_message`, `execute_command_ui`, `add_selected_contact` and `remove_selected_contact` fail with `ambiguous_config_entry`.

See [Two or more companions](../multiple-companions.md).

## Messaging card

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
      - condition: state
        entity: select.meshcore_recipient_type
        state: Channel
    card:
      type: entities
      entities:
        - entity: select.meshcore_channel
          name: Channel
  - type: conditional
    conditions:
      - condition: state
        entity: select.meshcore_recipient_type
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
  - type: button
    name: Send Message
    icon: mdi:send
    show_name: true
    show_icon: true
    icon_height: 24px
    tap_action:
      action: perform-action
      perform_action: meshcore.send_ui_message
      # Required only with two or more entries:
      # data:
      #   entry_id: YOUR_ENTRY_ID
```

## Command card

This card runs the command in `text.meshcore_command`. See [CLI Command Reference](../cli-commands.md).

```yaml
type: vertical-stack
cards:
  - type: entities
    entities:
      - entity: text.meshcore_command
        name: MeshCore Command
  - type: button
    name: Execute Command
    icon: mdi:console
    show_name: true
    show_icon: true
    icon_height: 24px
    tap_action:
      action: perform-action
      perform_action: meshcore.execute_command_ui
      # Required only with two or more entries:
      # data:
      #   entry_id: YOUR_ENTRY_ID
```

This card does not show the response. To see the response, use the [CLI Console](#cli-console). Only an administrator can run commands.

## CLI Console

The CLI Console shows each command and its response. To enable it, set **Enable CLI Console** in **Global Settings**. The integration then creates `sensor.meshcore_abc123_cli_console` and the run and clear buttons. They are hidden. Use their entity IDs in the card. For the second entry, use its own command helper, buttons and sensor.

````yaml
type: vertical-stack
cards:
  - type: entities
    entities:
      - entity: text.meshcore_command
        name: Command
      - entity: button.meshcore_abc123_cli_run
        name: Run
      - entity: button.meshcore_abc123_cli_clear
        name: Clear
  - type: markdown
    content: |
      ```
      {{ state_attr('sensor.meshcore_abc123_cli_console', 'transcript') }}
      ```
````

Use a literal block (`|`) for the markdown `content`. A folded block (`>-`) puts the transcript on one line.

To record a command from an automation, add `record_to_console: true` to `meshcore.execute_command`.

## Network map

This card shows the contacts that advertise a position.

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_contact
      options:
        label_mode: icon
card:
  type: map
  default_zoom: 15
```

A contact with an advertised coordinate of 0 has no `latitude` or `longitude` attribute and does not show on the map.

## Contact lists

### Simple contact list

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_contact
sort:
  method: name
card:
  type: entities
  title: Mesh Contacts
```

### Contact grid

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_contact
      options:
        type: tile
sort:
  method: name
card:
  type: grid
  columns: 3
  square: false
card_param: cards
```

## Status cards

### Companion status

```yaml
type: entities
title: MeshCore Status
entities:
  - entity: sensor.meshcore_abc123_node_status_mynode
  - entity: sensor.meshcore_abc123_battery_voltage_mynode
  - entity: sensor.meshcore_abc123_battery_percentage_mynode
  - entity: sensor.meshcore_abc123_node_count_mynode
  - entity: sensor.meshcore_abc123_tx_power_mynode
  - entity: sensor.meshcore_abc123_rate_limiter_tokens_mynode
  - entity: sensor.meshcore_abc123_last_message_delivery_mynode
```

### Repeater statistics

```yaml
type: entities
title: myrepeater
entities:
  - entity: binary_sensor.meshcore_def456abc0_online_myrepeater
  - entity: sensor.meshcore_def456abc0_nb_recv_myrepeater
  - entity: sensor.meshcore_def456abc0_nb_sent_myrepeater
  - entity: sensor.meshcore_def456abc0_airtime_utilization_myrepeater
  - entity: sensor.meshcore_def456abc0_noise_floor_myrepeater
  - entity: sensor.meshcore_def456abc0_out_path_len_myrepeater
```

## Message history

### All messages

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      entity_id: binary_sensor.meshcore_*_messages
card:
  type: logbook
  hours_to_show: 24
```

### One channel

This card shows channel 0 when `select.meshcore_channel` selects it. The `state` must match the option text exactly.

```yaml
type: logbook
hours_to_show: 24
target:
  entity_id:
    - binary_sensor.meshcore_abc123_ch_0_messages
visibility:
  - condition: state
    entity: select.meshcore_channel
    state: Public (0)
```

Make one card for each channel. A channel message sensor exists only after the first message on that channel.

## Compact status for phones

This card requires the [Mushroom](https://github.com/piitaya/lovelace-mushroom) custom cards from HACS.

```yaml
type: custom:mushroom-chips-card
chips:
  - type: entity
    entity: sensor.meshcore_abc123_node_status_mynode
  - type: entity
    entity: sensor.meshcore_abc123_battery_percentage_mynode
  - type: entity
    entity: sensor.meshcore_abc123_node_count_mynode
    icon: mdi:account-group
```
