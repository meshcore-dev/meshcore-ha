---
title: Basic Node
sidebar_position: 2
---

# Basic Node Dashboard

This dashboard shows one companion and the nodes that it tracks: messaging, map, commands, contact management, batteries, rate limiter history and a repeater table. It needs the [auto-entities](https://github.com/thomasloven/lovelace-auto-entities) custom card from HACS.

## Dashboard YAML

1. Go to **Settings > Dashboards**.
2. Add a new dashboard from scratch.
3. Open the dashboard.
4. Select the pencil icon.
5. In the three-dot menu, select **Raw configuration editor**.
6. Paste the YAML.
7. Replace `abc123` (companion prefix) and `mynode` (companion name) with your values. To find them, filter **Developer Tools > States** on `node_status`.
8. Select **Save**.

```yaml
title: MeshCore
views:
  - type: sections
    max_columns: 4
    title: MeshCore
    path: meshcore
    dense_section_placement: true
    badges:
      - type: entity
        entity: sensor.meshcore_abc123_node_status_mynode
        show_name: false
      - type: entity
        entity: sensor.meshcore_abc123_battery_percentage_mynode
        name: Battery
      - type: entity
        entity: sensor.meshcore_abc123_battery_voltage_mynode
        name: Volts
      - type: entity
        entity: sensor.meshcore_abc123_frequency_mynode
        name: Freq
      - type: entity
        entity: sensor.meshcore_abc123_tx_power_mynode
        name: TX
        icon: mdi:antenna
      - type: entity
        entity: sensor.meshcore_abc123_spreading_factor_mynode
        name: SF
        icon: mdi:video-input-antenna
      - type: entity
        entity: sensor.meshcore_abc123_node_count_mynode
        name: Nodes
    sections:
      - type: grid
        cards:
          - type: heading
            heading: Messaging
          - type: custom:auto-entities
            filter:
              include:
                - integration: meshcore
                  entity_id: binary_sensor.meshcore_*_messages
            card:
              type: logbook
              hours_to_show: 24
          - type: entities
            entities:
              - entity: select.meshcore_recipient_type
                name: Send To
              - type: conditional
                conditions:
                  - condition: state
                    entity: select.meshcore_recipient_type
                    state: Channel
                row:
                  entity: select.meshcore_channel
                  name: Channel
              - type: conditional
                conditions:
                  - condition: state
                    entity: select.meshcore_recipient_type
                    state: Contact
                row:
                  entity: select.meshcore_contact
                  name: Contact
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
          - type: entities
            entities:
              - entity: sensor.meshcore_abc123_last_message_delivery_mynode
                name: Last delivery
      - type: grid
        cards:
          - type: heading
            heading: Map and commands
          - type: custom:auto-entities
            filter:
              include:
                - integration: meshcore
                  entity_id: binary_sensor.meshcore_*_contact
                  options:
                    label_mode: icon
            card:
              type: map
              default_zoom: 15
          - type: markdown
            content: >-
              Run a command on the companion. See the
              [CLI Command Reference](https://meshcore-dev.github.io/meshcore-ha/docs/ha/cli-commands)
              page for the syntax.
          - type: entities
            entities:
              - entity: text.meshcore_command
                name: CLI Command
          - type: button
            name: Execute Command
            icon: mdi:console
            show_name: true
            show_icon: true
            icon_height: 24px
            tap_action:
              action: perform-action
              perform_action: meshcore.execute_command_ui
          - type: history-graph
            title: Battery (24 h)
            hours_to_show: 24
            entities:
              - entity: sensor.meshcore_abc123_battery_percentage_mynode
                name: Battery
      - type: grid
        cards:
          - type: heading
            heading: Contacts and batteries
          - type: entities
            title: Manage Contacts
            entities:
              - entity: select.meshcore_discovered_contact
                name: Discovered
              - type: button
                name: Add Contact
                icon: mdi:account-plus
                action_name: Add
                tap_action:
                  action: perform-action
                  perform_action: meshcore.add_selected_contact
              - entity: select.meshcore_added_contact
                name: Added
              - type: button
                name: Remove Contact
                icon: mdi:account-minus
                action_name: Remove
                tap_action:
                  action: perform-action
                  perform_action: meshcore.remove_selected_contact
          - type: custom:auto-entities
            filter:
              include:
                - integration: meshcore
                  domain: sensor
                  attributes:
                    device_class: battery
            sort:
              method: state
              numeric: true
            card:
              type: entities
              title: Device Batteries
              state_color: true
          - type: history-graph
            title: Rate Limiter (24 h)
            hours_to_show: 24
            entities:
              - entity: sensor.meshcore_abc123_rate_limiter_tokens_mynode
                name: Credits available
      - type: grid
        column_span: 2
        cards:
          - type: heading
            heading: Repeaters
          - type: markdown
            title: Repeater Statistics
            content: |
              | Repeater | Battery | Online | OK | Failed | SNR | Hops | Route |
              |:--|--:|:-:|--:|--:|--:|--:|:--|
              {% for s in states.sensor
                   | selectattr('entity_id', 'match', 'sensor.meshcore_[0-9a-f]{10}_battery_percentage_')
                   | sort(attribute='name') -%}
              {%- set id = s.entity_id -%}
              {%- set name_part = id.split('_battery_percentage_')[1] -%}
              {%- set pk = id.split('_')[1] -%}
              {%- set ok = states(id.replace('_battery_percentage_', '_request_successes_')) -%}
              {%- set bad = states(id.replace('_battery_percentage_', '_request_failures_')) -%}
              {%- set snr = states(id.replace('_battery_percentage_', '_last_snr_')) -%}
              {%- set hops = states(id.replace('_battery_percentage_', '_out_path_len_')) -%}
              {%- set route = states(id.replace('_battery_percentage_', '_out_path_')) -%}
              {%- set online = states('binary_sensor.meshcore_' ~ pk ~ '_online_' ~ name_part) -%}
              | {{ device_attr(id, 'name') | replace('MeshCore Repeater: ', '') }} | {{ (s.state | float(0)) | round(0) | int ~ ' %' if s.state | is_number else '-' }} | {{ online }} | {{ ok }} | {{ bad }} | {{ snr ~ ' dB' if snr | is_number else '-' }} | {{ hops if hops | is_number else '-' }} | {% if not hops | is_number %}no route{% elif hops | int == 0 %}direct{% else %}{% set w = (route | length) // (hops | int) %}{% for i in range(hops | int) %}{{ route[i * w:(i + 1) * w] }}{{ ' → ' if not loop.last else '' }}{% endfor %}{% endif %} |
              {% endfor %}
```

## Notes

- A message sensor exists only after the first message on that channel or with that contact.
- **Add**, **Remove** and **Execute Command** call admin services. Only an administrator can use them.
- The repeater table finds each repeater through `sensor.meshcore_<pk10>_battery_percentage_<name>`. To add a column, replace `_battery_percentage_` with another [repeater sensor](../sensors.md#repeater-sensors) key, for example `_noise_floor_`.
- In the **Route** column, `no route` means that the companion has no route. `direct` means that it hears the repeater directly.

### Two or more entries

Make one dashboard for each entry.

1. Replace each helper ID with the ID of entry 2, for example `select.meshcore_channel_2`.
2. Add `entry_id` to the 4 button actions: **Send Message**, **Execute Command**, **Add** and **Remove**.
3. Replace `abc123` and `mynode` with the values of the second companion.

```yaml
tap_action:
  action: perform-action
  perform_action: meshcore.send_ui_message
  data:
    entry_id: YOUR_ENTRY_ID
```

See [Two or more entries](overview.md#two-or-more-entries).

## Battery history for all battery sensors

This card shows every MeshCore battery sensor in one graph.

```yaml
type: custom:auto-entities
filter:
  include:
    - integration: meshcore
      domain: sensor
      attributes:
        device_class: battery
card:
  type: history-graph
  title: Battery history (48 h)
  hours_to_show: 48
```
