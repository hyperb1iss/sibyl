"""Graph compute steps run on their own threads, in the caller's context."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Iterator

import pytest
import structlog

from sibyl_core.services import graph_compute
from sibyl_core.services.graph_compute import (
    GRAPH_BUILD_WORKERS,
    INLINE_ROW_LIMIT,
    compute_rows,
    graph_build,
    graph_build_steps,
    off_loop,
    shutdown_compute_pools,
)

_REQUEST = contextvars.ContextVar[str]("graph_compute_request")


def _where() -> tuple[str, str | None]:
    return threading.current_thread().name, _REQUEST.get(None)


@pytest.fixture
def fresh_pools() -> Iterator[None]:
    shutdown_compute_pools()
    yield
    shutdown_compute_pools()


async def test_a_step_runs_on_a_compute_thread_with_the_callers_context() -> None:
    _REQUEST.set("request-1")

    thread, request = await off_loop(_where)

    assert thread.startswith("sibyl-request-compute")
    assert request == "request-1"


async def test_log_lines_from_a_step_carry_the_bound_request_context() -> None:
    structlog.contextvars.bind_contextvars(request_id="request-2")
    try:
        bound = await off_loop(structlog.contextvars.get_contextvars)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    assert bound["request_id"] == "request-2"


async def test_a_step_does_not_leak_context_back_to_the_caller() -> None:
    _REQUEST.set("caller")

    def rebind() -> None:
        _REQUEST.set("step")

    await off_loop(rebind)

    assert _REQUEST.get() == "caller"


async def test_small_steps_stay_on_the_loop_and_large_ones_leave_it() -> None:
    loop_thread = threading.current_thread().name

    small, _ = await compute_rows(INLINE_ROW_LIMIT - 1, _where)
    large, _ = await compute_rows(INLINE_ROW_LIMIT, _where)

    assert small == loop_thread
    assert large.startswith("sibyl-request-compute")


async def test_a_step_error_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_compute, "INLINE_ROW_LIMIT", 0)

    def fail() -> None:
        raise ValueError("row is malformed")

    with pytest.raises(ValueError, match="row is malformed"):
        await compute_rows(1, fail)


async def test_a_graph_build_and_the_tasks_it_starts_use_the_build_pool() -> None:
    @graph_build
    async def build() -> list[str]:
        own, _ = await off_loop(_where)
        gathered = await asyncio.gather(*(off_loop(_where) for _ in range(3)))
        return [own, *(thread for thread, _ in gathered)]

    threads = await build()
    outside, _ = await off_loop(_where)

    assert all(thread.startswith("sibyl-graph-build-compute") for thread in threads)
    assert outside.startswith("sibyl-request-compute")


async def test_a_request_step_never_queues_behind_build_steps(fresh_pools: None) -> None:
    release = threading.Event()

    def occupy() -> None:
        release.wait(timeout=10)

    with graph_build_steps():
        # Every build worker busy, one more build step queued behind them.
        builds = [asyncio.ensure_future(off_loop(occupy)) for _ in range(GRAPH_BUILD_WORKERS + 1)]
    try:
        thread, _ = await asyncio.wait_for(off_loop(_where), timeout=2)
        assert thread.startswith("sibyl-request-compute")
        assert not any(build.done() for build in builds)
    finally:
        release.set()
        await asyncio.gather(*builds)


async def test_shutdown_cancels_queued_steps_and_later_steps_get_fresh_pools(
    fresh_pools: None,
) -> None:
    release = threading.Event()

    def occupy() -> None:
        release.wait(timeout=10)

    with graph_build_steps():
        running = [asyncio.ensure_future(off_loop(occupy)) for _ in range(GRAPH_BUILD_WORKERS)]
        queued = asyncio.ensure_future(off_loop(occupy))
    await asyncio.sleep(0.05)

    shutdown_compute_pools()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await queued
    await asyncio.gather(*running)
    with graph_build_steps():
        thread, _ = await off_loop(_where)
    assert thread.startswith("sibyl-graph-build-compute")
