---
sidebar_position: 7
title: Repeater Neighbors
---

# Repeater Neighbors

Each repeater keeps a list of the nodes that it heard recently. The integration can read this list from a tracked repeater and show each neighbor as sensors. The feature is off by default. You enable it for each repeater.

## Enable neighbors for a repeater

You can enable the option when you add the repeater (see [Add Repeater Station](remote-device-tracking.md#add-repeater-station)). To enable it for a tracked repeater:

1. Go to **Settings > Devices & services > MeshCore > Configure**.
2. Select **Manage Monitored Devices**.
3. In **Select Device**, select the repeater.
4. In **Action**, select **Edit Device Settings**.
5. Select **Submit**.
6. Enable **Enable Neighbor Entities (creates SNR and activity sensors for each neighbor seen by the repeater)**.
7. Select **Submit**.

The integration creates the **Neighbor Count** sensor at once, and the neighbor sensors after the next scan. The UI gives MeshCore firmware 1.14.0 or later as the requirement for the repeater.

To disable neighbors, do the same steps and disable the option in step 6. The integration removes all neighbor sensors and saved neighbor data of that repeater at once.

## Scans

The integration reads the neighbor list only after a successful status poll, at the interval in **Telemetry Refresh Rate (seconds)** (default 7200). The repeater sends the list in pages. Each page costs one credit. If the lane has no credit for the first page, the integration skips the scan. A skipped scan is not a failure.

A scan stops when one of these conditions occurs:

- The integration has received all the entries.
- The first page, or two pages in a row, get no answer.
- The scan sent 32 page requests.
- A page adds no entries.
- The repeater reports a different total two times.
- The lane has no credit for the next page. The integration keeps the entries that it received.

A neighbor that is not in the latest scan keeps its last SNR. Its age (`secs_ago`) continues to increase, also across restarts, until the repeater hears it again.

## Sensors

All neighbor sensors are on the device of the repeater. `<pk10>` is the first 10 characters of the repeater public key. `<neighbor pk6>` is the first 6 characters of the neighbor public key.

| Sensor | Entity ID example | State | Attributes |
|---|---|---|---|
| **Neighbor Count** | `sensor.meshcore_def456abc0_neighbor_count` | Number of stored neighbors | `active` (heard in the last 72 hours), `stale` |
| **Neighbor `<name>` SNR** | `sensor.meshcore_def456abc0_neighbor_fed987` | Last SNR in dB | `secs_ago`, `last_seen`, `pubkey_prefix`, `resolved_name`, `last_updated`, `seen_48h` |
| **Neighbor `<name>` Seen** | `sensor.meshcore_def456abc0_neighbor_fed987_seen` | Sightings in the last 48 hours | `pubkey_prefix`, `resolved_name` |

- A sighting is a scan in which the repeater heard the neighbor again since the previous scan. At an interval of 7200 seconds, the maximum value is 24.
- `<name>` is the name of the matching contact. If no contact matches, it is the first 6 characters of the neighbor public key in upper case.
- The SNR and Seen sensors become unavailable when the repeater has not heard the neighbor for 72 hours.
- The integration saves the neighbor data. After a restart, it creates the sensors again from the saved data.

## Remove stale neighbors

Two options in **Global Settings** remove old neighbors from all tracked repeaters:

| Field | Default | Range |
|---|---|---|
| **Auto-Remove Stale Neighbors** | Off | On, off |
| **Stale Neighbor Threshold (days)** | 7 | 1 to 365 |

When the option is on, the integration does a check one time each day. It removes the sensors and saved data of each neighbor that the repeater last heard more than the threshold ago. Use this option if your repeater devices show many unavailable neighbor sensors.

## Related pages

- [Remote Node Tracking](remote-device-tracking.md): add a repeater and set its poll interval.
- [Mesh Traffic Policy](traffic-policy.md): lanes and credits.
