"""Tests for execute_command parsing helpers in services.py."""
import importlib.util
import os
import pytest
from unittest.mock import MagicMock

# Load services.py directly to avoid triggering the package __init__ chain
# (which pulls in homeassistant, cachetools, meshcore, etc.).
# Set __package__ so relative imports (.const, .coordinator, etc.) resolve
# against the already-mocked sys.modules entries in conftest.py.
_SERVICES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "services.py",
)
_spec = importlib.util.spec_from_file_location(
    "custom_components.meshcore.services", _SERVICES_PATH
)
_module = importlib.util.module_from_spec(_spec)
_module.__package__ = "custom_components.meshcore"
_spec.loader.exec_module(_module)

_parse_functional_command = _module._parse_functional_command
_resolve_contact = _module._resolve_contact


class TestParseFunctionalCommand:
    """Tests for _parse_functional_command."""

    # --- Detection ---

    def test_no_parens_returns_none(self):
        assert _parse_functional_command("send_advert") is None

    def test_space_separated_returns_none(self):
        assert _parse_functional_command("send_advert True") is None

    def test_partial_paren_returns_none(self):
        assert _parse_functional_command("send_advert(") is None

    # --- No-arg calls ---

    def test_empty_parens(self):
        result = _parse_functional_command("reboot()")
        assert result == ("reboot", [], {})

    def test_empty_parens_with_whitespace(self):
        result = _parse_functional_command("  reboot(  )  ")
        assert result == ("reboot", [], {})

    # --- Positional args ---

    def test_bool_true(self):
        name, args, kwargs = _parse_functional_command("send_advert(True)")
        assert name == "send_advert"
        assert args == [True]
        assert kwargs == {}

    def test_bool_false(self):
        _, args, _ = _parse_functional_command("send_advert(False)")
        assert args == [False]

    def test_int_arg(self):
        _, args, _ = _parse_functional_command("set_tx_power(20)")
        assert args == [20]
        assert isinstance(args[0], int)

    def test_float_args(self):
        _, args, _ = _parse_functional_command("set_coords(37.7749, -122.4194)")
        assert args == [37.7749, -122.4194]

    def test_string_arg(self):
        _, args, _ = _parse_functional_command('set_name("mynode")')
        assert args == ["mynode"]

    def test_bytes_arg(self):
        _, args, _ = _parse_functional_command('set_channel(0, "test", b"\\x00\\x01")')
        assert args[2] == b"\x00\x01"

    def test_multiple_positional(self):
        _, args, _ = _parse_functional_command("set_radio(915.0, 125.0, 9, 5)")
        assert args == [915.0, 125.0, 9, 5]

    # --- Keyword args ---

    def test_kwarg_bool(self):
        name, args, kwargs = _parse_functional_command("send_advert(flood=False)")
        assert name == "send_advert"
        assert args == []
        assert kwargs == {"flood": False}

    def test_kwarg_int(self):
        _, _, kwargs = _parse_functional_command("get_contacts(lastmod=100)")
        assert kwargs == {"lastmod": 100}

    def test_kwarg_string(self):
        _, _, kwargs = _parse_functional_command('set_name(name="hello")')
        assert kwargs == {"name": "hello"}

    # --- Mixed positional + keyword ---

    def test_mixed_pos_and_kwarg(self):
        _, args, kwargs = _parse_functional_command('send_msg("abc123", "hello", timestamp=0)')
        assert args == ["abc123", "hello"]
        assert kwargs == {"timestamp": 0}

    # --- Invalid / unsafe inputs ---

    def test_non_literal_expression_returns_none(self):
        """Expressions that aren't literals should fail safely."""
        assert _parse_functional_command("cmd(1 + 2)") is None

    def test_code_injection_returns_none(self):
        """Arbitrary code must not be evaluated."""
        assert _parse_functional_command("cmd(__import__('os').system('echo x'))") is None

    def test_nested_function_call_returns_none(self):
        assert _parse_functional_command("cmd(int('5'))") is None


class TestResolveContact:
    """Tests for _resolve_contact."""

    def _make_api(self, by_prefix=None, by_name=None):
        api = MagicMock()
        api.mesh_core.get_contact_by_key_prefix.return_value = by_prefix
        api.mesh_core.get_contact_by_name.return_value = by_name
        return api

    def _make_coordinator(self, discovered=None):
        coord = MagicMock()
        coord._discovered_contacts = discovered or {}
        return coord

    def test_found_by_prefix(self):
        contact = {"adv_name": "node1", "public_key": "abcdef123456"}
        api = self._make_api(by_prefix=contact)
        result = _resolve_contact("abcdef", "reset_path", api, self._make_coordinator())
        assert result == contact

    def test_fallback_to_name(self):
        contact = {"adv_name": "node1", "public_key": "abcdef123456"}
        api = self._make_api(by_prefix=None, by_name=contact)
        result = _resolve_contact("node1_", "reset_path", api, self._make_coordinator())
        assert result == contact

    def test_prefix_too_short_returns_none(self):
        api = self._make_api()
        result = _resolve_contact("abc", "reset_path", api, self._make_coordinator())
        assert result is None

    def test_not_found_returns_none(self):
        api = self._make_api(by_prefix=None, by_name=None)
        result = _resolve_contact("abcdef", "reset_path", api, self._make_coordinator())
        assert result is None

    def test_add_contact_checks_discovered(self):
        discovered = {
            "key1": {"adv_name": "remote", "public_key": "abcdef789012"},
        }
        api = self._make_api(by_prefix=None, by_name=None)
        coord = self._make_coordinator(discovered=discovered)
        result = _resolve_contact("abcdef", "add_contact", api, coord)
        assert result == discovered["key1"]

    def test_add_contact_by_name_in_discovered(self):
        discovered = {
            "key1": {"adv_name": "remote", "public_key": "abcdef789012"},
        }
        api = self._make_api(by_prefix=None, by_name=None)
        coord = self._make_coordinator(discovered=discovered)
        result = _resolve_contact("remote", "add_contact", api, coord)
        assert result == discovered["key1"]

    def test_non_add_contact_skips_discovered(self):
        discovered = {
            "key1": {"adv_name": "remote", "public_key": "abcdef789012"},
        }
        api = self._make_api(by_prefix=None, by_name=None)
        coord = self._make_coordinator(discovered=discovered)
        result = _resolve_contact("abcdef", "reset_path", api, coord)
        assert result is None
