"""Unit tests for utils.extract_pubkey_hex (string/dict-shaped public_key).

Regression coverage for the `_get_node_info` crash: coordinator contacts
carry ``public_key`` as a plain hex *string*, but both
``TelemetrySensorManager._get_node_info`` and
``DeviceTrackerManager._get_node_info`` did
``contact.get("public_key", {}).get("hex", "")`` — which raises
``AttributeError: 'str' object has no attribute 'get'`` on a string-shaped
key and kills the telemetry/GPS event handler before sensor creation. Both
call sites now route through ``extract_pubkey_hex``, which must return a hex
string for both the current string shape and the legacy ``{"hex": ...}`` dict.

utils.py is loaded directly from file via importlib (mirroring
test_flood_scope.py / test_decrypt_channel_message.py) so the real
implementation is exercised rather than the conftest MagicMock stub.
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

# Stub HA and integration modules that utils.py imports but aren't installed.
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

# Provide slugify as an attribute so `from homeassistant.util import slugify` works.
sys.modules["homeassistant.util"].slugify = lambda x: x

_MESHCORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore",
)
_UTILS_PATH = os.path.join(_MESHCORE_DIR, "utils.py")
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.utils", _UTILS_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)

extract_pubkey_hex = _module.extract_pubkey_hex


# A realistic 32-char hex public key as coordinator contacts carry it.
_HEX = "fe3af51b24b9c0d1e2f3a4b5c6d7e8f9"


def test_string_pubkey_returned_as_is():
    """The current coordinator shape: public_key is a plain hex string."""
    assert extract_pubkey_hex({"public_key": _HEX}) == _HEX


def test_string_pubkey_does_not_raise_on_startswith():
    """Regression: the _get_node_info loop does contact_pubkey.startswith(...).

    Before the fix a string-shaped public_key raised AttributeError inside the
    extractor (``'str'.get``). The result must be a real string usable by
    ``str.startswith``, not a crash.
    """
    contact_pubkey = extract_pubkey_hex({"public_key": _HEX, "name": "MattDub"})
    assert isinstance(contact_pubkey, str)
    assert contact_pubkey.startswith(_HEX[:12])


def test_legacy_dict_pubkey_extracted():
    """Legacy shape compatibility: {"hex": ...} still yields the hex string."""
    assert extract_pubkey_hex({"public_key": {"hex": _HEX}}) == _HEX


@pytest.mark.parametrize("contact", [
    {},                              # no public_key key at all
    {"public_key": ""},              # empty string
    {"public_key": None},            # explicit None
    {"public_key": {}},              # empty dict (legacy shape, no hex)
    {"public_key": {"other": "x"}},  # dict without a hex key
])
def test_missing_or_empty_pubkey_returns_empty_string(contact):
    """Every absent/empty shape collapses to '' (never None, never a raise)."""
    assert extract_pubkey_hex(contact) == ""


def test_node_info_call_sites_use_helper_not_inline_dict_access():
    """Both _get_node_info copies must route through the helper.

    Guards against re-introducing the inline
    ``.get("public_key", {}).get("hex")`` that crashed on string-shaped keys
    (the original two-file bug).
    """
    for fname in ("telemetry_sensor.py", "device_tracker.py"):
        with open(os.path.join(_MESHCORE_DIR, fname), encoding="utf-8") as fh:
            src = fh.read()
        assert 'get("public_key", {}).get("hex"' not in src, (
            f"{fname} still uses the crash-prone inline dict access"
        )
        assert "extract_pubkey_hex(contact)" in src, (
            f"{fname} does not route _get_node_info through extract_pubkey_hex"
        )
