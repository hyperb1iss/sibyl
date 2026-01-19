"""Helpers for keeping AgentState in Postgres in sync with graph updates."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select

from sibyl.db import AgentState, get_session
from sibyl_core.models import AgentStatus

log = structlog.get_logger()

_UNSET = object()


def _to_naive(value: datetime | None) -> datetime | None:
    """Normalize datetimes for TIMESTAMP WITHOUT TIME ZONE columns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def update_agent_state(
    *,
    org_id: str,
    agent_id: str,
    status: str | None | object = _UNSET,
    last_heartbeat: datetime | None | object = _UNSET,
    tokens_used: int | None | object = _UNSET,
    cost_usd: float | None | object = _UNSET,
    started_at: datetime | None | object = _UNSET,
    completed_at: datetime | None | object = _UNSET,
    error_message: str | None | object = _UNSET,
    task_id: str | None | object = _UNSET,
    orchestrator_id: str | None | object = _UNSET,
    current_activity: str | None | object = _UNSET,
) -> None:
    """Update AgentState in Postgres, creating it if missing.

    Best-effort: logs and returns on failures to avoid crashing agent execution.
    """
    try:
        org_uuid = UUID(org_id)
    except (TypeError, ValueError):
        log.debug(
            "Skipping AgentState update; org_id is not a UUID",
            org_id=org_id,
            agent_id=agent_id,
        )
        return

    try:
        async with get_session() as session:
            result = await session.execute(
                select(AgentState).where(AgentState.graph_agent_id == agent_id)
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = AgentState(
                    organization_id=org_uuid,
                    graph_agent_id=agent_id,
                    status=(
                        status
                        if status not in (_UNSET, None)
                        else AgentStatus.INITIALIZING.value
                    ),
                )
                if task_id is not _UNSET:
                    state.task_id = task_id
                if orchestrator_id is not _UNSET:
                    state.orchestrator_id = orchestrator_id
                session.add(state)

            if status is not _UNSET and status is not None:
                state.status = status
            if last_heartbeat is not _UNSET:
                state.last_heartbeat = _to_naive(last_heartbeat)
            if tokens_used is not _UNSET:
                state.tokens_used = tokens_used or 0
            if cost_usd is not _UNSET:
                state.cost_usd = cost_usd or 0.0
            if started_at is not _UNSET:
                state.started_at = _to_naive(started_at)
            if completed_at is not _UNSET:
                state.completed_at = _to_naive(completed_at)
            if error_message is not _UNSET:
                state.error_message = error_message
            if task_id is not _UNSET:
                state.task_id = task_id
            if orchestrator_id is not _UNSET:
                state.orchestrator_id = orchestrator_id
            if current_activity is not _UNSET:
                state.current_activity = current_activity

            await session.commit()
    except Exception as exc:
        log.warning("AgentState update failed", agent_id=agent_id, error=str(exc))
