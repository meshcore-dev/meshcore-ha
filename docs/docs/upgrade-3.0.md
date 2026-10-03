---
sidebar_position: 4
title: Upgrade to 3.0
---

# Upgrade to 3.0

## Summary of changes

| Area | Change in 3.0 |
|---|---|
| Requirements | Home Assistant 2025.6.0 or later. `meshcore` library 2.3.11 or later (Home Assistant installs it at startup). |
| Traffic policy | New installs use Governed. Installs from 2.x keep the deprecated [Legacy traffic policy](legacy-traffic-policy.md) until you change it. |
| Events | Each `meshcore_*` event has `entry_id` and `device_id`. The outgoing message events changed. The new `meshcore_message_send_failed` event reports send failures. |
| Options | Only a change to the connection, **Enable Self Diagnostics** or **Enable CLI Console** reloads the entry. See [Options apply without a reload](#options-apply-without-a-reload). |
| Entity IDs | New companion telemetry sensors and a new companion GPS tracker have `<pk6>` and the name in the entity ID. Existing entities keep their IDs. See [Companion entity IDs](#companion-entity-ids). |
| Telemetry | The integration creates Accelerometer and Color sensors from the fields `acc_x`, `acc_y`, `acc_z` and `red`, `green`, `blue`. 2.x did not create them. |
| `execute_command` | The integration refuses commands that reset the node, replace its identity or send raw frames |
| Secrets | Events and MQTT raw payloads do not contain channel secrets or private keys, unless you enable **Expose Node Secrets in Events** |
| Internals | `coordinator._repeater_stats`, `api.mesh_core` and `api._cache_self_info_event` are deprecated. They work in 3.0 and log a warning. |

## Upgrade checklist

1. Make a full backup of Home Assistant. After the upgrade, 2.x cannot load the entry.
2. Make sure that Home Assistant is 2025.6.0 or later.
3. Update the integration in HACS.
4. Restart Home Assistant.
5. If the log contains "is a removed meshcore internal", update the integration that the line names. See [Deprecated internals](#deprecated-internals).
6. Update the automations that use the outgoing message events. See [Outgoing messages](#outgoing-messages).
7. If you have two or more companions, add an `entry_id` or `device_id` filter to event triggers.
8. Remove `execute_command` calls that use a [denied command](cli-commands.md#denied-commands).
9. If a tool reads channel secrets from events or MQTT, enable **Expose Node Secrets in Events**. Read the caution in [Secrets](#secrets) first.
10. Read [Before you change to Governed](legacy-traffic-policy.md#before-you-change-to-governed).
11. Change the traffic policy to Governed. See [Change to Governed](legacy-traffic-policy.md#change-to-governed).

## Event changes

### Entry identity on every event

Each `meshcore_*` event has two new fields:

| Field | Value |
|---|---|
| `entry_id` | The ID of the integration entry that fired the event |
| `device_id` | The device registry ID of the companion. It is `null` before the device is registered. |

In `meshcore_cli_response`, `resolved_entry_id` is the entry that ran the command. `entry_id` is the entry that the call named, or `null` if the call named no entry.

To use events from one companion only, filter on `device_id`. The device ID is in the URL of the device page. See [Two or more companions](multiple-companions.md).

```yaml
triggers:
  - trigger: event
    event_type: meshcore_message
    event_data:
      device_id: <device_id>
```

### Outgoing messages

The integration now reports an outgoing message when the companion accepts it. The final result comes later as a `meshcore_delivery_update` event with `progressive: false`. Only `send_message`, `send_channel_message` and `send_ui_message` fire these events. See [Message event order](events.md#message-event-order).

| Item | 2.x | 3.0 |
|---|---|---|
| Outgoing channel `meshcore_message` | After the repeat collection, with the repeater count | When the companion accepts the message, with `repeater_count: 0` |
| Final repeater count of a channel message | In `meshcore_message` | In `meshcore_delivery_update` with `progressive: false` |
| Repeat collection time | 4 s | 4 to 20 s, from the airtime of the packet |
| `meshcore_message_sent` for a direct message | One time, after the ACK wait | Two times: on accept (`progressive: true`) and after the ACK wait |
| Send failure | A log line only | `meshcore_message_send_failed` |
| Direct message from a sender that is not a contact | No event | `meshcore_message` with `sender_name: null` |

To get only the final `meshcore_message_sent` event, add this condition:

```yaml
conditions:
  - condition: template
    value_template: "{{ not trigger.event.data.get('progressive', false) }}"
```

To get the final result of each message that you send:

```yaml
triggers:
  - trigger: event
    event_type: meshcore_delivery_update
    event_data:
      outgoing: true
      progressive: false
```

A channel message event has a `repeats_observable` field. It is `false` when the sender name, `: ` and the text are more than 139 bytes. Home Assistant cannot hear repeats of such a message, so **Last Message Delivery** shows `Unconfirmed` in place of `0 Repeaters`.

### meshcore_message_send_failed

The message services fire this event when a message does not leave the companion. The `reason` field is `contact_not_found`, `traffic_policy`, `rejected`, `send_failed` or `not_connected`. Only `traffic_policy` also raises an error in the service call. For all fields, see [Events](events.md).

```yaml
triggers:
  - trigger: event
    event_type: meshcore_message_send_failed
actions:
  - action: persistent_notification.create
    data:
      title: MeshCore send failed
      message: "{{ trigger.event.data.message_type }} message: {{ trigger.event.data.reason }}"
```

## Options apply without a reload

In 2.x, each option change reloaded the entry and reset the poll schedules. In 3.0, only a Reconfigure of the connection, **Enable Self Diagnostics** or **Enable CLI Console** reloads the entry.

A change in **Manage MQTT Brokers** restarts only the MQTT uploader. The broker connection sensors follow the change.

## execute_command

- The integration refuses some commands. See [Denied commands](cli-commands.md#denied-commands). The `reboot` command still works.
- `add_selected_contact` and `remove_selected_contact` are now admin-only, the same as `execute_command`. With two or more entries, they require `entry_id`.
- Each firmware error response has `"error": "rejected"`, so `is_error` is `true`. See [Response shapes](services.md#response-shapes).

## Secrets

**Expose Node Secrets in Events** is off by default. When it is off, the integration replaces each `channel_secret` and `secret` value with `<redacted>` in `meshcore_raw_event` and in MQTT raw payloads. It also does not forward private key exports. `meshcore_cli_response` and the CLI Console show `<redacted>` in place of private keys and channel secrets. The integration still decrypts channel messages.

CAUTION: Do not enable **Expose Node Secrets in Events** on a system that other people can read. All users and tools that read the event bus or the MQTT broker will see the secrets.

## Companion entity IDs

New companion telemetry sensors and a new companion GPS tracker have the public key prefix and the name of the companion in the entity ID. Thus two entries do not use the same entity ID.

| Entity | 2.x | 3.0 |
|---|---|---|
| Companion telemetry | `sensor.meshcore_meshcore_<type>_ch<n>` | `sensor.meshcore_<pk6>_<type>_ch<n>_<name>` |
| Companion GPS tracker | `device_tracker.meshcore_meshcore_gps` | `device_tracker.meshcore_<pk6>_gps_<name>` |

The unique IDs did not change. Thus an entity from 2.x keeps its old entity ID, and your automations continue to work.

## Deprecated internals

3.0 removed internal names that some companion integrations used, for example `meshcore-ha-chat`. Three names still work and log a warning the first time a caller uses them:

```
<name> is a removed meshcore internal, called from <file>:<line>. It still works in 3.0 but will be deleted in a future release; <advice>
```

| Name | Replacement |
|---|---|
| `coordinator._repeater_stats` | None. It was always empty. Use the repeater sensors. |
| `api.mesh_core` | Use the MeshCore services |
| `api._cache_self_info_event` | `api.cache_self_info_event` |

## Stored settings

The upgrade migrates each entry to version 4. 2.x cannot load an entry of version 4. To go back to 2.x, restore the backup that you made before the upgrade.

- The user settings move from the entry data to the entry options.
- The repeater firmware versions move to the device registry.
- The public key of the companion becomes the unique ID of the entry. You cannot add the same companion two times. If two entries have the same key, the second entry keeps working and the log shows "already identifies entry".
