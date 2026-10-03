---
sidebar_position: 9
title: Messaging
---

# Messaging

The integration sends and receives MeshCore direct messages and channel messages. It logs each message in the logbook and fires events for automations. For the event fields, see [Events](events.md). For the service fields, see [Services](services.md).

## Send messages

The recipient of a direct message must be a contact on the companion. Identify it by `node_id` (the contact name, not case-sensitive) or by `pubkey_prefix`.

```yaml
action: meshcore.send_message
data:
  pubkey_prefix: def456abc012
  message: Hello from Home Assistant
```

To send to a channel, give the channel index. To send a region-scoped flood, add `scope`.

```yaml
action: meshcore.send_channel_message
data:
  channel_idx: 1
  message: Test message
  scope: myregion
```

With more than one companion, add `entry_id`. See [Select the entry](services.md#select-the-entry).

What happens after a send:

- When the companion accepts the message, the integration fires `meshcore_message_sent`. This event does not confirm delivery.
- If the message does not leave the companion, the integration fires `meshcore_message_send_failed`, and `reason` gives the cause. The call does not raise an error, except for a traffic policy refusal.
- Under Governed, each message uses one credit from the messages lane. See [Mesh Traffic Policy](traffic-policy.md).
- A message that you send with `execute_command` (for example `send_msg`) fires no events and makes no logbook entry.

For the event order, see [Message event order](events.md#message-event-order).

## Receive messages

The companion keeps incoming messages in a queue. The integration reads the queue when the companion reports a waiting message and after each connection. It also reads the queue on the next scheduled update after 60 seconds without message activity. For each message, it fires `meshcore_message`.

| Message type | How the integration finds the sender |
|---|---|
| Direct | The firmware gives the public key prefix. If no contact has that prefix, `sender_name` is `null`. |
| Channel | The packet carries `Name: message`. If the name is not a contact name, `sender_name` is `"Unknown"` and `message` holds the full text. |

`pubkey_prefix` is on each incoming direct message. It is on an incoming channel message only when the sender is a contact. A text reply from a repeater to `send_cmd` also arrives as a direct message.

### RX_LOG Correlation

A channel message can reach the companion more than one time: from the sender and as relayed copies from repeaters. The integration decrypts each copy and attaches it to the message in `rx_log_data`, with SNR, RSSI and the route of the copy. It matches copies by channel index and sender timestamp, not by text.

| | Default | Adaptive |
|---|---|---|
| Delay before `meshcore_message` | 500 ms | 50 ms to 500 ms |
| `rx_log_data` on `meshcore_message` | All copies in 500 ms | The first copies only |
| Later copies | Not reported | In `meshcore_delivery_update`, from checks 0.5 s and 1.5 s after the event |

Keep the default if an automation reads the number of copies from `meshcore_message`. Use adaptive mode for less delay if your automations handle `meshcore_delivery_update`.

To enable adaptive mode, go to **Settings > Devices & services > MeshCore > Configure > Global Settings**. Enable **Adaptive Channel Message Delivery**. The setting applies to the next channel message.

## Delivery status

The **Last Message Delivery** sensor, `sensor.meshcore_<pk6>_last_message_delivery_<name>`, shows the result of the last message that this entry sent. For the states, see [Sensors: Last Message Delivery](sensors.md#last-message-delivery).

## Logbook

The integration writes each `meshcore_message` event to the logbook of the message entity.

| Message | Logbook text |
|---|---|
| Channel | `<channel> sender: message` |
| Direct | `sender: message` |

- For an outgoing message, the sender is the companion name that the entry stored at setup or at the last reconfigure. The logbook does not show the recipient.
- A sender that is not a contact shows as `Unknown` (channel) or `Unknown (<pk6>)` (direct), for example `Unknown (def456)`.
- A channel slot with an empty name shows as `public` for channel 0, and as its index for other channels. This applies to incoming and outgoing messages.

## Message entities

The integration creates a binary sensor for each conversation, on the first message in either direction.

| Conversation | Entity ID | Attribute |
|---|---|---|
| Channel | `binary_sensor.meshcore_<pk6>_ch_<channel_idx>_messages` | `channel_index` (string) |
| Contact | `binary_sensor.meshcore_<pk6>_<node pk6>_messages` | `public_key` (12-character prefix) |

The state is always `Active`. A sender that is not a contact gets no entity, but the event still fires.

## Channels

A MeshCore channel has an index, a name and a 16-byte secret key. All nodes that have the same key can read the channel.

| Channel type | Name | Key |
|---|---|---|
| Public | Channel 0 | A fixed key that the firmware sets |
| Hashtag | Starts with `#`, for example `#mychannel` | The first 16 bytes of the SHA-256 hash of the name (case-sensitive). All nodes that use the same name share the channel. |
| Private | Does not start with `#` | A random secret that you share with the members |

CAUTION: Do not use `set_channel` on channel 0. If the key changes, the companion cannot read or send on the Public channel.

### Add a hashtag channel

Quote the full command in YAML. If not, YAML reads `#` as the start of a comment.

```yaml
action: meshcore.execute_command
data:
  command: "set_channel 1 #mychannel"
```

### Add a private channel

1. Make a secret of 16 bytes on a computer: `openssl rand -hex 16`
2. Run `set_channel` with the index, the name and the secret:

   ```yaml
   action: meshcore.execute_command
   data:
     command: "set_channel 2 mygroup 0f1e2d3c4b5a69788796a5b4c3d2e1f0"
   ```

3. Give the name and the secret to each member. Use a secure method.

CAUTION: Do not start the name of a private channel with `#`, and do not omit the secret. In both cases, the library makes the key from the name, and any person who knows the name can read the channel.

CAUTION: Do not use the secret from the example.

The library uses a maximum of 32 bytes of the name. If the name has spaces, put it in double quotes inside the command.

### Add a hashtag channel from a dashboard

A dashboard card cannot render templates in an action. Use a script that reads two helpers.

To find a free slot, open the **MeshCore Channel** select. It lists each slot of the companion, from 0 to the last index. A free slot shows as `(unused)`. `get_channels` does not show free slots.

1. Create a **Number** helper `input_number.meshcore_channel_index`. Set the minimum to 1 to protect channel 0. Set the maximum to the last slot index in the select, and the step to 1.
2. Create a **Text** helper `input_text.meshcore_channel_name` with a maximum length of 32.
3. Add this script to `scripts.yaml`:

   ```yaml
   meshcore_set_hashtag_channel:
     alias: Set MeshCore hashtag channel
     sequence:
       - condition: template
         value_template: >-
           {{ states('input_text.meshcore_channel_name').startswith('#')
              and states('input_number.meshcore_channel_index') | int(0) >= 1 }}
       - action: meshcore.execute_command
         data:
           command: >-
             set_channel {{ states('input_number.meshcore_channel_index') | int }}
             "{{ states('input_text.meshcore_channel_name') }}"
   ```

4. Add this card to a dashboard:

   ```yaml
   type: vertical-stack
   cards:
     - type: entities
       entities:
         - entity: input_number.meshcore_channel_index
         - entity: input_text.meshcore_channel_name
     - type: button
       name: Set channel
       icon: mdi:pound
       tap_action:
         action: perform-action
         perform_action: script.meshcore_set_hashtag_channel
   ```

`meshcore.execute_command` is an admin action. The button fails for a user who is not an administrator.

### View the channels

The **MeshCore Channel** select shows each slot as `Name (index)`, for example `#mychannel (1)`. A slot with no name, or that the integration did not read, shows `(unused) (index)`. After a successful `set_channel`, the select updates.

In a script, use `meshcore.get_channels`. It returns the index, the name and `shared_secret_present` of each named channel, but not the secret.

## Share the companion with a phone {#sharing-a-companion-with-a-phone}

**Global Settings > Retrieve queued incoming messages** controls whether Home Assistant reads the message queue on the companion. The default is enabled. Each entry has its own setting.

Disable it when another client (for example a phone over BLE) must read the messages. Home Assistant stays connected (for example over USB) for repeater monitoring. This setting does not add simultaneous USB and BLE support to firmware that does not have it.

When the setting is disabled:

- Home Assistant does not read the queue. Repeater status, telemetry, contacts and commands continue to operate.
- `execute_command` refuses `get_msg`, with `{"error": "Incoming message consumption is disabled"}`.
- If the companion pushes a message to Home Assistant, Home Assistant still shows and logs it. The firmware controls delivery to more than one client.
- A text reply to a command can stay in the queue for the phone. To detect the firmware version of a repeater, the integration waits for a text reply, so the detection can time out.
- Messages that Home Assistant already read do not return to the queue. Before you disable the setting, read important queued messages with the phone.

To make sure that the phone gets the messages:

1. Disable **Retrieve queued incoming messages**.
2. Select **Submit**.
3. Disconnect the phone from BLE. Home Assistant stays connected.
4. Send a message to the companion from a different node.
5. Wait more than 60 seconds.
6. Connect the phone again.
7. Make sure that the phone receives the message.
8. Make sure that Home Assistant continues to update repeater telemetry.

## Troubleshooting

For messaging problems, see [Troubleshooting: Messages and delivery](troubleshooting.md#messages-and-delivery).
