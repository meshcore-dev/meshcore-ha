"""MQTT uploader for MeshCore integration events."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shlex
import ssl
import subprocess
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MQTT_BROKERS,
    CONF_MQTT_DECODER_CMD,
    CONF_MQTT_IATA,
    CONF_MQTT_PRIVATE_KEY,
    CONF_MQTT_TOKEN_TTL_SECONDS,
    CONF_NAME,
    CONF_PUBKEY,
)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

try:
    import nacl.bindings
except ImportError:
    nacl = None

try:
    from meshcore.events import EventType
except ImportError:
    EventType = None


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
    retain: bool
    username: str
    password: str
    use_auth_token: bool
    token_audience: str
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

    def __init__(self, hass: HomeAssistant, logger, entry: ConfigEntry, api=None) -> None:
        self.hass = hass
        self.logger = logger
        self.entry = entry
        self.api = api
        self.settings = entry.data.get(CONF_MQTT_BROKERS, {}) or {}
        self.node_name = str(entry.data.get(CONF_NAME, "meshcore") or "meshcore").strip()
        self.public_key = (entry.data.get(CONF_PUBKEY, "") or "").upper()
        self.global_iata = (
            str(entry.data.get(CONF_MQTT_IATA) or os.getenv("MESHCORE_HA_MQTT_IATA", "LOC"))
            .strip()
            .upper()
        )
        self.decoder_cmd = str(
            entry.data.get(CONF_MQTT_DECODER_CMD) or os.getenv("MESHCORE_HA_DECODER_CMD", "meshcore-decoder")
        ).strip()
        self.private_key = (
            str(entry.data.get(CONF_MQTT_PRIVATE_KEY) or os.getenv("MESHCORE_HA_PRIVATE_KEY", "")).strip()
        )
        self.token_ttl_seconds = _as_int(
            entry.data.get(CONF_MQTT_TOKEN_TTL_SECONDS) or os.getenv("MESHCORE_HA_TOKEN_TTL_SECONDS"),
            3600,
        )
        self._brokers = self._load_brokers()
        self._clients: list[dict[str, Any]] = []
        self._tokens: dict[int, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return mqtt is not None and len(self._brokers) > 0

    def _load_brokers(self) -> list[BrokerConfig]:
        """Load broker configs from environment variables."""
        brokers: list[BrokerConfig] = []
        for idx in range(1, 5):
            broker_settings = self.settings.get(str(idx), {}) if isinstance(self.settings, dict) else {}
            prefix = f"MESHCORE_HA_MQTT{idx}_"
            enabled = _as_bool(
                broker_settings.get("enabled", os.getenv(f"{prefix}ENABLED")),
                False,
            )
            server = str(broker_settings.get("server", os.getenv(f"{prefix}SERVER", "")) or "").strip()
            if not enabled or not server:
                continue

            iata = (
                str(broker_settings.get("iata", os.getenv(f"{prefix}IATA", "")) or "")
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
                port=_as_int(broker_settings.get("port", os.getenv(f"{prefix}PORT")), 1883),
                transport=str(
                    broker_settings.get("transport", os.getenv(f"{prefix}TRANSPORT", "tcp")) or "tcp"
                ).strip().lower(),
                use_tls=_as_bool(broker_settings.get("use_tls", os.getenv(f"{prefix}USE_TLS")), False),
                tls_verify=_as_bool(
                    broker_settings.get("tls_verify", os.getenv(f"{prefix}TLS_VERIFY")),
                    True,
                ),
                keepalive=_as_int(broker_settings.get("keepalive", os.getenv(f"{prefix}KEEPALIVE")), 60),
                qos=_as_int(broker_settings.get("qos", os.getenv(f"{prefix}QOS")), 0),
                retain=_as_bool(broker_settings.get("retain", os.getenv(f"{prefix}RETAIN")), True),
                username=str(
                    broker_settings.get("username", os.getenv(f"{prefix}USERNAME", "")) or ""
                ).strip(),
                password=str(
                    broker_settings.get("password", os.getenv(f"{prefix}PASSWORD", "")) or ""
                ).strip(),
                use_auth_token=_as_bool(
                    broker_settings.get("use_auth_token", os.getenv(f"{prefix}USE_AUTH_TOKEN")),
                    False,
                ),
                token_audience=str(
                    broker_settings.get("token_audience", os.getenv(f"{prefix}TOKEN_AUDIENCE", "")) or ""
                ).strip(),
                client_id_prefix=str(
                    broker_settings.get("client_id_prefix", os.getenv(f"{prefix}CLIENT_ID_PREFIX", "meshcore_")) or "meshcore_"
                ).strip(),
                topic_status=self._resolve_topic(
                    str(
                        broker_settings.get(
                            "topic_status",
                            os.getenv(f"{prefix}TOPIC_STATUS", "meshcore/{IATA}/{PUBLIC_KEY}/status"),
                        )
                    ),
                    iata,
                ),
                topic_packets=self._resolve_topic(
                    str(
                        broker_settings.get(
                            "topic_events",
                            os.getenv(f"{prefix}TOPIC_EVENTS", "meshcore/{IATA}/{PUBLIC_KEY}/packets"),
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
        if mqtt is None:
            self.logger.warning("MQTT uploader disabled: paho-mqtt is not installed")
            return
        if not self._brokers:
            return

        for broker in self._brokers:
            client = await self._async_create_client(broker)
            if client is None:
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
        client_id = self._sanitize_client_id(self.node_name or self.public_key or self.entry.entry_id, broker.client_id_prefix)
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
            retain=broker.retain,
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
                    self.logger.info("[%s] Connected", broker.name)
                    self._publish_status_for_client(client, broker, "online")
                else:
                    self.logger.error("[%s] Connect failed: %s", broker.name, reason_code)
                return

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """MQTT disconnect callback."""
        broker_num = userdata.get("broker_num")
        rc = self._reason_code_to_int(reason_code)
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                info["connected"] = False
                if rc != 0:
                    self.logger.warning("[%s] Disconnected: %s", broker.name, reason_code)
                return

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

    async def _async_get_token(self, broker: BrokerConfig) -> str | None:
        """Create or reuse cached JWT token using meshcore-decoder CLI."""
        if not self.public_key:
            self.logger.error("[%s] Missing device public key; cannot generate auth token", broker.name)
            return None
        if not self.private_key:
            fetched_private_key = await self._async_fetch_private_key_from_device(broker)
            if fetched_private_key:
                self.private_key = fetched_private_key
            else:
                self.logger.error(
                    "[%s] Missing private key (configure in MQTT global settings or enable device private key export)",
                    broker.name,
                )
                return None

        cached = self._tokens.get(broker.number)
        now = time.time()
        if cached and (now < (cached["expires_at"] - 60)):
            return cached["token"]

        claims = {}
        if broker.token_audience:
            claims["aud"] = broker.token_audience

        args = shlex.split(self.decoder_cmd)
        args.extend(
            [
                "auth-token",
                self.public_key,
                self.private_key,
                "--exp",
                str(self.token_ttl_seconds),
            ]
        )
        if claims:
            args.extend(["--claims", json.dumps(claims)])

        token = await self.hass.async_add_executor_job(self._run_decoder_command, args)
        if not token:
            token = await self.hass.async_add_executor_job(
                self._create_auth_token_python,
                broker.token_audience,
            )
            if token:
                self.logger.info("[%s] Created auth token using Python fallback signer", broker.name)
        if not token:
            return None

        self._tokens[broker.number] = {
            "token": token,
            "expires_at": now + self.token_ttl_seconds,
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

        if EventType is not None and getattr(result, "type", None) == EventType.PRIVATE_KEY:
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

        if EventType is not None and getattr(result, "type", None) == EventType.DISABLED:
            self.logger.warning(
                "[%s] Private key export disabled on firmware (needs ENABLE_PRIVATE_KEY_EXPORT=1)",
                broker.name,
            )
            return None

        if EventType is not None and getattr(result, "type", None) == EventType.ERROR:
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
        if nacl is None:
            raise RuntimeError("PyNaCl is required for Python auth token signing")
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

    def _create_auth_token_python(self, audience: str | None = None) -> str | None:
        """Create MeshCore JWT token in-process (no external decoder binary)."""
        try:
            if nacl is None:
                self.logger.error("Python token signing unavailable: PyNaCl not installed")
                return None
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
                "exp": now + self.token_ttl_seconds,
            }
            if audience:
                payload["aud"] = audience

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
                retain=broker.retain,
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

    def publish_raw_event(self, event_type: str, payload: Any) -> None:
        """Publish one event as packet-like payload to all connected brokers."""
        if not self._clients:
            return
        packet_payload = json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "origin": self.node_name,
                "origin_id": self.public_key or "DEVICE",
                "event_type": event_type,
                "payload": payload,
                "source": "meshcore-ha",
            }
        )
        for info in self._clients:
            if not info.get("connected"):
                continue
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            if not broker.topic_packets:
                continue
            try:
                result = client.publish(
                    broker.topic_packets,
                    packet_payload,
                    qos=broker.qos,
                    retain=False,
                )
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    self.logger.error("[%s] Event publish failed: rc=%s", broker.name, result.rc)
                else:
                    self.logger.debug("[%s] Packet published topic=%s event=%s", broker.name, broker.topic_packets, event_type)
            except Exception as ex:
                self.logger.error("[%s] Event publish error: %s", broker.name, ex)

    async def async_stop(self) -> None:
        """Stop all MQTT clients after publishing offline status."""
        if not self._clients:
            return
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            if info.get("connected"):
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
