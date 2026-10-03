---
sidebar_position: 16
title: Two or more companions
---

# Two or more companions

To add a second companion, add the integration again. See [Installation](installation.md).

- The public key of the companion identifies the entry. The same companion cannot have two entries ("Device is already configured").
- Each entry has its own connection, settings, tracked nodes, MQTT brokers, discovered contacts and CLI Console.
- Each entry has its own traffic policy budget. Two companions in the same area add two budgets of traffic to the mesh. See [Mesh Traffic Policy](traffic-policy.md).

To find the `entry_id` and the `device_id`, see [Services: Select the entry](services.md#select-the-entry).

## Entity IDs

When two entries make the same ID, Home Assistant adds a suffix such as `_2` to the second one. The registry then keeps that ID.

| Entities | ID contains | Suffix `_2` possible |
|---|---|---|
| Companion, message, radio fault and MQTT broker sensors, companion telemetry sensors and GPS tracker, CLI Console, CLI buttons | The companion prefix `<pk6>` | No |
| Select and text helpers, for example `select.meshcore_channel` | No prefix | Yes, always for the second entry |
| Companion telemetry sensors and GPS tracker from an earlier version | No prefix | Yes, when both companions send them |
| Tracked node sensors, Online, neighbor sensors, firmware buttons | The node prefix `<pk10>` | Yes, when both entries track the same node |
| Telemetry sensors and GPS trackers of other nodes | The node prefix `<pk10>` | Yes, when both entries receive the same node |
| Contact sensors | The contact name and `<pk12>` | Yes, when both companions know the same contact |

## Services

With two or more entries, set `entry_id` on every service call. Without it, the services select an entry as the table shows. For all rules, see [Services: Select the entry](services.md#select-the-entry).

| Services | Without `entry_id` |
|---|---|
| `send_ui_message`, `execute_command_ui` | Fail with `ambiguous_config_entry` |
| `send_message`, `send_channel_message`, `execute_command` | The first loaded entry that is connected. If no entry is connected, the first loaded entry. |
| `get_contacts`, `get_discovered_contact`, `get_channels`, `trace` | The first loaded entry |
| `cleanup_unavailable_contacts`, `cli_console_clear` | All entries |
| `add_selected_contact`, `remove_selected_contact` | Fail with `ambiguous_config_entry` |
| `remove_discovered_contact` | With `pubkey_prefix`: the first loaded entry. Without it: fail with `ambiguous_config_entry`. |
| `clear_discovered_contacts` | The first loaded entry |

## Events

Every `meshcore_*` event has `entry_id` and `device_id`. Filter each event trigger on one of them. Otherwise, the automation runs for the events of all companions.

```yaml
triggers:
  - trigger: event
    event_type: meshcore_message
    event_data:
      entry_id: YOUR_ENTRY_ID
```

- `device_id` is `null` until the integration registers the companion device. `entry_id` does not have this problem.
- On `meshcore_cli_response`, `entry_id` is the entry that the call named, or `null`. Filter on `resolved_entry_id` or `device_id`.

## Dashboards

Make one dashboard view for each companion.

1. Copy a dashboard from [Dashboard cards](dashboard/overview.md) or [Basic Node](dashboard/basic-node.md).
2. Replace the entity IDs with those of the second entry, for example `select.meshcore_channel_2`.
3. Add `entry_id` to each card action that calls a UI or contact service:

```yaml
tap_action:
  action: perform-action
  perform_action: meshcore.send_ui_message
  data:
    entry_id: YOUR_ENTRY_ID
```

See [Dashboard cards: Two or more entries](dashboard/overview.md#two-or-more-entries).
