---
title: Basic Node
sidebar_position: 2
---

# Basic Node Dashboard

```yaml
type: sections
max_columns: 4
title: MyNode (Node)
path: node
dense_section_placement: true
sections:
  - type: grid
    cards:
      - type: custom:auto-entities
        card:
          type: logbook
        filter:
          include:
            - entity_id: binary_sensor.meshcore_*_messages
          exclude: []
      - type: vertical-stack
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
  - type: grid
    cards:
      - type: custom:auto-entities
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
      - type: vertical-stack
        cards:
          - type: entities
            entities:
              - entity: text.meshcore_command
                name: CLI Command
          - show_name: true
            show_icon: true
            type: button
            tap_action:
              action: perform-action
              perform_action: meshcore.execute_command_ui
            name: Execute Command
            icon: mdi:console
            icon_height: 24px
      - type: history-graph
        entities:
          - entity: sensor.meshcore_<pubkey>_battery_percentage_<node_name>
  - type: grid
    cards:
      - type: entities
        title: Repeater Batteries
        entities:
          - entity: sensor.meshcore_<pubkey1>_battery_percentage_<repeater1>
            name: Repeater1
          - entity: sensor.meshcore_<pubkey2>_battery_percentage_<repeater2>
            name: Repeater2
      - type: custom:auto-entities
        filter:
          include:
            - device: MeshCore*
              entity_id: binary_sensor.meshcore_*_contact
              attributes:
                type: 2  # Repeaters
            - device: MeshCore*
              entity_id: binary_sensor.meshcore_*_contact
              attributes:
                type: 3  # Room Servers
        card:
          type: entities
          title: Repeaters/Rooms
          state_color: true
        sort:
          method: state
      - type: custom:auto-entities
        filter:
          include:
            - device: MeshCore*
              entity_id: binary_sensor.meshcore_*_contact
              attributes:
                type: 1  # Clients
        card:
          type: entities
          title: Clients
          state_color: true
        sort:
          method: state
badges:
  - type: entity
    entity: sensor.meshcore_<pubkey>_node_status_<node_name>
    show_name: false
  - type: entity
    entity: sensor.meshcore_<pubkey>_battery_percentage_<node_name>
    name: Battery
  - type: entity
    entity: sensor.meshcore_<pubkey>_battery_voltage_<node_name>
    name: Volts
  - type: entity
    entity: sensor.meshcore_<pubkey>_frequency_<node_name>
    name: Freq
  - type: entity
    entity: sensor.meshcore_<pubkey>_tx_power_<node_name>
    name: TX
    icon: mdi:antenna
  - type: entity
    entity: sensor.meshcore_<pubkey>_spreading_factor_<node_name>
    name: SF
    icon: mdi:video-input-antenna
  - type: entity
    entity: sensor.meshcore_<pubkey>_node_count_<node_name>
    name: Nodes
```

Replace placeholders:
- `<pubkey>` with your node's public key prefix (e.g., `a305ca`)
- `<node_name>` with your node's name (e.g., `ponybot`)
- `<repeater1>`, `<repeater2>` with your repeater names