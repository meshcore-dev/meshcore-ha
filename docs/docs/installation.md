---
sidebar_position: 1
title: Installation
---

# Install and Configure MeshCore for Home Assistant

If you upgrade from 2.x, read [Upgrade to 3.0](upgrade-3.0.md) first.

## Requirements

| Item | Requirement |
|---|---|
| Home Assistant | 2025.6.0 or later |
| `meshcore` Python library | 2.3.11 or later. Home Assistant installs it automatically. |
| Companion | A MeshCore node with companion firmware for your connection type |
| USB | A USB port on the Home Assistant host. The companion must have the USB companion firmware. |
| BLE | A Bluetooth adapter on the Home Assistant host. A Bluetooth proxy does not work with PIN pairing. |
| TCP | A network route to the companion (WiFi firmware) or to a TCP bridge |

Each integration entry connects to one companion, and the public key of the companion identifies the entry. To use two companions, add the integration two times. See [Two or more companions](multiple-companions.md).

## Install the integration

### HACS (recommended)

[![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=meshcore-dev&repository=meshcore-ha&category=integration)

1. Make sure that [HACS](https://hacs.xyz/) is installed.
2. In HACS, open the three-dot menu.
3. Select **Custom repositories**.
4. Enter `https://github.com/meshcore-dev/meshcore-ha`.
5. Select the type **Integration**.
6. Select **Add**.
7. Search for **MeshCore**.
8. Select **Download**.
9. Restart Home Assistant.

### Manual installation

1. Download the latest release from [GitHub](https://github.com/meshcore-dev/meshcore-ha/releases).
2. Copy `custom_components/meshcore` into the `custom_components` directory of your Home Assistant configuration.
3. Restart Home Assistant.

## Add the integration

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=meshcore)

1. Go to **Settings > Devices & services**.
2. Select **Add Integration**.
3. Search for **MeshCore**.
4. Select **MeshCore**.
5. In **Connection Type**, select `usb`, `ble` or `tcp`.
6. Complete the form.
7. Select **Submit**.

The connection must complete in 10 seconds. If you see "Failed to connect", correct the values and submit again.

| Connection | Field | Default |
|---|---|---|
| USB | USB Device Path, for example `/dev/ttyUSB0` or `/dev/ttyACM0` | (required) |
| USB | Connection Speed | 115200 |
| BLE | Bluetooth Device | (required) |
| TCP | Host Address | (required) |
| TCP | Port | 5000 |

For BLE, the integration scans for 5 seconds. If it finds devices with "MeshCore" in the name, the field is a list. If not, type the address.

All three forms also have Enable Self Telemetry, Enable Self Diagnostics, their intervals and Contact Discovery Mode. You can change these later in [Global Settings](#global-settings).

A new entry gets the title `MeshCore Node <name>`, the **Governed** traffic policy and no tracked nodes. See [Mesh Traffic Policy](traffic-policy.md). To add tracked nodes, see [Remote Node Tracking](remote-device-tracking.md).

## Reconfigure the connection

Use **Reconfigure** to change the connection type, port or host. The integration keeps all other settings, the tracked nodes and the MQTT brokers.

1. Go to **Settings > Devices & services > MeshCore**.
2. Open the three-dot menu of the entry.
3. Select **Reconfigure**.
4. Select the connection type.
5. Enter the new values.
6. Select **Submit**.

If the test passes, the entry reloads. If the new companion has a different public key, the integration changes the public key prefix in the entity IDs of the entry. It also creates the repair issue "MeshCore public key changed". Then do these steps:

1. Update the automations, scripts and dashboards that use the old entity IDs.
2. Dismiss the repair issue.

## Configure the options {#post-installation-configuration}

Go to **Settings > Devices & services > MeshCore > Configure**. In **Choose an action**, select one of these actions:

| Action | Use |
|---|---|
| Add Repeater Station | Track a repeater, room server or sensor node. See [Remote Node Tracking](remote-device-tracking.md). |
| Add Tracked Client | Track a client. See [Remote Node Tracking](remote-device-tracking.md). |
| Manage Monitored Devices | Edit or remove a tracked node |
| Global Settings | Change the settings of the entry |
| Manage MQTT Brokers | Add, edit or remove a maximum of 4 MQTT brokers. See [MQTT Upload](mqtt.md). |

Each **Submit** saves the change. To close the menu, select **Done**.

Most changes apply immediately, without a reload. A change to **Enable Self Diagnostics** or **Enable CLI Console** reloads the entry.

### Reload the entry

1. Go to **Settings > Devices & services > MeshCore**.
2. Open the three-dot menu of the entry.
3. Select **Reload**.

### Global Settings

| Field | Range | Default | Description |
|---|---|---|---|
| Message Poll Interval (seconds) | 1 to 300 | 5 | The scheduling tick. The integration reads queued messages when the companion reports them, and after 60 seconds with no message. No mesh traffic. |
| Contact Discovery Mode | Entity per contact, Data only, Disabled | Entity per contact | How the integration keeps discovered contacts. See [Contact Discovery Mode](contacts.md#contact-discovery-mode). |
| Limit Discovered Contacts | on / off | off | Limits the number of discovered contacts |
| Maximum Discovered Contacts | 1 to 10000 | 100 | The limit. The integration removes the oldest contacts first. |
| Enable Self Telemetry | on / off | off | Requests telemetry from the companion itself |
| Self Telemetry Interval (seconds) | 60 to 3600 | 300 | The time between self telemetry requests |
| Enable Self Diagnostics | on / off | off | Creates 14 diagnostic sensors and 3 radio fault binary sensors for the companion. No mesh traffic. |
| Self Diagnostics Interval (seconds) | 60 to 3600 | 300 | The time between diagnostic queries |
| Enable CLI Console | on / off | off | Creates the CLI Console sensor. See [CLI Command Reference](cli-commands.md). |
| Enable Map Auto Uploader (map.meshcore.io) | on / off | off | Uploads repeater, room server and sensor adverts. The firmware must have `ENABLE_PRIVATE_KEY_EXPORT=1`. See [Map Auto Uploader](map-upload.md). |
| Auto-Cleanup Stale Discovered Contacts (runs daily) | on / off | off | Removes discovered contacts older than the threshold. Contacts on the companion stay. |
| Stale Contact Threshold (days) | 1 to 365 | 30 | The age for the contact cleanup |
| Retrieve queued incoming messages | on / off | on | Disable this setting when a phone must get the queued messages. See [Share the companion with a phone](messaging.md#sharing-a-companion-with-a-phone). |
| Adaptive Channel Message Delivery | on / off | off | Fires an incoming channel message at the first reception data, in place of a fixed 500 ms wait. See [RX_LOG correlation](messaging.md#rx_log-correlation). |
| Flood Scope Allowlist | text | blank | Comma-separated region scope names, for example `myregion`. The integration compares incoming scoped channel messages with these names. The `flood_scope` field of each `rx_log_data` entry gives the matched name, or `null`. |
| Expose Node Secrets in Events | on / off | off | Sends channel secrets and private key exports to the event bus and MQTT raw payloads |
| Auto-Remove Stale Neighbors | on / off | off | Removes neighbor entries and their sensors, one time each day, after the threshold |
| Stale Neighbor Threshold (days) | 1 to 365 | 7 | The age for the neighbor cleanup |
| Mesh Traffic Policy | Governed | Governed | The budget and the retry rules for mesh traffic. See [Mesh Traffic Policy](traffic-policy.md). |

CAUTION: Do not enable **Expose Node Secrets in Events** on a system that other people can read. All users and tools that read the event bus or the MQTT broker will see the secrets.

## Verify the installation

1. Go to **Settings > Devices & services > MeshCore**.
2. Open the device of the companion and make sure that its sensors show values.
3. If you added tracked nodes, make sure that each node has a device.

## Solve problems

| Message | Cause | Action |
|---|---|---|
| Failed to connect | No connection or node information in 10 seconds | Check the cable, power, port, address and companion firmware |
| Device is already configured | An entry for this companion exists | Use Reconfigure on that entry |
| Unexpected error | An error that the integration did not expect | Read the Home Assistant log |

- **USB**: make sure that the port path is correct and that Home Assistant has permission to use it. The Bluetooth companion firmware does not answer on USB.
- **BLE**: use a direct adapter near the companion. For a companion that needs a PIN, you can use the external [MeshCore BLE Bridge for ESPHome](https://github.com/matthew73210/meshcore-ble-bridge), which gives a TCP connection.
- **TCP**: make sure that the host and port are correct and that no firewall blocks the connection.

For debug logging and other problems, see [Troubleshooting](troubleshooting.md).

## Next steps

- [First steps](first-steps.md)
- [Concepts](concepts.md)
- [Sensors](sensors.md)
- [Automation](automation.md)
