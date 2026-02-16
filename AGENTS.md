# AGENTS.md

This file tracks active engineering decisions and recent behavior changes for this fork.
Update this file in every change set that alters runtime behavior, configuration, payload schema, or deployment expectations.

## Maintenance Rule
- When making code changes, update `## Current Behavior` and add a new entry in `## Change Log`.
- Keep entries concise and include commit hashes when available.
- Do not remove old entries unless they are reverted; mark them as reverted instead.

## Current Behavior
- Integration supports direct MeshCore connection via USB/BLE/TCP.
- MQTT uploader is built into the integration and can be configured via Web UI options.
- Up to 4 MQTT brokers are supported.
- Broker auth supports username/password and MeshCore auth-token mode.
- Auth-token generation flow:
  1. Try `meshcore-decoder` CLI if configured/present.
  2. Fallback to in-process Python signer (PyNaCl).
- If private key is not configured, uploader attempts `export_private_key()` from connected device.
- MQTT client IDs are node-name based and sanitized, with broker suffix for brokers >1.
- Packet publishing defaults to topic template shape compatible with other MeshCore uploaders (`.../packets`), and status payloads include `origin`/`origin_id`.
- MQTT uploader emits startup INFO logs per broker and DEBUG logs for successful status/packet publishes.
- MQTT uploader defaults to relevant-event filtering (packet/message/radio-log style), not full event firehose.
- Global option `mqtt_publish_all_events` can disable filtering and publish all forwarded events.

## UI Configuration Keys
- Global:
  - `mqtt_iata`
  - `mqtt_decoder_cmd`
  - `mqtt_private_key`
  - `mqtt_token_ttl_seconds`
  - `mqtt_publish_all_events`
- Per broker:
  - `enabled`, `server`, `port`, `transport`
  - `use_tls`, `tls_verify`
  - `keepalive`, `qos`, `retain`
  - `username`, `password`
  - `use_auth_token`, `token_audience`
  - `topic_status`, `topic_events` (currently used as packets topic)
  - `iata`, `client_id_prefix`

## Known Notes
- If firmware does not allow private key export (`ENABLE_PRIVATE_KEY_EXPORT=1` missing), auth-token mode requires manual private key entry.
- In HA runtime, `meshcore-decoder` may be absent; Python fallback signer covers this case.

## Change Log
- 2026-02-16: Added integrated MQTT uploader with multi-broker support and auth-token mode.
- 2026-02-16: Added Web UI options flow for MQTT global settings and per-broker settings.
- 2026-02-16: Added private-key auto-fetch via `export_private_key()` when missing.
- 2026-02-16: Added Python auth-token fallback signer (PyNaCl) when `meshcore-decoder` is unavailable.
- 2026-02-16: Fixed paho ReasonCode handling and moved blocking TLS setup off HA event loop.
- 2026-02-16: Aligned MQTT client ID and packet/status payload shape with existing MeshCore uploader conventions.
- 2026-02-16: Added clearer MQTT runtime logs (broker init INFO + publish success DEBUG).
- 2026-02-16: Added default relevant-event filtering for MQTT uploads with UI toggle (`mqtt_publish_all_events`) for full event stream.
- 2026-02-16: Added explicit per-broker startup diagnostics (disabled/missing server/init failure) to simplify MQTT broker troubleshooting.
- 2026-02-16: LetsMesh auth-token `client` claim is now fixed (not user-configurable) as `meshcore-dev/meshcore-ha:<manifest version>`.
