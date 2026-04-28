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
| `domain` | string | always | Always `"meshcore"`. Internal; companions can ignore. |
| `outgoing` | bool | outgoing only | Present and `true` on outgoing fires; absent on incoming. |
| `hop_count` | int | DM only | Number of hops the message traversed. **experimental** |
| `snr` | float | DM only, V3 firmware | Signal-to-noise ratio of the inbound DM. **experimental** |

For outgoing channel messages, this event re-fires once after RX_LOG collection completes (~4 s typical) carrying the final `rx_log_data`, `repeater_count`, and `progressive: false`. Treat the re-fire as the terminal state — see the example below.

### `meshcore_delivery_update`

Fired only for *intermediate* updates while late-arriving `RX_LOG` data is being correlated to an already-emitted `meshcore_message`. The terminal update is delivered as a `meshcore_message` re-fire (see above), not a delivery_update.

| Field | Type | Notes |
|---|---|---|
| `entity_id`, `sender_name`, `message`, `timestamp` | (parent types) | Match the parent `meshcore_message`. Use `(entity_id, timestamp)` to correlate. |
| `rx_log_data` | array | Cumulative (not delta) per-repeater detail since the parent event. |
| `repeater_count` | int | `len(rx_log_data)`. |
| `progressive` | bool | Always `true`. The terminal `progressive: false` arrives on `meshcore_message`, not here. |
| `outgoing` | bool | Present and `true` on outgoing-channel updates; absent on incoming. |

Additional fields from the parent `meshcore_message` (e.g., `domain`, `message_type`, `channel_idx`) pass through unchanged.

### `meshcore_message_sent`

Fired when one of the `send_*` services successfully transmits. Distinct shape from `meshcore_message` — companions tracking sent-state should subscribe here, not infer from the message event.

| Field | Type | When | Notes |
|---|---|---|---|
| `message` | string | always | Message text. |
| `device` | string | always | The originating config-entry id. |
| `message_type` | `"channel"` \| `"direct"` | always | Discriminator. |
| `receiver` | string | always | DM: contact's `adv_name`. Channel: `"channel_<idx>"`. |
| `timestamp` | int (seconds) | always | Unix epoch — *not* ISO 8601 like `meshcore_message`. |
| `send_id` | string (8 hex) | always | Per-send identifier; useful for correlating progressive re-fires on the message event. |
| `contact_public_key` | string (hex) | DM only | Full public key — *not* the 12-char prefix. |
| `ack_received` | bool | DM only | Whether the device received an ACK before the suggested timeout. |
| `channel_idx` | int 0–255 | channel only | Channel index. |
| `send_timestamp` | int (seconds) | channel only | Timestamp the device used in the broadcast (may differ from `timestamp` due to clock skew). |

### `meshcore_connected` / `meshcore_disconnected`

Fired when the device connection comes up or goes down.

- `meshcore_connected`: `{"connection_type": "<usb|tcp|ble>"}`.
- `meshcore_disconnected`: `{}` on a clean disconnect, or `{"unexpected": true}` when the SDK has given up reconnecting.

Neither payload includes a config-entry identifier today. Multi-device companions can't disambiguate which entry connected/disconnected from the payload alone — read `coordinator.api.connected` after the event fires, or correlate with other state.

### Raw SDK events

The integration also re-fires every `meshcore_py` SDK event as `meshcore_raw_event` with wrapper `{event_type: str, payload: dict, timestamp: float}`. The wrapper is meshcore-ha's; only the inner `payload` follows the SDK's schema. **Experimental** — diagnostics only.

## Services

Call via `hass.services.async_call(...)` or the WebSocket `call_service` command. All services accept an optional `entry_id` for multi-device installs.

| Service | Purpose | Response | Stability |
|---|---|---|---|
| `meshcore.send_message` | Send a DM. Recipient is either `node_id` or `pubkey_prefix` (see below). | none | stable |
| `meshcore.send_channel_message` | Broadcast on a channel. | none | stable |
| `meshcore.get_contacts` | Device's known contacts as structured list. | `{contacts: [{adv_name, pubkey_prefix, type, ...}]}` | stable |
| `meshcore.get_channels` | Configured channels (shared secret omitted; presence reported via `shared_secret_present`). | `{channels: [{channel_idx, channel_name, shared_secret_present}]}` | stable |
| `meshcore.trace` | Path-trace to a contact (hop list, RTT). On failure returns `{trace: null, error: "..."}`. | `{trace: {hops, path, round_trip_ms, ...}}` | stable |
| `meshcore.execute_command` | Run a raw SDK command (`command` required; optional `node_id` / `pubkey_prefix` to scope). | text blob (CLI output) | **experimental** — output not versioned. |

**`send_message` recipient.** Provide exactly one of `node_id` (advertised name) or `pubkey_prefix` (≥6 hex chars of the public key).

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

