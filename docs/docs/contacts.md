---
sidebar_position: 6
title: Contact Management
---

# Contact Management

The MeshCore integration uses **manual contact management mode** to give you full control over which devices are added to your node's contact list.

## How It Works

### Manual Mode

When the integration starts, it automatically sets your node to manual contact management mode using `set_manual_add_contacts(True)`. This means:

- **Discovered contacts** are NOT automatically added to your node
- You must explicitly add contacts you want to communicate with
- Contacts remain in "discovered" state until you manually add them

### Contact States

Contacts can be in one of three states:

1. **Discovered** - Device has been seen on the mesh network but not added to your node
   - Shown with state `discovered`
   - Cannot send/receive messages until added
   - Persisted across Home Assistant restarts

2. **Fresh** - Contact is added to your node and recently active
   - Shown with state `fresh`
   - Last advertisement within 12 hours
   - Can send/receive messages

3. **Stale** - Contact is added to your node but not recently seen
   - Shown with state `stale`
   - Last advertisement over 12 hours ago
   - Can still send messages, but device may be offline

### Contact Discovery

When a device broadcasts on the mesh network, the integration:
1. Receives a `NEW_CONTACT` event from the SDK
2. Stores the contact in `_discovered_contacts` (persisted to `.storage`)
3. Creates a diagnostic binary sensor showing the contact as "discovered"
4. Makes it available in the "Discovered Contacts" dropdown

#### Disabling Contact Discovery

If you have many contacts on your mesh network but only want to track specific repeaters or clients, you can disable automatic contact discovery:

**To disable:**
1. Go to **Settings → Devices & Services**
2. Find your MeshCore integration
3. Click **Configure**
4. Select **Global Settings**
5. Enable **Disable Contact Discovery**
6. Click **Submit**

**When disabled:**
- No contact binary sensors are automatically created
- Discovered contacts are not persisted to storage
- Contact selectors (discovered/added) will not populate
- Reduces overhead if you have 50+ contacts on the network
- You can still manually track specific devices using repeater/client tracking

**When to use:**
- Large mesh networks with many nodes you don't need to monitor
- Performance optimization when you only care about tracked repeaters/clients
- Reducing entity count in Home Assistant
- You want to use services/automations without contact entities

#### Limiting Discovered Contacts

If you want to keep contact discovery enabled but prevent thousands of contacts from accumulating, you can set a maximum limit:

**To enable:**
1. Go to **Settings → Devices & Services**
2. Find your MeshCore integration
3. Click **Configure**
4. Select **Global Settings**
5. Enable **Limit Discovered Contacts**
6. Set **Maximum Discovered Contacts** (default: 100, range: 1–10,000)
7. Click **Submit**

**How it works:**
- Uses FIFO (first-in, first-out) eviction — the oldest discovered contacts are removed first
- When a contact re-advertises, it moves to the back of the queue so active contacts are not evicted
- Evicted contacts have their binary sensor entities automatically removed
- The limit is enforced on startup and whenever a new contact is discovered
- Changing the limit takes effect immediately

#### Large Mesh Mode

On dense meshes the integration can create hundreds of per-contact binary sensors that sit permanently in the `discovered` state — never promoted, never charted, never used in automations — while the create/evict/cleanup churn drives repeated entity-registry rewrites. **Large Mesh Mode** is an opt-in, default-off setting that tracks discovered contacts as data only: when enabled, discovered (un-added) contacts are not given a per-contact entity, while added/curated contacts keep their entities exactly as before.

This mirrors the firmware's own two-tier model — a transient "advert heard" tier versus a durable "stored contact" tier. Added contacts are the durable tier and always get an entity; discovered contacts are the transient tier and, in this mode, stay as data.

**To enable (existing install):**
1. Go to **Settings → Devices & Services**
2. Find your MeshCore integration
3. Click **Configure**
4. Select **Global Settings**
5. Enable **Large Mesh Mode (data-only discovered contacts)**
6. Click **Submit**

You can also enable it at install time — the **Large Mesh Mode** checkbox appears in the USB, Bluetooth, and Network setup steps. The same setting is used either way, so it can be flipped later in Global Settings.

