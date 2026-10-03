---
sidebar_position: 10
title: Sensors
---

# Sensors and Entities

This page lists every entity that the integration creates. To find the exact IDs on your system, open **Developer Tools > States** and filter on `meshcore`.

## Placeholders

The integration changes names to lowercase, changes accented letters to plain letters and replaces spaces and punctuation with `_`. For example, `My Café-Repeater` becomes `my_cafe_repeater`.

| Placeholder | Meaning | Example |
|---|---|---|
| `<pk6>` | First 6 hex characters of the companion public key | `abc123` |
| `<pk10>` | First 10 hex characters of the public key of a tracked node or other node | `def456abc0` |
| `<pk12>` | First 12 hex characters of the public key of a contact | `def456abc012` |
| `<name>` | Name of the companion, of the tracked node or of the other node | `mynode`, `myrepeater`, `node_def456` |
| `<node pk6>` | First 6 hex characters of the public key of another node | `def456` |
| `<n>` | LPP channel number of a telemetry value | `1` |
| `<channel_idx>` | Mesh channel slot on the companion | `0` |

A node that you do not track has its advertised name. If the contact has no name, the name is `Node <node pk6>`. If the node is not in the contact list, the name is `Unknown Node <node pk6>`.

## Devices

| Device | Name format | Entities on the device |
|---|---|---|
| Companion | `MeshCore <name> (<pk6>)` | Companion, Self Diagnostics, companion telemetry, contact, message, radio fault and MQTT broker sensors, companion GPS tracker |
| Repeater | `MeshCore Repeater: <name> (<node pk6>)` | Repeater, route, reliability, Online and neighbor sensors, telemetry sensors, GPS tracker, Refresh firmware version button |
| Client | `MeshCore Client: <name> (<node pk6>)` | Route, reliability and Online sensors, telemetry sensors, GPS tracker |
| Other node | `MeshCore Node: <name> (<node pk6>)` | Telemetry sensors and GPS tracker of a node that you do not track |
| No device | - | Select and text helpers, CLI Console sensor, CLI buttons |

A room server or sensor node that you add with **Add Repeater Station** gets a repeater device. When you remove a tracked node, the integration removes its device and entities. The contact sensor of the node stays.

## Availability summary

| Entity group | Unavailable when |
|---|---|
| Companion, Self Diagnostics, route, reliability, Online, message and radio fault sensors, Companion Prefix, Neighbor Count, Discovered Contacts, CLI Console, buttons | The companion is disconnected (the last coordinator update failed) |
| Repeater sensors | No status response in 3 x the update interval (6 h at the default 7200 s). After a restart, until the first status response. |
| Telemetry sensors | No telemetry reading in 3 x the update interval (default 6 h). After a restart, until the first reading. |
| GPS trackers | No GPS reading in 1.5 x the update interval (default 3 h) |
| Neighbor SNR and Neighbor Seen | The repeater last heard the neighbor 72 h ago or more |
| Contact sensors | The contact is not in the contact list of the integration |
| MQTT broker sensors | The broker was removed, or the MQTT uploader did not start |
| Request Rate Limiter, Last Message Delivery, select and text helpers | Never |

The update interval is **Telemetry Refresh Rate (seconds)** for a tracked repeater and **Update Frequency (seconds)** for a tracked client. The companion telemetry sensors and GPS tracker use 7200 s.

## Companion sensors

