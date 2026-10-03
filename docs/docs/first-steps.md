---
sidebar_position: 3
title: First steps
---

# First steps

Do these tasks in sequence after you [install](installation.md) the integration. Use an administrator account: `meshcore.execute_command` and `meshcore.add_selected_contact` are admin-only. The examples use the companion `mynode` with the prefix `abc123`.

## Step 0. Open the action editor

You run most actions on this page in the action editor.

1. Go to **Developer Tools > Actions**.
2. Select **Go to YAML mode**.

To run an action, paste its YAML and select **Perform action**. When an action returns data, the response shows below the form.

## Step 1. Check the companion entities

1. Go to **Settings > Devices & services > MeshCore**.
2. Open the device `MeshCore mynode (abc123)`.
3. Make sure that **Node Status** shows **Online**.
4. Compare **Frequency**, **Bandwidth** and **Spreading Factor** with the settings of your local mesh.

If **Node Status** is not **Online**, see [Troubleshooting](troubleshooting.md).

## Step 2. Send a test message on a private channel

CAUTION: Do not send test messages on the Public channel (channel 0). All nodes in the mesh receive it, and each message uses airtime for all users.

1. Make a 16-byte secret: `openssl rand -hex 16`.
2. Find a free channel index. See [Find a free channel index](#find-a-free-channel-index). The examples use index 2.
3. Run `meshcore.execute_command`:

   ```yaml
   action: meshcore.execute_command
   data:
     command: "set_channel 2 mytest <secret>"
   ```

4. Make sure that `select.meshcore_channel` has the option `mytest (2)`.
5. Send a test message:

   ```yaml
   action: meshcore.send_channel_message
   data:
     channel_idx: 2
     message: "Test 1"
   ```

6. Wait a maximum of 20 seconds. Then read `sensor.meshcore_abc123_last_message_delivery_mynode`.

| Final state | Meaning |
|---|---|
| `1 Repeater`, `2 Repeaters` and more | The companion heard this number of repeaters relay the message |
| `0 Repeaters` | The companion heard no repeat. This is usual when no repeater is in range. |
| `Unconfirmed` | The message is too long for the companion to report repeats |

See [Last Message Delivery](sensors.md#last-message-delivery).

### Find a free channel index

1. Run `meshcore.get_channels`. The response lists only the channels that are in use.
2. In **Developer Tools > States**, open `select.meshcore_channel`. Its options show all channel slots of the companion as `<name> (<channel_idx>)`.
3. Select an index that is not in the `get_channels` response.

## Step 3. Add a contact

The integration keeps the nodes that the companion hears as discovered contacts. Add a node to send direct messages to it or to track it.

1. Wait until the companion hears adverts.
2. In **Developer Tools > States**, filter on `_contact`.
3. Find a contact sensor with the state `discovered`, for example `binary_sensor.meshcore_myrepeater_def456abc012_contact`.
4. Copy its `pubkey_prefix` attribute.
5. Run this command:

   ```yaml
   action: meshcore.execute_command
   data:
     command: "add_contact def456abc012"
   ```

6. Make sure that the contact sensor changes to `fresh` or `stale`.

In **Data only** discovery mode, discovered contacts have no sensor. Use the options of `select.meshcore_discovered_contact` to find the prefix. See [Contact Management](contacts.md).

## Step 4. Add a hashtag channel

All nodes that use the same hashtag channel name share the channel.

1. Get the exact channel name from the other users. The name starts with `#` and is case-sensitive.
2. Find a free channel index. See [Find a free channel index](#find-a-free-channel-index).
3. Run this command. Use quotes, because YAML reads `#` as the start of a comment:

   ```yaml
   action: meshcore.execute_command
   data:
     command: "set_channel 1 #mychannel"
   ```

4. Make sure that `select.meshcore_channel` has the option `#mychannel (1)`.

CAUTION: Do not use `set_channel` on channel 0. If the key of channel 0 changes, the companion cannot read or send on the Public channel.

## Step 5. Track a repeater

The repeater must be an added contact (step 3).

1. Go to **Settings > Devices & services > MeshCore**.
2. Select **Configure**.
3. In **Choose an action**, select **Add Repeater Station**.
4. Select **Submit**.
5. In **Available Repeaters**, select the repeater.
6. In **Password**, enter the repeater password. If the repeater ACL gives access without a password, leave it blank.
7. Keep the default **Telemetry Refresh Rate (seconds)** of 7200.
8. Select **Submit**.

The integration adds the device `MeshCore Repeater: myrepeater (def456)`. The sensors stay unavailable until the first status response. Each poll uses airtime on the mesh. Do not set an interval shorter than the default unless you need more frequent data. See [Remote Node Tracking](remote-device-tracking.md).

## Step 6. Add a dashboard

Use the cards in [Dashboard cards](dashboard/overview.md), or a full dashboard: [Basic Node](dashboard/basic-node.md) or [Basic Repeater](dashboard/basic-repeater.md).

## Step 7. Create a first automation

This automation shows a notification for each received message. The condition ignores the messages that Home Assistant sends.

1. Go to **Settings > Automations & scenes**.
2. Create a new automation.
3. Open the three-dot menu.
4. Select **Edit in YAML**.
5. Paste this automation:

   ```yaml
   alias: MeshCore message notification
   triggers:
     - trigger: event
       event_type: meshcore_message
   conditions:
     - condition: template
       value_template: "{{ not (trigger.event.data.outgoing | default(false)) }}"
   actions:
     - action: persistent_notification.create
       data:
         title: MeshCore message
         message: >-
           {{ trigger.event.data.sender_name | default('Unknown', true) }}:
           {{ trigger.event.data.message }}
   mode: queued
   ```

6. Select **Save**.

For more examples, see [Automation](automation.md).