**What still works when enabled:**
- The **Discovered Contacts** dropdown still lists every discovered contact (it reads coordinator data, not entities)
- Adding a discovered contact still works — promotion to your node creates its entity exactly as before
- Messaging, channels, repeater/neighbor telemetry, the chat panel, and all services are unaffected (none are per-discovered-contact)
- The aggregate [Discovered Contact Summary sensor](#discovered-contact-summary-sensor) and the [`get_discovered_contact` service](#get-discovered-contact) keep the data-only contacts inspectable

**The one trade-off:** discovered contacts no longer have an individual `binary_sensor`, so you lose per-discovered-contact connectivity state, history charting, and per-contact automations *for un-added contacts*. Added contacts are unaffected. If you rely on per-discovered-contact entities, leave this off (the default).

**Toggling on** removes the now-orphaned per-discovered-contact entities in one pass on the reload that follows; the contact-selector entities and all added-contact entities are preserved.

## Managing Contacts via UI

Use this card to manage discovered and added contacts:

```yaml
type: entities
title: Manage Contacts
entities:
  - entity: select.meshcore_discovered_contact
    name: Discovered
    secondary_info: last-changed
  - type: button
    name: ➕ Add Contact
    action_name: Add
    tap_action:
      action: call-service
      service: meshcore.add_selected_contact
  - type: button
    name: 🗑️ Remove Discovered
    action_name: Remove
    tap_action:
      action: call-service
      service: meshcore.remove_discovered_contact
  - entity: select.meshcore_added_contact
    name: Added
    secondary_info: last-changed
  - type: button
    name: ➖ Remove Contact
    action_name: Remove
    tap_action:
      action: call-service
      service: meshcore.remove_selected_contact
```

**Actions:**

- **Add Contact**: Adds the selected discovered contact to your node
  - Contact is added to node's contact list
  - Sensor updates to show state `fresh` or `stale`
  - You can now send/receive messages

- **Remove Discovered**: Removes the selected discovered contact from Home Assistant
  - Contact removed from discovered list
  - Binary sensor entity removed
  - **Does NOT remove from node** (use if never added)

- **Remove Contact**: Removes the selected added contact from your node
  - Contact removed from node's contact list
  - Sensor becomes unavailable
  - If device broadcasts again, reappears as "discovered"

### Multiple Devices

If you have multiple MeshCore devices, specify the `entry_id` in the service call:

```yaml
tap_action:
  action: call-service
  service: meshcore.add_selected_contact
  data:
    entry_id: "abc123def456"  # Your config entry ID
```

You can find your `entry_id` in the URL when viewing the device in Settings → Devices & Services.

## Managing Contacts via Services

### Add Contact

Manually add a discovered contact to your node:

```yaml
service: meshcore.execute_command
data:
  command: add_contact <pubkey_prefix>
```

Example:
```yaml
service: meshcore.execute_command
data:
  command: add_contact 1a2b3c4d5e6f
```

### Remove Contact

Remove a contact from your node:

```yaml
service: meshcore.execute_command
data:
  command: remove_contact <pubkey_prefix>
```

Example:
```yaml
service: meshcore.execute_command
data:
  command: remove_contact 1a2b3c4d5e6f
```

### Remove Discovered Contact

Remove a discovered contact from Home Assistant (without removing from node):

```yaml
service: meshcore.remove_discovered_contact
data:
  pubkey_prefix: <pubkey_prefix>
```

Example:
```yaml
service: meshcore.remove_discovered_contact
data:
  pubkey_prefix: 1a2b3c4d5e6f
```

Or use without specifying pubkey_prefix to use the selected contact from the discovered contact dropdown:

```yaml
service: meshcore.remove_discovered_contact
```

**Note**: This only removes the contact from Home Assistant's discovered list and removes the binary sensor entity. It does **NOT** remove the contact from your node's contact list. Use this to clean up discovered contacts you don't want to track.

### Get Discovered Contact

Return the full data dict for a single discovered (un-added) contact, matched by public-key prefix. This is the supported way to inspect a discovered contact in [Large Mesh Mode](#large-mesh-mode), where discovered contacts have no per-contact entity:

```yaml
service: meshcore.get_discovered_contact
data:
  pubkey_prefix: 1a2b3c4d5e6f
```

The `pubkey_prefix` accepts the 12-character prefix shown in the discovered-contact dropdown, or a full public key. This service returns a response (`SupportsResponse.ONLY`); call it from a script/automation with `response_variable`, or use **Developer Tools → Actions** and check **Return response**:

```yaml
action: meshcore.get_discovered_contact
data:
  pubkey_prefix: 1a2b3c4d5e6f
response_variable: result
```

The response is `{"contact": { ...full contact dict... }}` on a match, or `{"contact": null, "error": "not_found", "pubkey_prefix": "..."}` when no discovered contact starts with that prefix. The returned data is the same already exposed via `get_contacts` and the dropdown — pubkeys are mesh-advertised, not secret.

For multiple devices, specify the entry_id:
```yaml
service: meshcore.get_discovered_contact
data:
  pubkey_prefix: 1a2b3c4d5e6f
  entry_id: "abc123def456"
```

### Cleanup Unavailable Contacts

After removing contacts, their sensors become unavailable but remain in your entity list. Use this service to remove all unavailable contact sensors at once:

```yaml
service: meshcore.cleanup_unavailable_contacts
```

**Dashboard Button:**
```yaml
type: button
name: Cleanup Unavailable Contacts
icon: mdi:broom
tap_action:
  action: call-service
  service: meshcore.cleanup_unavailable_contacts
```

For multiple devices, specify the entry_id:
```yaml
service: meshcore.cleanup_unavailable_contacts
data:
  entry_id: "abc123def456"
```

## Contact Persistence

### Discovered Contacts

Discovered contacts are persisted to Home Assistant's `.storage` directory:
- Location: `.storage/meshcore.<entry_id>.discovered_contacts`
- Format: JSON dictionary keyed by public key
- Automatically saved when new contacts are discovered
- Loaded on integration startup

#### Clearing Discovered Contacts

To remove all discovered contacts at once:

```yaml
service: meshcore.clear_discovered_contacts
```

This removes all discovered contacts and their binary sensor entities from Home Assistant. It does **NOT** remove contacts from your node's contact list.

##### Clearing Only Stale Contacts

To remove only contacts whose last update is older than a threshold, pass `days_threshold`. Contacts saved to the node (`added_to_node`) are always preserved:

```yaml
service: meshcore.clear_discovered_contacts
data:
  days_threshold: 30
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `days_threshold` | No | — (clears all) | Remove contacts whose last update is older than this many days (1–365). When omitted, all discovered contacts are removed. |
| `entry_id` | No | First available | Config entry ID for multi-device setups. |

##### Automatic Cleanup

Enable automatic daily cleanup in **Integration Options → Global Settings**:

- **Auto-Cleanup Stale Discovered Contacts** — toggle to enable/disable
- **Stale Contact Threshold (days)** — age threshold for removal (default: 30)

When enabled, stale contacts are removed once per day during the coordinator update cycle.

##### Recommended Automation

If you prefer to control timing via an automation instead of the built-in auto-cleanup:

```yaml
alias: "Clear stale discovered MeshCore contacts"
trigger:
  - trigger: time
    at: "03:00:00"
action:
  - action: meshcore.clear_discovered_contacts
    data:
      days_threshold: 30
```

**Dashboard Button:**
```yaml
type: button
name: Clear Stale Contacts
icon: mdi:account-clock
tap_action:
  action: perform-action
  perform_action: meshcore.clear_discovered_contacts
  data:
    days_threshold: 30
```

For multiple devices, specify the entry_id:
```yaml
service: meshcore.clear_discovered_contacts
data:
  days_threshold: 30
  entry_id: "abc123def456"
```

### Added Contacts

Contacts added to your node are managed by the MeshCore device itself:
- Stored in the device's internal memory
- Synced to Home Assistant on startup
- Re-synced whenever the contact list changes

## Contact Sensors

The integration creates diagnostic binary sensors for each contact:

### Attributes

Each contact sensor includes detailed attributes:
- `public_key` - Full public key
- `pubkey_prefix` - First 12 characters
- `adv_name` - Advertised name
- `added_to_node` - Whether contact is added (true/false)
- `type` - Node type (1=Client, 2=Repeater, 3=Room Server, 4=Sensor)
- `last_advert` - Unix timestamp of last advertisement
- `last_advert_formatted` - ISO formatted timestamp
- Location data (if available): `latitude`, `longitude`

### Entity Icons

Sensors show different icons based on node type and state:
- **Client**: `mdi:account` (fresh) / `mdi:account-off` (stale)
- **Repeater**: `mdi:radio-tower` (fresh) / `mdi:radio-tower-off` (stale)
- **Room Server**: `mdi:forum` (fresh) / `mdi:forum-outline` (stale)
- **Sensor**: `mdi:smoke-detector-variant` (fresh) / `mdi:smoke-detector-variant-off` (stale)
- **Unknown**: `mdi:help-network`

### Entity Pictures

Contact sensors include custom entity pictures showing the node type and status with visual indicators.

### Discovered Contact Summary Sensor

The integration also creates one aggregate summary sensor per device, `sensor.meshcore_<node>_discovered_summary`, whose state is the total count of discovered contacts. It is useful in both modes — and is the at-a-glance rollup for the data-only contacts in [Large Mesh Mode](#large-mesh-mode).

Attributes are a small, bounded rollup (constant in size regardless of how many contacts are discovered):

- `fresh_count` / `stale_count` — split on the 12-hour advert freshness window
- `by_type` — counts by node type: `chat`, `repeater`, `room_server`, `sensor`, `unknown`
- `newest` — the most-recently-heard advert: `adv_name`, `pubkey_short` (12-char prefix), `last_advert`
- `capacity` — `max_discovered_contacts` when **Limit Discovered Contacts** is enabled, otherwise `unlimited`
- `capacity_used_pct` — percent of capacity in use (only when the limit is enabled)

This sensor is **disabled by default** and lives under the **diagnostic** category. Its state changes on every advert, so leaving it always-on would write a recorder time-series on every install — exactly the recorder churn Large Mesh Mode exists to reduce. Enable it from the entity's settings only if you want to chart the discovered count; the `by_type` / `newest` attributes are then chartable too.

## Automatic Contact Syncing

The integration automatically syncs contacts with your node:

1. **On Startup**: Loads discovered contacts from storage and syncs added contacts from node
2. **Periodic Updates**: Checks every update interval (default 10 seconds) if contacts need syncing
3. **After Add/Remove**: Immediately syncs after manual contact changes
4. **Dirty Flag Detection**: Uses SDK's internal `_contacts_dirty` flag to minimize unnecessary syncs

The `ensure_contacts(follow=True)` method efficiently syncs only when changes are detected.
