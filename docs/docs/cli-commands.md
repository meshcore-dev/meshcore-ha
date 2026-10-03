---
sidebar_position: 12
title: CLI Command Reference
---

# CLI Command Reference

`meshcore.execute_command`, `meshcore.execute_command_ui` and the CLI Console run the commands on this page. For the service fields and the [response shapes](services.md#response-shapes), see [Services: Execute Command](services.md#execute-command).

The commands are the method names of the meshcore-py library, in snake_case. For example, use `send_advert`, not `advert`. Each `async def <name>(...)` in the library [`meshcore/commands/`](https://github.com/meshcore-dev/meshcore_py/tree/main/meshcore/commands) folder is a command. Repeater CLI commands and `meshcli` commands are different command sets.

## Syntax

| Form | Example | Rules |
|---|---|---|
| Space-separated | `set_tx_power 20` | Separate arguments with spaces. Put quotes around an argument that has spaces. |
| Functional | `set_tx_power(20)` | Use Python literals: strings in quotes, numbers, `True`, `False`, `None`, bytes such as `b'\x01'`. Keyword arguments are permitted. |

```
get_bat
set_name "My Node"
send_msg def456abc012 "Status check"
send_advert true
req_neighbours_sync("def456abc012", count=10)
```

### Argument types

In the space form, the integration converts each argument to the parameter type of the command.

| Type | Accepted values (space form) | Example |
|---|---|---|
| contact | A public key prefix or a contact name, 6 characters minimum | `def456abc012` |
| int | A whole number | `20` |
| float | A decimal number | `12.345` |
| bool | `true`, `yes`, `y`, `1`, `false`, `no`, `n`, `0` (not case-sensitive) | `true` |
| bytes | A hex string | `0a1b2c` |
| string | Any text | `"My Node"` |

- If the integration does not know the type of an argument, it sends a string. To send a different type, use the functional form.
- In the functional form, the integration converts only contact arguments.
- A keyword that is not a parameter of the command gives `unknown_keyword`. A value that is not a Python literal gives no data.

### Contact arguments

A command that sends to another node takes a `<contact>` argument. The integration finds the contact on the companion as follows:

1. If the argument has fewer than 6 characters, the response is `pubkey_prefix_too_short`. This rule also applies to a name.
2. It looks for a public key that starts with the argument (not case-sensitive).
3. If no key matches, it looks for a contact name that is the argument (exact, not case-sensitive).
4. For `add_contact` only, it also looks in the discovered contacts (case-sensitive).
5. If no contact matches, the response is `contact_not_found`.

Use a 12-character prefix. A short prefix can match more than one contact, and the integration uses the first match.

## Denied commands

The integration refuses these commands for every user, also for an administrator:

| Command | Reason |
|---|---|
| `request_factory_reset`, `confirm_factory_reset` | Erases the companion configuration |
| `import_private_key` | Replaces the companion identity |
| `send_raw_packet`, `send_raw_data` | Sends raw frames that bypass the integration checks |
| Any name that starts with `_` | Private library attribute |

The call fails with this error, before it sends anything to the companion:

```
The command 'import_private_key' is not available through this integration: it would reset the node, replace its identity, or hand the radio raw frames
```

No `meshcore_cli_response` event fires, and the CLI Console does not record the command.

## Traffic policy

Under the Governed traffic policy, these commands take one credit from a lane before they send. If the lane is empty, the call fails with `Mesh traffic <lane> lane is empty; try again in <seconds> seconds`. See [Mesh Traffic Policy](traffic-policy.md).

| Command | Lane |
|---|---|
| `send_advert`, `share_contact`, `send_path_discovery`, `send_path_discovery_sync`, `send_node_discover_req`, `send_control_data` | Flood |
| `send_msg`, `send_cmd`, `send_msg_with_retry`, `send_chan_msg`, `send_trace` | Messages |
| `send_login`, `send_login_sync`, `send_logout`, `send_statusreq`, `send_telemetry_req`, `send_binary_req`, `send_anon_req`, `fetch_all_neighbours` | Direct if the contact has a known route, else Flood |
| `req_status`, `req_telemetry`, `req_acl`, the `req_*_async` commands and every other command whose name ends in `_sync` | Direct if the contact has a known route, else Flood |

These commands also wait for the single request slot of the companion if another mesh request is in progress.

The integration does not meter other commands. A message that you send with `send_msg` or `send_chan_msg` fires no message event. Use [`send_message`](services.md#send-message) and [`send_channel_message`](services.md#send-channel-message) instead.

Installs from 2.x that still use the deprecated [Legacy traffic policy](legacy-traffic-policy.md) do not meter commands.

## Other rules

- If **Global Settings > Retrieve queued incoming messages** is off, the integration refuses `get_msg` with `{"error": "Incoming message consumption is disabled"}`.
- After a successful `set_*` command that changes the companion information (for example `set_radio`, `set_name` or `set_coords`), the integration reads the information again. The sensors update at once.
- After a successful `set_channel`, `add_contact` or `remove_contact`, the integration updates the channel select or the contact entities.

## Query commands

These commands read data. They do not change the companion.

| Command | Arguments | Returns |
|---|---|---|
| `send_device_query` | None | Device information, for example the firmware version and the model |
| `send_appstart` | None | Self information: name, public key, radio settings |
| `get_bat` | None | `level` (mV). Also `used_kb` and `total_kb` if the firmware reports storage. |
| `get_time` | None | The companion clock |
| `get_self_telemetry` | None | The local telemetry data |
| `get_custom_vars` | None | The custom variables |
| `get_stats_core` | None | `battery_mv`, `uptime_secs`, `errors`, `queue_len` |
| `get_stats_radio` | None | `noise_floor`, `last_rssi`, `last_snr`, `tx_air_secs`, `rx_air_secs` |
| `get_stats_packets` | None | `recv`, `sent`, `flood_tx`, `direct_tx`, `flood_rx`, `direct_rx`, `recv_errors` |
| `get_contacts` | `[lastmod]` (int) | The contact list of the companion |
| `get_channel` | `<channel_idx>` (int) | The channel name and secret |
| `get_path_hash_mode` | None | `{"result": <mode>}` |
| `get_allowed_repeat_freq` | None | The allowed repeat frequencies |
| `get_tuning` | None | The tuning parameters |
| `get_autoadd_config` | None | The automatic contact add setting |

CAUTION: Do not share the response of `get_channel`. It contains the channel secret.

## Configuration commands

These commands change the companion. Some changes are permanent.

| Command | Arguments | Notes |
|---|---|---|
| `set_name` | `<name>` (string) | The advertised name |
| `set_tx_power` | `<dbm>` (int) | Transmit power in dBm |
| `set_time` | `<epoch>` (int) | The companion clock, in Unix seconds |
| `set_coords` | `<lat> <lon>` (float, float) | The advertised position |
| `set_radio` | `<freq> <bw> <sf> <cr>` (float, float, int, int) | Radio settings. A wrong value can stop the companion from reaching the mesh. |
| `set_tuning` | `<rx_dly> <af>` (int, int) | Tuning parameters |
| `set_path_hash_mode` | `<mode>` (int) | `0` is 1 byte, `1` is 2 bytes, `2` is 3 bytes for each hop |
| `set_telemetry_mode_base`, `set_telemetry_mode_loc`, `set_telemetry_mode_env` | `<mode>` (int) | Telemetry modes |
| `set_advert_loc_policy` | `<policy>` (int) | Advert position policy |
| `set_manual_add_contacts` | `<bool>` | Manual contact add mode |
| `set_autoadd_config` | `<flag>` (int) | Automatic contact add setting |
| `set_multi_acks` | `<count>` (int) | Multiple ACK setting |
| `set_custom_var` | `<key> <value>` (string, string) | A custom variable |
| `set_devicepin` | `<pin>` (int) | The BLE PIN |
| `set_flood_scope` | `<scope>` (string) | The flood scope of the companion. For one message only, use the `scope` field of `send_channel_message`. |
| `send_advert` | `[flood]` (bool, default `false`) | `false` sends a zero-hop advert. `true` sends a flood advert. |
| `reboot` | None | Restarts the companion |
| `export_private_key` | None | Returns the private key if the firmware permits export |

CAUTION: Do not share the response of `export_private_key`. It contains the private key of the companion. The CLI Console and `meshcore_cli_response` show `<redacted>` in its place, unless **Expose Node Secrets in Events** is on.

## Channel commands

| Command | Arguments | Notes |
|---|---|---|
| `get_channel` | `<channel_idx>` (int) | Reads one channel |
| `set_channel` | `<channel_idx> <name> [secret]` (int, string, hex) | Writes one channel. The secret is 16 bytes (32 hex characters). If the name starts with `#` or you give no secret, the library makes the key from the SHA-256 hash of the name. |

## Contact commands

| Command | Arguments | Notes |
|---|---|---|
| `add_contact` | `<contact>` | Adds a contact to the companion. The argument can be a discovered contact. |
| `remove_contact` | `<contact>` | Removes a contact from the companion |
| `reset_path` | `<contact>` | Clears the known route. The next request to the contact floods. |
| `change_contact_path` | `<contact> <path>` | Sets the route as a hex path. To set the hash width, add `:<mode>`, for example `a1b2:0`. |
| `change_contact_flags` | `<contact> <flags>` (int) | Sets the contact flags |
| `share_contact` | `<contact>` | Shares the contact |
| `export_contact` | `<contact>` | Returns the contact card |
| `import_contact` | `<card>` (hex) | Imports a contact card |

## Remote node commands

These commands send a request to another node. The `*_sync` commands, `fetch_all_neighbours` and `send_msg_with_retry` wait for the reply and return it. The other commands return when the companion accepts the request.

| Command | Arguments |
|---|---|
| `send_login`, `send_login_sync` | `<contact> <password>` |
| `send_logout` | `<contact>` |
| `send_statusreq`, `send_telemetry_req` | `<contact>` |
| `req_status_sync`, `req_neighbours_sync`, `fetch_all_neighbours` | `<contact>` |
| `req_telemetry_sync`, `req_acl_sync` | `<contact> [timeout]` |
| `req_owner_sync`, `req_basic_sync`, `req_regions_sync` | `<contact>` |
| `send_path_discovery`, `send_path_discovery_sync` | `<contact>` |
| `send_msg` | `<contact> <message> [timestamp]` |
| `send_msg_with_retry` | `<contact> <message>` |
| `send_cmd` | `<contact> <command> [timestamp]` |
| `send_trace` | `<auth_code> <tag> <flags> <path>` (int, int, int, hex) |

To run a CLI command on a remote repeater:

1. Log in: `send_login def456abc012 "mypassword"`.
2. Send the command: `send_cmd def456abc012 "<repeater command>"`.

The repeater firmware defines the repeater commands and their replies. For a route trace, use the [`trace`](services.md#trace) service.
