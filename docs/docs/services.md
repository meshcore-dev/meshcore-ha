---
sidebar_position: 3
title: Services
---

# Services

The Meshcore Home Assistant integration provides several services to interact with your mesh network, from sending messages to executing advanced commands.

## Available Services

### Send Message
Send a direct message to a specific node in the mesh network.

**Service:** `meshcore.send_message`

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `node_id` | string | One required* | The name of the node to send to |
| `pubkey_prefix` | string | One required* | Public key prefix (min 6 chars) |
| `message` | string | Yes | The message text to send |
| `entry_id` | string | No | Config entry ID for multiple devices |

*Either `node_id` or `pubkey_prefix` is required, not both. A call that names
neither is refused with an error rather than failing silently.

**Examples:**

Using node name:
```yaml
service: meshcore.send_message
data:
  node_id: "Weather Station"
  message: "Request status update"
```

Using public key prefix:
```yaml
service: meshcore.send_message
data:
  pubkey_prefix: "f293ac"
  message: "Hello from Home Assistant!"
```

### Send Channel Message
Broadcast a message to all nodes on a specific channel.

**Service:** `meshcore.send_channel_message`

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `channel_idx` | integer | Yes | Channel index (0-255, typically 0-3) |
| `message` | string | Yes | The message text to broadcast |
| `entry_id` | string | No | Config entry ID for multiple devices |
| `scope` | string | No | Region scope for flood routing (e.g. `"pl-waw"` or `"#pl-waw"`) |

**Examples:**

Global broadcast (no scope):
```yaml
service: meshcore.send_channel_message
data:
  channel_idx: 0
  message: "General announcement to all nodes"
```

Region-scoped broadcast:
```yaml
service: meshcore.send_channel_message
data:
  channel_idx: 0
  message: "Hello Warsaw region!"
  scope: "waw"
```

:::info
When `scope` is provided the integration calls `set_flood_scope` on the device before sending, then immediately resets it to no scope — even if the send fails. The `scope` used is included in the resulting `meshcore_message_sent` event.
:::

### Execute Command
Execute Meshcore SDK commands directly for advanced control.

**Service:** `meshcore.execute_command`

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `command` | string | Yes | Command with parameters |
| `entry_id` | string | No | Config entry ID for multiple devices |
| `record_to_console` | boolean | No | Record the command/response to the CLI Console and fire `meshcore_cli_response` (default `false`) |

**Common Commands:**

- `get_bat` - Get battery level
- `set_name "NewName"` - Set node name
- `set_tx_power 20` - Set transmit power (dBm)
- `send_msg "NodeName" "Message"` - Send direct message
- `send_chan_msg 0 "Message"` - Send channel message

