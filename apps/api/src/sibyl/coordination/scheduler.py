"""Scheduler backend resolution."""

from __future__ import annotations

from typing import Protocol, cast

from sibyl.coordination import CoordinationBackend, get_coordination_backend


class Scheduler(Protocol):
    """Backend contract for scheduled maintenance work."""

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...


_scheduler: Scheduler | None = None
_scheduler_backend: CoordinationBackend | None = None


def get_scheduler() -> Scheduler:
    """Return the scheduler for the active coordination backend."""
    global _scheduler, _scheduler_backend  # noqa: PLW0603

    backend = get_coordination_backend()
    if _scheduler is not None and _scheduler_backend == backend:
        return _scheduler

    scheduler: Scheduler
    if backend == "redis":
        from sibyl.coordination._redis.scheduler import RedisScheduler

        scheduler = cast("Scheduler", RedisScheduler())
    else:
        from sibyl.coordination._local.scheduler import LocalScheduler

        scheduler = cast("Scheduler", LocalScheduler())

    _scheduler = scheduler
    _scheduler_backend = backend
    return scheduler
