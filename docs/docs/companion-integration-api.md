---
sidebar_position: 10
title: Companion Integration API
---

# Companion Integration API

The stable public surface that meshcore-ha exposes to companion integrations and long-lived automations: events, services, and entity patterns you can build against. For end-user docs see [Messaging](./messaging), [Services](./services), and [Events](./events).

## Scope and stability

This page is for authors of companion integrations (such as `MeshCore-HA-UI` or `meshcore-ha-chat`) and writers of automations or external scripts that consume the meshcore-ha event bus. Casual users should start at [Installation](./installation) and [Automation](./automation).

Surfaces listed here are the **stable public surface**. Additive changes (new fields, new services, new attributes) may land in any release; companions should ignore unknown fields. Renames and removals require at least one release of deprecation notice in the change log. Field-type changes are treated as a deprecate-and-replace pair that coexist for one release. Anything marked **experimental** is exempt — it may change or disappear without notice.

Anything not listed here is internal and may change without warning. If you depend on something undocumented, open an issue requesting it be promoted to the stable surface.

## Events

Subscribe via `hass.bus.async_listen(...)` or the standard HA event trigger.

### `meshcore_message`

Fired when meshcore-ha receives a direct or channel message — the primary event most companions listen to.

| Field | Type | When | Notes |
|---|---|---|---|
| `message` | string | always | Message text. |
| `sender_name` | string | always | Human-readable sender name. |
| `pubkey_prefix` | string (hex, 12 chars) | DM always; channel only when sender resolves to a known contact | Stable correlation key across events. |
| `entity_id` | string | always | The `binary_sensor.meshcore_*` entity for this message. |
| `timestamp` | ISO 8601 | always | When meshcore-ha received the message. |
| `message_type` | `"channel"` \| `"direct"` | always | Discriminator. |
| `channel` / `channel_idx` | string / int | channel only | Channel name + 0–255 index. |
| `receiver_name` | string | DM only | Local device's advertised name. |
| `rx_log_data` | array | channel, when RX_LOG correlated | Per-repeater detail (`snr`, `rssi`, `path_len`); see [Events](./events#meshcore_message). |

Recent releases also add `hop_count` (always) and `snr` (V3 firmware only) on direct-message payloads — **experimental** until shipped on a stable release.

### `meshcore_delivery_update`

Fired progressively as late-arriving `RX_LOG` data is correlated to an already-emitted `meshcore_message`. Companions update existing UI rather than re-firing their own event. Carries `entity_id`, `sender_name`, `message`, `timestamp` (matching the parent), plus `rx_log_data` (cumulative, not delta), `repeater_count`, `progressive` (`true` for intermediate, `false` for final), and `outgoing` (present + `true` for outgoing only). Key on `(entity_id, timestamp)` to match. See [Events](./events#meshcore_delivery_update) for optional fields.

### `meshcore_message_sent`

Fired when one of the `send_*` services successfully transmits. Mirrors `meshcore_message` shape. Most companions don't need it — the existing message event is enough.

### `meshcore_connected` / `meshcore_disconnected`

Fired when the device connection comes up or goes down. Payload is the config-entry identifier — use to gate UI on connectivity.

### Raw SDK events

The integration also re-fires every `meshcore_py` SDK event as `meshcore_raw_event`. **Experimental** — schema follows the SDK, not meshcore-ha. Diagnostics only.

## Services

Call via `hass.services.async_call(...)` or the WebSocket `call_service` command. All services accept an optional `entry_id` for multi-device installs.

| Service | Purpose | Response | Stability |
|---|---|---|---|
| `meshcore.send_message` | Send a DM (by `node_id` or `pubkey_prefix`). | none | stable |
| `meshcore.send_channel_message` | Broadcast on a channel. | none | stable |
| `meshcore.get_contacts` | Device's known contacts as structured list. | `{contacts: [{name, pubkey_prefix, type, ...}]}` | stable |
| `meshcore.get_channels` | Configured channels (shared secret omitted; presence reported via `shared_secret_present`). | `{channels: [{idx, name, shared_secret_present}]}` | stable |
| `meshcore.trace` | Path-trace to a contact (hop list, RTT). On failure returns `{trace: null, error: "..."}`. | `{trace: {hop_count, path, rtt_ms, ...}}` | stable |
| `meshcore.execute_command` | Run a raw SDK command. | text blob (CLI output) | **experimental** — output not versioned. |

Contact-management services (`add_selected_contact`, `remove_selected_contact`, `remove_discovered_contact`, `cleanup_unavailable_contacts`, `clear_discovered_contacts`) exist for the bundled UI and are **experimental** for companion use.

**Prefer structured services over scraping.** Use `meshcore.get_contacts` and `meshcore.get_channels` instead of regex-parsing `execute_command` output. If you need a structured surface that doesn't exist yet (e.g., set-radio-config, sync-clock, or per-repeater stats), open an issue.

### Structured query response shapes

These three services return typed objects companions may rely on. Unknown fields may appear in additive releases — companions should ignore them.

**`get_contacts.contacts[]`:**

| Field | Type | Notes |
|---|---|---|
| `adv_name` | string | Contact's advertised name. |
| `public_key` | string (hex) | Full public key. |
| `pubkey_prefix` | string (12 hex chars) | Short identifier; correlation key across events. |
| `type` | int | `1` = client, `2` = repeater, `3` = room server, `4` = sensor. |
| `added_to_node` | bool | `true` if saved to the device's contact table; `false` for discovered-only. |
| `out_path_len` | int | `-1` = flood-routed, `0` = direct neighbor, `N` = N-hop fixed path. |
| `out_path` | string (hex) | Concatenated 1-byte hop hashes; meaningful only when `out_path_len > 0`. |
| `out_path_hash_mode` | int | `-1` = flood, `0` = 1-byte hashes (current mode). |

**`get_channels.channels[]`:**

| Field | Type | Notes |
|---|---|---|
| `channel_idx` | int 0–255 | Channel index. |
| `channel_name` | string | Display name. |
| `shared_secret_present` | bool | Whether a shared secret is configured (the secret itself is never returned). |

**`trace`** returns either a success or failure shape:

```python
# success
{"trace": {"hops": int, "path": [hex, ...], "round_trip_ms": int, "final_snr": float, "tag": int}}

# failure (any error)
{"trace": None, "error": "<code>"}
```

Error codes: `no_coordinator`, `not_connected`, `contact_not_found`, `contact_not_on_device`, `contact_missing_pubkey`, `flood_discovery_timeout`, `flood_discovery_error`, `trace_timeout`.

## Entities

Read via `hass.states.get(entity_id)`. The naming pattern is stable — companions may parse the `entity_id` to extract the device short-key (first 6 chars of the device pubkey) and contact pubkey prefix where applicable.

| Pattern | Kind | Purpose |
|---|---|---|
| `binary_sensor.meshcore_<device>_messages` | binary_sensor | Last-message indicator per device. |
| `binary_sensor.meshcore_<device>_<pubkey>_messages` | binary_sensor | Per-contact last-message indicator (DM `entity_id` points here). |
| `binary_sensor.meshcore_<device>_ch_<idx>_messages` | binary_sensor | Per-channel last-message indicator. |
| `sensor.meshcore_<device>_*` | sensor | Battery, signal, node-count, diagnostics. |

Entity *attributes* change more often than events — only depend on attributes documented in [Sensors](./sensors) as stable.

## Example: tracking delivery with progressive updates

Listen to both `meshcore_message` (initial) and `meshcore_delivery_update` (progressive), key by `(entity_id, timestamp)`:

```python
pending = {}

@callback
def _on_message(event):
    pending[(event.data["entity_id"], event.data["timestamp"])] = event.data

@callback
def _on_delivery_update(event):
    key = (event.data["entity_id"], event.data["timestamp"])
    if key in pending:
        pending[key]["rx_log_data"] = event.data.get("rx_log_data", [])
        pending[key]["repeater_count"] = event.data.get("repeater_count", 0)
        if event.data.get("progressive") is False:
            # Final pass — commit pending[key] to durable store, then drop.
            pending.pop(key, None)

hass.bus.async_listen("meshcore_message", _on_message)
hass.bus.async_listen("meshcore_delivery_update", _on_delivery_update)
```

## Reference implementations

- **[`MeshCore-HA-UI`](https://github.com/Ratty7198/MeshCore-HA-UI)** — companion UI consuming the event bus and `send_*` services.
- **[`meshcore-ha-chat`](https://github.com/mwolter805/meshcore-ha-chat)** — sidebar chat panel + persistent message store; uses all four events plus the structured query services.

## Deprecation and reporting

Deprecated surfaces ship a change-log notice, remain functional for at least one full feature release, and are removed no earlier than the next feature release after that — companions get one to two cycles to migrate. Experimental surfaces are exempt. Security- or correctness-critical changes may bypass the cycle and will be called out in release notes.

If a release regresses a documented surface, file an issue at [meshcore-dev/meshcore-ha](https://github.com/meshcore-dev/meshcore-ha/issues) naming the companion you maintain and the specific field/service/pattern that changed. Regressions in documented surfaces are higher priority than ordinary bug reports. If you rely on an undocumented surface and want it promoted, file an issue describing your use case.
