"""Local in-process scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl.jobs.worker import ScheduleSpec, get_schedule_specs, log_schedule_specs

log = structlog.get_logger()


class LocalScheduler:
    """Run scheduled maintenance jobs in-process for local coordination."""

    def __init__(
        self,
        *,
        schedule_specs: list[ScheduleSpec] | None = None,
        now: Callable[[], datetime] | None = None,
        tick_seconds: float = 30.0,
    ) -> None:
        self._schedule_specs = schedule_specs or get_schedule_specs()
        self._now = now or (lambda: datetime.now(UTC))
        self._tick_seconds = tick_seconds
        self._runner: asyncio.Task[None] | None = None
        self._active_jobs: dict[str, asyncio.Task[Any]] = {}
        self._last_fired: dict[str, datetime] = {}
        self._lifecycle_lock = asyncio.Lock()

    async def startup(self) -> None:
        """Start the scheduler loop."""
        async with self._lifecycle_lock:
            if self._runner is not None:
                return

            self._runner = asyncio.create_task(
                self._run_loop(),
                name="sibyl-local-scheduler",
            )
            log_schedule_specs(self._schedule_specs)
            log.info("Local scheduler ready", jobs=len(self._schedule_specs))

    async def shutdown(self) -> None:
        """Stop the scheduler loop and any active scheduled tasks."""
        async with self._lifecycle_lock:
            runner = self._runner
            active_jobs = list(self._active_jobs.values())
            self._runner = None
            self._active_jobs = {}
            self._last_fired = {}

        for task in active_jobs:
            task.cancel()
        if active_jobs:
            await asyncio.gather(*active_jobs, return_exceptions=True)

        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def _run_loop(self) -> None:
        while True:
            current_minute = self._truncate_to_minute(self._now())
            await self._fire_due_jobs(current_minute)
            await asyncio.sleep(self._tick_seconds)

    async def _fire_due_jobs(self, current_minute: datetime) -> None:
        for spec in self._schedule_specs:
            if not _matches_schedule(spec, current_minute):
                continue

            if self._last_fired.get(spec.name) == current_minute:
                continue

            active_task = self._active_jobs.get(spec.name)
            if active_task is not None and not active_task.done():
                self._last_fired[spec.name] = current_minute
                log.warning("Scheduled job already running", job=spec.name)
                continue

            task = asyncio.create_task(
                self._run_spec(spec, current_minute),
                name=f"sibyl-scheduled-{spec.name}",
            )
            self._active_jobs[spec.name] = task
            self._last_fired[spec.name] = current_minute

    async def _run_spec(self, spec: ScheduleSpec, current_minute: datetime) -> None:
        try:
            log.info(
                "Scheduled job started", job=spec.name, scheduled_for=current_minute.isoformat()
            )
            await spec.function({"scheduled_for": current_minute.isoformat()})
            log.info("Scheduled job complete", job=spec.name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Scheduled job failed", job=spec.name, error=str(e))
        finally:
            self._active_jobs.pop(spec.name, None)

    def _truncate_to_minute(self, value: datetime) -> datetime:
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.replace(second=0, microsecond=0)


def _matches_schedule(spec: ScheduleSpec, when: datetime) -> bool:
    return all(
        [
            _matches_field(spec.minute, when.minute),
            _matches_field(spec.hour, when.hour),
            _matches_field(spec.day, when.day),
            _matches_field(spec.month, when.month),
            _matches_field(spec.weekday, when.weekday()),
        ]
    )


def _matches_field(field: int | set[int] | None, value: int) -> bool:
    if field is None:
        return True
    if isinstance(field, set):
        return value in field
    return field == value
