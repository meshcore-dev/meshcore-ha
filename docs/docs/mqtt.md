---
sidebar_position: 8
title: MQTT Upload
---

# MQTT Upload

The Meshcore Home Assistant integration can publish Meshcore packet data to MQTT brokers directly from the integration.

## Overview

MQTT upload supports:

- Up to 4 brokers
- Dynamic broker management (add/edit/remove)
- Custom MQTT brokers (username/password or no auth)
- LetsMesh brokers using MeshCore auth-token mode
- Per-broker topic templates and auth settings
- Per-broker payload mode (`packet` or `raw`)
- Per-broker connection binary sensors under the main node device

## Configure in Home Assistant

1. Go to **Settings** -> **Devices & Services**
2. Open your **Meshcore** integration
3. Click **Configure**
4. Open **Manage MQTT Brokers**
5. Use **Add Broker**, **Edit Broker**, or **Remove Broker**

## Broker Settings

Per broker, configure:

- **Enabled**
- **Server**
- **Port**
- **Transport** (`tcp` or `websockets`)
- **Use TLS**
- **TLS Verify**
- **Username / Password** (not needed when using auth token)
- **Use MeshCore Auth Token**
- **Token Audience** (usually broker hostname for token-based setups)
- **Owner Public Key** (optional JWT `owner` claim)
- **Owner Email** (optional JWT `email` claim)
- **Auth Token TTL** (seconds)
- **Payload Mode**:
  - `packet` = normalized packet payloads (LetsMesh-compatible behavior)
  - `raw` = raw MeshCore event payloads
- **Status Topic**
- **Packets Topic**
- **IATA** (per-broker topic region code)
- **Client ID Prefix**

## LetsMesh Setup

Typical LetsMesh settings:

- `Server`: `mqtt-us-v1.letsmesh.net` (or regional LetsMesh endpoint)
- `Port`: `443`
- `Transport`: `websockets`
- `Use TLS`: enabled
- `Use MeshCore Auth Token`: enabled
- `Token Audience`: same as broker hostname
- `Payload Mode`: `packet`
- `Packets Topic`: `meshcore/{IATA}/{PUBLIC_KEY}/packets`

:::info
When uploading to LetsMesh, you do not need to provide a username or password. Authentication is handled automatically when **Use MeshCore Auth Token** is enabled.
:::

:::info
If a LetsMesh broker is configured with an `/events` packets topic, the integration auto-corrects it to `/packets`.
:::

## Auth Token Behavior

Auth-token mode works as follows:

1. Integration requests private key from the connected node via `export_private_key()`
2. It tries `meshcore-decoder` first if available
3. If `meshcore-decoder` is missing/unavailable, it falls back to in-process Python signing (`PyNaCl`)
4. If the broker rejects auth (for example after token expiry), the integration refreshes token credentials and attempts reconnect automatically

`meshcore-decoder` is optional for normal installs.

:::warning
If the node cannot export its private key (firmware/export disabled), auth-token upload cannot start.
:::

## Published Payload Behavior

MQTT publishing behavior depends on broker `Payload Mode`.

### `packet` mode

- Publishes packet-style payloads only (RX/RF/PACKET path)
- Uses topic shape `meshcore/{IATA}/{PUBLIC_KEY}/packets` by default
- Normalizes RX/RF packet data into legacy packet JSON fields (`type=PACKET`, `direction`, `route`, `packet_type`, `hash`, etc.)
- Applies duplicate suppression to reduce duplicate callback publishes

### `raw` mode

- Publishes raw event payloads without packet normalization
- Payload includes:
  - `event_type`
  - `payload` (sanitized MeshCore event payload)
  - `timestamp`
  - `origin` / `origin_id`

Packet publishes are non-retained. Status publishes (`online` / `offline` / LWT) are retained.

## MQTT Connection Sensors

For each configured broker, the integration exposes a connection binary sensor under the main MeshCore device:

- `binary_sensor.meshcore_*_mqtt_broker_1_connection`
- `binary_sensor.meshcore_*_mqtt_broker_2_connection`
- etc.

## Troubleshooting

If MQTT upload is not working:

1. Confirm broker is **Enabled**
2. Check Home Assistant logs for `[MQTT1]`, `[MQTT2]`, etc.
3. Verify auth-token broker has valid `Token Audience` and `Auth Token TTL`
4. Verify private key export works on the connected node
5. Check broker connection diagnostic sensors in Home Assistant

Common log examples:

- `meshcore-decoder not found ... will try Python fallback signer`
- `Private key export command failed`
- `Auth token requested but token generation failed`
- `Refreshed auth token; attempting reconnect`
