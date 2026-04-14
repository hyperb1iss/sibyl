import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _load_queue_module():
    queue_spec = importlib.util.spec_from_file_location(
        "test_jobs_queue_module",
        Path(__file__).resolve().parents[1] / "src" / "sibyl" / "jobs" / "queue.py",
    )
    assert queue_spec is not None
    assert queue_spec.loader is not None

    queue_module = importlib.util.module_from_spec(queue_spec)
    queue_spec.loader.exec_module(queue_module)
    return queue_module


queue_module = _load_queue_module()
JobInfo = queue_module.JobInfo
JobStatus = queue_module.JobStatus


class FakePool:
    def __init__(self, keys: list[str | bytes]) -> None:
        self._keys = keys
        self.keys = AsyncMock(side_effect=AssertionError("list_jobs should not call KEYS"))

    async def scan_iter(self, match: str):
        assert match == "arq:job:*"
        for key in self._keys:
            yield key


class RecordingEnqueuePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def enqueue_job(self, function: str, first_arg: str, **kwargs: object):
        self.calls.append((function, first_arg, kwargs))
        return SimpleNamespace(job_id=kwargs["_job_id"])


@pytest.mark.asyncio
async def test_list_jobs_uses_scan_and_sorts_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    pool = FakePool([b"arq:job:older", b"arq:job:newest", "arq:job:no-time"])
    infos = {
        "older": JobInfo(
            job_id="older",
            function="crawl_source",
            status=JobStatus.QUEUED,
            enqueue_time=now - timedelta(minutes=5),
        ),
        "newest": JobInfo(
            job_id="newest",
            function="crawl_source",
            status=JobStatus.IN_PROGRESS,
            enqueue_time=now,
        ),
        "no-time": JobInfo(
            job_id="no-time",
            function="crawl_source",
            status=JobStatus.COMPLETE,
        ),
    }

    monkeypatch.setattr(queue_module, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        queue_module,
        "get_job_status",
        AsyncMock(side_effect=lambda job_id: infos[job_id]),
    )

    jobs = await queue_module.list_jobs(limit=10)

    assert [job.job_id for job in jobs] == ["newest", "older", "no-time"]
    pool.keys.assert_not_called()


@pytest.mark.asyncio
async def test_list_jobs_filters_limits_and_skips_failed_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    pool = FakePool(
        [
            "arq:job:alpha",
            "arq:job:beta",
            "arq:job:gamma",
            "arq:job:broken",
        ]
    )
    infos = {
        "alpha": JobInfo(
            job_id="alpha",
            function="crawl_source",
            status=JobStatus.QUEUED,
            enqueue_time=now - timedelta(minutes=10),
        ),
        "beta": JobInfo(
            job_id="beta",
            function="sync_source",
            status=JobStatus.QUEUED,
            enqueue_time=now - timedelta(minutes=2),
        ),
        "gamma": JobInfo(
            job_id="gamma",
            function="crawl_source",
            status=JobStatus.IN_PROGRESS,
            enqueue_time=now - timedelta(minutes=1),
        ),
    }

    async def fake_get_job_status(job_id: str) -> JobInfo:
        if job_id == "broken":
            raise RuntimeError("boom")
        return infos[job_id]

    get_job_status = AsyncMock(side_effect=fake_get_job_status)
    monkeypatch.setattr(queue_module, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(queue_module, "get_job_status", get_job_status)

    jobs = await queue_module.list_jobs(function="crawl_source", limit=1)

    assert [job.job_id for job in jobs] == ["gamma"]
    assert get_job_status.await_count == 4


@pytest.mark.asyncio
async def test_enqueue_backup_uses_unique_backup_id_for_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingEnqueuePool()
    monkeypatch.setattr(queue_module, "get_pool", AsyncMock(return_value=pool))

    first_job_id = await queue_module.enqueue_backup("org-123", backup_id="backup_a")
    second_job_id = await queue_module.enqueue_backup("org-123", backup_id="backup_b")

    assert first_job_id == "backup:backup_a"
    assert second_job_id == "backup:backup_b"
    assert pool.calls[0][2]["backup_id"] == "backup_a"
    assert pool.calls[1][2]["backup_id"] == "backup_b"


@pytest.mark.asyncio
async def test_enqueue_backup_generates_backup_id_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingEnqueuePool()
    monkeypatch.setattr(queue_module, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(
        queue_module,
        "generate_backup_id",
        lambda organization_id: f"backup_generated_for_{organization_id}",
    )

    job_id = await queue_module.enqueue_backup("org-123")

    assert job_id == "backup:backup_generated_for_org-123"
    assert pool.calls[0][2]["backup_id"] == "backup_generated_for_org-123"


@pytest.mark.asyncio
async def test_enqueue_crawl_includes_org_metadata_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingEnqueuePool()
    monkeypatch.setattr(queue_module, "get_pool", AsyncMock(return_value=pool))

    job_id = await queue_module.enqueue_crawl("source-123", organization_id="org-123")

    assert job_id == "crawl:source-123"
    assert pool.calls[0][0] == "crawl_source"
    assert pool.calls[0][1] == "source-123"
    assert pool.calls[0][2]["organization_id"] == "org-123"


@pytest.mark.asyncio
async def test_enqueue_sync_includes_org_metadata_when_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingEnqueuePool()
    monkeypatch.setattr(queue_module, "get_pool", AsyncMock(return_value=pool))

    job_id = await queue_module.enqueue_sync("source-123", organization_id="org-123")

    assert job_id == "sync:source-123"
    assert pool.calls[0][0] == "sync_source"
    assert pool.calls[0][1] == "source-123"
    assert pool.calls[0][2]["organization_id"] == "org-123"