| Sensor | Entity ID format | State and unit | Precision | Source |
|---|---|---|---|---|
| Node Status | `sensor.meshcore_<pk6>_node_status_<name>` | `online` or `offline` | - | Link state of the companion |
| Battery Voltage | `sensor.meshcore_<pk6>_battery_voltage_<name>` | V | 3 | Battery reading of the companion |
| Battery Percentage | `sensor.meshcore_<pk6>_battery_percentage_<name>` | % | 2 | Linear scale: 3.0 V is 0 %, 4.2 V is 100 % |
| Node Count | `sensor.meshcore_<pk6>_node_count_<name>` | Number | - | Contacts added to the companion, plus 1 |
| TX Power | `sensor.meshcore_<pk6>_tx_power_<name>` | dBm | 0 | Radio settings of the companion |
| Latitude | `sensor.meshcore_<pk6>_latitude_<name>` | Degrees | - | Advertised position |
| Longitude | `sensor.meshcore_<pk6>_longitude_<name>` | Degrees | - | Advertised position |
| Frequency | `sensor.meshcore_<pk6>_frequency_<name>` | MHz | 3 | Radio settings of the companion |
| Bandwidth | `sensor.meshcore_<pk6>_bandwidth_<name>` | kHz | 1 | Radio settings of the companion |
| Spreading Factor | `sensor.meshcore_<pk6>_spreading_factor_<name>` | Number | - | Radio settings of the companion |
| Request Rate Limiter | `sensor.meshcore_<pk6>_rate_limiter_tokens_<name>` | tokens | 1 | See [Request Rate Limiter](#request-rate-limiter) |
| Companion Prefix | `sensor.meshcore_<pk6>_companion_prefix` | Hex text | - | See [Companion Prefix](#companion-prefix) |
| Last Message Delivery | `sensor.meshcore_<pk6>_last_message_delivery_<name>` | Text | - | See [Last Message Delivery](#last-message-delivery) |
| Discovered Contacts | `sensor.meshcore_<pk6>_discovered_summary_<name>` | Number | - | See [Discovered Contacts](#discovered-contacts) |

Example: `sensor.meshcore_abc123_battery_voltage_mynode`.

### Request Rate Limiter

The state is the credits in the direct lane (0 to 20). For the attributes, see [Mesh Traffic Policy](traffic-policy.md#watching-the-budget). For the state on installs from 2.x that still use the deprecated Legacy policy, see [Legacy traffic policy](legacy-traffic-policy.md).

### Companion Prefix

The state is the routing prefix of the companion in uppercase hex. Other nodes show this prefix in a route. The `path_hash_mode` setting of the companion firmware sets the length.

| `path_hash_mode` | Prefix length | Example state |
|---|---|---|
| 0, or not reported | 1 byte | `AB` |
| 1 | 2 bytes | `ABC1` |
| 2 | 3 bytes | `ABC123` |

Attributes: `public_key`, `path_hash_mode`, `prefix_length`.

### Last Message Delivery

This sensor shows the result of the last message that this entry sent. For the delivery events, see [Events](events.md).

| State | Message type | Meaning |
|---|---|---|
| `Idle` | - | No message sent since the entry loaded |
| `Waiting` | Both | The companion accepted the message. For a channel message, the integration still listens for repeats. |
| `1 Repeater`, `2 Repeaters`, and so on | Channel | The number of repeats that the companion heard |
| `0 Repeaters` | Channel | No repeat heard |
| `Unconfirmed` | Channel | No repeat heard, and the message is too long for the companion to report a repeat |
| `Delivered` | Direct | The recipient sent an acknowledgement |
| `Unconfirmed` | Direct | No acknowledgement arrived |
| `Sent` | Direct | The send ended with no acknowledgement result |

Attributes: `message_type`, `last_message`, `last_send_time`. Channel messages add `repeater_count`, `channel`, `rx_log_data` and `repeater_details` (one item per repeat, with `snr`, `rssi`, `path_len` and `path`). Direct messages add `ack_received` and `receiver`.

### Discovered Contacts

The state is the number of contacts in the discovered list. The integration disables this diagnostic sensor by default. For the attributes, see [Contact Management](contacts.md#discovered-contact-summary-sensor).

## Self Diagnostics sensors

These 14 sensors exist only when **Enable Self Diagnostics** is set in **Global Settings**. The integration reads them from the companion with local queries. These queries send nothing over the mesh. **Self Diagnostics Interval (seconds)** sets the refresh rate (default 300 s). The same setting creates the [radio fault sensors](#radio-fault-sensors).

The entity ID format is `sensor.meshcore_<pk6>_<key>_<name>`, for example `sensor.meshcore_abc123_noise_floor_mynode`. The keys are `uptime`, `tx_queue_len`, `noise_floor`, `last_rssi`, `last_snr`, `tx_airtime`, `rx_airtime`, `nb_recv`, `nb_sent`, `sent_flood`, `sent_direct`, `recv_flood`, `recv_direct` and `recv_errors`. Each sensor has the unit, precision and state class of the [repeater sensor](#repeater-sensors) with the same key. `tx_airtime` matches `airtime`.

## Repeater sensors

The values come from the status response of each tracked repeater, once per update interval. The entity ID format is `sensor.meshcore_<pk10>_<key>_<name>`, for example `sensor.meshcore_def456abc0_noise_floor_myrepeater`.

| Sensor | Key | Unit | Precision | State class |
|---|---|---|---|---|
| Battery Voltage | `bat` | V | 3 | Measurement |
| Battery Percentage | `battery_percentage` | % | 2 | Measurement |
| Uptime | `uptime` | min (shown as days) | - | Measurement |
| Airtime | `airtime` | min | 1 | Total increasing |
| RX Airtime | `rx_airtime` | min | 1 | Total increasing |
| Airtime Utilization | `airtime_utilization` | % | 1 | Measurement |
| RX Airtime Utilization | `rx_airtime_utilization` | % | 1 | Measurement |
| Messages Sent | `nb_sent` | - | - | Total increasing |
| Messages Received | `nb_recv` | - | - | Total increasing |
| Sent Flood Messages | `sent_flood` | - | - | Total increasing |
| Sent Direct Messages | `sent_direct` | - | - | Total increasing |
| Received Flood Messages | `recv_flood` | - | - | Total increasing |
| Received Direct Messages | `recv_direct` | - | - | Total increasing |
| Direct Duplicates | `direct_dups` | - | - | Total increasing |
| Flood Duplicates | `flood_dups` | - | - | Total increasing |
| Full Events | `full_evts` | - | - | Total increasing |
| Receive Errors | `recv_errors` | - | - | Total increasing |
| TX Queue Length | `tx_queue_len` | - | - | Measurement |
| Noise Floor | `noise_floor` | dBm | 0 | Measurement |
| Last RSSI | `last_rssi` | dBm | 0 | Measurement |
| Last SNR | `last_snr` | dB | 1 | Measurement |

All repeater sensors have the attribute `last_updated`. Battery Voltage adds `raw_millivolts`. Uptime adds `human_readable`, for example `3d 4h 5m 6s`.

### Rate and utilization sensors

These 9 counters also get a rate sensor (unit msg/min, precision 1): `nb_sent`, `nb_recv`, `sent_flood`, `sent_direct`, `recv_flood`, `recv_direct`, `direct_dups`, `flood_dups` and `recv_errors`. The key is the counter key with `_rate` added, for example `sensor.meshcore_def456abc0_nb_recv_rate_myrepeater`.

The integration calculates each rate from the last two status responses: the counter change divided by the uptime change, times 60. Utilization is the airtime change divided by the uptime change, times 100. The state is 0 after the first response and after a counter goes down, for example after a repeater restart.

## Route and reliability sensors

The integration creates these 4 sensors for each tracked repeater and tracked client. The entity ID format is `sensor.meshcore_<pk10>_<key>_<name>`.

| Sensor | Key | State | Unit | State class |
|---|---|---|---|---|
| Routing Path | `out_path` | The route as one hex string, with no separators | - | - |
| Path Length | `out_path_len` | The number of hops on the route | hops | Measurement |
| Request Successes | `request_successes` | Successful requests to the node | requests | Total increasing |
| Request Failures | `request_failures` | Failed requests to the node | requests | Total increasing |

- Path Length `0` means that the companion hears the node directly. Routing Path is then empty.
- Path Length `unknown` means that the companion has no route. Requests to the node are then flood requests.
- To get the hex characters per hop, divide the length of Routing Path by Path Length.
- The reliability counters start again at 0 after a Home Assistant restart.

## Neighbor sensors

A repeater with **Enable Neighbor Entities** set gets the Neighbor Count, Neighbor SNR and Neighbor Seen sensors. For the entity IDs, states and attributes, see [Repeater Neighbors](repeater-neighbors.md#sensors).

## Telemetry sensors (Cayenne LPP)

The integration creates a telemetry sensor when a node first sends a reading with a new channel and type. It requests telemetry from each tracked client and from each tracked repeater with **Enable Telemetry Polling** set. It also requests it from the companion when **Enable Self Telemetry** is set. A node can also send telemetry with no request.

| Node | Entity ID format | Example |
|---|---|---|
| Tracked or other node | `sensor.meshcore_<pk10>_ch<n>_<type>_<name>` | `sensor.meshcore_def456abc0_ch1_temperature_myrepeater` |
| Multi-value type | `sensor.meshcore_<pk10>_ch<n>_<type>_<axis>_<name>` | `sensor.meshcore_def456abc0_ch1_accelerometer_x_myrepeater` |
| Companion | `sensor.meshcore_<pk6>_<type>_ch<n>_<name>` | `sensor.meshcore_abc123_temperature_ch1_mynode` |

### Supported LPP types

| LPP code | Type part of the entity ID | Unit | Precision | Device class |
|---|---|---|---|---|
| 0 | `digital_input` | - | - | - |
| 1 | `digital_output` | - | - | - |
| 2 | `analog_input` | V | 2 | - |
| 3 | `analog_output` | V | 2 | - |
| 100 | `generic_sensor` | - | - | - |
| 101 | `illuminance` | lx | - | Illuminance |
| 102 | `presence` | - | - | - |
| 103 | `temperature` | °C | 1 | Temperature |
| 104 | `humidity` | % | 1 | Humidity |
| 113 | `accelerometer_x`, `accelerometer_y`, `accelerometer_z` | G | 3 | - |
| 115 | `barometer` | hPa | 1 | Atmospheric pressure |
| 116 | `voltage` | V | 2 | Voltage |
| 117 | `current` | A (shown as mA) | 1 | Current |
| 118 | `frequency` | Hz | 0 | Frequency |
| 120 | `percentage` | % | 0 | - |
| 121 | `altitude` | m | 1 | Distance |
| 122 | `load` | kg | 3 | Weight |
| 125 | `concentration` | ppm | 0 | - |
| 128 | `power` | W | 0 | Power |
| 130 | `distance` | m | 3 | Distance |
| 131 | `energy` | kWh | 3 | Energy |
| 132 | `direction` | ° | 0 | - |
| 133 | `time` | - | - | - |
| 134 | `gyrometer_x`, `gyrometer_y`, `gyrometer_z` | °/s | 2 | - |
| 135 | `color_red`, `color_green`, `color_blue` | - | - | - |
| 142 | `switch` | - | - | - |

- Presence, Digital Input, Digital Output and Switch are numeric sensors, not binary sensors.
- GPS (LPP code 136) creates a [GPS tracker](#gps-trackers), not a sensor.
- An LPP type that is not in the table creates a generic sensor.
- A tracked client that sends a voltage on channel 1 gets 2 sensors in place of the voltage sensor: `sensor.meshcore_<pk10>_ch1_battery_voltage_<name>` (V) and `sensor.meshcore_<pk10>_ch1_battery_<name>`. The second sensor has the device class Battery and uses the 3.0 V to 4.2 V scale.

Attributes: `channel`, `lpp_type`, `pubkey_prefix`, `node_type` (`root`, `repeater`, `client`, `contact` or `unknown`), `node_name`, `field` (multi-value types only), `last_updated`, `raw_value`.

## Binary sensors

| Binary sensor | Entity ID format | Device | States | When created |
|---|---|---|---|---|
| Contact | `binary_sensor.meshcore_<adv name>_<pk12>_contact` | Companion | `fresh`, `stale`, `discovered` | See [Contact sensors](#contact-sensors) |
| Online | `binary_sensor.meshcore_<pk10>_online_<name>` | Tracked node | `on`, `off`, `unknown` | For each tracked repeater and tracked client |
| Channel messages | `binary_sensor.meshcore_<pk6>_ch_<channel_idx>_messages` | Companion | `Active` | At the first message on the channel |
| Contact messages | `binary_sensor.meshcore_<pk6>_<node pk6>_messages` | Companion | `Active` | At the first direct message with the contact |
| MQTT broker | `binary_sensor.meshcore_<pk6>_mqtt_broker_<number>_connection` | Companion | `on`, `off` | For each MQTT broker that the uploader loads |
| Radio fault | `binary_sensor.meshcore_<pk6>_<fault key>_<name>` | Companion | `on`, `off`, `unknown` | When **Enable Self Diagnostics** is set |

### Contact sensors

Example: `binary_sensor.meshcore_myrepeater_def456abc012_contact`. For the states, the attributes and the **Contact Discovery Mode** rules, see [Contact Management](contacts.md#contact-entities).

### Online sensors

The Online sensor shows if a tracked node answers the requests of the integration. Node Status shows only the link between Home Assistant and the companion.

| State | Condition |
|---|---|
| `on` | The last successful request is in the staleness window |
| `off` | The last successful request is older than the staleness window |
| `unknown` | No successful request since Home Assistant started |

The staleness window is 2.5 x the update interval of the node, with a minimum interval of 300 s. At the default of 7200 s, the window is 5 h.

Attributes: `last_successful_request`, `update_interval`, `staleness_window` (seconds).

### Message sensors

The state is always `Active`. The logbook uses these sensors to show the messages. See [Messaging](messaging.md).

- Channel messages: attribute `channel_index`.
- Contact messages: attribute `public_key` (12-character prefix). A sender that is not a contact gets no sensor.

### Radio fault sensors

These 3 sensors decode the `errors` value of the companion. The firmware clears a flag only when the companion restarts. Thus `on` means that the fault occurred at least one time since the companion started. The state is `unknown` until the first Self Diagnostics reading.

| Fault key | Name | Meaning |
|---|---|---|
| `err_pool_full` | Radio Fault: Packet Pool Exhausted | The packet pool was full, and the companion dropped a packet |
| `err_cad_timeout` | Radio Fault: CAD Timeout | Channel Activity Detection stayed busy for too long |
| `err_rx_timeout` | Radio Fault: RX-Start Timeout | The companion did not go back into receive mode |

### MQTT broker sensors

Attributes: `broker_number`, `server`. The sensors follow broker changes without a reload. See [MQTT](mqtt.md).

## GPS trackers

The integration creates a GPS tracker when a node first sends a GPS reading (LPP code 136).

| Node | Entity ID format |
|---|---|
| Tracked or other node | `device_tracker.meshcore_<pk10>_gps_<name>` |
| Companion | `device_tracker.meshcore_<pk6>_gps_<name>` |

Companion telemetry sensors and the companion GPS tracker from before 3.0 keep their old entity IDs. See [Upgrade to 3.0](upgrade-3.0.md).

The location accuracy is 10 m when the reading gives no accuracy. Attributes: `pubkey_prefix`, `node_type`, `node_name`, `altitude` (when present), `last_updated`.

## Buttons

| Button | Entity ID format | Device | When created |
|---|---|---|---|
| Refresh firmware version | `button.meshcore_<pk10>_refresh_firmware` | Repeater | For each tracked repeater |
| CLI Run Command | `button.meshcore_<pk6>_cli_run` | None (hidden) | When **Enable CLI Console** is set |
| CLI Clear Console | `button.meshcore_<pk6>_cli_clear` | None (hidden) | When **Enable CLI Console** is set |

A press of **Refresh firmware version** costs one credit. If the lane has no credit, the press fails with an error.

## CLI Console sensor

| Item | Value |
|---|---|
| Entity ID | `sensor.meshcore_<pk6>_cli_console` (no device, hidden) |
| When created | When **Enable CLI Console** is set in **Global Settings** |
| State | The number of command and response pairs in the transcript (last 50 kept) |
| Attributes | `history`, `transcript`, `last_command`, `last_response`, `last_is_error`, `command_count`, `max_lines` |

The recorder does not store the transcript attributes, because a command can contain a password. For a card, see [Dashboard cards](dashboard/overview.md#cli-console).

## Select and text helpers

These helpers have no device, are hidden by default and are always available.

| Entity | Options or value | Attributes |
|---|---|---|
| `select.meshcore_recipient_type` | `Channel`, `Contact` | - |
| `select.meshcore_channel` | One option per channel slot, as `<channel name> (<index>)` | `channel_idx` |
| `select.meshcore_contact` | Added contacts that are not repeaters, as `<name> (<pk12>)` | `public_key_prefix`, `public_key`, `contact_name` |
| `select.meshcore_discovered_contact` | Discovered contacts, as `<name> (<pk12>)` | `pubkey_prefix`, `public_key`, `contact_name` |
| `select.meshcore_added_contact` | All added contacts, as `<name> (<pk12>)` | `pubkey_prefix`, `public_key`, `contact_name` |
| `text.meshcore_message` | The message text, 200 characters maximum | - |
| `text.meshcore_command` | The command text, 255 characters maximum | - |

With two entries, the helpers of the second entry get a suffix such as `_2`. See [Two or more companions](multiple-companions.md).

## Usage examples

This template sensor counts the MeshCore battery sensors below 20 %. It ignores `unknown` and `unavailable`.

```yaml
template:
  - sensor:
      - name: "Mesh low battery nodes"
        unique_id: mesh_low_battery_nodes
        state: >
          {% set ns = namespace(count=0) %}
          {% for id in integration_entities('meshcore') | select('match', 'sensor[.]') %}
            {% if state_attr(id, 'device_class') == 'battery'
                  and states(id) | is_number
                  and states(id) | float < 20 %}
              {% set ns.count = ns.count + 1 %}
            {% endif %}
          {% endfor %}
          {{ ns.count }}
```
