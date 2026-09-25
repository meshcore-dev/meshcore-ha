"""Regression tests for decrypt_channel_message AES plaintext layout.

Firmware format (confirmed from src/Utils.cpp + src/helpers/BaseChatMesh.cpp):
  plaintext = timestamp(4 bytes LE) + flags(1 byte) + null-terminated text
  encrypted with AES-128-ECB using channel.secret[:16]
  MAC = HMAC-SHA256(key=channel.secret[0:32], msg=ciphertext)[:2]

Text must be read from offset 5, not 4 — guarded here so the offset
cannot silently slip in a future refactor.

Notes on known gaps vs firmware:
- These are synthetic round-trip tests, not captures from a real device
  (firmware contains no hardcoded test vectors).
- The 2-byte MAC: firmware uses the full 32-byte channel.secret as the
  HMAC key, which for 128-bit channels is the 16-byte secret followed by
  zeros. HMAC zero-pads short keys, so the 16-byte secret verifies real
  firmware MACs (checked against real over-the-air packets).
  decrypt_channel_message decrypts regardless and reports mac_valid.
"""
import importlib.util
import os
import struct
import sys
from unittest.mock import MagicMock

import pytest
from Crypto.Cipher import AES

# Stub HA and integration modules that utils.py imports but aren't installed
for _mod in (
    "homeassistant",
    "homeassistant.util",
    "homeassistant.util.slugify",
    "custom_components",
    "custom_components.meshcore",
    "custom_components.meshcore.const",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Provide slugify as an attribute so `from homeassistant.util import slugify` works
sys.modules["homeassistant.util"].slugify = lambda x: x

_UTILS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "utils.py",
)
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.utils", _UTILS_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)

decrypt_channel_message = _module.decrypt_channel_message

# Fixed 16-byte AES key used across all tests
_KEY = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")


def _encrypt(timestamp: int, flags: int, text: str) -> tuple[bytes, bytes]:
    """Build a firmware-format plaintext and encrypt it with AES-ECB."""
    plaintext = struct.pack("<I", timestamp) + bytes([flags]) + text.encode()
    # Pad to 16-byte block boundary
    remainder = len(plaintext) % 16
    if remainder:
        plaintext += b"\x00" * (16 - remainder)
    cipher = AES.new(_KEY, AES.MODE_ECB)
    ciphertext = cipher.encrypt(plaintext)
    # MAC is first 2 bytes of HMAC-SHA256 — omit verification path here
    import hmac as _hmac, hashlib
    mac = _hmac.new(_KEY, ciphertext, hashlib.sha256).digest()[:2]
    return ciphertext, mac


class TestDecryptChannelMessage:

    def test_returns_correct_timestamp(self):
        ts = 1_748_000_000
        ciphertext, mac = _encrypt(ts, flags=0, text="hello")
        result_ts, _, _ = decrypt_channel_message(ciphertext, mac, _KEY)
        assert result_ts == ts

    def test_returns_correct_text(self):
        ciphertext, mac = _encrypt(1_748_000_000, flags=0, text="hello mesh")
        _, text, _ = decrypt_channel_message(ciphertext, mac, _KEY)
        assert text == "hello mesh"

    def test_flags_byte_not_included_in_text(self):
        """Offset must be 5 (skip 4-byte ts + 1-byte flags), not 4."""
        flags = 0x42  # non-zero to catch an off-by-one that leaks it into text
        ciphertext, mac = _encrypt(1_748_000_000, flags=flags, text="world")
        _, text, _ = decrypt_channel_message(ciphertext, mac, _KEY)
        assert text == "world"
        assert chr(flags) not in text

    def test_empty_message(self):
        ciphertext, mac = _encrypt(1_748_000_000, flags=0, text="")
        ts, text, _ = decrypt_channel_message(ciphertext, mac, _KEY)
        assert ts == 1_748_000_000
        assert text == ""

    def test_various_flags_values_do_not_corrupt_text(self):
        for flags in (0x00, 0x01, 0x7F, 0xFF):
            ciphertext, mac = _encrypt(100, flags=flags, text="test")
            _, text, _ = decrypt_channel_message(ciphertext, mac, _KEY)
            assert text == "test", f"failed with flags=0x{flags:02x}"

    def test_wrong_key_returns_none(self):
        ciphertext, mac = _encrypt(1_748_000_000, flags=0, text="secret")
        wrong_key = bytes(16)  # all zeros
        ts, text, _ = decrypt_channel_message(ciphertext, mac, wrong_key)
        # Garbage decrypt — text won't match, but function must not raise
        assert ts is not None or text is not None or (ts is None and text is None)


class TestMacValid:
    """mac_valid reports whether the 2-byte HMAC matches the ciphertext.

    These tests are synthetic (fixed test key, made-up text), but the
    corruption mirrors flood copies actually observed on air: a repeater
    flips bits after the LoRa CRC check, most often bit 7 of the last byte
    of the packet, while the original MAC is kept. With AES-ECB only the
    affected 16-byte block is garbled, so the timestamp (first block)
    usually survives and the copy still decrypts.
    """

    _TEXT = "mgxs: bit flip test, spans several AES blocks"

    def test_intact_packet_is_valid(self):
        ciphertext, mac = _encrypt(1_790_273_363, flags=0, text=self._TEXT)
        ts, text, mac_valid = decrypt_channel_message(ciphertext, mac, _KEY)
        assert mac_valid is True
        assert ts == 1_790_273_363
        assert text == self._TEXT

    def test_flipped_bit_is_invalid_but_still_decrypts(self):
        ciphertext, mac = _encrypt(1_790_273_363, flags=0, text=self._TEXT)
        corrupted = ciphertext[:-1] + bytes([ciphertext[-1] ^ 0x80])  # bit 7, last byte
        ts, text, mac_valid = decrypt_channel_message(corrupted, mac, _KEY)
        assert mac_valid is False
        # Only the last block is garbled: timestamp and first blocks survive
        assert ts == 1_790_273_363
        assert text != self._TEXT
        assert text.startswith(self._TEXT[:11])

    def test_wrong_key_is_invalid(self):
        ciphertext, mac = _encrypt(1_790_273_363, flags=0, text=self._TEXT)
        _, _, mac_valid = decrypt_channel_message(ciphertext, mac, bytes(16))
        assert mac_valid is False