Failure shape: `{"contacts": [], "error": "no_coordinator" | "coordinator_error"}`. Companions should check for `error` before consuming `contacts`.

**`get_channels.channels[]`:**

| Field | Type | Notes |
|---|---|---|
| `channel_idx` | int 0–255 | Channel index. |
| `channel_name` | string | Display name. |
| `shared_secret_present` | bool | Whether a shared secret is configured (the secret itself is never returned). |

Failure shape: `{"channels": [], "error": "no_coordinator"}`.

**`trace`** returns either a success or failure shape:

```python
# success
{"trace": {
    "hops": int,
    "path": [{"hash": str, "snr": float}, ...],   # final entry omits "hash" — it's the local device receiving the echo
    "round_trip_ms": int,
    "final_snr": float | None,                     # None when path is empty (0-hop direct reception)
    "tag": int,
}}

# failure (any error)
{"trace": None, "error": "<code>", ...}
```

Documented error codes: `no_coordinator`, `not_connected`, `contact_not_found`, `contact_not_on_device`, `contact_missing_pubkey`, `path_discovery_failed`, `path_discovery_rejected`, `path_discovery_timeout`, `timeout`, `send_failed`, `await_failed`, `internal_error`. Firmware-supplied error strings may also appear as the `error` value when the radio rejects the trace request — companions should treat any non-listed string as opaque diagnostic text.

Failure responses may carry additional fields:
- `reason`: human-readable detail accompanying `path_discovery_failed` and `path_discovery_rejected`.
- `round_trip_ms`: present on `timeout` so companions can show how long the wait actually was.

## Entities

Read via `hass.states.get(entity_id)`. The naming pattern is stable — companions may parse the `entity_id` to extract the device short-key (first 6 chars of the device pubkey) and contact pubkey prefix where applicable.

| Pattern | Kind | Purpose |
|---|---|---|
| `binary_sensor.meshcore_<device>_<pubkey>_messages` | binary_sensor | Per-contact last-message indicator (DM `entity_id` points here). `<pubkey>` is the first 6 chars of the contact's pubkey — *not* the 12-char `pubkey_prefix` from events. |
| `binary_sensor.meshcore_<device>_ch_<idx>_messages` | binary_sensor | Per-channel last-message indicator. |
| `sensor.meshcore_<device>_*` | sensor | Battery, signal, node-count, diagnostics. |

Entity *attributes* change more often than events — only depend on attributes documented in [Sensors](./sensors) as stable.

## Example: tracking delivery with progressive updates

Listen to `meshcore_message` (initial fire *and* outgoing-channel terminal re-fire) plus `meshcore_delivery_update` (intermediate only). Key on `(entity_id, timestamp)`:

```python
pending = {}

@callback
def _on_message(event):
    key = (event.data["entity_id"], event.data["timestamp"])
    if event.data.get("progressive") is False:
        # Outgoing-channel terminal re-fire: commit and drop.
        pending.pop(key, None)
        # ... persist event.data here
    else:
        # Initial fire (incoming or outgoing first emit).
        pending[key] = dict(event.data)

@callback
def _on_delivery_update(event):
    # Always intermediate, always progressive=True. Merge cumulative rx_log_data.
    key = (event.data["entity_id"], event.data["timestamp"])
    if key in pending:
        pending[key]["rx_log_data"] = event.data.get("rx_log_data", [])
        pending[key]["repeater_count"] = event.data.get("repeater_count", 0)

hass.bus.async_listen("meshcore_message", _on_message)
hass.bus.async_listen("meshcore_delivery_update", _on_delivery_update)
```

For *incoming* channel messages there is no terminal re-fire — RX_LOG is correlated synchronously up to a 500 ms wait, so the initial event is usually complete on first fire. Companions worried about stale `pending` entries should apply their own retention timeout.

## Reference implementations

- **[`MeshCore-HA-UI`](https://github.com/Ratty7198/MeshCore-HA-UI)** — companion UI consuming the event bus and `send_*` services.
- **[`meshcore-ha-chat`](https://github.com/mwolter805/meshcore-ha-chat)** — sidebar chat panel + persistent message store; uses the events listed above plus the structured query services.

## Deprecation and reporting

Deprecated surfaces ship a change-log notice, remain functional for at least one full feature release, and are removed no earlier than the next feature release after that — companions get one to two cycles to migrate. Experimental surfaces are exempt. Security- or correctness-critical changes may bypass the cycle and will be called out in release notes.

If a release regresses a documented surface, file an issue at [meshcore-dev/meshcore-ha](https://github.com/meshcore-dev/meshcore-ha/issues) naming the companion you maintain and the specific field/service/pattern that changed. Regressions in documented surfaces are higher priority than ordinary bug reports. If you rely on an undocumented surface and want it promoted, file an issue describing your use case.
