---
sidebar_position: 4
title: CLI Command Reference
---

# CLI Command Reference

The `meshcore.cli_command` and `meshcore.execute_command` services (and the CLI
Console card) run commands against your **local companion radio**. This page
lists what you can type.

## Which "CLI" is this?

MeshCore has several command vocabularies — they are **not** interchangeable:

| Vocabulary | Where | Example |
|---|---|---|
| **This CLI** (meshcore-py SDK methods) | HA `cli_command` / `execute_command` / CLI Console | `send_device_query`, `get_bat` |
| Companion app (iOS/Android) | The phone GUI | buttons/screens (same protocol underneath) |
| `meshcli` (meshcore-cli) | A separate terminal tool | `infos`, `advert`, `send` |
| Repeater / room-server CLI | Sent to a **remote** repeater after `send_login` | `reboot`, `set freq …` |

The commands here are the **Python SDK method names** (snake_case). So it's
`send_advert`, not `advert`; `send_device_query`, not `version`; `get_time`, not
`time`. The authoritative source is the meshcore-py library's
[`meshcore/commands/`](https://github.com/meshcore-dev/meshcore_py/tree/main/meshcore/commands)
modules — every `async def <name>(...)` is a command.

## Syntax

- **Space-separated arguments:** `set_tx_power 20`, `get_channel 0`
- **Quote strings with spaces:** `set_name "Drew Node"`
- **`<contact>`** means a contact's **public-key prefix** (≥6 hex chars) or its
  name — used for commands that talk to a *remote* node.
- Functional form also works: `send_advert(flood=True)`.

Responses appear in the CLI Console transcript, in the Developer Tools → Actions
result, and (for `cli_command`) on the `meshcore_cli_response` event.

:::warning
Commands prefixed `set_*`, `import_*`, `reboot`, and `send_advert` **change your
device or put traffic on the mesh**. Read-only `get_*` / `send_device_query`
commands are safe to experiment with.
:::

## Query / read (safe)

| Command | Args | Returns |
|---|---|---|
| `send_device_query` | — | Firmware version, hardware model (the "version" command) |
| `send_appstart` | — | Self info (name, public key, radio params) |
| `get_bat` | — | Battery voltage |
| `get_time` | — | Device clock |
| `get_self_telemetry` | — | Local telemetry payload |
| `get_custom_vars` | — | Custom variables |
| `get_stats_core` | — | Uptime, queue length, error flags |
| `get_stats_radio` | — | Noise floor, last RSSI/SNR, airtime |
| `get_stats_packets` | — | Packet counters |
| `get_contacts` | `[lastmod]` | Contact list |
| `get_channel` | `<idx>` | Channel name + secret |
| `get_path_hash_mode` | — | Path hash mode (0–2) |
| `get_allowed_repeat_freq` | — | Allowed repeat frequency |
| `get_tuning` | — | RX delay / AF tuning params |
| `get_autoadd_config` | — | Auto-add-contacts config |

## Configuration / write (changes the device)

| Command | Args | Notes |
|---|---|---|
| `set_name` | `<str>` | Device name |
| `set_tx_power` | `<int>` | TX power (dBm) |
| `set_time` | `<epoch>` | Set clock |
| `set_coords` | `<lat> <lon>` | Set GPS coords |
| `set_radio` | `<freq> <bw> <sf> <cr>` | Radio params |
| `set_tuning` | `<rx_dly> <af>` | Tuning params |
| `set_path_hash_mode` | `<int>` | 0=1 byte, 1=2, 2=3 |
| `set_telemetry_mode_base` / `_loc` / `_env` | `<int>` | Telemetry modes |
| `set_advert_loc_policy` | `<int>` | Advert location policy |
| `set_manual_add_contacts` | `<bool>` | Manual contact-add mode |
| `set_multi_acks` | `<int>` | Multi-ack setting |
| `set_custom_var` | `<key> <value>` | Set a custom variable |
| `set_devicepin` | `<int>` | Device/BLE PIN |
| `send_advert` | `[flood]` | Broadcast an advert packet to the mesh |
| `reboot` | — | **Reboots the radio** |

## Channels

| Command | Args |
|---|---|
| `get_channel` | `<idx>` |
| `set_channel` | `<idx> <name> <secret-hex>` |

## Remote nodes (need a `<contact>`)

These send traffic over the mesh to another node. The `*_sync` variants wait for
a reply and return it; the async variants return immediately.

| Command | Args |
|---|---|
| `send_login` / `send_logout` | `<contact> [password]` |
| `send_msg` | `<contact> "<message>"` |
| `send_cmd` | `<contact> "<command>"` (repeater CLI command) |
| `req_status_sync` | `<contact>` |
| `req_telemetry_sync` | `<contact>` |
| `req_neighbours_sync` | `<contact>` |
| `req_owner_sync` / `req_basic_sync` / `req_regions_sync` | `<contact>` |
| `send_path_discovery` | `<contact>` |
| `reset_path` | `<contact>` |
| `share_contact` / `export_contact` / `remove_contact` | `<contact>` |

:::tip
To run a **repeater's** text CLI (e.g. `reboot`, `advert`) on a *remote*
repeater, log in first (`send_login <contact> <password>`) and then use
`send_cmd <contact> "<repeater-command>"`. Those repeater verbs are a different
vocabulary from the local commands on this page.
:::

## Full list

The complete set the integration recognizes — with argument types — lives in the
`command_param_types` mapping in
[`custom_components/meshcore/services.py`](https://github.com/meshcore-dev/meshcore-ha/blob/main/custom_components/meshcore/services.py).
Any method that exists on the meshcore-py `commands` object can be called even if
it isn't listed above.
