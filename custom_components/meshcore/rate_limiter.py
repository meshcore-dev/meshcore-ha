"""Token bucket rate limiter for MeshCore requests."""
from __future__ import annotations

import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)


class TokenBucket:
    """Token bucket rate limiter for controlling mesh request frequency.

    Allows bursts up to capacity, then enforces average rate over time.
    """

    def __init__(self, capacity: int, refill_rate_seconds: float):
        """Initialize token bucket.

        Args:
            capacity: Maximum number of tokens (burst tolerance)
            refill_rate_seconds: Seconds to add one token
        """
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate_seconds
        self.last_refill = time.time()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time since last refill."""
        now = time.time()
        elapsed = now - self.last_refill
        tokens_to_add = int(elapsed / self.refill_rate)
        if tokens_to_add > 0:
            self.tokens = min(self.capacity, self.tokens + tokens_to_add)
            self.last_refill = now

    def get_tokens(self) -> int:
        """Get current token count (with refill applied).

        Returns:
            Current number of tokens available
        """
        self._refill()
        return self.tokens

    def try_consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens without waiting.

        Args:
            tokens: Number of tokens to consume (default 1)

        Returns:
            True if tokens were consumed, False if not enough tokens available
        """
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True

        return False

    async def consume(self, tokens: int = 1) -> None:
        """Consume tokens, waiting if necessary.

        If enough tokens are available, consumes immediately.
        Otherwise, waits until enough tokens have been refilled.

        Args:
            tokens: Number of tokens to consume (default 1)
        """
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return

        # Not enough tokens - calculate wait time
        tokens_needed = tokens - self.tokens
        wait_time = tokens_needed * self.refill_rate
        _LOGGER.debug(
            f"Rate limiter: need {tokens} tokens but only have {self.tokens}, "
            f"waiting {wait_time:.1f}s"
        )
        await asyncio.sleep(wait_time)

        # After waiting, refill and consume
        self._refill()
        self.tokens -= tokens
