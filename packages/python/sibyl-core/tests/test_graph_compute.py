"""Graph compute steps run on their own threads, in the caller's context."""

from __future__ import annotations

import contextvars
import threading

import pytest
import structlog

from sibyl_core.services import graph_compute
from sibyl_core.services.graph_compute import INLINE_ROW_LIMIT, compute_rows, off_loop

_REQUEST: contextvars.ContextVar[str] = contextvars.ContextVar("graph_compute_request")


def _where() -> tuple[str, str | None]:
    return threading.current_thread().name, _REQUEST.get(None)


async def test_a_step_runs_on_a_compute_thread_with_the_callers_context() -> None:
    _REQUEST.set("request-1")

    thread, request = await off_loop(_where)

    assert thread.startswith("sibyl-graph-compute")
    assert thread != threading.current_thread().name
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
    assert large.startswith("sibyl-graph-compute")


async def test_a_step_error_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph_compute, "INLINE_ROW_LIMIT", 0)

    def fail() -> None:
        raise ValueError("row is malformed")

    with pytest.raises(ValueError, match="row is malformed"):
        await compute_rows(1, fail)
