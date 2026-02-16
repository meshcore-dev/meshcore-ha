"""MQTT uploader for MeshCore integration events."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import ssl
import subprocess
import time
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
    CONF_PUBKEY,
)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


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
    topic_status: str
    topic_events: str

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

    def __init__(self, hass: HomeAssistant, logger, entry: ConfigEntry) -> None:
        self.hass = hass
        self.logger = logger
        self.entry = entry
        self.settings = entry.data.get(CONF_MQTT_BROKERS, {}) or {}
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
                topic_status=self._resolve_topic(
                    str(
                        broker_settings.get(
                            "topic_status",
                            os.getenv(f"{prefix}TOPIC_STATUS", "meshcore/{IATA}/{PUBLIC_KEY}/status"),
                        )
                    ),
                    iata,
                ),
                topic_events=self._resolve_topic(
                    str(
                        broker_settings.get(
                            "topic_events",
                            os.getenv(f"{prefix}TOPIC_EVENTS", "meshcore/{IATA}/{PUBLIC_KEY}/events"),
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
                client.connect(broker.server, broker.port, keepalive=broker.keepalive)
                client.loop_start()
            except Exception as ex:
                self.logger.error("[%s] Connection error: %s", broker.name, ex)

    async def _async_create_client(self, broker: BrokerConfig):
        """Create an MQTT client for one broker."""
        client_id = f"meshcore_ha_{(self.public_key or self.entry.entry_id)[:16]}_{broker.number}"
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

        if broker.use_tls:
            if broker.tls_verify:
                client.tls_set()
                client.tls_insecure_set(False)
            else:
                client.tls_set(cert_reqs=ssl.CERT_NONE)
                client.tls_insecure_set(True)
                self.logger.warning("[%s] TLS verification disabled", broker.name)

        if broker.transport == "websockets":
            client.ws_set_options(path="/", headers=None)

        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """MQTT connect callback."""
        broker_num = userdata.get("broker_num")
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                if int(reason_code) == 0:
                    info["connected"] = True
                    self.logger.info("[%s] Connected", broker.name)
                    self._publish_status_for_client(client, broker, "online")
                else:
                    self.logger.error("[%s] Connect failed: %s", broker.name, reason_code)
                return

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """MQTT disconnect callback."""
        broker_num = userdata.get("broker_num")
        for info in self._clients:
            broker: BrokerConfig = info["broker"]
            if broker.number == broker_num:
                info["connected"] = False
                if int(reason_code) != 0:
                    self.logger.warning("[%s] Disconnected: %s", broker.name, reason_code)
                return

    async def _async_get_token(self, broker: BrokerConfig) -> str | None:
        """Create or reuse cached JWT token using meshcore-decoder CLI."""
        if not self.public_key:
            self.logger.error("[%s] Missing device public key; cannot generate auth token", broker.name)
            return None
        if not self.private_key:
            self.logger.error("[%s] Missing private key (MESHCORE_HA_PRIVATE_KEY)", broker.name)
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
            return None

        self._tokens[broker.number] = {
            "token": token,
            "expires_at": now + self.token_ttl_seconds,
        }
        return token

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

    def _publish_status_for_client(self, client, broker: BrokerConfig, state: str) -> None:
        """Publish status for one broker/client pair."""
        if not broker.topic_status:
            return
        payload = {
            "status": state,
            "timestamp": int(time.time()),
            "source": "meshcore-ha",
            "public_key": self.public_key,
        }
        try:
            result = client.publish(
                broker.topic_status,
                json.dumps(payload),
                qos=broker.qos,
                retain=broker.retain,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.logger.error("[%s] Status publish failed: rc=%s", broker.name, result.rc)
        except Exception as ex:
            self.logger.error("[%s] Status publish error: %s", broker.name, ex)

    def publish_raw_event(self, event_type: str, payload: Any) -> None:
        """Publish one raw event to all connected brokers."""
        if not self._clients:
            return
        event_payload = json.dumps(
            {
                "event_type": event_type,
                "payload": payload,
                "timestamp": time.time(),
                "source": "meshcore-ha",
                "public_key": self.public_key,
            }
        )
        for info in self._clients:
            if not info.get("connected"):
                continue
            broker: BrokerConfig = info["broker"]
            client = info["client"]
            if not broker.topic_events:
                continue
            try:
                result = client.publish(
                    broker.topic_events,
                    event_payload,
                    qos=broker.qos,
                    retain=False,
                )
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    self.logger.error("[%s] Event publish failed: rc=%s", broker.name, result.rc)
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
