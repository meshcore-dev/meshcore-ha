---
title: Basic Repeater
sidebar_position: 3
---

# Basic Repeater Dashboard

```yaml
type: sections
max_columns: 4
title: MyRepeater (Repeater)
path: myrepeater
sections:
  - type: grid
    cards:
      - type: history-graph
        entities:
          - entity: sensor.meshcore_<pubkey>_airtime_utilization_<repeater_name>
        title: Airtime Utilization
      - type: vertical-stack
        cards:
          - type: history-graph
            entities:
              - entity: sensor.meshcore_<pubkey>_direct_dups_<repeater_name>
              - entity: sensor.meshcore_<pubkey>_flood_dups_<repeater_name>
          - type: history-graph
            entities:
              - entity: sensor.meshcore_<pubkey>_tx_queue_len_<repeater_name>
        title: Packets
  - type: grid
    cards:
      - type: vertical-stack
        cards:
          - type: history-graph
            entities:
              - entity: sensor.meshcore_<pubkey>_battery_percentage_<repeater_name>
              - entity: sensor.meshcore_<pubkey>_bat_<repeater_name>
        title: Battery
      - type: history-graph
        entities:
          - entity: sensor.meshcore_<pubkey>_airtime_<repeater_name>
        title: Airtime
  - type: grid
    cards:
      - type: vertical-stack
        cards:
          - type: history-graph
            entities:
              - entity: sensor.meshcore_<pubkey>_nb_recv_<repeater_name>
              - entity: sensor.meshcore_<pubkey>_nb_sent_<repeater_name>
          - type: history-graph
            entities:
              - entity: sensor.meshcore_<pubkey>_recv_direct_<repeater_name>
              - entity: sensor.meshcore_<pubkey>_sent_direct_<repeater_name>
          - type: history-graph
            entities:
              - entity: sensor.meshcore_<pubkey>_recv_flood_<repeater_name>
              - entity: sensor.meshcore_<pubkey>_sent_flood_<repeater_name>
        title: Messages
badges:
  - type: entity
    entity: sensor.meshcore_<pubkey>_battery_percentage_<repeater_name>
    name: Battery
  - type: entity
    entity: sensor.meshcore_<pubkey>_bat_<repeater_name>
    name: Voltage
  - type: entity
    entity: sensor.meshcore_<pubkey>_uptime_<repeater_name>
    name: Uptime
  - type: entity
    entity: binary_sensor.meshcore_<repeater_name>_<pubkey>_contact
    name: Status
```

Replace placeholders:
- `<pubkey>` with repeater's public key prefix (e.g., `03f63d160f`)
- `<repeater_name>` with repeater's name (e.g., `hyperion`)