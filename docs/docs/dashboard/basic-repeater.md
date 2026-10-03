---
title: Basic Repeater
sidebar_position: 3
---

# Basic Repeater Dashboard

This dashboard shows the status and the traffic of one tracked repeater. It uses only standard cards.

## Dashboard YAML

1. Go to **Settings > Dashboards**.
2. Add a new dashboard from scratch.
3. Open the dashboard.
4. Select the pencil icon.
5. In the three-dot menu, select **Raw configuration editor**.
6. Paste the YAML.
7. Replace `def456abc0` (repeater prefix, 10 characters), `def456abc012` (contact sensor prefix, 12 characters) and `myrepeater` with your values. To find them, filter **Developer Tools > States** on `_bat_`.
8. Select **Save**.

```yaml
title: myrepeater
views:
  - type: sections
    max_columns: 4
    title: myrepeater (Repeater)
    path: myrepeater
    badges:
      - type: entity
        entity: binary_sensor.meshcore_def456abc0_online_myrepeater
        name: Online
      - type: entity
        entity: sensor.meshcore_def456abc0_battery_percentage_myrepeater
        name: Battery
      - type: entity
        entity: sensor.meshcore_def456abc0_bat_myrepeater
        name: Voltage
      - type: entity
        entity: sensor.meshcore_def456abc0_uptime_myrepeater
        name: Uptime
      - type: entity
        entity: sensor.meshcore_def456abc0_out_path_len_myrepeater
        name: Hops
      - type: entity
        entity: binary_sensor.meshcore_myrepeater_def456abc012_contact
        name: Advert
    sections:
      - type: grid
        cards:
          - type: heading
            heading: Radio
          - type: history-graph
            title: Airtime Utilization
            entities:
              - entity: sensor.meshcore_def456abc0_airtime_utilization_myrepeater
              - entity: sensor.meshcore_def456abc0_rx_airtime_utilization_myrepeater
          - type: history-graph
            title: Signal
            entities:
              - entity: sensor.meshcore_def456abc0_noise_floor_myrepeater
              - entity: sensor.meshcore_def456abc0_last_rssi_myrepeater
          - type: history-graph
            title: Last SNR
            entities:
              - entity: sensor.meshcore_def456abc0_last_snr_myrepeater
      - type: grid
        cards:
          - type: heading
            heading: Power and requests
          - type: history-graph
            title: Battery
            entities:
              - entity: sensor.meshcore_def456abc0_battery_percentage_myrepeater
          - type: history-graph
            title: Voltage
            entities:
              - entity: sensor.meshcore_def456abc0_bat_myrepeater
          - type: entities
            title: Requests from Home Assistant
            entities:
              - entity: sensor.meshcore_def456abc0_request_successes_myrepeater
                name: Successes
              - entity: sensor.meshcore_def456abc0_request_failures_myrepeater
                name: Failures
              - entity: sensor.meshcore_def456abc0_out_path_myrepeater
                name: Route
              - entity: button.meshcore_def456abc0_refresh_firmware
                name: Refresh firmware version
      - type: grid
        cards:
          - type: heading
            heading: Messages (msg/min)
          - type: history-graph
            title: All messages
            entities:
              - entity: sensor.meshcore_def456abc0_nb_recv_rate_myrepeater
              - entity: sensor.meshcore_def456abc0_nb_sent_rate_myrepeater
          - type: history-graph
            title: Direct
            entities:
              - entity: sensor.meshcore_def456abc0_recv_direct_rate_myrepeater
              - entity: sensor.meshcore_def456abc0_sent_direct_rate_myrepeater
          - type: history-graph
            title: Flood
            entities:
              - entity: sensor.meshcore_def456abc0_recv_flood_rate_myrepeater
              - entity: sensor.meshcore_def456abc0_sent_flood_rate_myrepeater
      - type: grid
        cards:
          - type: heading
            heading: Queue and duplicates
          - type: history-graph
            title: Duplicates (msg/min)
            entities:
              - entity: sensor.meshcore_def456abc0_direct_dups_rate_myrepeater
              - entity: sensor.meshcore_def456abc0_flood_dups_rate_myrepeater
          - type: history-graph
            title: TX Queue Length
            entities:
              - entity: sensor.meshcore_def456abc0_tx_queue_len_myrepeater
          - type: entities
            title: Totals
            entities:
              - entity: sensor.meshcore_def456abc0_nb_recv_myrepeater
              - entity: sensor.meshcore_def456abc0_nb_sent_myrepeater
              - entity: sensor.meshcore_def456abc0_full_evts_myrepeater
              - entity: sensor.meshcore_def456abc0_recv_errors_myrepeater
```

## Notes

- The **Advert** badge shows the [contact sensor](../contacts.md#contact-states) state: `fresh`, `stale` or `discovered`.
- The **Online** badge shows if the repeater answers the requests of Home Assistant.
- The `*_rate` sensors show messages per minute between the last two status responses.
- The **Route** row is empty when the companion hears the repeater directly or has no route. With no route, **Hops** shows `unknown`.
- **Refresh firmware version** costs one credit. See [Mesh Traffic Policy](../traffic-policy.md).
- With **Enable Neighbor Entities** set, add `sensor.meshcore_def456abc0_neighbor_count`. See [Repeater Neighbors](../repeater-neighbors.md).
- If two entries track the same repeater, the IDs of the second entry get a suffix such as `_2`. Use the IDs of one entry.
