"""Durable sandbox task dispatcher.

Manages DB-backed sandbox task queue and dispatch lifecycle:
- enqueue
- dispatch
- ack
- complete / retry / fail
"""

from __future__ import annotations

import inspect
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.agents.sandbox_utils import set_if_present as _set_if_present, status_of as _status_of

log = structlog.get_logger()


class SandboxDispatcherError(RuntimeError):
    """Sandbox task queue/dispatch error."""


class SandboxDispatcher:
    """DB-backed sandbox task queue with retry/fail semantics."""

    PENDING_STATUSES = {"queued", "retry"}

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        enabled: bool = False,
        max_attempts: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.enabled = enabled
        self.max_attempts = max_attempts or int(os.getenv("SIBYL_SANDBOX_MAX_ATTEMPTS", "3"))

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SandboxDispatcherError("Sandbox dispatcher is disabled")

    def _task_model(self) -> type[Any]:
        from sibyl.db import models as db_models

        model = getattr(db_models, "SandboxTask", None)
        if model is None:
            raise SandboxDispatcherError(
                "SandboxTask DB model is unavailable; ensure sandbox migrations/models are loaded"
            )
        return model

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _parse_uuid(self, value: UUID | str) -> UUID | None:
        if isinstance(value, UUID):
            return value
        with suppress(ValueError, TypeError):
            return UUID(str(value))
        return None

    async def _get_task(self, session: AsyncSession, task_id: UUID | str) -> Any | None:
        sandbox_task_model = self._task_model()
        task_uuid = self._parse_uuid(task_id)
        if task_uuid is None:
            return None
        result = await session.execute(
            select(sandbox_task_model).where(sandbox_task_model.id == task_uuid)
        )
        return result.scalar_one_or_none()

    async def enqueue_task(
        self,
        *,
        sandbox_id: UUID,
        organization_id: UUID,
        task_type: str = "agent_execution",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Enqueue a durable sandbox task."""
        start = time.monotonic()
        self._require_enabled()
        sandbox_task_model = self._task_model()
        payload = payload or {}

        async with self._session_factory() as session:
            if idempotency_key and hasattr(sandbox_task_model, "idempotency_key"):
                existing_result = await session.execute(
                    select(sandbox_task_model).where(
                        sandbox_task_model.organization_id == organization_id,
                        sandbox_task_model.sandbox_id == sandbox_id,
                        sandbox_task_model.idempotency_key == idempotency_key,
                        sandbox_task_model.status.in_(
                            list(self.PENDING_STATUSES | {"dispatched", "acked"})
                        ),
                    )
                )
                existing = existing_result.scalar_one_or_none()
                if existing is not None:
                    return existing

            task = sandbox_task_model(
                sandbox_id=sandbox_id,
                organization_id=organization_id,
            )
            _set_if_present(task, "task_type", task_type)
            _set_if_present(task, "status", "queued")
            _set_if_present(task, "payload", payload)
            _set_if_present(task, "attempt_count", 0)
            _set_if_present(task, "max_attempts", self.max_attempts)
            _set_if_present(task, "error_message", None)
            if idempotency_key:
                _set_if_present(task, "idempotency_key", idempotency_key)

            session.add(task)
            await session.commit()
            await session.refresh(task)

            log.info(
                "sandbox_task_enqueued",
                sandbox_id=str(sandbox_id),
                task_id=str(getattr(task, "id", "")),
                task_type=task_type,
                org_id=str(organization_id),
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
            return task

    async def dispatch_pending_for_sandbox(
        self,
        *,
        sandbox_id: UUID,
        send_fn: Callable[[dict[str, Any]], Awaitable[bool] | bool],
        runner_id: UUID | None = None,
        limit: int = 20,
    ) -> int:
        """Dispatch queued tasks for a sandbox to a connected runner."""
        start = time.monotonic()
        self._require_enabled()
        sandbox_task_model = self._task_model()
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._session_factory() as session:
            stmt = (
                select(sandbox_task_model)
                .where(
                    sandbox_task_model.sandbox_id == sandbox_id,
                    sandbox_task_model.status.in_(list(self.PENDING_STATUSES)),
                )
                .order_by(sandbox_task_model.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            tasks = result.scalars().all()

            dispatched = 0
            for task in tasks:
                attempts = int(getattr(task, "attempt_count", 0) or 0)
                task_max_attempts = int(getattr(task, "max_attempts", self.max_attempts) or self.max_attempts)
                if attempts >= task_max_attempts:
                    _set_if_present(task, "status", "failed")
                    _set_if_present(task, "error_message", "max_attempts_exceeded")
                    _set_if_present(task, "failed_at", now)
                    continue

                payload = dict(getattr(task, "payload", {}) or {})
                message = dict(payload)
                message["type"] = "task_assign"
                message["task_id"] = str(task.id)
                ok = await self._maybe_await(send_fn(message))

                _set_if_present(task, "attempt_count", attempts + 1)
                _set_if_present(task, "last_dispatch_at", now)
                if runner_id is not None:
                    _set_if_present(task, "runner_id", runner_id)

                if ok:
                    _set_if_present(task, "status", "dispatched")
                    _set_if_present(task, "error_message", None)
                    dispatched += 1
                elif attempts + 1 >= task_max_attempts:
                    _set_if_present(task, "status", "failed")
                    _set_if_present(task, "error_message", "dispatch_failed_max_attempts")
                    _set_if_present(task, "failed_at", now)
                else:
                    _set_if_present(task, "status", "retry")
                    _set_if_present(task, "error_message", "dispatch_failed_retrying")

            await session.commit()
            if dispatched:
                log.info(
                    "sandbox_tasks_dispatched",
                    sandbox_id=str(sandbox_id),
                    count=dispatched,
                    duration_ms=round((time.monotonic() - start) * 1000, 1),
                )
            return dispatched

    async def ack_task(
        self,
        *,
        task_id: UUID | str,
        runner_id: UUID | None = None,
    ) -> Any:
        """Acknowledge a dispatched task."""
        self._require_enabled()
        async with self._session_factory() as session:
            task = await self._get_task(session, task_id)
            if task is None:
                raise SandboxDispatcherError(f"Sandbox task not found: {task_id}")

            _set_if_present(task, "status", "acked")
            if runner_id is not None:
                _set_if_present(task, "runner_id", runner_id)
            _set_if_present(task, "acked_at", datetime.now(UTC).replace(tzinfo=None))
            await session.commit()
            await session.refresh(task)
            return task

    async def complete_task(
        self,
        *,
        task_id: UUID | str,
        success: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        retryable: bool = False,
        canceled: bool = False,
    ) -> Any:
        """Mark task completion, with retry/fail behavior using attempt_count."""
        self._require_enabled()
        now = datetime.now(UTC).replace(tzinfo=None)

        async with self._session_factory() as session:
            task = await self._get_task(session, task_id)
            if task is None:
                raise SandboxDispatcherError(f"Sandbox task not found: {task_id}")

            attempts = int(getattr(task, "attempt_count", 0) or 0)
            task_max_attempts = int(getattr(task, "max_attempts", self.max_attempts) or self.max_attempts)

            if canceled:
                _set_if_present(task, "status", "canceled")
                _set_if_present(task, "result", result or {})
                _set_if_present(task, "error_message", error)
                _set_if_present(task, "completed_at", now)
            elif success:
                _set_if_present(task, "status", "completed")
                _set_if_present(task, "result", result or {})
                _set_if_present(task, "error_message", None)
                _set_if_present(task, "completed_at", now)
            else:
                if retryable and attempts < task_max_attempts:
                    _set_if_present(task, "status", "retry")
                else:
                    _set_if_present(task, "status", "failed")
                    _set_if_present(task, "failed_at", now)
                _set_if_present(task, "error_message", error or "sandbox_task_failed")
                if result is not None:
                    _set_if_present(task, "result", result)

            await session.commit()
            await session.refresh(task)
            log.info(
                "sandbox_task_completed",
                task_id=str(getattr(task, "id", task_id)),
                status=_status_of(task),
                success=success,
                retryable=retryable,
                canceled=canceled,
                task_type=str(getattr(task, "task_type", "")),
                attempt=int(getattr(task, "attempt_count", 0) or 0),
            )
            return task

    async def reap_stale_tasks(
        self,
        *,
        dispatch_ttl_seconds: int = 300,
        ack_ttl_seconds: int = 1800,
    ) -> int:
        """Requeue tasks stuck in dispatched (unacknowledged) past dispatch TTL.

        Acked tasks get a much longer TTL since they may be legitimately running.
        Only tasks that have not been acknowledged within dispatch_ttl, or acked
        tasks that exceeded ack_ttl, are considered stale.
        """
        start = time.monotonic()
        self._require_enabled()
        sandbox_task_model = self._task_model()
        now = datetime.now(UTC).replace(tzinfo=None)
        dispatch_cutoff = now - timedelta(seconds=dispatch_ttl_seconds)
        ack_cutoff = now - timedelta(seconds=ack_ttl_seconds)

        async with self._session_factory() as session:
            # Dispatched but never acknowledged — short TTL
            # Acked but no completion — long TTL (may be legitimately running)
            stmt = (
                select(sandbox_task_model)
                .where(
                    sa.or_(
                        sa.and_(
                            sandbox_task_model.status == "dispatched",
                            sandbox_task_model.last_dispatch_at < dispatch_cutoff,
                        ),
                        sa.and_(
                            sandbox_task_model.status == "acked",
                            sandbox_task_model.last_dispatch_at < ack_cutoff,
                        ),
                    )
                )
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            stale = result.scalars().all()

            reaped = 0
            for task in stale:
                attempts = int(getattr(task, "attempt_count", 0) or 0)
                max_att = int(
                    getattr(task, "max_attempts", self.max_attempts) or self.max_attempts
                )
                if attempts >= max_att:
                    _set_if_present(task, "status", "failed")
                    _set_if_present(task, "error_message", "lease_expired_max_attempts")
                    _set_if_present(task, "failed_at", datetime.now(UTC).replace(tzinfo=None))
                else:
                    _set_if_present(task, "status", "retry")
                    _set_if_present(task, "error_message", "lease_expired_requeued")
                reaped += 1

            await session.commit()
            if reaped:
                log.info(
                    "sandbox_tasks_reaped",
                    count=reaped,
                    duration_ms=round((time.monotonic() - start) * 1000, 1),
                )
            return reaped

    async def fail_all_pending(self, org_id: UUID) -> int:
        """Fail all pending/dispatched/acked tasks for an org. Returns count."""
        self._require_enabled()
        sandbox_task_model = self._task_model()
        now = datetime.now(UTC).replace(tzinfo=None)
        drainable_statuses = list(self.PENDING_STATUSES | {"dispatched", "acked"})

        async with self._session_factory() as session:
            stmt = (
                select(sandbox_task_model)
                .where(
                    sandbox_task_model.organization_id == org_id,
                    sandbox_task_model.status.in_(drainable_statuses),
                )
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            tasks = result.scalars().all()

            count = 0
            for task in tasks:
                _set_if_present(task, "status", "failed")
                _set_if_present(task, "error_message", "admin_rollback")
                _set_if_present(task, "failed_at", now)
                count += 1

            await session.commit()

        if count:
            log.info("sandbox_fail_all_pending", org_id=str(org_id), count=count)
        return count
