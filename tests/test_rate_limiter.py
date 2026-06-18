"""Unit tests for rate_limiter.TokenBucket.

TokenBucket is pure Python (no Home Assistant imports) but was untested. The
parent package is MagicMock-stubbed in conftest, so the real module is loaded
directly from its file (the pattern used by test_decrypt_channel_message.py).
"""
import importlib.util
import os
import time
from unittest.mock import AsyncMock

import pytest

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "custom_components", "meshcore", "rate_limiter.py",
)
_spec = importlib.util.spec_from_file_location("meshcore_rate_limiter", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
TokenBucket = _mod.TokenBucket


def test_starts_full():
    assert TokenBucket(5, 60).get_tokens() == 5


def test_try_consume_decrements():
    tb = TokenBucket(5, 60)
    assert tb.try_consume() is True
    assert tb.get_tokens() == 4


def test_try_consume_fails_when_empty():
    tb = TokenBucket(1, 60)
    assert tb.try_consume() is True
    assert tb.try_consume() is False


def test_try_consume_multiple():
    tb = TokenBucket(5, 60)
    assert tb.try_consume(3) is True
    assert tb.get_tokens() == 2
    assert tb.try_consume(3) is False  # only 2 left


def test_refill_adds_tokens_over_elapsed_time():
    tb = TokenBucket(5, 60)
    tb.tokens = 0
    tb.last_refill = time.time() - 120  # two refill periods elapsed
    assert tb.get_tokens() == 2


def test_refill_caps_at_capacity():
    tb = TokenBucket(5, 60)
    tb.tokens = 0
    tb.last_refill = time.time() - 6000  # far more than capacity
    assert tb.get_tokens() == 5


async def test_consume_immediate_when_available():
    tb = TokenBucket(5, 60)
    _mod.asyncio.sleep = AsyncMock()
    await tb.consume(1)
    _mod.asyncio.sleep.assert_not_awaited()
    assert tb.get_tokens() == 4


async def test_consume_waits_when_insufficient():
    tb = TokenBucket(1, 60)
    assert tb.try_consume() is True  # drain to 0
    sleep_mock = AsyncMock()
    _mod.asyncio.sleep = sleep_mock
    await tb.consume(1)
    sleep_mock.assert_awaited_once()
    # one token short -> waits one refill period
    assert sleep_mock.await_args.args[0] == pytest.approx(60, abs=1)
