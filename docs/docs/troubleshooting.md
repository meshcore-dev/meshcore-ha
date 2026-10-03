---
sidebar_position: 19
title: Troubleshooting
---

# Troubleshooting

Find your symptom in the tables below. The links go to the full details. To reset the state of the integration, reload the entry. The **Reload** command is in the three-dot menu of the entry.

## Enable debug logging

To keep debug logging after a restart:

1. Add this to `configuration.yaml`:

   ```yaml
   logger:
     logs:
       custom_components.meshcore: debug
   ```

2. Restart Home Assistant.

To enable debug logging until the next restart, run this action:

```yaml
action: logger.set_level
data:
  custom_components.meshcore: debug
```

To find the log lines:

1. Make the problem occur again.
2. Go to **Settings > System > Logs**.
3. Search for `meshcore`.

Traffic policy decisions, uploads and route changes also show at the `info` level.

CAUTION: Disable debug logging after the diagnosis. Debug logging makes the log file large.

## Connection

| Symptom | Cause | Fix |
|---|---|---|
| Setup or Reconfigure shows "Failed to connect" | No connection in 10 seconds, or no node information from the companion | Check the cable, power, port, address and firmware. USB needs the USB companion firmware. BLE needs the Bluetooth companion firmware. See [Installation](installation.md). |
| After a restart, the entry does not load. Log: `Failed to connect to MeshCore device at <address> after 3 attempts`. | 3 connection attempts failed | Check the companion and the address in the message (USB path, BLE address or TCP host and port). Home Assistant retries the setup later. |
| Node Status is `offline` or unavailable. Log: `MeshCore link lost (...); starting recovery`. | The link to the companion is lost | The integration reconnects with waits of 5, 10, 20, 40 and then 60 seconds. Check the power, cable or network. If it does not recover, reload the entry. |
| BLE through a Bluetooth proxy does not connect | The proxy does not pass the MeshCore PIN pairing | Use a Bluetooth adapter on the host, or a BLE to TCP bridge |
| USB fails | Wrong port, no permission, or Bluetooth firmware | Check the path (for example `/dev/ttyUSB0`), the permissions and the firmware |
| TCP fails | Wrong host or port, or a firewall | Check the host, the port (default 5000) and the network |
| Repair issue "MeshCore public key changed" | A companion with a different public key is on the connection | The entity ID prefixes changed. Update your automations and dashboards, then dismiss the issue. |
| After a downgrade to 2.x, the entry does not load | 3.0 migrated the entry to version 4 | Restore the backup from before the upgrade. See [Upgrade to 3.0](upgrade-3.0.md). |

## Setup and adding nodes

