---
sidebar_position: 11
title: Services
---

# Services

The integration registers 15 services in the `meshcore` domain. For event fields, see [Events](events.md). For examples, see [Automation](automation.md).

## Service summary

| Service | Purpose | Response | Admin only | Governed lane |
|---|---|---|---|---|
| [`send_message`](#send-message) | Send a direct message to a contact | None | No | Messages |
| [`send_channel_message`](#send-channel-message) | Send a message to a channel | None | No | Messages |
| [`send_ui_message`](#send-ui-message) | Send the text in the UI helpers | Optional | No | Messages |
| [`execute_command`](#execute-command) | Run one companion command | Optional | Yes | Mesh commands only |
| [`execute_command_ui`](#execute-command-ui) | Run the command in the UI helper | Optional | Yes | Mesh commands only |
| [`cli_console_clear`](#cli-console-clear) | Empty the CLI Console transcript | None | No | Not metered |
| [`get_contacts`](#get-contacts) | Return all contacts | Only | No | Not metered |
| [`get_discovered_contact`](#get-discovered-contact) | Return one discovered contact | Only | No | Not metered |
| [`get_channels`](#get-channels) | Return the configured channels | Only | No | Not metered |
| [`trace`](#trace) | Trace the route to a contact | Only | No | Messages |
| [`add_selected_contact`](#contact-services) | Add the selected discovered contact to the companion | None | Yes | Not metered |
| [`remove_selected_contact`](#contact-services) | Remove the selected contact from the companion | None | Yes | Not metered |
| [`remove_discovered_contact`](#contact-services) | Remove one discovered contact from Home Assistant | None | No | Not metered |
| [`cleanup_unavailable_contacts`](#contact-services) | Remove unavailable contact sensors | None | No | Not metered |
| [`clear_discovered_contacts`](#contact-services) | Remove discovered contacts from Home Assistant | None | No | Not metered |

Response: **Optional** means that the caller can request a response with `response_variable` or **Return response**. **Only** means that a call without `response_variable` fails.

Admin only: Home Assistant refuses a call from a user who is not an administrator, with the error `Unauthorized`. Home Assistant permits a call with no user, for example from an automation.

## Select the entry

Each service accepts an optional `entry_id`: the entry of one companion. If you have one companion, do not set it.

To find the `entry_id` and the `device_id` of a companion:

1. Open **Developer Tools > States** and filter on `node_status`.
2. Copy the entity ID of the Node Status sensor, for example `sensor.meshcore_abc123_node_status_mynode`.
3. Open **Developer Tools > Template**.
4. Enter `{{ config_entry_id('sensor.meshcore_abc123_node_status_mynode') }}`. The result is the `entry_id`.
5. Enter `{{ device_id('sensor.meshcore_abc123_node_status_mynode') }}`. The result is the `device_id`.

With more than one companion, each service selects an entry as follows:

| Services | `entry_id` not set | `entry_id` not found, or no entry loaded |
|---|---|---|
| `send_message`, `send_channel_message`, `execute_command` | The first connected entry. If no entry is connected, the first loaded entry. | Logs an error and does nothing. No event fires. |
| `send_ui_message`, `execute_command_ui` | Error `ambiguous_config_entry` | Error `config_entry_not_found` |
| `get_contacts`, `get_discovered_contact`, `get_channels`, `trace` | The first loaded entry | Error `no_coordinator` |
| `cleanup_unavailable_contacts`, `cli_console_clear` | All entries | Nothing changes |
| `clear_discovered_contacts` | The first loaded entry | Logs an error and does nothing |
| `remove_discovered_contact` | With `pubkey_prefix`: the first loaded entry. Without it: error `ambiguous_config_entry`. | Error `config_entry_not_found` |
| `add_selected_contact`, `remove_selected_contact` | Error `ambiguous_config_entry` | Error `config_entry_not_found` |

See also [Two or more companions](multiple-companions.md).

## Traffic policy and service calls

Under the Governed traffic policy, each metered call takes one credit from a lane before it sends. A service call does not wait for credit. If the lane is empty, the call fails at once with this error:

```
Mesh traffic messages lane is empty; try again in 60 seconds
```

The number of seconds is the time to the next credit of the lane: a maximum of 180 (flood), 30 (direct) or 60 (messages). See [Mesh Traffic Policy: When a lane is empty](traffic-policy.md#when-a-lane-runs-dry).

| Service | Lane | Event when the lane is empty |
|---|---|---|
| `send_message`, `send_channel_message`, `send_ui_message` | Messages | `meshcore_message_send_failed` with `reason: traffic_policy` |
| `trace` | Messages | None |
| `execute_command`, `execute_command_ui` | Depends on the command. See [CLI Command Reference: Traffic policy](cli-commands.md#traffic-policy). | None. The CLI Console does not record the command. |

The error stops the automation. To continue, set `continue_on_error: true` on the action. See [Automation](automation.md#continue-when-the-traffic-policy-refuses-a-send).

Installs from 2.x that still use the deprecated [Legacy traffic policy](legacy-traffic-policy.md) do not meter service calls.

## Send Message

Sends a direct message to one contact on the companion.

| Field | Type | Required | Description |
|---|---|---|---|
| `node_id` | string | One of `node_id` or `pubkey_prefix` | The advertised name of the contact. Exact match, not case-sensitive. |
| `pubkey_prefix` | string | One of `node_id` or `pubkey_prefix` | The start of the public key of the contact. Not case-sensitive. |
| `message` | string | Yes | The message text |
| `entry_id` | string | No | See [Select the entry](#select-the-entry) |

- Set `node_id` or `pubkey_prefix`, not both. If you set neither, the call fails and no event fires.
- The contact must be on the contact list of the companion. A contact that is only discovered does not match.
- The integration does not check the prefix length, so a short prefix can match a different contact. Use 12 characters.

```yaml
action: meshcore.send_message
data:
  pubkey_prefix: "def456abc012"
  message: "Hello from Home Assistant"
```

The service returns when the companion accepts the message. It does not wait for the acknowledgement (ACK). The ACK wait is 1.2 times the timeout that the companion suggests, or 12 seconds if the companion gives no value. For the events that follow, see [Message event order](events.md#message-event-order).

If the message does not leave the companion, the integration fires `meshcore_message_send_failed`. The service call does not fail, except for `traffic_policy`. For an alert, see [Automation: Alert when a send fails](automation.md#alert-when-a-send-fails).

| `reason` | Cause |
|---|---|
| `contact_not_found` | No contact matches `node_id` or `pubkey_prefix` |
| `not_connected` | The entry is not connected to its companion |
| `rejected` | The companion refused the message. `detail` gives the firmware reason. |
| `send_failed` | An exception occurred. `detail` gives the text. |
| `traffic_policy` | The messages lane is empty. The call also fails. |

## Send Channel Message

Sends a message to a channel on the companion.

| Field | Type | Required | Description |
|---|---|---|---|
| `channel_idx` | integer | Yes | The channel index (0 or more) |
| `message` | string | Yes | The message text |
| `scope` | string | No | A region scope name for this flood, for example `myregion` or `#myregion` |
| `entry_id` | string | No | See [Select the entry](#select-the-entry) |

```yaml
action: meshcore.send_channel_message
data:
  channel_idx: 1
  message: "Hello region"
  scope: "myregion"
```

- With `scope`, the integration sets the flood scope on the companion, sends the message, then resets the scope. It also resets the scope after a failed send.
- If the companion refuses the scope, the integration logs a warning and sends the message without a scope.
- A failed send fires `meshcore_message_send_failed` with `message_type: channel`. The `reason` values are the same as for `send_message`, except `contact_not_found`.

## Send UI Message

Sends the message in the UI helper entities of one entry. Use it from a dashboard button.

| Field | Type | Required | Description |
|---|---|---|---|
| `entry_id` | string | Yes, if there is more than one entry | The entry whose helpers the service reads |

| Helper | Default entity ID (first entry) | Value used |
|---|---|---|
| Recipient type | `select.meshcore_recipient_type` | `Channel` or `Contact` |
| Channel | `select.meshcore_channel` | Attribute `channel_idx` |
| Contact | `select.meshcore_contact` | Attribute `public_key_prefix` |
| Message | `text.meshcore_message` | The message text |

The service finds each helper by its registry identity, so a renamed entity ID continues to work. It calls `send_message` or `send_channel_message`, and their rules and events apply. After the send, it clears the message helper and returns `{"success": true}`. After a traffic policy error, the text stays in the helper.

### UI service errors

`send_ui_message` and `execute_command_ui` return an error dictionary when they cannot read the helpers. They do not raise an error. Request a response or read the log (ERROR level) to see the error. Each dictionary has `error`, `message` and the extra keys below.

| `error` | Cause | Extra keys |
|---|---|---|
| `config_entry_not_found` | `entry_id` does not exist, or no entry is loaded | `entry_id`, if the call named one |
| `ambiguous_config_entry` | More than one entry and no `entry_id` | `entry_ids` |
| `helper_not_found` | The helper is not in the entity registry | `entry_id`, `helper`, `unique_id` |
| `helper_state_not_found` | The helper has no state | `entry_id`, `helper`, `entity_id` |
| `helper_state_unavailable` | The helper state is `unavailable` or `unknown` | `entry_id`, `helper`, `entity_id`, `state` |
| `message_empty` | The message helper is empty | `entry_id`, `entity_id` |
| `command_empty` | The command helper is empty | `entry_id`, `entity_id` |
| `helper_value_missing` | The channel has no `channel_idx`, or the contact has no `public_key_prefix` | `entry_id`, `helper` |
| `invalid_recipient_type` | The recipient type is not `Channel` or `Contact` | `entry_id`, `recipient_type` |

## Execute Command

Runs one command on the companion. For the syntax and the command list, see [CLI Command Reference](cli-commands.md).

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `command` | string | Yes | | The command and its arguments, for example `get_bat` or `set_tx_power 20` |
| `record_to_console` | boolean | No | `false` | Record the command in the CLI Console, and fire `meshcore_cli_response` |
| `entry_id` | string | No | | See [Select the entry](#select-the-entry) |

CAUTION: Do not run a `set_*` command if you do not know its effect. These commands make permanent changes to the companion configuration.

```yaml
action: meshcore.execute_command
data:
  command: "get_stats_radio"
response_variable: result
```

A message that you send with `send_msg` or `send_chan_msg` fires no message event and makes no logbook entry. Use `send_message` or `send_channel_message` instead.

### Response shapes

`execute_command_ui` and the `response` field of `meshcore_cli_response` use the same shapes. Bytes become hex strings.

| Result | Response | Example |
|---|---|---|
| Event with data | The event data | `get_bat` returns `{"level": 4123}` (mV) |
| Event without data | `{"event": <event type>, "command": <name>}` | `{"event": "command_ok", "command": "set_tx_power"}` |
| Error event without data | `{"error": "rejected", "command": <name>}` | |
| Error event with data | The error data, with `"error": "rejected"` added | `{"error": "rejected", "error_code": 2, "code_string": "..."}` or `{"error": "rejected", "reason": "no_event_received"}` |
| A dictionary (most `*_sync` commands) | The dictionary | `req_status_sync` returns the status fields |
| A list, number or text | `{"result": <value>}` | `get_path_hash_mode` returns `{"result": 0}` |
| No result | `{"error": "no_response", "command": <name>}` | A `*_sync` command with no reply |

Errors returned as a dictionary:

| `error` | Cause | Extra keys |
|---|---|---|
| `pubkey_prefix_too_short` | A contact argument has fewer than 6 characters | `command`, `argument`, `detail` |
| `not_connected` | The companion is not connected when the integration looks up a contact | `command`, `argument`, `detail` |
| `contact_not_found` | No contact matches the prefix or the name | `command`, `argument`, `detail` |
| `invalid_argument` | An argument is not a valid `int`, `float`, `bool` or hex value | `command`, `detail` |
| `unknown_keyword` | A keyword argument is not a parameter of the command | `command`, `argument` |
| `exception` | The library raised an exception, for example on a lost link | `command`, `detail` |
| `Incoming message consumption is disabled` | **Retrieve queued incoming messages** is off, and you ran `get_msg` | None |

Errors that make the call fail:

- `command_denied`: see [Denied commands](cli-commands.md#denied-commands).
- `traffic_deferred`: the lane is empty under Governed.
- `Unauthorized`: the user is not an administrator.

The service returns no data when the command does not parse, the command does not exist, or no entry is connected. If you request a response, Home Assistant then reports `expected a dictionary, but got <class 'NoneType'>`. The log gives the cause.

### CLI Console

Set `record_to_console: true` to record the command and its response. The integration then fires `meshcore_cli_response`, also when the CLI Console is off. See [meshcore_cli_response](events.md#meshcore_cli_response).

- The CLI Console sensor, `sensor.meshcore_<pk6>_cli_console`, shows the last 50 commands. The integration creates it only when you enable **Global Settings > Enable CLI Console**. Home Assistant hides it by default.
- The console shows only the commands that you run. It does not show the companion log.

The CLI Console and the event show `<redacted>` in place of private keys and channel secrets, unless **Expose Node Secrets in Events** is on. The service response to the caller is not redacted.

For a dashboard card, see [Dashboard cards: CLI Console](dashboard/overview.md#cli-console).

## Execute Command UI

Runs the command in the command helper of one entry (`text.meshcore_command` for the first entry). Then it clears the helper.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `entry_id` | string | Yes, if there is more than one entry | | The entry whose helper the service reads |
| `record_to_console` | boolean | No | `false` | Record the command in the CLI Console |

The service returns the same response as `execute_command`, or a [UI service error](#ui-service-errors). The **CLI Run Command** button (`button.meshcore_<pk6>_cli_run`) calls it with `record_to_console: true`.

## CLI Console Clear

Empties the CLI Console transcript. Without `entry_id`, it clears all entries. The **CLI Clear Console** button (`button.meshcore_<pk6>_cli_clear`) does the same for its entry.

## Query services

These services return data that the integration already has. Only `trace` sends a request to the mesh. Each one accepts `entry_id`.

### Get Contacts

Returns `{"contacts": [...]}`: the contacts on the companion and the discovered contacts. When a contact is in both lists, the response has the record with the newer `lastmod`.

| Field | Description |
|---|---|
| `public_key` | Full public key, 64 hex characters |
| `pubkey_prefix` | The first 12 characters of `public_key` |
| `added_to_node` | `true` if the contact is on the contact list of the companion |
| `adv_name` | Advertised name |
| `type` | 1 client, 2 repeater, 3 room server, 4 sensor |
| `out_path_len` | Number of hops in the route. `-1` means no known route. |
| `out_path` | The route as hex, without separators |
| `out_path_hash_mode` | Hash width of each hop: `0` is 1 byte, `1` is 2 bytes, `2` is 3 bytes. `-1` means no known route. |
| `flags`, `last_advert`, `adv_lat`, `adv_lon`, `lastmod` | Other library fields |

On a failure, the response is `{"contacts": [], "error": "no_coordinator"}` (no entry) or `"coordinator_error"` (read error).

### Get Discovered Contact

Returns the first discovered contact whose public key starts with `pubkey_prefix`. Use it in Data only mode, where discovered contacts have no entity. See [Contact Management](contacts.md#get-discovered-contact).

- `pubkey_prefix` is required, 2 characters minimum. The match is case-sensitive, so use 12 lowercase hex characters.

| Result | Response |
|---|---|
| Match | `{"contact": {...}}` with the same fields as `get_contacts` |
| No match | `{"contact": null, "error": "not_found", "pubkey_prefix": "def456"}` |
| No entry | `{"contact": null, "error": "no_coordinator"}` |
| Read error | `{"contact": null, "error": "coordinator_error"}` |

### Get Channels

Returns the channels that the integration read from the companion, without a new request:

```json
{"channels": [{"channel_idx": 0, "channel_name": "Public", "shared_secret_present": true}]}
```

The response omits unused slots and never contains the secret. With no entry, it is `{"channels": [], "error": "no_coordinator"}`.

### Trace

Traces the route to a contact and returns the hops and the round-trip time.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `pubkey_prefix` | string | Yes | | The start of the public key. Use 12 lowercase hex characters. |
| `timeout` | number (seconds) | No | `15` | The requested wait for the reply, 1 to 120 |

```yaml
action: meshcore.trace
data:
  pubkey_prefix: "def456abc012"
response_variable: result
```

- The contact must be on the companion. A discovered contact, an uppercase prefix or a contact name returns `contact_not_on_device`.
- If the contact has no known route, the integration first sends a path discovery and waits 15 to 30 seconds. The full call can then take approximately 90 seconds.
- The wait for the reply is `timeout`, a minimum of 5 seconds and a maximum of 60.
- Routes with hop hashes of 1 to 4 bytes are supported.
- Under Governed, the trace takes one credit from the messages lane. The path discovery does not take a separate credit.

```json
{"trace": {"hops": 3, "path": [{"hash": "a1", "snr": 6.25}, {"hash": "de", "snr": 4.5}, {"hash": "a1", "snr": 5.0}, {"snr": 7.0}], "round_trip_ms": 3412, "final_snr": 7.0, "tag": 123456789}}
```

| Field | Description |
|---|---|
| `hops` | Hops to the contact and back. A contact one repeater away gives 3. |
| `path` | One item for each hop, with `hash` and `snr`. The last item is the companion and has only `snr`. |
| `round_trip_ms` | Time from the send to the reply |
| `final_snr` | SNR of the reply at the companion |
| `tag` | The random tag of this trace |

On a failure, the response is `{"trace": null, "error": "<code>"}`. Only a traffic policy refusal raises an error.

| `error` | Cause | Extra keys |
|---|---|---|
| `no_coordinator` | No entry is loaded, or `entry_id` is not found | |
| `not_connected` | The companion is not connected | |
| `contact_not_found` | No contact matches the prefix | |
| `contact_not_on_device` | The contact is not on the companion, or the prefix is uppercase or a name | |
| `contact_missing_pubkey` | The contact record has no valid public key | |
| `path_discovery_failed` | The path discovery was not sent, or its reply was not valid | `reason` (`no_firmware_ack` or `malformed_path_response`), if known |
| `path_discovery_rejected` | The companion refused the path discovery | `reason` |
| `path_discovery_timeout` | No path discovery reply | |
| `internal_error` | The integration could not build the trace path | |
| `send_failed` | An exception occurred when the integration sent the trace | |
| `unknown` | The firmware refused the trace packet | |
| `no_response`, or a library reason such as `no_event_received` | The companion did not answer the trace send | |
| `await_failed` | An exception occurred during the wait | |
| `timeout` | No trace reply in the wait time | `round_trip_ms` |

## Contact services

These services manage contacts. See [Contact Management](contacts.md).

| Service | Fields | Description |
|---|---|---|
| `add_selected_contact` | `entry_id` | Adds the contact in the discovered contact select to the companion |
| `remove_selected_contact` | `entry_id` | Removes the contact in the added contact select from the companion |
| [`remove_discovered_contact`](contacts.md#remove-discovered-contact) | `pubkey_prefix` (optional), `entry_id` | Removes one discovered contact and its entity from Home Assistant. The prefix must match one contact. Without `pubkey_prefix`, it uses the discovered contact select. |
| [`cleanup_unavailable_contacts`](contacts.md#cleanup-unavailable-contacts) | `entry_id` | Removes the contact binary sensors that are `unavailable` |
| [`clear_discovered_contacts`](contacts.md#clearing-discovered-contacts) | `days_threshold` (1 to 365, optional), `entry_id` | Removes discovered contacts from Home Assistant |

- With more than one entry, `add_selected_contact` and `remove_selected_contact` require `entry_id`. They use the select entity and the companion of that entry.
- `clear_discovered_contacts` without `days_threshold` removes all discovered contacts. It keeps the entities of the contacts on the companion. With `days_threshold`, the service removes contacts whose `lastmod` is older than the threshold or missing. It keeps the contacts that Home Assistant added to the companion.

## Problems and solutions

| Problem | Cause | Solution |
|---|---|---|
| `expected a dictionary, but got <class 'NoneType'>` | `execute_command` returned no data | Read the Home Assistant log |
| `Unauthorized` | The user is not an administrator | Use an administrator account, or call the service from an automation |

For other problems, see [Troubleshooting](troubleshooting.md).
