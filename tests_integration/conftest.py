"""Fixtures for the integration test tier, which runs against a real Home
Assistant instance via pytest-homeassistant-custom-component.

This tier is deliberately separate from tests/ (the unit tier). tests/conftest.py
injects MagicMock stubs for homeassistant.* into sys.modules at import time, which
is incompatible with the real Home Assistant these tests need. The two tiers must
run as separate pytest invocations (see .github/workflows/tests.yml).

Tests request `enable_custom_integrations` explicitly (not autouse) so that, where
the recorder is needed, `recorder_mock` can be ordered before the `hass` fixture
(phcc requires recorder fixtures to resolve before Home Assistant starts).
"""
import logging
import os
import sys

# Ensure the repo root (which contains custom_components/) is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# The recorder (pulled in via the logbook dependency) emits very chatty
# SQL/engine logging during setup; keep test output readable.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
