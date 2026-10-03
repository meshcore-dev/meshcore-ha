---
sidebar_position: 8
title: Contact Management
---

# Contact Management

The integration keeps two contact lists for each entry:

- **Added contacts** are the contacts that the companion stores. You can send messages to them.
- **Discovered contacts** are nodes that the companion heard but did not store. Home Assistant keeps this list.

The integration puts the companion in manual contact mode each time it connects. The firmware then does not store a new node by itself. It reports the advert to Home Assistant, and the integration adds the node to the discovered list. You decide which discovered contacts to add to the companion.

## Contact states

| State | Meaning |
|---|---|
| `discovered` | The node is in the discovered list, and it is not on the companion. You cannot send a direct message to it. |
| `fresh` | The contact is on the companion, and its last advert is less than 12 hours old. |
| `stale` | The contact is on the companion, and its last advert is 12 hours old or older. The node can be offline. |

The advert time (`last_advert`) comes from the clock of the node that sent the advert. A node with a wrong clock can show the wrong state.

## Contact Discovery Mode

**Contact Discovery Mode** sets what the integration does with discovered contacts. Added contacts always get a contact entity.

| Mode | Stored value | Discovered list | Entity for each discovered contact | Use it when |
|---|---|---|---|---|
| **Entity per contact** (default) | `full` | Kept and saved | Yes | You want history and automations for each discovered contact. |
| **Data only** | `data_only` | Kept and saved | No | You do not want an entity for each discovered contact, for example on a mesh with hundreds of nodes. |
| **Disabled** | `off` | Cleared; new adverts are ignored | No | You only monitor tracked nodes. |

To set the mode:

1. Go to **Settings > Devices & services > MeshCore > Configure**.
2. Select **Global Settings**.
3. Select a **Contact Discovery Mode**.
4. Select **Submit**.

The new mode applies at once, without a reload. **Data only** and **Disabled** remove the entities of discovered contacts. This includes their telemetry and GPS entities, but not the entities of tracked nodes. **Disabled** also clears the discovered list.

