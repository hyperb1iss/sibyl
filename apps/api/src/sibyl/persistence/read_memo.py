"""Per-organization memo for read aggregates that many clients ask for at once.

A write broadcasts to every tab in the organization, and every tab answers
by refetching the same dashboard aggregates within the same few
milliseconds. Each memo here keys one such aggregate by organization: the
first caller computes it, concurrent callers join that computation instead
of starting their own (single flight), and the value is served for a short
window afterwards. The write path drops the entry through
``invalidate_org_read_memos`` as part of the same broadcast the tabs react
to, so the refetch a broadcast triggers always computes against fresh rows;
the TTL only bounds staleness for writes that never broadcast.

A compute that overlaps an invalidation is returned to its callers but not
stored: the generation it started under is gone, and keeping its value
would serve the pre-write answer for a whole window.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()

_MEMOS: list[OrgReadMemo[Any]] = []


@dataclass(slots=True)
class _OrgState[T]:
    generation: int = 0
    value: T | None = None
    expires_at: float = 0.0
    inflight: asyncio.Task[T] | None = None
    # The generation the in-flight compute started under. A reader arriving
    # after a write must not join a compute that began before it.
    inflight_generation: int = 0


@dataclass(slots=True)
class OrgReadMemo[T]:
    """A single-flight, short-TTL memo keyed by organization."""

    name: str
    ttl_seconds: float
    _states: dict[str, _OrgState[T]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _MEMOS.append(self)

    def peek(self, group_id: str) -> T | None:
        """Return the stored value without computing, or None when cold."""
        state = self._states.get(group_id)
        if state is None or state.value is None:
            return None
        if state.expires_at <= time.monotonic():
            state.value = None
            self._sweep(group_id)
            return None
        return state.value

    async def get(self, group_id: str, compute: Callable[[], Awaitable[T]]) -> T:
        """Return the memoized value, computing it once for concurrent callers."""
        cached = self.peek(group_id)
        if cached is not None:
            return cached
        state = self._states.setdefault(group_id, _OrgState())
        task = state.inflight
        if task is None or state.inflight_generation != state.generation:
            # The generation is taken here, when the request is made, so a
            # write that lands before the compute even starts still outdates
            # its result. A compute from an older generation keeps running
            # for the readers that joined it before the write; this reader
            # came after, so it gets its own.
            task = asyncio.create_task(
                self._compute(group_id, state, state.generation, compute),
                name=f"read-memo:{self.name}:{group_id}",
            )
            # A waiter that is cancelled must not leave an unretrieved
            # exception behind when the shared task fails after it left.
            task.add_done_callback(_retrieve_exception)
            state.inflight = task
            state.inflight_generation = state.generation
        # Shielded so one cancelled waiter does not cancel the computation
        # every other waiter is sharing.
        return await asyncio.shield(task)

    def invalidate(self, group_id: str | None = None) -> None:
        """Drop the value for one organization, or for all of them."""
        keys = [group_id] if group_id is not None else list(self._states)
        for key in keys:
            state = self._states.get(key)
            if state is None:
                continue
            state.generation += 1
            state.value = None
            self._sweep(key)

    def clear(self) -> None:
        self._states.clear()

    async def _compute(
        self,
        group_id: str,
        state: _OrgState[T],
        generation: int,
        compute: Callable[[], Awaitable[T]],
    ) -> T:
        try:
            value = await compute()
        finally:
            if state.inflight is asyncio.current_task():
                state.inflight = None
        if state.generation == generation:
            state.value = value
            state.expires_at = time.monotonic() + self.ttl_seconds
        else:
            log.debug("read_memo_result_discarded", memo=self.name, group_id=group_id)
            self._sweep(group_id)
        return value

    def _sweep(self, group_id: str) -> None:
        state = self._states.get(group_id)
        if state is not None and state.value is None and state.inflight is None:
            self._states.pop(group_id, None)


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def invalidate_org_read_memos(group_id: str | None) -> None:
    """Drop every memoized read aggregate for an organization after a write."""
    for memo in _MEMOS:
        memo.invalidate(group_id)


def reset_org_read_memos() -> None:
    """Forget every memoized value in the process (tests and shutdown)."""
    for memo in _MEMOS:
        memo.clear()
