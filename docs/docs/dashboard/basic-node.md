---
title: Basic Node
sidebar_position: 2
---

# Basic Node Dashboard

A comprehensive dashboard for your MeshCore node featuring:
- **Messaging**: Send messages to channels or contacts
- **Contact Management**: Add/remove contacts with dedicated UI
- **Map View**: Visualize contact locations
- **Battery Graph**: 24-hour battery percentage trend
- **Rate Limiting**: Monitor request token availability
- **Device Batteries**: Auto-populated battery levels for all tracked devices
- **Advanced Repeater Table**: Detailed repeater statistics in tabular format
- **Command Interface**: Execute CLI commands directly

```yaml
type: sections
max_columns: 4
title: Meshcore
path: meshcore
dense_section_placement: true
sections:
  - type: grid
    cards:
      - type: vertical-stack
        cards:
          - type: custom:auto-entities
            card:
              type: logbook
            filter:
              include:
                - entity_id: binary_sensor.meshcore_*_messages
              exclude: []
          - type: entities
            entities:
              - entity: select.meshcore_recipient_type
                name: Send To
              - entity: select.meshcore_channel
                name: Channel
              - entity: select.meshcore_contact
                name: Contact
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
        title: MeshCore Messaging
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
          - type: markdown
            content: >-
              Execute CLI commands on your MeshCore node.

              [📖 Docs](https://meshcore-dev.github.io/meshcore-ha/docs/ha/services#execute-command)

              [📖 SDK commands](https://github.com/meshcore-dev/meshcore_py?tab=readme-ov-file#available-commands)
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
      - type: custom:apexcharts-card
        header:
          show: true
          title: Battery (24h)
          show_states: true
        graph_span: 24h
        series:
          - entity: sensor.meshcore_<pubkey>_battery_percentage_<node_name>
            name: Battery
            stroke_width: 2
            color: '#4caf50'
        apex_config:
          chart:
            height: 200
          yaxis:
            min: 0
            max: 100
  - type: grid
    cards:
      - type: entities
        title: Manage Contacts
        entities:
          - entity: select.meshcore_discovered_contact
            name: Discovered
            secondary_info: last-changed
          - type: button
            name: ➕ Add Contact
            action_name: Add
            tap_action:
              action: call-service
              service: meshcore.add_selected_contact
          - entity: select.meshcore_added_contact
            name: Added
            secondary_info: last-changed
          - type: button
            name: ➖ Remove Contact
            action_name: Remove
            tap_action:
              action: call-service
              service: meshcore.remove_selected_contact
      - type: custom:auto-entities
        filter:
          include:
            - entity_id: sensor.meshcore_*_battery_percentage*
        card:
          type: entities
          title: Device Batteries
          state_color: true
        sort:
          method: state
          numeric: true
      - type: custom:apexcharts-card
        header:
          show: true
          title: Rate Limiter (24h)
          show_states: true
        graph_span: 24h
        series:
          - entity: sensor.meshcore_<pubkey>_rate_limiter_tokens_<node_name>
            name: Tokens Available
            stroke_width: 1
            color: orange
        apex_config:
          chart:
            height: 200
          yaxis:
            min: 0
            max: 20
  - type: grid
    columns: 2
    cards:
      - type: custom:flex-table-card
        title: Repeater Statistics
        entities:
          include: sensor.meshcore_*_repeater*_battery_percentage*
        columns:
          - name: Repeater
            data: friendly_name
            modify: x.replace('Battery Percentage', '').replace('MeshCore', '').trim()
          - name: Battery
            data: state
            suffix: '%'
            align: center
          - name: Success
            data: request_successes
            align: center
          - name: Failed
            data: request_failures
            align: center
          - name: SNR
            data: last_snr
            suffix: ' dB'
            align: center
          - name: Path
            data: path
            modify: x.split(',').join(' → ')
        sort_by: friendly_name+
        css:
          table+: 'font-size: 12px;'
          th+: 'background-color: var(--primary-color); color: white;'
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

## Setup Instructions

### Prerequisites
This dashboard requires these custom cards from HACS:
- **auto-entities**: Automatically populate cards with entities matching filters
- **apexcharts-card**: Display battery and rate limiter history as line graphs
- **flex-table-card**: Display repeater statistics in a custom table format

### Configuration Steps

1. Replace placeholders:
   - `<pubkey>` with your node's public key prefix (e.g., `a305ca`)
   - `<node_name>` with your node's name (e.g., `ponybot`)

2. The dashboard automatically discovers:
   - All device batteries (repeaters and clients) sorted by battery level
   - All contacts (repeaters, room servers, and clients)
   - Battery percentage displayed as a 24-hour trend graph
   - Rate limiting token availability displayed as a 24-hour trend graph
   - Repeater statistics in a detailed table view

3. Contact management:
   - Select a discovered contact and click "➕ Add Contact" to add it to your node
   - Select an added contact and click "➖ Remove Contact" to remove it from your node

4. CLI commands:
   - Enter any MeshCore CLI command in the text field
   - Click "Execute Command" to run it
   - View available commands in the documentation links

## Advanced: Repeater Table with Sparklines

For an even more advanced view with sparklines showing battery trends, use this alternative:

```yaml
  - type: grid
    columns: 2
    cards:
      - type: custom:flex-table-card
        title: Repeater Statistics
        entities:
          include: sensor.meshcore_*_repeater*_battery_percentage*
        columns:
          - name: Repeater
            data: friendly_name
            modify: x.replace('Battery Percentage', '').replace('MeshCore', '').trim()
          - name: ''
            data: entity
            modify: '''<ha-chart-base entity="'' + x + ''" height="40" sparkline></ha-chart-base>'''
          - name: Battery
            data: state
            suffix: '%'
            align: center
          - name: ✓
            data: request_successes
            align: center
          - name: ✗
            data: request_failures
            align: center
          - name: SNR
            data: last_snr
            suffix: ' dB'
            align: center
          - name: Path
            data: path
            modify: x ? x.split(',').join(' → ') : 'Direct'
        sort_by: state-
        css:
          table+: 'font-size: 12px; width: 100%;'
          th+: 'background-color: var(--primary-color); color: white; padding: 8px;'
          td+: 'padding: 4px;'
```

This version includes:
- **Sparkline column**: Shows battery trend over time
- **Sorted by battery**: Lowest battery first (descending)
- **Compact symbols**: ✓/✗ for success/failed
- **Path formatting**: Converts comma-separated to arrows
- **Full width**: Utilizes extra-wide space
