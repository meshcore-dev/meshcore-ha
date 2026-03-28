"""Map Auto Uploader for MeshCore integration - uploads repeater/room server adverts to map.meshcore.dev.

Matches working of recrof/map.meshcore.dev-uploader.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from cachetools import TTLCache
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MAP_UPLOAD_ENABLED, CONF_PUBKEY

try:
    import nacl.bindings
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

MAP_API_URL = "https://map.meshcore.dev/api/v1/uploader/node"
ADVERT_TYPE_CHAT = 0
REPLAY_COOLDOWN_SECONDS = 3600
_SEEN_ADVERTS_MAX_SIZE = 1000


def _extract_advert_payload_from_raw(raw_hex: str) -> bytes | None:
    """Extract ADVERT pkt_payload from raw LoRa packet hex.

    Uses same parsing as meshcore.js Packet.fromBytes: header, path_byte,
    path_hash_size/count, then payload. Fallback when pkt_payload from
    meshcore_py differs or is missing.
    """
    try:
        raw = bytes.fromhex(str(raw_hex or "").replace(" ", "").replace("\n", ""))
        if len(raw) < 2 + 32 + 4 + 64 + 1:
            return None
        header = raw[0]
        route_type = header & 0x03
        skip = 2
        if route_type in (0, 3):
            skip += 4
        if len(raw) <= skip:
            return None
        path_byte = raw[skip]
        path_hash_size = ((path_byte & 0xC0) >> 6) + 1
        path_hash_count = path_byte & 0x3F
        path_len = path_hash_count * path_hash_size
        payload_start = skip + 1 + path_len
        if len(raw) <= payload_start:
            return None
        return raw[payload_start:]
    except Exception:
        return None


def _verify_advert_signature(log_data: dict, logger=None) -> bool:
    """Verify Ed25519 signature of an ADVERT packet.

    Firmware signs: pub_key (32) + timestamp (4) + app_data (raw bytes).
    Matches meshcore.js Advert.isVerified() and MeshCore payloads.md.
    """
    if not HAS_NACL:
        return True

    def _verify(payload: bytes) -> bool:
        if len(payload) < 32 + 4 + 64 + 1:
            return False
        pubkey_bytes = payload[0:32]
        sig_bytes = payload[36:100]
        msg = payload[0:36] + payload[100:]
        try:
            signed = sig_bytes + msg
            nacl.bindings.crypto_sign_open(signed, pubkey_bytes)
            return True
        except Exception as ex:
            if logger:
                logger.debug(
                    "Map Auto Uploader: verify failed pkt_len=%d: %s",
                    len(payload),
                    ex,
                )
            return False

    pkt_payload = log_data.get("pkt_payload")
    if pkt_payload:
        if isinstance(pkt_payload, str):
            pkt_payload = bytes.fromhex(pkt_payload)
        if _verify(pkt_payload):
            return True

    raw_hex = log_data.get("payload") or log_data.get("raw_hex")
    if isinstance(raw_hex, bytes):
        raw_hex = raw_hex.hex()
    if raw_hex:
        extracted = _extract_advert_payload_from_raw(str(raw_hex or ""))
        if extracted and _verify(extracted):
            return True

    return False


class MeshCoreMapUploader:
    """Upload repeater/room server adverts to map.meshcore.dev."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger,
        entry: ConfigEntry,
        api=None,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.entry = entry
        self.api = api
        self.enabled = bool(entry.data.get(CONF_MAP_UPLOAD_ENABLED, False))
        self.public_key = (entry.data.get(CONF_PUBKEY, "") or "").lower()
        self.private_key = ""
        self._seen_adverts: TTLCache = TTLCache(
            maxsize=_SEEN_ADVERTS_MAX_SIZE,
            ttl=REPLAY_COOLDOWN_SECONDS,
        )
        self._self_info: dict[str, Any] = {}
        self._upload_lock = asyncio.Lock()

    async def _ensure_private_key(self) -> bool:
        """Fetch private key from device if not yet available."""
        if self.private_key:
            return True
        if not self.api:
            return False
        try:
            mesh_core = self.api.mesh_core
            result = await mesh_core.commands.export_private_key()
        except Exception as ex:
            self.logger.debug("Private key export failed: %s", ex)
            return False
        from meshcore.events import EventType
        if not result or getattr(result, "type", None) != EventType.PRIVATE_KEY:
            return False
        payload = getattr(result, "payload", {}) or {}
        pk = payload.get("private_key")
        if isinstance(pk, (bytes, bytearray)):
            pk = pk.hex()
        pk = str(pk or "").strip()
        if len(pk) == 128 and all(c in "0123456789abcdefABCDEF" for c in pk):
            self.private_key = pk
            self.logger.info("Map Auto Uploader: private key fetched from device")
            return True
        return False

    @staticmethod
    def _ed25519_sign_supercop(message: bytes, scalar: bytes, prefix: bytes, pubkey: bytes) -> bytes:
        """Sign using MeshCore/supercop expanded Ed25519 format (not libsodium).

        Device exports 64-byte key: [scalar 32][prefix 32]. Libsodium expects
        [seed 32][pub 32] and produces incompatible signatures.
        """
        group_order = 2**252 + 27742317777372353535851937790883648493

        def _to_int_le(b: bytes) -> int:
            return int.from_bytes(b, byteorder="little")

        def _to_bytes_le(val: int, length: int) -> bytes:
            return val.to_bytes(length, byteorder="little")

        h_r = hashlib.sha512(prefix + message).digest()
        r = _to_int_le(h_r) % group_order
        r_bytes = _to_bytes_le(r, 32)
        r_point = nacl.bindings.crypto_scalarmult_ed25519_base_noclamp(r_bytes)

        h_k = hashlib.sha512(r_point + pubkey + message).digest()
        k = _to_int_le(h_k) % group_order
        scalar_int = _to_int_le(scalar)
        s = (r + k * scalar_int) % group_order
        s_bytes = _to_bytes_le(s, 32)
        return r_point + s_bytes

    def _sign_upload_data(self, data: dict) -> dict | None:
        if not self.private_key or len(self.private_key) != 128:
            return None
        if not HAS_NACL:
            self.logger.warning("Map Auto Uploader: PyNaCl required for signing")
            return None
        pubkey_hex = (self.entry.data.get(CONF_PUBKEY, "") or "").strip().lower()
        if len(pubkey_hex) != 64:
            return None
        try:
            json_str = json.dumps(data, separators=(",", ":"))
            data_hash = hashlib.sha256(json_str.encode("utf-8")).digest()
            private_bytes = bytes.fromhex(self.private_key)
            scalar = private_bytes[:32]
            prefix = private_bytes[32:64]
            pubkey_bytes = bytes.fromhex(pubkey_hex)
            sig_bytes = self._ed25519_sign_supercop(
                data_hash, scalar, prefix, pubkey_bytes
            )
            signature_hex = sig_bytes.hex()
            return {
                "data": json_str,
                "signature": signature_hex,
                "publicKey": pubkey_hex,
            }
        except Exception as ex:
            self.logger.error("Map Auto Uploader sign failed: %s", ex)
            return None

    def _norm_param(self, val: float) -> int | float:
        """Normalize param for JSON: use int when whole number (matches JS JSON.stringify)."""
        return int(val) if val == int(val) else val

    async def _do_upload(
        self,
        raw_hex: str,
        params: dict,
        node_name: str = "",
        pubkey_prefix: str = "",
    ) -> bool:
        freq = self._norm_param(params.get("freq", 0))
        bw = self._norm_param(params.get("bw", 0))
        data = {
            "params": {
                "freq": freq,
                "cr": int(params.get("cr", 5)),
                "sf": int(params.get("sf", 7)),
                "bw": bw,
            },
            "links": [f"meshcore://{raw_hex}"],
        }
        signed = self._sign_upload_data(data)
        if not signed:
            return False
        try:
            import urllib.request
            import urllib.error

            req = urllib.request.Request(
                MAP_API_URL,
                data=json.dumps(signed).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            def _do_post():
                return urllib.request.urlopen(req, timeout=15)

            async with self._upload_lock:
                resp = await self.hass.async_add_executor_job(_do_post)
            result = json.loads(resp.read().decode())
            self.logger.info("Map Auto Uploader: uploaded node, response: %s", result)
            return True
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            err_msg = body or str(ex)
            if "ERR_PARAMS_INVALID" in err_msg or "Params" in err_msg:
                self.logger.warning(
                    "Map Auto Uploader: params rejected (freq=%s, bw=%s, sf=%s, cr=%s) - %s",
                    data["params"]["freq"],
                    data["params"]["bw"],
                    data["params"]["sf"],
                    data["params"]["cr"],
                    err_msg,
                )
            else:
                self.logger.warning(
                    "Map Auto Uploader: upload failed: HTTP %s - %s",
                    ex.code,
                    err_msg,
                )
            return False
        except Exception as ex:
            self.logger.warning("Map Auto Uploader: upload failed: %s", ex)
            return False

    async def async_handle_rx_log(self, event_type: str, payload: Any) -> None:
        """Handle RX_LOG_DATA event - filter ADVERT, verify, and upload."""
        if not self.enabled or not isinstance(payload, dict):
            return
        payload_type = payload.get("payload_type")
        if payload_type != 4:
            return
        adv_type = payload.get("adv_type", 0)
        if adv_type == ADVERT_TYPE_CHAT:
            return
        adv_key = payload.get("adv_key")
        adv_timestamp = payload.get("adv_timestamp")
        raw_pkt = payload.get("payload")
        if not raw_pkt and payload.get("raw_hex"):
            raw_pkt = payload["raw_hex"][4:] if len(payload["raw_hex"]) > 4 else payload["raw_hex"]
        if isinstance(raw_pkt, bytes):
            raw_pkt = raw_pkt.hex()
        raw_hex = str(raw_pkt or "").strip()
        if not adv_key or adv_timestamp is None or not raw_hex:
            return
        if not _verify_advert_signature(payload, self.logger):
            self.logger.debug("Map Auto Uploader: signature verification failed for %s", adv_key[:12])
            return
        prev_ts = self._seen_adverts.get(adv_key)
        if prev_ts is not None:
            if adv_timestamp <= prev_ts:
                self.logger.debug("Map Auto Uploader: ignoring possible replay for %s", adv_key[:12])
                return
            if adv_timestamp < prev_ts + REPLAY_COOLDOWN_SECONDS:
                self.logger.debug("Map Auto Uploader: too soon to reupload %s", adv_key[:12])
                return
        if not await self._ensure_private_key():
            self.logger.warning("Map Auto Uploader: cannot sign (private key export disabled?)")
            return
        params = {
            "freq": self._self_info.get("radio_freq", 0),
            "cr": self._self_info.get("radio_cr", 5),
            "sf": self._self_info.get("radio_sf", 7),
            "bw": self._self_info.get("radio_bw", 0),
        }
        if params["freq"] == 0:
            self.logger.debug("Map Auto Uploader: no radio params yet, skipping")
            return
        node_name = payload.get("adv_name", "?") or "?"
        pubkey_prefix = adv_key[:12]
        self.logger.info("Map Auto Uploader: uploading %s (%s)", node_name, pubkey_prefix)
        ok = await self._do_upload(raw_hex, params, node_name, pubkey_prefix)
        if ok:
            self._seen_adverts[adv_key] = adv_timestamp

    def update_self_info(self, payload: dict) -> None:
        """Update cached SELF_INFO radio params from device events."""
        if not isinstance(payload, dict):
            return
        for key in ("radio_freq", "radio_bw", "radio_sf", "radio_cr"):
            if key in payload and payload[key] is not None:
                self._self_info[key] = payload[key]