| Symptom | Cause | Fix |
|---|---|---|
| "Device is already configured" | An entry for this companion exists | Use **Reconfigure** on that entry |
| "No repeaters found" or "No client devices found" | No contacts of that node type | Wait for adverts, or add the nodes to the companion. Then open the form again. |
| **Add Repeater Station** shows "Contact not found" | The node is a discovered contact | Add the node to the companion first. See [Contact Management](contacts.md). |
| "Device not connected" | The companion is not connected | Fix the [connection](#connection), then submit again |
| "Failed to log in to repeater" | Wrong password, or no answer | Check the password, power and range. For a node without a password, check that its ACL allows your companion. |
| "Mesh traffic `<lane>` lane is empty. Try again in `<seconds>` seconds." | The lane has no credit for the login | Wait for the time in the message. Then submit again. See [Mesh Traffic Policy](traffic-policy.md#when-a-lane-runs-dry). |
| A tracked client never answers | Not an added contact, or its ACL does not allow your companion | Add the client to the companion. Check its ACL. |

## Repeaters and polling

| Symptom | Cause | Fix |
|---|---|---|
| Repeater sensors unavailable | No status response in 3 x the interval (6 h at default). After a restart, until the first response. | Check the **Request Failures** and **Online** sensors, then the rows below |
| Telemetry sensors unavailable or missing | No reading in 3 x the interval. Sensors appear only at the first reading. | Wait one poll cycle. For a repeater, enable **Enable Telemetry Polling**. |
| **Online** is `off` | No successful request in 2.5 x the interval | See the rows for failed polls |
| Log: `Login to repeater myrepeater failed or timed out` | The password changed, or no answer | Enter the new password in **Manage Monitored Devices** |
| **Request Failures** increases often | The node does not answer, because of a weak radio link or a bad route. A poll that waits for credit is not a failure. | Look at **Path Length** and **Routing Path**. To use a better route, see [Pin a route](traffic-policy.md#pinning-a-route-and-turning-off-route-resets). |
| Route healing finds the same weak route | The integration keeps the first route that answers | Set the route with `change_contact_path`, then enable **Disable Path Reset**. See [Pin a route](traffic-policy.md#pinning-a-route-and-turning-off-route-resets). |
| A node gets no more polls. Log: `... no successful requests in 120.0 hours. Automatically disabling ...`. | Auto-disable | Edit the node in **Manage Monitored Devices** and select **Submit**, or wait for its next advert. See [Auto-disable](traffic-policy.md#auto-disable). |
| No firmware version on the repeater device | No credit, or the query timed out | Press **Refresh firmware version** |
| No neighbor sensors | **Enable Neighbor Entities** is off, firmware older than 1.14.0, or no successful status poll | Enable the option and wait for a status poll. See [Repeater Neighbors](repeater-neighbors.md). |
| A neighbor sensor is unavailable | Not heard for 72 hours | No fix is necessary. Enable **Auto-Remove Stale Neighbors** to remove old neighbors. |

## Traffic policy

For installs from 2.x that still use the deprecated Legacy policy, see [Legacy traffic policy](legacy-traffic-policy.md).

| Symptom | Cause | Fix |
|---|---|---|
| A service call or `execute_command` fails with `Mesh traffic <lane> lane is empty; try again in <seconds> seconds` | The lane has no credit. A service call cannot wait. | Wait for the time in the message. Send fewer requests. See [When a lane is empty](traffic-policy.md#when-a-lane-runs-dry). |
| An automation stops at a send action | The lane error stops it | Set `continue_on_error: true` on the action |
| A poll is late. Log: `Deferring status for myrepeater (flood lane empty, next at ...)`. | The flood lane is empty. No failure is recorded. | No fix is necessary. Track fewer nodes that have no route. |
| **Request Rate Limiter** is often at 0 | The routed requests to your tracked nodes use the credits of the direct lane faster than the lane refills (120 credits each hour) | Increase **Telemetry Refresh Rate (seconds)** or **Update Frequency (seconds)**. Disable **Enable Telemetry Polling** on the repeaters that do not need it. See [Monitor the budget](traffic-policy.md#watching-the-budget). |

## Messages and delivery

| Symptom | Cause | Fix |
|---|---|---|
| **Last Message Delivery** shows `0 Repeaters` | The companion heard no repeat | No fix is necessary. The count shows only what the companion heard. |
| `Unconfirmed` for a channel message | The message is too long to report repeats | Send a shorter message |
| `Unconfirmed` for a direct message | No acknowledgement before the timeout | Check the range and the route to the recipient |
| A message is not sent and no error shows | Most send failures do not raise an error | Trigger on `meshcore_message_send_failed` and read `reason`. See [Services](services.md#send-message). |
| `reason: contact_not_found` | The contact is not on the companion, or the name does not match | Run `get_contacts` and check `added_to_node` |
| A message is missing from the logbook | Not connected, **Retrieve queued incoming messages** is off, or a direct message waits for the ACK | Check the connection and the setting |
| Sender name is `null` or `Unknown`, or a contact has no message entity | The sender is not a contact of the companion | Add the sender to the companion |
| A phone that shares the companion gets no messages | Home Assistant reads the message queue | Disable **Retrieve queued incoming messages**. See [Share the companion with a phone](messaging.md#sharing-a-companion-with-a-phone). |
| `set_channel` for a hashtag channel sets the wrong name | YAML read `#` as a comment | Put the full command in quotes |

## Contacts

| Symptom | Cause | Fix |
|---|---|---|
| Hundreds of contact sensors stay `discovered` | **Entity per contact** mode on a dense mesh | Select **Data only**, or enable **Limit Discovered Contacts**. See [Contact Management](contacts.md#contact-discovery-mode). |
| Contact sensors are unavailable | The contact is in no contact list | Run `meshcore.cleanup_unavailable_contacts` |
| A contact shows `stale` but the node is active | No advert in 12 hours, or the node clock is wrong | Check the clock of the node |
| `add_contact` returns `pubkey_prefix_too_short` | Fewer than 6 characters | Use the 12-character prefix |
| **Add Contact** does nothing. Log: `No contact selected`. | No contact selected | Select a contact, then press the button again |
| The discovered contact select is empty | **Contact Discovery Mode** is **Disabled** | Select **Entity per contact** or **Data only** |
| **Add Contact** or **Remove Contact** fails with `Unauthorized` | The user is not an administrator | Use an administrator account |

## MQTT and map upload

| Symptom | Cause | Fix |
|---|---|---|
| A broker connection sensor stays `off` | The broker did not start or cannot connect | Search the log for `[MQTT1]` to `[MQTT4]`. See [MQTT Upload](mqtt.md). |
| `[MQTTn] Disabled: Let's Mesh broker requires a non-default IATA code` | **Broker IATA Code** is `XYZ` | Set a real region code |
| `[MQTTn] Private key export disabled on firmware` or `Map Auto Uploader: cannot sign` | The firmware refuses the private key export | Use companion firmware with `ENABLE_PRIVATE_KEY_EXPORT=1`, then reload the entry |
| `[MQTTn] Connect failed` or `Connection timed out after 10s` | The broker refused or did not answer | Check the server, port, TLS settings and credentials |
| The map shows no nodes from your companion | The uploader is off, or no repeater, room server or sensor node adverts | Enable **Enable Map Auto Uploader (map.meshcore.io)**. See [Map Auto Uploader](map-upload.md). |
| `Map Auto Uploader: params rejected` | The map API refused the radio settings | Check the radio settings of the companion |
| A node does not update on the map | Each node uploads a maximum of one time each hour | Wait one hour |

## Events and automations

| Symptom | Cause | Fix |
|---|---|---|
| An automation runs two times for each direct message sent | `meshcore_message_sent` fires two times | Ignore the event with `progressive: true`. See [Automation](automation.md). |
| A reply automation answers itself | `meshcore_message` also fires for sent messages | Ignore `outgoing: true` |
| `repeater_count` is always 0 on an outgoing channel `meshcore_message` | The count arrives later | Use `meshcore_delivery_update` with `progressive: false`. See [Events](events.md#message-event-order). |
| An automation runs for all companions | No entry filter | Filter on `entry_id` or `device_id` |
| An automation drops a message that arrives soon after another | `mode: single` | Use `mode: queued` |
| `channel_secret` or `secret` shows `<redacted>` | **Expose Node Secrets in Events** is off | Enable it only on a private system. See [Events](events.md). |
| `ambiguous_config_entry` | More than one entry and no `entry_id` | Set `entry_id`. See [Services](services.md#select-the-entry). |
| `The command '...' is not available through this integration` | The command resets the node, replaces its identity or sends raw frames | Do not use these commands. See [CLI Command Reference](cli-commands.md#denied-commands). |
| Log: `... is a removed meshcore internal, called from <file>:<line>` | Another integration uses a name that 3.0 removed | Update the integration that the line names |
