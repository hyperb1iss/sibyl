"""Per-user recall concurrency limits."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sibyl_core.auth import OrganizationRole

DEFAULT_MAX_CONCURRENT_RECALLS_PER_USER = 3
DEFAULT_RECALL_QUEUE_TIMEOUT_SECONDS = 30.0
RECALL_MAX_CONCURRENT_ENV = "SIBYL_RECALL_MAX_CONCURRENT_PER_USER"
RECALL_QUEUE_TIMEOUT_ENV = "SIBYL_RECALL_QUEUE_TIMEOUT_SECONDS"


class RecallConcurrencyLimitExceededError(Exception):
    def __init__(self, *, user_id: str, max_concurrent: int, waited_seconds: float = 0.0) -> None:
        super().__init__("recall_concurrency_limit_exceeded")
        self.user_id = user_id
        self.max_concurrent = max_concurrent
        self.waited_seconds = waited_seconds


class RecallConcurrencyLimiter:
    """Bound a user's concurrent recalls, queueing the overflow.

    The bound is backpressure on the retrieval pools, not a budget: a burst
    from one agent that exceeds it waits its turn for a bounded time instead
    of failing on arrival, and only a wait that outlives the queue timeout
    is refused.
    """

    def __init__(
        self,
        *,
        default_max_concurrent: int = DEFAULT_MAX_CONCURRENT_RECALLS_PER_USER,
        queue_timeout_seconds: float | None = None,
    ):
        self._default_max_concurrent = default_max_concurrent
        self._queue_timeout_seconds = queue_timeout_seconds
        self._active: dict[tuple[str, str], int] = {}
        self._waiting: dict[tuple[str, str], int] = {}
        self._turns: dict[tuple[str, str], asyncio.Condition] = {}

    @asynccontextmanager
    async def slot(
        self,
        *,
        organization_id: str,
        user_id: str,
        organization_role: OrganizationRole | str | None,
    ) -> AsyncIterator[None]:
        if _is_owner(organization_role):
            yield
            return

        max_concurrent = _max_concurrent(default=self._default_max_concurrent)
        timeout = (
            self._queue_timeout_seconds
            if self._queue_timeout_seconds is not None
            else _queue_timeout(default=DEFAULT_RECALL_QUEUE_TIMEOUT_SECONDS)
        )
        key = (organization_id, user_id)
        turn = self._turns.get(key)
        if turn is None:
            turn = self._turns[key] = asyncio.Condition()
        async with turn:
            if self._active.get(key, 0) >= max_concurrent:
                self._waiting[key] = self._waiting.get(key, 0) + 1
                started = time.monotonic()
                try:
                    await asyncio.wait_for(
                        turn.wait_for(lambda: self._active.get(key, 0) < max_concurrent),
                        timeout,
                    )
                except TimeoutError:
                    raise RecallConcurrencyLimitExceededError(
                        user_id=user_id,
                        max_concurrent=max_concurrent,
                        waited_seconds=time.monotonic() - started,
                    ) from None
                finally:
                    waiting = self._waiting.get(key, 0) - 1
                    if waiting <= 0:
                        self._waiting.pop(key, None)
                    else:
                        self._waiting[key] = waiting
            self._active[key] = self._active.get(key, 0) + 1

        try:
            yield
        finally:
            async with turn:
                active = self._active.get(key, 0) - 1
                if active <= 0:
                    self._active.pop(key, None)
                else:
                    self._active[key] = active
                turn.notify()
                if key not in self._active and key not in self._waiting:
                    self._turns.pop(key, None)

    def active(self, *, organization_id: str, user_id: str) -> int:
        return self._active.get((organization_id, user_id), 0)

    def waiting(self, *, organization_id: str, user_id: str) -> int:
        return self._waiting.get((organization_id, user_id), 0)


_recall_concurrency_limiter = RecallConcurrencyLimiter()


@asynccontextmanager
async def recall_concurrency_slot(
    *,
    organization_id: str,
    user_id: str,
    organization_role: OrganizationRole | str | None,
) -> AsyncIterator[None]:
    async with _recall_concurrency_limiter.slot(
        organization_id=organization_id,
        user_id=user_id,
        organization_role=organization_role,
    ):
        yield


def _is_owner(role: OrganizationRole | str | None) -> bool:
    if role is None:
        return False
    return str(role) == OrganizationRole.OWNER.value


def _max_concurrent(*, default: int) -> int:
    raw_value = os.environ.get(RECALL_MAX_CONCURRENT_ENV, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid {RECALL_MAX_CONCURRENT_ENV}: {raw_value}") from exc
    if value < 1:
        raise ValueError(f"Invalid {RECALL_MAX_CONCURRENT_ENV}: {raw_value}")
    return value


def _queue_timeout(*, default: float) -> float:
    raw_value = os.environ.get(RECALL_QUEUE_TIMEOUT_ENV, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"Invalid {RECALL_QUEUE_TIMEOUT_ENV}: {raw_value}") from exc
    if value < 0:
        raise ValueError(f"Invalid {RECALL_QUEUE_TIMEOUT_ENV}: {raw_value}")
    return value
