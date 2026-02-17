---
sidebar_position: 8
title: MQTT Upload
---

# MQTT Upload

The Meshcore Home Assistant integration can publish Meshcore packet data to MQTT brokers directly from the integration.

## Overview

MQTT upload supports:

- Up to 4 brokers
- Custom MQTT brokers (username/password or no auth)
- LetsMesh brokers using MeshCore auth-token mode
- Per-broker topic templates

## Configure in Home Assistant

1. Go to **Settings** -> **Devices & Services**
2. Open your **Meshcore** integration
3. Click **Configure**
4. Open:
   - **MQTT Global Settings**
   - **MQTT Broker Settings** (Broker 1-4)

## Global MQTT Settings

- **IATA Code**: Region code used in MQTT topic templates
- **Auth Token TTL**: Token lifetime (seconds)

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
- **Status Topic**
- **Packets Topic**
- **IATA override** (optional)
- **Client ID Prefix**

## LetsMesh Setup

Typical LetsMesh settings:

- `Server`: `mqtt-us-v1.letsmesh.net` (or regional LetsMesh endpoint)
- `Port`: `443`
- `Transport`: `websockets`
- `Use TLS`: enabled
- `Use MeshCore Auth Token`: enabled
- `Token Audience`: same as broker hostname
- `Packets Topic`: `meshcore/{IATA}/{PUBLIC_KEY}/packets`

:::info
If a LetsMesh broker is configured with an `/events` packets topic, the integration auto-corrects it to `/packets`.
:::

## Auth Token Behavior

Auth-token mode works as follows:

1. Integration requests private key from the connected node via `export_private_key()`
2. It tries `meshcore-decoder` first if available
3. If `meshcore-decoder` is missing/unavailable, it falls back to in-process Python signing (`PyNaCl`)

`meshcore-decoder` is optional for normal installs.

:::warning
If the node cannot export its private key (firmware/export disabled), auth-token upload cannot start.
:::

## Published Payload Behavior

MQTT publishing is aligned with `meshcoretomqtt` behavior:

- Publishes packet-style payloads only
- Uses topic shape `meshcore/{IATA}/{PUBLIC_KEY}/packets` by default
- Normalizes RX/RF packet data into legacy packet JSON fields (`type=PACKET`, `direction`, `route`, `packet_type`, `hash`, etc.)
- Applies duplicate suppression to reduce duplicate callback publishes

## Troubleshooting

If MQTT upload is not working:

1. Confirm broker is **Enabled**
2. Check Home Assistant logs for `[MQTT1]`, `[MQTT2]`, etc.
3. Verify auth-token broker has valid `Token Audience`
4. Verify private key export works on the connected node
5. Restart Home Assistant after updates to ensure new code is loaded

Common log examples:

- `meshcore-decoder not found ... will try Python fallback signer`
- `Private key export command failed`
- `Auth token requested but token generation failed`
