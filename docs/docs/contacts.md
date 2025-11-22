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

If you want to completely clear all discovered contacts (e.g., starting fresh or removing old/invalid entries):

1. Stop Home Assistant
2. Delete the storage file: `.storage/meshcore.<entry_id>.discovered_contacts`
3. Start Home Assistant

Replace `<entry_id>` with your config entry ID (found in the URL when viewing the device in Settings → Devices & Services).

**Note**: This only clears the discovered contacts list. Contacts that are already added to your node will remain added and will need to be removed separately using the remove contact service.

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
- `type` - Node type (0=Client, 1=Repeater, 2=Room Server)
- `last_advert` - Unix timestamp of last advertisement
- `last_advert_formatted` - ISO formatted timestamp
- Location data (if available): `latitude`, `longitude`

### Entity Icons

Sensors show different icons based on node type and state:
- **Client**: `mdi:account` (fresh) / `mdi:account-off` (stale)
- **Repeater**: `mdi:radio-tower` (fresh) / `mdi:radio-tower-off` (stale)
- **Room Server**: `mdi:forum` (fresh) / `mdi:forum-outline` (stale)
- **Unknown**: `mdi:help-network`

### Entity Pictures

Contact sensors include custom entity pictures showing the node type and status with visual indicators.

## Automatic Contact Syncing

The integration automatically syncs contacts with your node:

1. **On Startup**: Loads discovered contacts from storage and syncs added contacts from node
2. **Periodic Updates**: Checks every update interval (default 10 seconds) if contacts need syncing
3. **After Add/Remove**: Immediately syncs after manual contact changes
4. **Dirty Flag Detection**: Uses SDK's internal `_contacts_dirty` flag to minimize unnecessary syncs

The `ensure_contacts(follow=True)` method efficiently syncs only when changes are detected.
