"""MQTT uploader for MeshCore integration events."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shlex
import ssl
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MQTT_BROKERS,
    CONF_MQTT_DECODER_CMD,
    CONF_MQTT_IATA,
    CONF_MQTT_TOKEN_TTL_SECONDS,
    CONF_NAME,
    CONF_PUBKEY,
)

import paho.mqtt.client as mqtt
import nacl.bindings
from meshcore.events import EventType


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    """Convert string-ish value to bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | int | None, default: int) -> int:
    """Convert string-ish value to int with fallback."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class BrokerConfig:
    """Configuration for one MQTT broker."""

    number: int
    enabled: bool
    server: str
    port: int
    transport: str
    use_tls: bool
    tls_verify: bool
    keepalive: int
    qos: int
    username: str
    password: str
    use_auth_token: bool
    token_audience: str
    token_ttl_seconds: int
    payload_mode: str
    client_id_prefix: str
    topic_status: str
    topic_packets: str

    @property
    def name(self) -> str:
        return f"MQTT{self.number}"

    @property
    def is_letsmesh(self) -> bool:
        host = (self.server or "").lower()
        aud = (self.token_audience or "").lower()
        return "letsmesh.net" in host or "letsmesh.net" in aud


class MeshCoreMqttUploader:
    """Publish MeshCore raw events and status to MQTT brokers."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger,
        entry: ConfigEntry,
        api=None,
        integration_version: str = "unknown",
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.entry = entry
        self.api = api
        self.integration_version = (integration_version or "unknown").strip() or "unknown"
        self.settings = entry.data.get(CONF_MQTT_BROKERS, {}) or {}
        self.node_name = str(entry.data.get(CONF_NAME, "meshcore") or "meshcore").strip()
        self.public_key = (entry.data.get(CONF_PUBKEY, "") or "").upper()
        self.global_iata = str(entry.data.get(CONF_MQTT_IATA, "LOC") or "LOC").strip().upper()
        self.decoder_cmd = str(entry.data.get(CONF_MQTT_DECODER_CMD, "meshcore-decoder") or "meshcore-decoder").strip()
        self.default_token_ttl_seconds = _as_int(
            entry.data.get(CONF_MQTT_TOKEN_TTL_SECONDS),
            3600,
        )
        # Auth token signing key is sourced from connected radio only.
        self.private_key = ""
        self.client_agent = self._build_client_agent()
        self._brokers = self._load_brokers()
        self._clients: list[dict[str, Any]] = []
        self._tokens: dict[int, dict[str, Any]] = {}
        self._auth_refresh_tasks: dict[int, asyncio.Task[None]] = {}
        self._connection_state_callbacks: set[Callable[[int, bool], None]] = set()
        self._recent_packet_signatures: dict[str, float] = {}
        self._packet_dedupe_ttl_seconds = 1.0

    def _build_client_agent(self) -> str:
        """Build fixed LetsMesh client agent label."""
        return f"meshcore-dev/meshcore-ha:{self.integration_version}"

    @property
    def enabled(self) -> bool:
        return len(self._brokers) > 0

    def get_brokers(self) -> list[BrokerConfig]:
        """Return configured broker definitions."""
        return list(self._brokers)

    def is_broker_connected(self, broker_num: int) -> bool:
        """Return current connected state for one broker."""
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                return bool(info.get("connected"))
        return False

    def register_connection_state_callback(
        self, callback: Callable[[int, bool], None]
    ) -> Callable[[], None]:
        """Register callback for broker connection state changes."""
        self._connection_state_callbacks.add(callback)

        def _unsub() -> None:
            self._connection_state_callbacks.discard(callback)

        return _unsub

    def _notify_connection_state(self, broker_num: int, connected: bool) -> None:
        """Notify subscribers about broker connection state changes."""
        if not isinstance(broker_num, int):
            return

        callbacks = tuple(self._connection_state_callbacks)

        def _dispatch() -> None:
            for callback in callbacks:
                try:
                    callback(broker_num, connected)
                except Exception as ex:
                    self.logger.debug("MQTT state callback failed for broker %s: %s", broker_num, ex)

        self.hass.loop.call_soon_threadsafe(_dispatch)

    def _load_brokers(self) -> list[BrokerConfig]:
        """Load broker configs from config entry data."""
        brokers: list[BrokerConfig] = []
        for idx in range(1, 5):
            broker_settings = self.settings.get(str(idx), {}) if isinstance(self.settings, dict) else {}
            broker_name = f"MQTT{idx}"
            enabled = _as_bool(
                broker_settings.get("enabled"),
                False,
            )
            server = str(broker_settings.get("server", "") or "").strip()
            if not enabled:
                self.logger.info("[%s] Disabled, skipping", broker_name)
                continue
            if not server:
                self.logger.warning("[%s] Enabled but no server configured, skipping", broker_name)
                continue

            iata = (
                str(broker_settings.get("iata", "") or "")
                .strip()
                .upper()
                or self.global_iata
            )
            if iata == "LOC":
                iata = self.global_iata

            broker = BrokerConfig(
                number=idx,
                enabled=True,
                server=server,
                port=_as_int(broker_settings.get("port"), 1883),
                transport=str(
                    broker_settings.get("transport", "tcp") or "tcp"
                ).strip().lower(),
                use_tls=_as_bool(broker_settings.get("use_tls"), False),
                tls_verify=_as_bool(
                    broker_settings.get("tls_verify"),
                    True,
                ),
                keepalive=_as_int(broker_settings.get("keepalive"), 60),
                qos=0,
                username=str(
                    broker_settings.get("username", "") or ""
                ).strip(),
                password=str(
                    broker_settings.get("password", "") or ""
                ).strip(),
                use_auth_token=_as_bool(
                    broker_settings.get("use_auth_token"),
                    False,
                ),
                token_audience=str(
                    broker_settings.get("token_audience", "") or ""
                ).strip(),
                token_ttl_seconds=_as_int(
                    broker_settings.get("token_ttl_seconds"),
                    self.default_token_ttl_seconds,
                ),
                payload_mode=str(
                    broker_settings.get("payload_mode", "packet") or "packet"
                ).strip().lower(),
                client_id_prefix=str(
                    broker_settings.get("client_id_prefix", "meshcore_") or "meshcore_"
                ).strip(),
                topic_status=self._resolve_topic(
                    str(
                        broker_settings.get(
                            "topic_status",
                            "meshcore/{IATA}/{PUBLIC_KEY}/status",
                        )
                    ),
                    iata,
                ),
                topic_packets=self._resolve_topic(
                    str(
                        broker_settings.get(
                            "topic_events",
                            "meshcore/{IATA}/{PUBLIC_KEY}/packets",
                        )
                    ),
                    iata,
                ),
            )

            if broker.is_letsmesh and iata in {"", "LOC"}:
                self.logger.warning(
                    "[%s] Disabled: Let's Mesh broker requires a non-default IATA code",
                    broker.name,
                )
                continue
            if broker.payload_mode not in {"packet", "raw"}:
                broker.payload_mode = "packet"
            if broker.is_letsmesh and broker.topic_packets.endswith("/events"):
                broker.topic_packets = f"{broker.topic_packets[:-7]}/packets"
                self.logger.info(
                    "[%s] Adjusted packets topic to LetsMesh-compatible path: %s",
                    broker.name,
                    broker.topic_packets,
                )

            brokers.append(broker)
        return brokers

    def _resolve_topic(self, template: str | None, iata: str) -> str:
        raw = (template or "").strip()
        if not raw:
            return ""
        return (
            raw.replace("{IATA}", iata.upper())
            .replace("{IATA_lower}", iata.lower())
            .replace("{PUBLIC_KEY}", self.public_key or "DEVICE")
        )

    @staticmethod
    def _sanitize_client_id(raw: str, prefix: str) -> str:
        """Build MQTT client id with same style as other uploaders."""
        client_id = f"{prefix}{raw.replace(' ', '_')}"
        client_id = re.sub(r"[^a-zA-Z0-9_-]", "", client_id)
        return client_id[:23]

    async def async_start(self) -> None:
        """Initialize configured MQTT clients and publish online status."""
        if not self._brokers:
            return

        for broker in self._brokers:
            client = await self._async_create_client(broker)
            if client is None:
                self.logger.warning("[%s] Client initialization failed; broker disabled for this run", broker.name)
                continue
            self._clients.append({"broker": broker, "client": client, "connected": False})

        if not self._clients:
            self.logger.warning("MQTT uploader enabled but no brokers could be initialized")
            return

        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            try:
                await self.hass.async_add_executor_job(
                    client.connect, broker.server, broker.port, broker.keepalive
                )
                client.loop_start()
            except Exception as ex:
                self.logger.error("[%s] Connection error: %s", broker.name, ex)

    async def _async_create_client(self, broker: BrokerConfig):
        """Create an MQTT client for one broker."""
        client_id = self._sanitize_client_id(self.public_key or self.node_name or self.entry.entry_id, broker.client_id_prefix)
        if broker.number > 1:
            client_id = f"{client_id[:20]}_{broker.number}"[:23]
        self.logger.info(
            "[%s] Initializing client_id=%s server=%s:%s transport=%s",
            broker.name,
            client_id,
            broker.server,
            broker.port,
            broker.transport,
        )
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                clean_session=True,
                transport=broker.transport,
            )
        except Exception as ex:
            self.logger.error("[%s] Failed to create client: %s", broker.name, ex)
            return None

        client.user_data_set({"broker_num": broker.number})
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect

        if broker.use_auth_token:
            password = await self._async_get_token(broker)
            if not password:
                self.logger.error("[%s] Auth token requested but token generation failed", broker.name)
                return None
            client.username_pw_set(f"v1_{self.public_key}", password)
        elif broker.username:
            client.username_pw_set(broker.username, broker.password)

        await self.hass.async_add_executor_job(self._configure_tls, client, broker)

        # Mirror other uploaders: set LWT offline status message.
        lwt_payload = json.dumps(self._build_status_payload("offline"))
        client.will_set(
            broker.topic_status,
            lwt_payload,
            qos=broker.qos,
            retain=False,
        )

        if broker.transport == "websockets":
            client.ws_set_options(path="/", headers=None)

        self.logger.info(
            "[%s] Ready (status_topic=%s packets_topic=%s auth_token=%s)",
            broker.name,
            broker.topic_status,
            broker.topic_packets,
            broker.use_auth_token,
        )
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """MQTT connect callback."""
        broker_num = userdata.get("broker_num")
        rc = self._reason_code_to_int(reason_code)
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                if rc == 0:
                    info["connected"] = True
                    self._notify_connection_state(broker_num, True)
                    self.logger.info("[%s] Connected", broker.name)
                    self._publish_status_for_client(client, broker, "online")
                else:
                    info["connected"] = False
                    self._notify_connection_state(broker_num, False)
                    self.logger.error("[%s] Connect failed: %s", broker.name, reason_code)
                    if broker.use_auth_token and self._is_auth_error(reason_code, rc):
                        self._schedule_auth_token_refresh(broker_num)
                return

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """MQTT disconnect callback."""
        broker_num = userdata.get("broker_num")
        rc = self._reason_code_to_int(reason_code)
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                info["connected"] = False
                self._notify_connection_state(broker_num, False)
                if rc != 0:
                    self.logger.warning("[%s] Disconnected: %s", broker.name, reason_code)
                    if broker.use_auth_token and self._is_auth_error(reason_code, rc):
                        self._schedule_auth_token_refresh(broker_num)
                return

    @staticmethod
    def _is_auth_error(reason_code, rc: int) -> bool:
        """Detect broker authorization failures across MQTT v3/v5 reason formats."""
        if rc in {4, 5, 134, 135}:
            return True
        reason_text = str(reason_code or "").strip().lower()
        return "not authorized" in reason_text or "bad user name or password" in reason_text

    def _schedule_auth_token_refresh(self, broker_num: int) -> None:
        """Schedule async auth token refresh and reconnect from paho callback thread."""
        if not isinstance(broker_num, int):
            return

        def _create_task() -> None:
            existing = self._auth_refresh_tasks.get(broker_num)
            if existing and not existing.done():
                return
            task = self.hass.async_create_task(self._async_refresh_auth_and_reconnect(broker_num))
            self._auth_refresh_tasks[broker_num] = task

        self.hass.loop.call_soon_threadsafe(_create_task)

    def _get_client_info(self, broker_num: int) -> dict[str, Any] | None:
        """Find broker/client info for a broker number."""
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                return info
        return None

    async def _async_refresh_auth_and_reconnect(self, broker_num: int) -> None:
        """Refresh auth token credentials and reconnect broker client."""
        try:
            info = self._get_client_info(broker_num)
            if info is None:
                return
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            if not broker.use_auth_token:
                return

            token = await self._async_get_token(broker, force_refresh=True)
            if not token:
                self.logger.error("[%s] Failed to refresh auth token after authorization error", broker.name)
                return

            client.username_pw_set(f"v1_{self.public_key}", token)
            self.logger.info("[%s] Refreshed auth token; attempting reconnect", broker.name)
            try:
                await self.hass.async_add_executor_job(client.reconnect)
            except Exception as ex:
                self.logger.warning("[%s] Reconnect after token refresh failed: %s", broker.name, ex)
        finally:
            self._auth_refresh_tasks.pop(broker_num, None)

    @staticmethod
    def _reason_code_to_int(reason_code) -> int:
        """Normalize paho reason code types to int."""
        try:
            return int(reason_code)
        except Exception:
            pass
        value = getattr(reason_code, "value", None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                pass
        return -1

    def _configure_tls(self, client, broker: BrokerConfig) -> None:
        """Configure client TLS options (runs in executor)."""
        if not broker.use_tls:
            return
        if broker.tls_verify:
            client.tls_set()
            client.tls_insecure_set(False)
        else:
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
            self.logger.warning("[%s] TLS verification disabled", broker.name)

    async def _async_get_token(self, broker: BrokerConfig, *, force_refresh: bool = False) -> str | None:
        """Create or reuse cached JWT token for one broker."""
        if not self.public_key:
            self.logger.error("[%s] Missing device public key; cannot generate auth token", broker.name)
            return None
        if not self.private_key:
            fetched_private_key = await self._async_fetch_private_key_from_device(broker)
            if fetched_private_key:
                self.private_key = fetched_private_key
            else:
                self.logger.error(
                    "[%s] Missing private key from device (export_private_key failed/disabled); auth-token upload disabled",
                    broker.name,
                )
                return None

        if force_refresh:
            self._tokens.pop(broker.number, None)

        cached = self._tokens.get(broker.number)
        now = time.time()
        if not force_refresh and cached and (now < (cached["expires_at"] - 60)):
            return cached["token"]

        ttl_seconds = max(60, _as_int(broker.token_ttl_seconds, self.default_token_ttl_seconds))
        claims = {}
        if broker.token_audience:
            claims["aud"] = broker.token_audience
        if self.client_agent:
            claims["client"] = self.client_agent

        args = shlex.split(self.decoder_cmd)
        args.extend(
            [
                "auth-token",
                self.public_key,
                self.private_key,
                "--exp",
                str(ttl_seconds),
            ]
        )
        if claims:
            args.extend(["--claims", json.dumps(claims)])

        token = await self.hass.async_add_executor_job(self._run_decoder_command, args)
        if not token:
            token = await self.hass.async_add_executor_job(
                self._create_auth_token_python,
                broker.token_audience,
                ttl_seconds,
            )
            if token:
                self.logger.info("[%s] Created auth token using Python fallback signer", broker.name)
        if not token:
            return None

        self._tokens[broker.number] = {
            "token": token,
            "expires_at": now + ttl_seconds,
        }
        return token

    async def _async_fetch_private_key_from_device(self, broker: BrokerConfig) -> str | None:
        """Try to fetch private key from connected MeshCore device."""
        if not self.api:
            self.logger.debug("[%s] No API instance available for private key export", broker.name)
            return None
        try:
            mesh_core = self.api.mesh_core
        except Exception as ex:
            self.logger.debug("[%s] MeshCore instance not ready for private key export: %s", broker.name, ex)
            return None

        try:
            self.logger.info("[%s] Attempting to fetch private key from device (export_private_key)", broker.name)
            result = await mesh_core.commands.export_private_key()
        except Exception as ex:
            self.logger.warning("[%s] Private key export command failed: %s", broker.name, ex)
            return None

        if not result:
            self.logger.warning("[%s] Private key export returned no result", broker.name)
            return None

        if getattr(result, "type", None) == EventType.PRIVATE_KEY:
            payload = getattr(result, "payload", {}) or {}
            private_key = payload.get("private_key")
            if isinstance(private_key, (bytes, bytearray)):
                private_key = private_key.hex()
            private_key = str(private_key or "").strip()
            if len(private_key) == 128 and all(c in "0123456789abcdefABCDEF" for c in private_key):
                self.logger.info("[%s] Private key fetched from device", broker.name)
                return private_key
            self.logger.warning("[%s] Device returned invalid private key format", broker.name)
            return None

        if getattr(result, "type", None) == EventType.DISABLED:
            self.logger.warning(
                "[%s] Private key export disabled on firmware (needs ENABLE_PRIVATE_KEY_EXPORT=1)",
                broker.name,
            )
            return None

        if getattr(result, "type", None) == EventType.ERROR:
            self.logger.warning("[%s] Device refused private key export: %s", broker.name, getattr(result, "payload", ""))
            return None

        self.logger.warning("[%s] Unexpected response for private key export: %s", broker.name, getattr(result, "type", None))
        return None

    def _run_decoder_command(self, args: list[str]) -> str | None:
        """Run meshcore-decoder CLI command to generate an auth token."""
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except FileNotFoundError:
            self.logger.warning(
                "meshcore-decoder not found in runtime PATH, will try Python fallback signer"
            )
            return None
        except Exception as ex:
            self.logger.error("Failed to execute decoder command: %s", ex)
            return None

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self.logger.error("meshcore-decoder auth-token failed: %s", stderr)
            return None

        token = (result.stdout or "").strip()
        if not token:
            self.logger.error("meshcore-decoder returned empty token")
            return None
        return token

    @staticmethod
    def _base64url_encode(data: bytes) -> str:
        """Base64url encode without padding."""
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _int_to_bytes_le(value: int, length: int) -> bytes:
        """Convert int to little-endian bytes."""
        return value.to_bytes(length, byteorder="little")

    @staticmethod
    def _bytes_to_int_le(data: bytes) -> int:
        """Convert little-endian bytes to int."""
        return int.from_bytes(data, byteorder="little")

    def _ed25519_sign_with_expanded_key(
        self, message: bytes, scalar: bytes, prefix: bytes
    ) -> bytes:
        """Sign message using MeshCore/orlp expanded Ed25519 private key format."""
        # Ed25519 group order
        group_order = 2**252 + 27742317777372353535851937790883648493

        # r = H(prefix || message) mod L
        h_r = hashlib.sha512(prefix + message).digest()
        r = self._bytes_to_int_le(h_r) % group_order
        r_bytes = self._int_to_bytes_le(r, 32)
        r_point = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(r_bytes)

        # k = H(R || public_key || message) mod L
        public_key_bytes = bytes.fromhex(self.public_key)
        h_k = hashlib.sha512(r_point + public_key_bytes + message).digest()
        k = self._bytes_to_int_le(h_k) % group_order

        # s = (r + k * scalar) mod L
        scalar_int = self._bytes_to_int_le(scalar)
        s = (r + k * scalar_int) % group_order
        s_bytes = self._int_to_bytes_le(s, 32)

        return r_point + s_bytes

    def _create_auth_token_python(self, audience: str | None = None, ttl_seconds: int = 3600) -> str | None:
        """Create MeshCore JWT token in-process (no external decoder binary)."""
        try:
            private_key_hex = (self.private_key or "").strip()
            if len(private_key_hex) != 128:
                self.logger.error("Python token signing failed: invalid private key length")
                return None
            if len(self.public_key or "") != 64:
                self.logger.error("Python token signing failed: invalid public key length")
                return None

            now = int(time.time())
            header = {"alg": "Ed25519", "typ": "JWT"}
            payload = {
                "publicKey": self.public_key.upper(),
                "iat": now,
                "exp": now + max(60, ttl_seconds),
            }
            if audience:
                payload["aud"] = audience
            if self.client_agent:
                payload["client"] = self.client_agent

            header_json = json.dumps(header, separators=(",", ":")).encode("utf-8")
            payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            header_encoded = self._base64url_encode(header_json)
            payload_encoded = self._base64url_encode(payload_json)
            signing_input = f"{header_encoded}.{payload_encoded}".encode("utf-8")

            private_bytes = bytes.fromhex(private_key_hex)
            scalar = private_bytes[:32]
            prefix = private_bytes[32:64]
            signature = self._ed25519_sign_with_expanded_key(signing_input, scalar, prefix).hex()
            return f"{header_encoded}.{payload_encoded}.{signature}"
        except Exception as ex:
            self.logger.error("Python token signing failed: %s", ex)
            return None

    def _publish_status_for_client(self, client, broker: BrokerConfig, state: str) -> None:
        """Publish status for one broker/client pair."""
        if not broker.topic_status:
            return
        payload = self._build_status_payload(state)
        try:
            result = client.publish(
                broker.topic_status,
                json.dumps(payload),
                qos=broker.qos,
                retain=False,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.logger.error("[%s] Status publish failed: rc=%s", broker.name, result.rc)
            else:
                self.logger.debug("[%s] Status published state=%s topic=%s", broker.name, state, broker.topic_status)
        except Exception as ex:
            self.logger.error("[%s] Status publish error: %s", broker.name, ex)

    def _build_status_payload(self, state: str) -> dict[str, Any]:
        """Build status payload compatible with other uploaders."""
        return {
            "status": state,
            "timestamp": datetime.now().isoformat(),
            "origin": self.node_name,
            "origin_id": self.public_key or "DEVICE",
            "source": "meshcore-ha",
        }

    def _build_raw_event_payload(self, event_type: str, payload: Any) -> dict[str, Any]:
        """Build raw event payload for non-normalized broker mode."""
        return {
            "timestamp": datetime.now().isoformat(),
            "origin": self.node_name,
            "origin_id": self.public_key or "DEVICE",
            "source": "meshcore-ha",
            "event_type": (event_type or "").upper(),
            "payload": payload,
        }

    async def async_publish_raw_event(self, event_type: str, payload: Any) -> None:
        """Publish one event without blocking the HA event loop callback path."""
        await self.hass.async_add_executor_job(self.publish_raw_event, event_type, payload)

    def publish_raw_event(self, event_type: str, payload: Any) -> None:
        """Publish one event to all connected brokers with per-broker payload mode."""
        if not self._clients:
            return

        packet_checked = False
        packet_skip = False
        packet_payload: str | None = None
        raw_payload: str | None = None

        for info in self._clients:
            if not info.get("connected"):
                continue
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            if not broker.topic_packets:
                continue

            payload_to_publish: str | None = None
            mode = broker.payload_mode if broker.payload_mode in {"packet", "raw"} else "packet"
            if mode == "raw":
                if raw_payload is None:
                    raw_payload = json.dumps(self._build_raw_event_payload(event_type, payload))
                payload_to_publish = raw_payload
            else:
                if not packet_checked:
                    packet_checked = True
                    normalized_packet = self._normalize_packet_event(event_type, payload)
                    if normalized_packet is None or self._is_duplicate_packet(normalized_packet):
                        packet_skip = True
                    else:
                        packet_payload = json.dumps(normalized_packet)
                if packet_skip or packet_payload is None:
                    continue
                payload_to_publish = packet_payload

            try:
                result = client.publish(
                    broker.topic_packets,
                    payload_to_publish,
                    qos=broker.qos,
                    retain=False,
                )
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    self.logger.error("[%s] Event publish failed: rc=%s", broker.name, result.rc)
                else:
                    self.logger.debug(
                        "[%s] Event published mode=%s topic=%s event=%s",
                        broker.name,
                        mode,
                        broker.topic_packets,
                        event_type,
                    )
            except Exception as ex:
                self.logger.error("[%s] Event publish error: %s", broker.name, ex)

    def _is_duplicate_packet(self, packet: dict[str, Any]) -> bool:
        """Avoid duplicate publishes from callback double-fires."""
        now = time.time()
        cutoff = now - self._packet_dedupe_ttl_seconds
        stale_keys = [key for key, ts in self._recent_packet_signatures.items() if ts < cutoff]
        for key in stale_keys:
            self._recent_packet_signatures.pop(key, None)

        signature = "|".join(
            [
                str(packet.get("hash", "")),
                str(packet.get("raw", "")),
                str(packet.get("route", "")),
                str(packet.get("packet_type", "")),
                str(packet.get("SNR", "")),
                str(packet.get("RSSI", "")),
            ]
        )
        if signature in self._recent_packet_signatures:
            self.logger.debug("Skipping duplicate packet publish signature=%s", signature[:32])
            return True
        self._recent_packet_signatures[signature] = now
        return False

    def _normalize_packet_event(self, event_type: str, payload: Any) -> dict[str, Any] | None:
        """Normalize RX/RF log events to legacy packet schema used by uploader tools."""
        if not isinstance(payload, dict):
            return None

        et = (event_type or "").upper()
        if "RX_LOG" not in et and "RF_LOG" not in et and "PACKET" not in et:
            return None

        now = datetime.now()
        payload_hex = str(payload.get("payload") or "").strip()
        raw_hex_fallback = str(payload.get("raw_hex") or "").strip()
        # Match packet-capture behavior: prefer payload, fallback to raw_hex without first 2 bytes.
        raw_hex = payload_hex or (raw_hex_fallback[4:] if len(raw_hex_fallback) > 4 else raw_hex_fallback)
        parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
        decrypted = payload.get("decrypted") if isinstance(payload.get("decrypted"), dict) else {}

        payload_len_total = len(raw_hex) // 2 if raw_hex else _as_int(payload.get("payload_length"), 0)

        path_len = _as_int(parsed.get("path_len"), 0) if parsed else 0
        payload_len = payload_len_total
        if payload_len_total > 0:
            # Legacy packet schema reports payload_len without header/path bytes.
            payload_len = max(0, payload_len_total - (2 + max(0, path_len)))

        packet_type = ""
        route = "U"
        route_type = None
        hash_value = str(payload.get("hash") or "").strip().upper()
        if raw_hex:
            try:
                packet_bytes = bytes.fromhex(raw_hex)
                header = packet_bytes[0]
                payload_type_value = (header >> 2) & 0x0F
                route_type = header & 0x03
                packet_type = str(payload_type_value)

                route_map = {
                    0x00: "F",  # TRANSPORT_FLOOD
                    0x01: "D",  # DIRECT
                    0x03: "T",  # TRANSPORT_DIRECT
                }
                route = route_map.get(route_type, "U")

                # Match meshcore-packet-capture hash logic from packet.cpp.
                if not hash_value:
                    has_transport = route_type in (0x00, 0x03)
                    offset = 1 + (4 if has_transport else 0)
                    path_len_value = packet_bytes[offset] if len(packet_bytes) > offset else 0
                    payload_start = offset + 1 + path_len_value
                    payload_data = packet_bytes[payload_start:] if len(packet_bytes) > payload_start else b""

                    hash_obj = hashlib.sha256()
                    hash_obj.update(bytes([payload_type_value]))
                    if payload_type_value == 9:  # TRACE
                        hash_obj.update(path_len_value.to_bytes(2, byteorder="little"))
                    hash_obj.update(payload_data)
                    hash_value = hash_obj.hexdigest()[:16].upper()
            except Exception:
                if not hash_value:
                    hash_value = "0000000000000000"

        if not packet_type and decrypted:
            packet_type = str(decrypted.get("payload_type", ""))
        if not packet_type and parsed:
            packet_type = str(parsed.get("payload_type", ""))
        if route == "U":
            route = str(payload.get("route") or "").strip().upper() or "F"

        packet = {
            "timestamp": now.isoformat(),
            "origin": self.node_name,
            "origin_id": self.public_key or "DEVICE",
            "type": "PACKET",
            "direction": "rx",
            "time": now.strftime("%H:%M:%S"),
            "date": f"{now.day}/{now.month}/{now.year}",
            "len": str(payload_len_total),
            "packet_type": packet_type,
            "route": route,
            "payload_len": str(payload_len),
            "raw": raw_hex,
            "SNR": str(payload.get("snr", "")),
            "RSSI": str(payload.get("rssi", "")),
            "score": str(payload.get("score", "1000")),
            "duration": str(payload.get("duration", "0")),
            "hash": hash_value,
        }

        path = str(parsed.get("path", "")).strip().upper() if parsed else ""
        if path and route == "D":
            packet["path"] = path

        return packet

    async def async_stop(self) -> None:
        """Stop all MQTT clients after publishing offline status."""
        if not self._clients:
            return
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            was_connected = bool(info.get("connected"))
            info["connected"] = False
            self._notify_connection_state(broker.number, False)
            if was_connected:
                self._publish_status_for_client(client, broker, "offline")
            try:
                client.loop_stop()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass
        self._clients = []
