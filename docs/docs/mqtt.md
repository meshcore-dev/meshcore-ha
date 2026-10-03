---
sidebar_position: 17
title: MQTT Upload
---

# MQTT Upload

The integration can publish MeshCore packets and events to a maximum of 4 MQTT brokers. It connects to the brokers directly. It does not use the Home Assistant MQTT integration. Each broker has its own server, authentication, topics and payload mode.

## Configure a broker

1. Go to **Settings > Devices & services > MeshCore > Configure**.
2. Select **Manage MQTT Brokers**.
3. If you edit or remove a broker, select it in **Broker**.
4. In **Action**, select **Add Broker**, **Edit Broker** or **Remove Broker**.
5. Select **Submit**.
6. If you add or edit a broker, complete the **MQTT Broker Settings** form.
7. If you add or edit a broker, select **Submit**.

The change applies at once, without a reload. The [connection sensors](#connection-sensors) follow the new broker list.

## Broker settings

| Field | Default | Notes |
|---|---|---|
| **Enabled** | Off | The integration skips a disabled broker. |
| **Server** | Empty | Host name or IP address. The integration skips an enabled broker with no server. |
| **Port** | 1883 | 1 to 65535. |
| **Transport** | `tcp` | `tcp` or `websockets`. The WebSocket path is `/`. |
| **Use TLS** | Off | |
| **Verify TLS Certificate** | On | If off, the integration accepts any certificate and logs a warning. |
| **Keepalive (seconds)** | 60 | 15 to 300. |
| **Username (not needed with Auth Token)** | Empty | Used only when **Use MeshCore Auth Token** is off. |
| **Password (not needed with Auth Token)** | Empty | Used only with a username. |
| **Use MeshCore Auth Token** | Off | See [Auth token](#auth-token). |
| **Token Audience** | Empty | Sent as the `aud` claim of the token. |
| **Owner Public Key (JWT owner claim)** | Empty | Optional, 64 hex characters. Sent only when **Use TLS** and **Verify TLS Certificate** are on. |
| **Owner Email (JWT email claim)** | Empty | Optional. Sent only when **Use TLS** and **Verify TLS Certificate** are on. |
| **Payload Mode** | Packet (LetsMesh-compatible) | Or Raw Event. See [Payloads](#payloads). |
| **Auth Token TTL (seconds)** | 3600 | 60 to 86400. |
| **Status Topic** | `meshcore/{IATA}/{PUBLIC_KEY}/status` | See [Topics](#topics). If empty, the broker starts without a last will and the integration logs a warning. |
| **Packets Topic** | `meshcore/{IATA}/{PUBLIC_KEY}/packets` | See [Topics](#topics). |
| **Broker IATA Code** | `XYZ` | The region code for the topics. See [IATA code](#iata-code). |

The integration publishes with QoS 0. It sets the retain flag on status messages, but not on packet messages.

## Topics

| Placeholder | Value |
|---|---|
| `{IATA}` | The **Broker IATA Code** in upper case, for example `LAX`. |
| `{IATA_lower}` | The **Broker IATA Code** in lower case, for example `lax`. |
| `{PUBLIC_KEY}` | The full public key of the companion (64 hex characters, upper case). |

### IATA code

If the **Broker IATA Code** is empty or `XYZ`, the integration uses the IATA code from the 2.x MQTT settings of the entry. If the entry has no 2.x code, the code stays `XYZ`.

## LetsMesh setup

The integration treats a broker as a LetsMesh broker when **Server** or **Token Audience** contains `letsmesh.net`.

CAUTION: Set **Broker IATA Code** to your region code before you enable a LetsMesh broker. If the code stays `XYZ`, the integration skips the broker.

| Field | Value |
|---|---|
| **Server** | `mqtt-us-v1.letsmesh.net`, or the LetsMesh server for your region |
| **Port** | `443` |
| **Transport** | `websockets` |
| **Use TLS** | On |
| **Verify TLS Certificate** | On |
| **Use MeshCore Auth Token** | On |
| **Token Audience** | The host name in **Server** |
| **Payload Mode** | Packet (LetsMesh-compatible) |
| **Broker IATA Code** | The IATA airport code near your companion, for example `LAX` |

You do not need a username or password. The companion firmware must allow private key export (`ENABLE_PRIVATE_KEY_EXPORT=1`).

## Auth token

When **Use MeshCore Auth Token** is on, the integration signs a JSON Web Token with the private key of the companion:

1. The integration asks the companion for its private key. If the companion does not supply the key, the broker does not start.
2. The integration makes the token with `meshcore-decoder` if it is on the PATH. Otherwise, it signs the token in Python.
3. The integration connects with the username `v1_<PUBLIC_KEY>` and the token as the password.
4. If the broker refuses the token, the integration makes a new token and connects again.

The token contains `aud` (the **Token Audience**) and `client` (`meshcore-dev/meshcore-ha:<version>`). It contains `owner` and `email` only when **Use TLS** and **Verify TLS Certificate** are on.

## Payloads

| Payload Mode | What the integration publishes to **Packets Topic** |
|---|---|
| Packet (LetsMesh-compatible) | Packet log events (`RX_LOG_DATA` and RF or raw logs) that have a payload, in the packet JSON format that LetsMesh uses. Duplicates within 1 second are dropped. |
| Raw Event | Every MeshCore event, including received direct and channel messages. |

CAUTION: Do not send Raw Event payloads to a public broker. Raw events contain the text of your messages and of decrypted channel messages.

Packet example:

```json
{
  "timestamp": "2026-06-04T21:42:31.123456+00:00",
  "origin": "mynode",
  "origin_id": "<PUBLIC_KEY>",
  "type": "PACKET",
  "direction": "rx",
  "time": "21:42:31",
  "date": "4/6/2026",
  "len": "52",
  "packet_type": "4",
  "route": "F",
  "payload_len": "48",
  "raw": "<packet hex>",
  "SNR": "7.25",
  "RSSI": "-92",
  "score": "1000",
  "duration": "0",
  "hash": "<16 hex characters>"
}
```

`route` is `F` for flood and transport flood packets, or `D` for direct and transport direct packets. A packet with `route` `D` also has a `path` field.

Raw Event example:

```json
{
  "timestamp": "2026-06-04T21:42:31.123456+00:00",
  "origin": "mynode",
  "origin_id": "<PUBLIC_KEY>",
  "source": "meshcore-ha",
  "event_type": "EVENTTYPE.RX_LOG_DATA",
  "payload": {}
}
```

### Status messages

The integration publishes a retained `online` status to **Status Topic** when the broker connects, and every 5 minutes after. It publishes `offline` when it stops. The broker publishes `offline` (the last will) if the connection drops. The message includes the `model`, `firmware_version` and `radio` of the companion, and its `stats`.

The publish queue holds a maximum of 500 events. When it is full, the integration drops the oldest `RX_LOG_DATA` event.

## Secrets

The integration removes node secrets before an event reaches the Home Assistant event bus or MQTT. It does not forward private key export events. It replaces `channel_secret` and `secret` values with `<redacted>`. **Expose Node Secrets in Events** in **Global Settings** stops this redaction.

CAUTION: Do not enable **Expose Node Secrets in Events** when a broker uses Raw Event mode. The broker can receive your channel secrets and the private key of your companion.

## Connection sensors

Each broker has a connection binary sensor on the companion device, for example `binary_sensor.meshcore_abc123_mqtt_broker_1_connection`. It is on when the broker is connected. Its attributes are `broker_number` and `server`.

The integration creates the sensor for each enabled broker with a **Server**. A LetsMesh broker also needs an IATA code that is not `XYZ`. After a broker change, a new broker gets a sensor at once, and the sensor of a removed broker becomes unavailable. A broker that fails to start keeps its sensor, but the sensor stays off.

## Solve problems

1. Make sure that the broker is **Enabled** and has a **Server**.
2. For LetsMesh, make sure that **Broker IATA Code** is a real code, not `XYZ`.
3. For auth token brokers, make sure that the companion allows private key export.
4. Look in the log for lines that start with `[MQTT1]` to `[MQTT4]`.

| Message | Meaning |
|---|---|
| `[MQTTn] Disabled: Let's Mesh broker requires a non-default IATA code` | Set **Broker IATA Code**. |
| `[MQTTn] Private key export disabled on firmware (needs ENABLE_PRIVATE_KEY_EXPORT=1)` | The companion refused the key export. |
| `[MQTTn] Auth token requested but token generation failed` | No token. The broker does not start. |
| `meshcore-decoder not found in runtime PATH, will try Python fallback signer` | Normal. The integration signs the token in Python. |
| `[MQTTn] Connect failed: <reason>` | The broker refused the connection. |
| `[MQTTn] Connection timed out after 10s; paho keeps retrying` | The broker did not answer in 10 seconds. |
| `[MQTTn] Empty status topic; skipping MQTT will` | **Status Topic** is empty. The broker starts without a last will. |

## Related pages

- [Events](events.md): the event types that Raw Event mode publishes.
- [Map Auto Uploader](map-upload.md): the other feature that uses private key export.
- [Troubleshooting](troubleshooting.md): general problems with the integration.
