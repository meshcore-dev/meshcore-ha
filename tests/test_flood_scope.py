"""Unit tests for the flood-scope allowlist parser (load_flood_scope_keys).

The Flood Scope Allowlist is a comma-separated free-text field. Named
regions are hashed into a name->16-byte-key inbound matcher keyset
(SHA256("#name")[:16]); the '*' wildcard means "all regions / global
flood" and must be recognized as the global sentinel, never hashed as a
region. These tests pin both behaviors so the wildcard handling cannot
regress and the named-region derivation stays byte-stable.

utils.py is loaded directly from file via importlib (mirroring
test_decrypt_channel_message.py) so the real implementation is exercised
rather than the conftest MagicMock stub.
"""
import hashlib
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

load_flood_scope_keys = _module.load_flood_scope_keys
_normalize_flood_scope_name = _module._normalize_flood_scope_name
FLOOD_SCOPE_WILDCARD_TOKENS = _module.FLOOD_SCOPE_WILDCARD_TOKENS


def _expected_key(name: str) -> bytes:
    """The firmware/auto-key derivation for a normalized #region name."""
    return hashlib.sha256(name.encode()).digest()[:16]


def test_wildcard_plus_named_regions_yields_only_named_keys():
    """'*, pl-mz, pl-waw' -> exactly the two named-region keys, no '#*'."""
    keys = load_flood_scope_keys("*, pl-mz, pl-waw")
    assert set(keys) == {"#pl-mz", "#pl-waw"}
    assert "#*" not in keys
    assert "*" not in keys
    assert keys["#pl-mz"] == _expected_key("#pl-mz")
    assert keys["#pl-waw"] == _expected_key("#pl-waw")


@pytest.mark.parametrize("scopes_str", ["*", " * ", "0", "None", "#", "", "   "])
def test_wildcard_forms_alone_yield_empty_keyset(scopes_str):
    """Every wildcard/blank form on its own parses to an empty keyset."""
    assert load_flood_scope_keys(scopes_str) == {}


def test_lowercase_none_is_a_normal_region():
    """Lowercase 'none' is a valid region (only capital-N 'None' is the SDK sentinel)."""
    keys = load_flood_scope_keys("none")
    assert set(keys) == {"#none"}
    assert keys["#none"] == _expected_key("#none")


def test_hash_prefix_idempotent_named_region():
    """'#pl-mz' and 'pl-mz' derive the identical key (named handling unchanged)."""
    with_prefix = load_flood_scope_keys("#pl-mz")
    without_prefix = load_flood_scope_keys("pl-mz")
    assert set(with_prefix) == {"#pl-mz"}
    assert with_prefix == without_prefix
    assert with_prefix["#pl-mz"] == _expected_key("#pl-mz")


def test_named_region_keys_byte_identical_to_pre_fix_derivation():
    """Named-region handling is byte-identical to the pre-fix function.

    Guards that only the wildcard handling changed: on a named-only input
    (where the old guard and the new guard agree), the new parser must
    produce the exact same keyset the old parser would have.
    """
    def _old_load_flood_scope_keys(scopes_str):
        # Pre-fix implementation, reproduced for regression comparison.
        result = {}
        if not scopes_str:
            return result
        for entry in scopes_str.split(","):
            name = _normalize_flood_scope_name(entry)
            if name and name not in ("*", "#"):
                result[name] = hashlib.sha256(name.encode()).digest()[:16]
        return result

    named_only = "pl-mz, pl-waw, foo-bar, #already-hashed"
    assert load_flood_scope_keys(named_only) == _old_load_flood_scope_keys(named_only)


def test_whitespace_and_hash_permutations_parse_to_named_only():
    """Whitespace- and '#'-mixed wildcard inputs all reduce to named regions only."""
    for scopes_str in ("*,#", "#, *, pl-mz", "  *  ,  pl-mz  "):
        keys = load_flood_scope_keys(scopes_str)
        assert "#*" not in keys
        assert all(k != "*" and k != "#" for k in keys)
    # The last two contain pl-mz; the first contains no named region.
    assert load_flood_scope_keys("*,#") == {}
    assert set(load_flood_scope_keys("#, *, pl-mz")) == {"#pl-mz"}
    assert set(load_flood_scope_keys("  *  ,  pl-mz  ")) == {"#pl-mz"}


def test_wildcard_tokens_constant_shape():
    """The wildcard-token set mirrors the SDK disable set plus parser '#'."""
    assert set(FLOOD_SCOPE_WILDCARD_TOKENS) == {"*", "0", "None", "#", ""}
