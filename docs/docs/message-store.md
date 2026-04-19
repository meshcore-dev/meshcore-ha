---
sidebar_position: 5.5
title: Message Store
---

# Message Store

The integration maintains a per-conversation message store independent of the Home Assistant logbook. It records each message's text, sender, timestamp, delivery status, and RX_LOG routing data, and exposes them through two services and an internal API consumed by the sidebar panel.

The design goal is explicit: keep the store small, bounded, and predictable, while giving the frontend and user automations information the logbook cannot provide.

## Why a purpose-built store

The logbook is a good surface for individual message *events*, but it does not work well as a backing store for a conversation UI:

- **Unread tracking.** The logbook has no concept of read state. The sidebar panel needs to know which messages the user has and has not seen since the last visit — that has to live somewhere persistent.
- **RX_LOG correlation is lost on re-read.** When a channel message is received, the integration correlates multiple RX_LOG entries (each with SNR, RSSI, hop count, routing path) onto the message event as `rx_log_data`. The logbook only stores the event summary; the routing detail is gone once the message scrolls off the event bus. The message store keeps `rx_log_data` attached to each message.
- **Delivery status is asynchronous.** For outgoing messages, delivery acks and round-trip timing arrive after the initial send event. The store updates the existing message record in-place; the logbook would need a second entry.
- **Bounded retention, predictable footprint.** The logbook grows with overall HA activity and is tuned for the whole system. Conversation history should roll off on its own schedule, independent of whatever the user has configured for the recorder.
- **Efficient scrollback.** Loading a conversation in the sidebar reads a single JSON file, not a recorder query spanning months of logbook rows.

## Size and retention

The store is deliberately small. The following limits are enforced:

| Setting | Default | Notes |
|---|---|---|
| Messages per conversation | **500** | When exceeded, oldest messages are dropped FIFO. Configurable via options. |
| Retention window | **90 days** | Daily cleanup removes messages older than this. Configurable via options. |
| Idle eviction | **5 minutes** | Conversations not accessed for 5 minutes are flushed to disk and dropped from memory. |

In practice a busy public channel hits the 500-message cap well before it hits 90 days, so the store naturally stays in single-digit megabytes per conversation even on active networks. Unused conversations consume zero memory — they're written to disk and evicted.

Each conversation is stored in its own file under Home Assistant's `.storage/` directory (`meshcore.<entry_id>.msgs.<entity_id>`). A lightweight index (`meshcore.<entry_id>.msgidx`) holds one row per conversation with message count, last sender, last timestamp, and a 50-character preview — this is what the conversation list in the sidebar renders from without loading any message bodies.

## Services

### Get Messages

Retrieve stored messages for a specific conversation.

**Service:** `meshcore.get_messages` — returns a response

**Fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `entity_id` | entity | Yes | Conversation binary sensor (channel or contact messages entity) |
| `limit` | integer | No | Max messages to return (1–500, default 50) |
| `before` | string | No | Message ID to paginate before (returns older messages) |
| `entry_id` | string | No | Config entry ID for multi-device setups |

**Example:**

```yaml
service: meshcore.get_messages
data:
  entity_id: binary_sensor.meshcore_abc123_def456_messages
  limit: 100
response_variable: result
```

Response:

```yaml
messages:
  - id: "..."
    sender: "..."
    text: "..."
    timestamp: "2026-04-18T12:34:56"
    delivery_status: "acked"
    rx_log_data: [...]
    repeater_count: 2
```

### Search Messages

Substring search across message text and sender name. Scans all conversations by default, or a specific one if `entity_id` is supplied.

**Service:** `meshcore.search_messages` — returns a response

**Fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | Yes | Substring to match (case-insensitive) |
| `entity_id` | entity | No | Limit search to a specific conversation |
| `limit` | integer | No | Max results (1–100, default 20) |
| `entry_id` | string | No | Config entry ID for multi-device setups |

**Example:**

```yaml
service: meshcore.search_messages
data:
  query: "weather"
  limit: 50
response_variable: result
```

## Integration with the sidebar panel

The sidebar panel reads from the message store over the integration's WebSocket API, with progressive loading: the conversation list is rendered from the index alone, then full message history loads on demand when a conversation is opened. The 5-minute idle eviction means closing a conversation returns its memory to the OS within a few minutes of inactivity.

Outgoing messages are written to the store before the wire send completes (the "fire-then-enhance" pattern); delivery updates replace the record's `delivery_status` field once the ack arrives, and RX_LOG data is attached progressively as repeater relays report in.

## Use in automations and bots

Because each message is kept as a full record with `rx_log_data`, `delivery_status`, and timestamps, the store is a practical source of truth for automations that need more context than a logbook entry. Examples:

- **Activity scan.** Use `search_messages` to flag if a keyword has appeared in any channel in the last N messages.
- **Delivery verification.** After sending a message, call `get_messages` on the destination entity and check `delivery_status` on the latest entry.
- **Mesh path analysis.** The `rx_log_data` on each incoming message lists every repeater that relayed it, with SNR and RSSI per hop — usable directly in a template sensor or python_script to track routing diversity over time.

## Notes

- Conversations are created lazily — the first time a message is stored for an entity, its file and index entry appear. There is no "enable" flag; the store is always on.
- Persistence uses Home Assistant's `Store` helper, so files survive restarts and participate in HA backups.
- The store is scoped per-integration-entry. Deleting the integration entry removes all conversation files for that entry.
