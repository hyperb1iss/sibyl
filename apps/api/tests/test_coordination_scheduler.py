from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from sibyl.coordination._local import scheduler as local_scheduler
from sibyl.coordination._local.scheduler import LocalScheduler
from sibyl.jobs import worker as worker_module
from sibyl.jobs.worker import ScheduleSpec


def test_get_schedule_specs_does_not_log_registered_jobs(monkeypatch) -> None:
    info = MagicMock()
    monkeypatch.setattr(worker_module.log, "info", info)

    worker_module.get_schedule_specs()

    assert all(call.args[:1] != ("cron_job_registered",) for call in info.call_args_list)


def test_privacy_purge_job_is_scheduled_and_registered() -> None:
    spec_names = {spec.name for spec in worker_module.get_schedule_specs()}

    assert "purge_due_deleted_personal_memories" in spec_names
    assert worker_module.purge_due_deleted_personal_memories in (
        worker_module.WorkerSettings.functions
    )


def test_raw_capture_changefeed_poll_is_scheduled_and_registered() -> None:
    spec_names = {spec.name for spec in worker_module.get_schedule_specs()}

    assert "poll_all_raw_capture_changefeeds" in spec_names
    assert worker_module.poll_raw_capture_changefeed in worker_module.WorkerSettings.functions
    assert worker_module.poll_all_raw_capture_changefeeds in (
        worker_module.WorkerSettings.functions
    )


@pytest.mark.asyncio
async def test_local_scheduler_logs_registered_jobs_on_startup(monkeypatch) -> None:
    log_schedule_specs = MagicMock()
    monkeypatch.setattr(local_scheduler, "log_schedule_specs", log_schedule_specs)

    async def scheduled_job(ctx: dict[str, str]) -> None:
        del ctx

    schedule_specs = [
        ScheduleSpec(
            name="every_minute",
            function=scheduled_job,
            schedule_label="* * * * *",
        )
    ]
    scheduler = LocalScheduler(schedule_specs=schedule_specs)

    await scheduler.startup()
    await scheduler.shutdown()

    log_schedule_specs.assert_called_once_with(schedule_specs)


@pytest.mark.asyncio
async def test_local_scheduler_fires_matching_jobs_once_per_minute() -> None:
    now = {"value": datetime(2026, 4, 21, 12, 0, tzinfo=UTC)}
    fired = asyncio.Event()
    calls: list[datetime] = []

    async def scheduled_job(ctx: dict[str, str]) -> None:
        del ctx

    async def enqueue_scheduled_job(spec: ScheduleSpec, current_minute: datetime) -> str:
        calls.append(current_minute)
        fired.set()
        return f"scheduled:{spec.name}"

    scheduler = LocalScheduler(
        schedule_specs=[
            ScheduleSpec(
                name="every_minute",
                function=scheduled_job,
                schedule_label="* * * * *",
            )
        ],
        now=lambda: now["value"],
        tick_seconds=0.01,
        enqueue_scheduled_job=enqueue_scheduled_job,
    )

    await scheduler.startup()
    await asyncio.wait_for(fired.wait(), timeout=1)
    await asyncio.sleep(0.05)

    assert len(calls) == 1

    fired.clear()
    now["value"] = now["value"] + timedelta(minutes=1)
    await asyncio.wait_for(fired.wait(), timeout=1)
    assert len(calls) == 2

    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_local_scheduler_skips_duplicate_slot_while_job_is_still_running() -> None:
    now = {"value": datetime(2026, 4, 21, 12, 0, tzinfo=UTC)}
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def scheduled_job(ctx: dict[str, str]) -> None:
        del ctx

    async def enqueue_scheduled_job(spec: ScheduleSpec, _current_minute: datetime) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return f"scheduled:{spec.name}"

    scheduler = LocalScheduler(
        schedule_specs=[
            ScheduleSpec(
                name="every_minute",
                function=scheduled_job,
                schedule_label="* * * * *",
            )
        ],
        now=lambda: now["value"],
        tick_seconds=0.01,
        enqueue_scheduled_job=enqueue_scheduled_job,
    )

    await scheduler.startup()
    await asyncio.wait_for(started.wait(), timeout=1)

    now["value"] = now["value"] + timedelta(minutes=1)
    await asyncio.sleep(0.05)
    assert calls == 1

    release.set()
    await scheduler.shutdown()