For a complete list of available commands and their parameters, see the [Meshcore Python SDK documentation](https://github.com/meshcore-dev/meshcore_py).

**Commands that are not available:**

These are refused before they reach the radio, because each one either
destroys the node's identity, wipes its configuration, or hands the radio raw
frames that bypass every check the integration makes:

`reboot`, `request_factory_reset`, `confirm_factory_reset`,
`import_private_key`, `send_raw_packet`, `send_raw_data`, and any name
starting with `_`.

Use the node's own console (USB/BLE) for these.

**Syntax Formats:**

Commands can be written in two formats:

- **Space-separated** (traditional): `set_tx_power 15`
- **Functional** (Python-style): `set_tx_power(15)`

Both formats are equivalent. The functional syntax supports positional arguments, keyword arguments, strings, numbers, booleans, and bytes literals.

**Examples:**

Set transmit power:
```yaml
service: meshcore.execute_command
data:
  command: "set_tx_power 15"
```

Using functional syntax:
```yaml
service: meshcore.execute_command
data:
  command: "set_coords(37.7749, -122.4194)"
```

Send message with command:
```yaml
service: meshcore.execute_command
data:
  command: 'send_msg "Repeater1" "Status check"'
```

### Send UI Message
Send messages using the UI helper entities. This service reads values from the Meshcore UI helpers.

**Service:** `meshcore.send_ui_message`

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entry_id` | string | No | Config entry ID for multiple devices |

This service automatically reads from:
- `select.meshcore_recipient_type` - Channel or Contact
- `select.meshcore_channel` - Selected channel
- `select.meshcore_contact` - Selected contact
- `text.meshcore_message` - Message text

**Example:**

```yaml
service: meshcore.send_ui_message
data: {}
```

### Execute Command UI
Execute commands from the UI text input helper.

**Service:** `meshcore.execute_command_ui`

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entry_id` | string | No | Config entry ID for multiple devices |
| `record_to_console` | boolean | No | Record the command/response to the CLI Console and fire `meshcore_cli_response` (default `false`) |

This service reads from `text.meshcore_command` and clears it after execution.

**Example:**

```yaml
service: meshcore.execute_command_ui
data: {}
```

### CLI Console
Pass `record_to_console: true` to `execute_command` (or `execute_command_ui`) to
**record the command and its response to the CLI Console sensor**, so the output
is visible in the UI (without the flag the response is only returned to the
caller and, for `execute_command_ui`, discarded). Enable the CLI Console under
**Global Settings → Enable CLI Console** to get the `sensor.*_cli_console`
output entity. The console records only command/response pairs — it does not
stream the radio's continuous diagnostic/noise-floor log.

> See the [CLI Command Reference](./cli-commands) for the full list of commands
> and their arguments.

**Example:**

```yaml
service: meshcore.execute_command
data:
  command: get_stats_radio
  record_to_console: true
```

Use `meshcore.cli_console_clear` to empty the transcript (no `entry_id` clears
every configured device's console).

### Query Services

These return structured responses (`response_variable`) instead of text to
parse. Full field-by-field shapes are in the
[companion integration API](./companion-integration-api).

| Service | Returns |
|---|---|
| `meshcore.get_contacts` | `{contacts: [...]}` — the device's contacts as the SDK stores them, each with `adv_name`, `public_key`, `pubkey_prefix`, `type`, `added_to_node`, `out_path_len`, `out_path`, `out_path_hash_mode` |
| `meshcore.get_discovered_contact` | `{contact: {...}}` — one discovered contact, same keys |
| `meshcore.get_channels` | `{channels: [{channel_idx, channel_name, shared_secret_present}]}` — the secret itself is never returned |
| `meshcore.trace` | `{trace: {hops, path, round_trip_ms, final_snr, tag}}`, or `{trace: null, error: "..."}` |

`trace` accepts a `pubkey_prefix` of 6 characters or more; it waits for the
reply the firmware actually sends back, so a short prefix traces the same
contact a full key does. Its `timeout` is a request, not a promise: the
effective wait is at least the firmware's suggested timeout and is capped at
60 seconds.

## Usage in Automations

### Battery Check Automation
```yaml
alias: Daily Battery Check
trigger:
  - platform: time
    at: "09:00:00"
action:
  - service: meshcore.execute_command
    data:
      command: "get_bat"
  - delay:
      seconds: 5
  - service: meshcore.send_channel_message
    data:
      channel_idx: 0
      message: "Daily battery check completed"
```

### Emergency Broadcast
```yaml
alias: Emergency Alert
trigger:
  - platform: state
    entity_id: input_boolean.emergency_mode
    to: "on"
action:
  - service: meshcore.send_channel_message
    data:
      channel_idx: 0
      message: "EMERGENCY ALERT ACTIVATED"
  - service: meshcore.execute_command
    data:
      command: "set_tx_power 20"
```

### Region-Scoped Alert
```yaml
alias: Warsaw Region Alert
trigger:
  - platform: state
    entity_id: input_boolean.warsaw_alert
    to: "on"
action:
  - service: meshcore.send_channel_message
    data:
      channel_idx: 0
      message: "Local alert for Warsaw region"
      scope: "waw"
```

### Periodic Status Request
```yaml
alias: Hourly Status Check
trigger:
  - platform: time_pattern
    hours: "*"
    minutes: "0"
action:
  - service: meshcore.send_message
    data:
      node_id: "Remote Sensor"
      message: "STATUS?"
```

## Service Events

The `meshcore_message_sent` event is fired when a message is successfully sent, which can be monitored in automations:

```yaml
alias: Log Sent Messages
trigger:
  - platform: event
    event_type: meshcore_message_sent
action:
  - service: logbook.log
    data:
      name: "Meshcore Message"
      message: "Sent: {{ trigger.event.data.message }} to {{ trigger.event.data.receiver }}"
```

## Troubleshooting

### Message Not Sent
- Verify the node name or public key exists in contacts
- Check that the Meshcore device is connected
- Ensure the target node is within range

### Command Failed
- Verify command syntax and parameters
- Check that the command is supported by your device
- Review Home Assistant logs for detailed error messages

### UI Services Not Working
- Ensure helper entities are created and have values
- Check that the Meshcore integration is properly configured
- Verify the device is connected and responding