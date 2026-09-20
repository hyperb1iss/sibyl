"""Experimental receipt readiness only; no authorization or publication authority.

The caller must freshly authorize the current request. A ready receipt still
needs the unchanged semantic routing policy; readiness alone cannot accept a
candidate. No observation is rebound to a new request or source revision.
"""

from __future__ import annotations

import asyncio
import math
from typing import Literal

from sibyl_core.ai.decisions import DecisionObservation, DecisionRequest

Readiness = Literal["ready", "missing", "pending", "stale", "unauthorized", "invalid"]


def select_ready(
    request: DecisionRequest,
    observation: DecisionObservation | None,
    *,
    completed_ms: float | None,
    now_ms: float,
    authorized: bool,
    expected_model_id: str,
) -> Readiness:
    """Evaluate a synthetic timeline without moving its consumer deadline."""
    if type(now_ms) not in (int, float) or not math.isfinite(now_ms) or now_ms < 0:
        raise ValueError("consumer time must be finite and nonnegative")
    if not authorized:
        return "unauthorized"
    if observation is None:
        return "missing"
    if completed_ms is not None and (
        type(completed_ms) not in (int, float)
        or not math.isfinite(completed_ms)
        or completed_ms < 0
    ):
        raise ValueError("completion time must be finite and nonnegative")
    if completed_ms is None or completed_ms > now_ms:
        return "pending"
    if (
        observation.request_digest != request.request_digest
        or observation.semantic_input_sha256 != request.semantic_input_sha256
    ):
        return "stale"
    try:
        # Revalidate immutable model copies too; model_copy(update=...) bypasses
        # Pydantic validators and must not manufacture a usable observation.
        checked = DecisionObservation.model_validate_json(observation.model_dump_json())
        checked.validate_for(request, expected_model_id=expected_model_id)
    except ValueError:
        return "invalid"
    return "ready" if checked.execution_status == "completed" else "invalid"


def probe_task(
    request: DecisionRequest,
    task: asyncio.Task[DecisionObservation] | None,
    *,
    authorized: bool,
    expected_model_id: str,
) -> Readiness:
    """Inspect owned background work synchronously; never wait or cancel it.

    The experiment owner must settle and account for every dispatched task,
    including a task that completes after the consumer takes its fallback.
    """
    if not authorized:
        return "unauthorized"
    if task is None:
        return "missing"
    if not task.done():
        return "pending"
    if task.cancelled():
        return "invalid"
    try:
        observation = task.result()
    except Exception:
        return "invalid"
    return select_ready(
        request,
        observation,
        completed_ms=0,
        now_ms=0,
        authorized=authorized,
        expected_model_id=expected_model_id,
    )
