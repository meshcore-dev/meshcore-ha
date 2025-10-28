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

## Managing Contacts via UI

### Adding Discovered Contacts

Use this card to add discovered contacts to your node:

```yaml
type: vertical-stack
cards:
  - type: entities
    entities:
      - entity: select.meshcore_discovered_contact
  - type: button
    name: Add Contact
    icon: mdi:plus-circle
    tap_action:
      action: call-service
      service: meshcore.add_selected_contact
```

**Steps:**
1. Select a discovered contact from the dropdown
2. Click "Add Contact"
3. The contact is added to your node
4. The sensor updates to show state `fresh` or `stale`
5. You can now send/receive messages with this contact

### Removing Added Contacts

Use this card to remove contacts from your node:

```yaml
type: vertical-stack
cards:
  - type: entities
    entities:
      - entity: select.meshcore_added_contact
  - type: button
    name: Remove Contact
    icon: mdi:minus-circle
    tap_action:
      action: call-service
      service: meshcore.remove_selected_contact
```

**Steps:**
1. Select an added contact from the dropdown
2. Click "Remove Contact"
3. The contact is removed from your node
4. The sensor becomes unavailable
5. If the device broadcasts again, it will reappear as "discovered"

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