In **Data only** mode, the **MeshCore Discovered Contact** dropdown, **Add Contact** and all services work. Use the [Discovered Contact Summary sensor](#discovered-contact-summary-sensor) and the [`get_discovered_contact` service](#get-discovered-contact) to see the discovered contacts.

In **Disabled** mode, a contact that another app adds gets its entity at the next start of the entry.

## Limit the discovered contacts {#limiting-discovered-contacts}

1. Go to **Settings > Devices & services > MeshCore > Configure**.
2. Select **Global Settings**.
3. Enable **Limit Discovered Contacts**.
4. Set **Maximum Discovered Contacts** (default 100, range 1 to 10,000).
5. Select **Submit**.

The list is first in, first out. A contact that advertises again moves to the end of the list, so active contacts stay. The integration removes the entity of an evicted contact.

When the entry starts, the integration trims the list to the limit but does not remove the entities of the trimmed contacts. These entities become unavailable. To remove them, run [Cleanup Unavailable Contacts](#cleanup-unavailable-contacts).

## Contact entities

```text
binary_sensor.meshcore_<adv name>_<pk12>_contact
```

Example: `binary_sensor.meshcore_myrepeater_def456abc012_contact`. `<adv name>` is the advertised name as a slug. `<pk12>` is the first 12 hex characters of the public key.

The entity is a diagnostic entity on the companion device. Its name is the advertised name plus the node type, for example `myrepeater (Repeater)`. Its state is `discovered`, `fresh` or `stale`. If the contact is in neither list, the entity is unavailable. For a known node type, the icon changes when the advert is 12 hours old or older.

### Attributes

The entity shows all fields of the contact record, plus fields that the integration adds.

| Attribute | Notes |
|---|---|
| `public_key` | Full public key, 64 hex characters. |
| `pubkey_prefix` | First 12 hex characters of the public key. |
| `pubkey_short` | First 2 hex characters. A route with 1-byte hashes shows this value for the node. |
| `adv_name` | Advertised name. |
| `type`, `node_type_str` | `1` `Client`, `2` `Repeater`, `3` `Room Server`, `4` `Sensor`. |
| `added_to_node` | `true` if the contact is on the companion. |
| `out_path_len` | `-1`: no known route. `0`: a direct neighbor. `N`: the number of hops. |
| `out_path_hash_mode` | `-1`: no known route. Otherwise each hop hash is `out_path_hash_mode + 1` bytes. |
| `out_path` | The route as hop hashes in hex. Empty if there is no route. |
| `last_advert`, `last_advert_formatted` | Time of the last advert, from the clock of the advertising node. Unix time and ISO 8601. |
| `lastmod` | Unix time of the last change to the record, from the companion clock. |
| `latitude`, `longitude` | Advertised location. Only if the node sends a location. |
| `adv_path`, `adv_path_len`, `adv_path_time` | The route, hop count and timestamp of the last advert. Absent until the integration gets an advert route after it starts. |
| `entity_picture` | The icon of the node type. Green when the last advert is less than 12 hours old. Absent for an unknown node type. |

The integration reads `adv_path` from the companion. This request sends no mesh traffic. If the firmware does not support the request, the integration stops after 3 failures in a row.

### Discovered Contact Summary Sensor

The integration creates one **Discovered Contacts** sensor for each entry, for example `sensor.meshcore_abc123_discovered_summary_mynode`. The state is the number of contacts in the discovered list.

| Attribute | Notes |
|---|---|
| `fresh_count`, `stale_count` | Contacts with an advert less than 12 hours old, and all others. |
| `by_type` | Counts with the keys `chat`, `repeater`, `room_server`, `sensor` and `unknown`. |
| `newest` | The contact with the latest advert, as `adv_name`, `pubkey_short` (12 characters) and `last_advert`. Null if the list is empty. |
| `capacity` | **Maximum Discovered Contacts** if the limit is enabled. Otherwise `unlimited`. |
| `capacity_used_pct` | The percentage of the limit in use. Null if the limit is not enabled. |

The default for this sensor is disabled. Its state changes on each advert, so the recorder writes many rows. Enable it only if you want to chart the count.

## Manage contacts in the UI

The integration creates two hidden dropdowns for each entry: `select.meshcore_discovered_contact` and `select.meshcore_added_contact`. With two or more entries, Home Assistant adds a suffix such as `_2` to the entity IDs of the second entry.

```yaml
type: entities
title: Manage Contacts
entities:
  - entity: select.meshcore_discovered_contact
    name: Discovered
  - type: button
    name: Add Contact
    action_name: Add
    tap_action:
      action: perform-action
      perform_action: meshcore.add_selected_contact
  - type: button
    name: Remove Discovered
    action_name: Remove
    tap_action:
      action: perform-action
      perform_action: meshcore.remove_discovered_contact
  - entity: select.meshcore_added_contact
    name: Added
  - type: button
    name: Remove Contact
    action_name: Remove
    tap_action:
      action: perform-action
      perform_action: meshcore.remove_selected_contact
```

| Button | Result |
|---|---|
| **Add Contact** | Adds the selected discovered contact to the companion. |
| **Remove Discovered** | Removes the selected contact from the discovered list and removes its entity. The companion does not change. |
| **Remove Contact** | Removes the selected contact from the companion. In **Entity per contact** mode, if the contact is still in the discovered list, its entity changes to `discovered`. If the node advertises again, it comes back as a discovered contact, except in **Disabled** mode. |

If you have two or more entries, add `entry_id` to each action. Without `entry_id`, the service uses the first dropdown that it finds, and can send the command to a different companion. To find the `entry_id`, see [Select the entry](services.md#select-the-entry).

```yaml
tap_action:
  action: perform-action
  perform_action: meshcore.add_selected_contact
  data:
    entry_id: YOUR_ENTRY_ID
```

## Contact services

| Service | Admin only | Response | Purpose |
|---|---|---|---|
| `meshcore.add_selected_contact` | yes | none | Add the contact selected in the discovered dropdown. |
| `meshcore.remove_selected_contact` | yes | none | Remove the contact selected in the added dropdown from the companion. |
| `meshcore.remove_discovered_contact` | no | none | Remove one contact from the discovered list. |
| `meshcore.get_discovered_contact` | no | only | Return one discovered contact. |
| `meshcore.get_contacts` | no | only | Return all contacts. See [Companion Integration API](companion-integration-api.md). |
| `meshcore.clear_discovered_contacts` | no | none | Remove all, or only old, discovered contacts. |
| `meshcore.cleanup_unavailable_contacts` | no | none | Remove unavailable contact entities. |
| `meshcore.execute_command` | yes | optional | Run `add_contact` or `remove_contact` directly. |

All of these services accept an optional `entry_id`.

### Admin-only services

Home Assistant refuses a call to an admin-only service from a user who is not an administrator. It accepts a call with no user, for example from an automation.

### Add or remove a contact by key

```yaml
action: meshcore.execute_command
data:
  command: add_contact def456abc012
```

Use `remove_contact` in the same way. The argument is a public key prefix or the advertised name, with 6 or more characters. For a contact on the companion, the match is not case-sensitive. The search of the discovered list is case-sensitive: use the lowercase prefix or the exact name.

### Remove Discovered Contact

```yaml
action: meshcore.remove_discovered_contact
data:
  pubkey_prefix: def456abc012
```

The prefix must match one discovered contact. The service removes the contact and its entity. A later advert from the node creates them again. If you do not give `pubkey_prefix`, the service uses the contact selected in the discovered dropdown.

### Get Discovered Contact

This service returns the full record of one discovered contact. Use it in **Data only** mode, where discovered contacts have no entity.

```yaml
action: meshcore.get_discovered_contact
data:
  pubkey_prefix: def456abc012
response_variable: result
```

For the prefix rules and the responses, see [Services](services.md#get-discovered-contact).

### Cleanup Unavailable Contacts

```yaml
action: meshcore.cleanup_unavailable_contacts
```

The service removes contact entities that are unavailable. It does not remove other MeshCore entities. Without `entry_id`, it acts on all entries.

## Cleanup of discovered contacts

The integration saves the discovered list in Home Assistant storage and loads it when the entry starts.

### Clear the discovered contacts {#clearing-discovered-contacts}

```yaml
action: meshcore.clear_discovered_contacts
data:
  days_threshold: 30
```

Without `days_threshold`, the service removes all discovered contacts. It removes their entities, except the entities of contacts on the companion. It also removes contact entities that have no contact in either list. The companion does not change.

With `days_threshold` (1 to 365), the service removes contacts whose `lastmod` is older than this number of days. It keeps contacts that you added through the integration, also after you remove them from the companion. It also removes contact entities that have no contact in either list.

### Automatic cleanup

Enable automatic cleanup in **Global Settings**:

| Setting | Default | Range |
|---|---|---|
| **Auto-Cleanup Stale Discovered Contacts (runs daily)** | Off | |
| **Stale Contact Threshold (days)** | 30 | 1 to 365 |

The cleanup runs after the entry starts, and then every 24 hours. It does the same work as `clear_discovered_contacts` with `days_threshold`. To control the time of the cleanup, call the service from an automation with a time trigger instead.
