---
sidebar_position: 20
title: Companion Integration API
---

# Companion Integration API

This page is the contract between meshcore-ha and other integrations, automations and scripts that build on it. The supported surface is the **events**, the **services** and the **entity ID formats** on this page. Everything else is internal, including all objects in `hass.data["meshcore"]`.

## Stability rules

- Additive changes (new fields, services or attributes) can occur in any release. Ignore fields that you do not know.
- A rename, a removal or a type change gets a notice in the change log. The old form continues to work for at least one feature release. The earliest removal is the feature release after that.
- Items marked **experimental** can change or disappear without notice.
- A change for security or correctness can skip this cycle. The release notes will say so.

If you need something that is not on this page, open an issue at [meshcore-dev/meshcore-ha](https://github.com/meshcore-dev/meshcore-ha/issues). If a release breaks a surface on this page, open an issue that names your integration and the changed field, service or format.

## Unsupported surface

The coordinator in `hass.data["meshcore"][entry_id]`, its `api` session and their attributes are internal. They changed in 3.0 and will change again without notice.

3.0 removed three internals that companion integrations used in 2.x. Each one is a deprecated shim that a future release will delete:

| Removed internal | What the shim does | Replacement |
|---|---|---|
| `coordinator._repeater_stats` | Returns an empty dict, as in 2.x | Remove the read. Use the repeater [sensors](sensors.md). |
| `api.mesh_core` | Returns the raw meshcore-py `MeshCore` instance, or `None` when the link is down. Calls bypass the session lock. | Use the `meshcore` services |
| `api._cache_self_info_event` | Calls `api.cache_self_info_event`, which is also internal | Read node identity from the companion sensors |

The first read of a shim logs one warning for each name and Home Assistant run. The warning names the calling file and line, for example:

```text
api.mesh_core is a removed meshcore internal, called from meshcore_ha_chat/store.py:120. It still works in 3.0 but will be deleted in a future release; use the meshcore services, or open an issue at github.com/meshcore-dev/meshcore-ha for anything they don't cover
```

## Events

Subscribe with `hass.bus.async_listen(...)` or with a standard event trigger. [Events](events.md) gives the fields of each event.

| Event | Stability |
|---|---|
| [`meshcore_message`](events.md#meshcore_message) | Stable. `hop_count` and `snr` are experimental. |
| [`meshcore_delivery_update`](events.md#meshcore_delivery_update) | Stable |
| [`meshcore_message_sent`](events.md#meshcore_message_sent) | Stable |
| [`meshcore_message_send_failed`](events.md#meshcore_message_send_failed) | Stable |
| [`meshcore_cli_response`](events.md#meshcore_cli_response) | Stable |
| [`meshcore_connected`, `meshcore_disconnected`](events.md#connection-events) | Stable |
| [`meshcore_raw_event`](events.md#meshcore_raw_event) | Experimental. The payload follows the meshcore-py schema, which can change with meshcore-py releases. |

### Event rules

An integration can rely on these rules. See also [Events: Message event order](events.md#message-event-order).

- Every `meshcore_*` event carries `entry_id` and `device_id`. `device_id` is null before the integration registers the companion. With two or more entries, filter every event on `entry_id`.
- On `meshcore_cli_response`, `entry_id` is the entry that the caller named. `resolved_entry_id` is the entry that ran the command.
- The `timestamp` format is different for each event. See [Events: Timestamps](events.md#timestamps).
- All events of one outgoing message carry the same `send_id`.
- A direct message fires `meshcore_message_sent` two times, then `meshcore_message`, then one `meshcore_delivery_update` with `progressive: false`.
- A channel message fires `meshcore_message_sent`, then `meshcore_message`, then 4 to 20 delivery updates. Only the last update has `progressive: false`. If `meshcore_message` has `collecting: false`, no update follows.
- A delivery update for an incoming channel message has `progressive: true` and no `send_id`. No final update follows.
- If the send fails before it leaves the companion, the caller gets only `meshcore_message_send_failed`. That event has no `send_id`.
- If a `send_message` call has no `node_id` and no `pubkey_prefix`, the call raises an error. No event fires.
- If `entry_id` names no loaded entry, or no entry is loaded, a send call does nothing. No event fires and the call raises no error.
- `execute_command` sends (for example `send_msg`) fire no message events.
- If **Expose Node Secrets in Events** is off, secrets read `"<redacted>"` and private-key events do not reach the bus.

### Example: track the final state of each message

This example keeps one record per message and correlates outgoing messages on `send_id`. The final state is an incoming `meshcore_message`, a `meshcore_delivery_update` with `progressive: false`, or an outgoing channel `meshcore_message` with `collecting: false`.

```python
from homeassistant.core import Event, HomeAssistant, callback

EVENT_MESSAGE = "meshcore_message"
EVENT_DELIVERY_UPDATE = "meshcore_delivery_update"


class MessageTracker:
    """Store the final state of each MeshCore message for one entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry_id = entry_id
        self._pending: dict[str, dict] = {}
        self._unsubs = [
            hass.bus.async_listen(EVENT_MESSAGE, self._on_message),
            hass.bus.async_listen(EVENT_DELIVERY_UPDATE, self._on_update),
        ]

    @callback
    def _on_message(self, event: Event) -> None:
        data = event.data
        if data.get("entry_id") != self._entry_id:
            return
        send_id = data.get("send_id")
        if not data.get("outgoing") or send_id is None:
            self._commit(dict(data))
            return
        if data.get("message_type") == "channel" and data.get("collecting") is False:
            self._commit(dict(data))
            return
        self._pending[send_id] = dict(data)

    @callback
    def _on_update(self, event: Event) -> None:
        data = event.data
        if data.get("entry_id") != self._entry_id:
            return
        send_id = data.get("send_id")
        if send_id is None or send_id not in self._pending:
            return
        record = self._pending[send_id]
        for key in ("rx_log_data", "repeater_count", "ack_received"):
            if key in data:
                record[key] = data[key]
        if data.get("progressive") is False:
            self._commit(self._pending.pop(send_id))

    def _commit(self, record: dict) -> None:
        """Persist the final record."""

    @callback
    def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
```

The example ignores incoming delivery updates, which have no `send_id`. Remove a pending record after 60 seconds if no final update arrives. The longest collection window is 20 seconds.

## Services

For a service that returns data, pass `return_response=True` (Python) or `return_response: true` (WebSocket). All services accept an optional `entry_id`. Give it if two or more entries exist. For the rules without it, see [Services: Select the entry](services.md#select-the-entry).

### Supported services

| Service | Fields | Response | Stability |
|---|---|---|---|
| `meshcore.send_message` | `message`, and `node_id` or `pubkey_prefix` (one of the two) | none | stable |
| `meshcore.send_channel_message` | `channel_idx`, `message`, `scope` (optional) | none | stable |
| `meshcore.get_contacts` | none | `{"contacts": [...]}` | stable |
| `meshcore.get_discovered_contact` | `pubkey_prefix` (2 characters minimum) | `{"contact": {...}}` | stable |
| `meshcore.get_channels` | none | `{"channels": [...]}` | stable |
| `meshcore.trace` | `pubkey_prefix`, `timeout` (1 to 120 s, default 15) | `{"trace": {...}}` | stable |

`node_id` is the advertised name of the contact. For `pubkey_prefix`, use 12 lowercase hex characters. Under the Governed traffic policy, `send_message`, `send_channel_message` and `trace` use credit from the messages lane. If the lane is empty, the call raises an error that gives the wait in seconds. See [Mesh Traffic Policy](traffic-policy.md).

For the response fields and the error codes, see [Services: Query services](services.md#query-services). An integration can rely on these rules:

- Check for `error` before you read the data. A failed query returns an empty list or `null`, and an `error` key. Only a traffic policy refusal raises an error.
- The `get_discovered_contact` prefix match is case-sensitive.
- `last_advert` in `get_contacts` comes from the clock of the advertising node.
- `trace` needs a contact on the companion. It can take approximately 90 seconds when it must first discover a route. Treat an unknown `trace` error code as diagnostic text.

### Other services

These services exist for the bundled UI and for users. They are **experimental** for companion use.

| Service | Admin only | Response | Purpose |
|---|---|---|---|
| `meshcore.execute_command` | yes | optional | Run a meshcore-py command. See [CLI Command Reference](cli-commands.md). |
| `meshcore.execute_command_ui` | yes | optional | Run the command in the command text helper. |
| `meshcore.send_ui_message` | no | optional | Send the text in the message helper to the selected recipient. |
| `meshcore.cli_console_clear` | no | none | Clear the CLI Console transcript. |
| `meshcore.add_selected_contact`, `meshcore.remove_selected_contact` | yes | none | See [Contact Management](contacts.md). |
| `meshcore.remove_discovered_contact`, `meshcore.cleanup_unavailable_contacts`, `meshcore.clear_discovered_contacts` | no | none | See [Contact Management](contacts.md). |

- Home Assistant refuses an admin-only call from a user who is not an administrator. It permits a call with no user, for example from an automation.
- The UI services return `{"error": "<code>", "message": "...", ...}` on failure. `send_ui_message` returns `{"success": true}` on success. See [Services: UI service errors](services.md#ui-service-errors).
- Do not parse `execute_command` output if a structured service gives the same data. For the response shapes, see [Services: Response shapes](services.md#response-shapes).
- `execute_command` raises an error when the integration denies the command or the traffic policy refuses it.

## Entity ID formats

`<pk6>` is the first 6 hex characters of the companion public key, and `<name>` is the entry name as a slug. The `entity_id` field in events uses these formats, also after a user renames the entity.

| Format | Example | Purpose |
|---|---|---|
| `binary_sensor.meshcore_<pk6>_<node pk6>_messages` | `binary_sensor.meshcore_abc123_def456_messages` | Conversation with one contact. `<node pk6>` is the first 6 hex characters of the public key of the contact, not the 12-character `pubkey_prefix`. |
| `binary_sensor.meshcore_<pk6>_ch_<channel_idx>_messages` | `binary_sensor.meshcore_abc123_ch_1_messages` | Conversation on one channel. |
| `binary_sensor.meshcore_<adv name>_<pk12>_contact` | `binary_sensor.meshcore_myclient_def456abc012_contact` | One contact. `<adv name>` is the advertised name as a slug. See [Contact Management](contacts.md#contact-entities). |
| `sensor.meshcore_<pk6>_node_status_<name>` | `sensor.meshcore_abc123_node_status_mynode` | Link state: `online` or `offline`. Unavailable after the companion disconnects. |
| `sensor.meshcore_<pk6>_last_message_delivery_<name>` | `sensor.meshcore_abc123_last_message_delivery_mynode` | Delivery state of the last outgoing message of this entry. |
| `sensor.meshcore_<pk6>_discovered_summary_<name>` | `sensor.meshcore_abc123_discovered_summary_mynode` | Count of discovered contacts. Disabled by default. |

For tracked repeaters and clients, see [Sensors](sensors.md). Only the attributes that [Sensors](sensors.md) and [Contact Management](contacts.md) document are supported.

The UI helper entities (`select.meshcore_*`, `text.meshcore_*`) are not part of the supported surface. With two or more entries, Home Assistant adds a suffix to their IDs. Use the services instead.

## Reference implementations

- [`MeshCore-HA-UI`](https://github.com/Ratty7198/MeshCore-HA-UI): a companion UI that uses the event bus and the send services.
- [`meshcore-ha-chat`](https://github.com/mwolter805/meshcore-ha-chat): a chat panel with a message store. It uses the events and the query services on this page.

For changes between 2.x and 3.0, see [Upgrade to 3.0](upgrade-3.0.md).
