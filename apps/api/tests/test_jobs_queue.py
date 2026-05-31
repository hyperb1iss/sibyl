from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.coordination._redis.broker import RedisQueueBroker
from sibyl.coordination.broker import (
    RECENT_JOB_INDEX_KEY,
    RECENT_JOB_INDEX_LIMIT,
    JobInfo,
    JobStatus,
    raw_promotion_job_id,
)


class FakePool:
    def __init__(
        self, recent_ids: list[str | bytes], scan_ids: list[str | bytes] | None = None
    ) -> None:
        self._recent_ids = recent_ids
        self._scan_ids = scan_ids
        self.keys = AsyncMock(side_effect=AssertionError("list_jobs should not call KEYS"))
        self.zadd = AsyncMock()
        self.zremrangebyrank = AsyncMock()
        self.zrevrange_calls = 0
        self.scan_iter_calls = 0

    async def zrevrange(self, key: str, start: int, stop: int):
        assert key == RECENT_JOB_INDEX_KEY
        assert start == 0
        assert stop == -1
        self.zrevrange_calls += 1
        return self._recent_ids

    async def scan_iter(self, match: str):
        assert match == "arq:job:*"
        self.scan_iter_calls += 1
        if self._scan_ids is None:
            raise AssertionError("list_jobs should not call scan_iter when the index is populated")
        for key in self._scan_ids:
            yield key


class RecordingEnqueuePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, dict[str, object]]] = []
        self.extra_args: list[tuple[object, ...]] = []
        self.delete = AsyncMock()
        self.zadd = AsyncMock()
        self.zremrangebyrank = AsyncMock()

    async def enqueue_job(
        self,
        function: str,
        first_arg: object | None = None,
        *args: object,
        **kwargs: object,
    ):
        self.calls.append((function, first_arg, kwargs))
        self.extra_args.append(args)
        return SimpleNamespace(job_id=kwargs["_job_id"])


def make_broker(pool: object) -> RedisQueueBroker:
    broker = RedisQueueBroker()
    broker.get_pool = AsyncMock(return_value=pool)  # type: ignore[method-assign]
    return broker


def assert_recent_job_indexed(pool: RecordingEnqueuePool, job_id: str) -> None:
    assert pool.zadd.await_count >= 1
    key, mapping = pool.zadd.await_args_list[-1].args
    assert key == RECENT_JOB_INDEX_KEY
    assert mapping == {job_id: mapping[job_id]}
    pool.zremrangebyrank.assert_awaited_with(
        RECENT_JOB_INDEX_KEY,
        0,
        -(RECENT_JOB_INDEX_LIMIT + 1),
    )


@pytest.mark.asyncio
async def test_list_jobs_uses_recent_index_and_sorts_newest_first() -> None:
    now = datetime.now(UTC)
    pool = FakePool([b"newest", b"older", "no-time"])
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

    broker = make_broker(pool)
    broker.get_job_status = AsyncMock(side_effect=lambda job_id: infos[job_id])  # type: ignore[method-assign]

    jobs = await broker.list_jobs(limit=10)

    assert [job.job_id for job in jobs] == ["newest", "older", "no-time"]
    pool.keys.assert_not_called()
    assert pool.scan_iter_calls == 0
    assert pool.zrevrange_calls == 1


@pytest.mark.asyncio
async def test_list_jobs_filters_limits_and_skips_failed_statuses() -> None:
    now = datetime.now(UTC)
    pool = FakePool(["alpha", "beta", "gamma", "broken"])
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

    broker = make_broker(pool)
    broker.get_job_status = AsyncMock(side_effect=fake_get_job_status)  # type: ignore[method-assign]

    jobs = await broker.list_jobs(function="crawl_source", limit=1)

    assert [job.job_id for job in jobs] == ["gamma"]
    assert broker.get_job_status.await_count == 4  # type: ignore[attr-defined]
    assert pool.scan_iter_calls == 0
    assert pool.zrevrange_calls == 1


@pytest.mark.asyncio
async def test_list_jobs_with_non_positive_limit_short_circuits() -> None:
    pool = FakePool(["alpha", "beta"])

    broker = make_broker(pool)
    broker.get_job_status = AsyncMock()  # type: ignore[method-assign]

    assert await broker.list_jobs(limit=0) == []
    assert await broker.list_jobs(limit=-5) == []
    broker.get_job_status.assert_not_called()
    assert pool.scan_iter_calls == 0
    assert pool.zrevrange_calls == 2


@pytest.mark.asyncio
async def test_list_jobs_bounds_status_lookups_by_limit() -> None:
    now = datetime.now(UTC)
    recent_ids = [f"job-{i}" for i in range(500)]
    pool = FakePool(recent_ids)

    async def fake_get_job_status(job_id: str) -> JobInfo:
        return JobInfo(
            job_id=job_id,
            function="crawl_source",
            status=JobStatus.QUEUED,
            enqueue_time=now,
        )

    broker = make_broker(pool)
    broker.get_job_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_get_job_status
    )

    jobs = await broker.list_jobs(limit=5)

    # An unfiltered listing only loads the newest ``limit`` ids, so the
    # request cannot fan out across the whole recent index.
    assert len(jobs) == 5
    assert broker.get_job_status.await_count == 5  # type: ignore[attr-defined]
    assert [job.job_id for job in jobs] == recent_ids[:5]


@pytest.mark.asyncio
async def test_list_jobs_falls_back_to_scan_when_index_is_empty() -> None:
    now = datetime.now(UTC)
    pool = FakePool([], scan_ids=["arq:job:older", b"arq:job:newest"])
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
    }

    broker = make_broker(pool)
    broker.get_job_status = AsyncMock(side_effect=lambda job_id: infos[job_id])  # type: ignore[method-assign]

    jobs = await broker.list_jobs(limit=10)

    assert [job.job_id for job in jobs] == ["newest", "older"]
    assert pool.scan_iter_calls == 1
    assert pool.zrevrange_calls == 1


@pytest.mark.asyncio
async def test_enqueue_backup_uses_unique_backup_id_for_job_id() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    first_job_id = await broker.enqueue_backup("org-123", backup_id="backup_a")
    second_job_id = await broker.enqueue_backup("org-123", backup_id="backup_b")

    assert first_job_id == "backup:backup_a"
    assert second_job_id == "backup:backup_b"
    assert pool.calls[0][2]["backup_id"] == "backup_a"
    assert pool.calls[1][2]["backup_id"] == "backup_b"
    assert_recent_job_indexed(pool, "backup:backup_b")
    assert pool.zadd.await_count == 2


@pytest.mark.asyncio
async def test_enqueue_backup_generates_backup_id_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)
    monkeypatch.setattr(
        "sibyl.coordination._redis.broker.generate_backup_id",
        lambda organization_id: f"backup_generated_for_{organization_id}",
    )

    job_id = await broker.enqueue_backup("org-123")

    assert job_id == "backup:backup_generated_for_org-123"
    assert pool.calls[0][2]["backup_id"] == "backup_generated_for_org-123"
    assert_recent_job_indexed(pool, "backup:backup_generated_for_org-123")


@pytest.mark.asyncio
async def test_enqueue_backup_uses_database_dump_kwarg_when_requested() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_backup(
        "org-123",
        include_database_dump=False,
        backup_id="backup_a",
    )

    assert job_id == "backup:backup_a"
    assert pool.calls[0][2]["include_database_dump"] is False


@pytest.mark.asyncio
async def test_enqueue_crawl_includes_org_metadata_when_provided() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_crawl("source-123", organization_id="org-123")

    assert job_id == "crawl:source-123"
    assert pool.calls[0][0] == "crawl_source"
    assert pool.calls[0][1] == "source-123"
    assert pool.calls[0][2]["organization_id"] == "org-123"
    assert_recent_job_indexed(pool, "crawl:source-123")


@pytest.mark.asyncio
async def test_enqueue_sync_includes_org_metadata_when_provided() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_sync("source-123", organization_id="org-123")

    assert job_id == "sync:source-123"
    assert pool.calls[0][0] == "sync_source"
    assert pool.calls[0][1] == "source-123"
    assert pool.calls[0][2]["organization_id"] == "org-123"
    assert_recent_job_indexed(pool, "sync:source-123")


@pytest.mark.asyncio
async def test_enqueue_source_import_drain_indexes_run_scoped_job() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "org-123",
        "memory_space": "private",
    }

    job_id = await broker.enqueue_source_import_drain(
        "source_import:run-123",
        organization_id="org-123",
        principal_id="user-1",
        policy_context=policy_context,
        batch_size=25,
        promotion_preview_approved=False,
    )

    assert job_id == "source_import_drain:source_import:run-123"
    assert pool.calls[0][0] == "drain_source_import"
    assert pool.calls[0][1] == "source_import:run-123"
    assert pool.calls[0][2]["organization_id"] == "org-123"
    assert pool.calls[0][2]["principal_id"] == "user-1"
    assert pool.calls[0][2]["policy_context"] == policy_context
    assert pool.calls[0][2]["batch_size"] == 25
    assert pool.calls[0][2]["promotion_preview_approved"] is False
    assert pool.delete.await_args_list[-1].args == (
        "arq:result:source_import_drain:source_import:run-123",
    )


@pytest.mark.asyncio
async def test_enqueue_raw_promotion_indexes_org_scoped_job() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)
    raw_memory_ids = ["raw-a", "raw-b"]

    job_id = await broker.enqueue_raw_promotion(
        "org-123",
        raw_memory_ids=raw_memory_ids,
        limit=50,
        force=True,
    )

    expected_job_id = raw_promotion_job_id("org-123", raw_memory_ids=raw_memory_ids)
    assert job_id == expected_job_id
    assert pool.calls[0][0] == "promote_raw_captures"
    assert pool.calls[0][1] == "org-123"
    assert pool.calls[0][2]["raw_memory_ids"] == raw_memory_ids
    assert pool.calls[0][2]["limit"] == 50
    assert pool.calls[0][2]["force"] is True
    assert pool.delete.await_args_list[-1].args == (f"arq:result:{expected_job_id}",)
    assert_recent_job_indexed(pool, expected_job_id)
    assert_recent_job_indexed(pool, job_id)


@pytest.mark.asyncio
async def test_enqueue_backup_cleanup_indexes_recent_job() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_backup_cleanup(retention_days=7)

    assert job_id == "backup_cleanup"
    assert pool.calls[0][0] == "cleanup_old_backups"
    assert pool.calls[0][2]["retention_days"] == 7
    assert pool.delete.await_args_list[-1].args == ("arq:result:backup_cleanup",)
    assert_recent_job_indexed(pool, "backup_cleanup")


@pytest.mark.asyncio
async def test_enqueue_create_learning_procedure_indexes_recent_job() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_create_learning_procedure(
        {"id": "task-123", "title": "Ship the thing"},
        "org-123",
    )

    assert job_id == "learning_procedure:task-123"
    assert pool.calls[0][0] == "create_learning_procedure"
    assert pool.calls[0][1] == {"id": "task-123", "title": "Ship the thing"}
    assert pool.calls[0][2] == {"_job_id": "learning_procedure:task-123"}
    assert pool.extra_args[0] == ("org-123",)
    assert_recent_job_indexed(pool, "learning_procedure:task-123")


@pytest.mark.asyncio
async def test_enqueue_create_learning_jobs_include_policy_payload() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)
    policy_context = {
        "actor_user_id": "user-1",
        "memory_space": "project",
        "scope_key": "project-1",
    }

    job_id = await broker.enqueue_create_learning_episode(
        {"id": "task-123", "title": "Ship the thing"},
        "org-123",
        policy_context=policy_context,
    )

    assert job_id == "learning_episode:task-123"
    assert pool.calls[0][0] == "create_learning_episode"
    assert pool.calls[0][2]["policy_context"] == policy_context
    assert_recent_job_indexed(pool, "learning_episode:task-123")


@pytest.mark.asyncio
async def test_enqueue_memory_extraction_uses_source_scoped_job_id() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_memory_extraction(
        [{"id": "session-original", "entity_type": "session", "content": "memory"}],
        "org-123",
        created_source_ids=["session-created"],
        max_entities_per_source=4,
        max_source_chars=2000,
        max_concurrent=1,
        max_tokens=512,
    )

    assert job_id.startswith("extract_memory:")
    assert pool.calls[0][0] == "extract_memory_entities"
    assert pool.calls[0][1] == [
        {"id": "session-original", "entity_type": "session", "content": "memory"}
    ]
    assert pool.calls[0][2]["created_source_ids"] == ["session-created"]
    assert pool.calls[0][2]["max_entities_per_source"] == 4
    assert pool.calls[0][2]["max_source_chars"] == 2000
    assert pool.calls[0][2]["max_concurrent"] == 1
    assert pool.calls[0][2]["max_tokens"] == 512
    assert pool.extra_args[0] == ("org-123",)
    assert_recent_job_indexed(pool, job_id)


@pytest.mark.asyncio
async def test_enqueue_consolidation_uses_org_scoped_job_id() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_consolidation("org-123")

    assert job_id == "consolidate:org-123"
    assert pool.calls[0][0] == "consolidate_org"
    assert pool.calls[0][1] == "org-123"
    assert pool.calls[0][2]["similarity_threshold"] == 0.90
    assert pool.calls[0][2]["max_merges_per_run"] == 50
    assert pool.delete.await_args_list[-1].args == ("arq:result:consolidate:org-123",)
    assert_recent_job_indexed(pool, "consolidate:org-123")


@pytest.mark.asyncio
async def test_enqueue_priority_decay_uses_org_scoped_job_id() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_priority_decay("org-123")

    assert job_id == "priority_decay:org-123"
    assert pool.calls[0][0] == "priority_decay"
    assert pool.calls[0][1] == "org-123"
    assert pool.calls[0][2]["min_age_days"] == 180
    assert pool.calls[0][2]["max_archives_per_run"] == 100
    assert pool.delete.await_args_list[-1].args == ("arq:result:priority_decay:org-123",)
    assert_recent_job_indexed(pool, "priority_decay:org-123")


@pytest.mark.asyncio
async def test_enqueue_reflection_dream_cycle_uses_org_scoped_job_id() -> None:
    pool = RecordingEnqueuePool()
    broker = make_broker(pool)

    job_id = await broker.enqueue_reflection_dream_cycle(
        "org-123",
        dry_run=True,
        source_limit=3,
        candidate_limit=7,
        archive_exceptions=False,
        confidence_threshold=0.91,
    )

    assert job_id == "reflection_dream:org-123"
    assert pool.calls[0][0] == "run_reflection_dream_cycle"
    assert pool.calls[0][1] == "org-123"
    assert pool.calls[0][2]["dry_run"] is True
    assert pool.calls[0][2]["source_limit"] == 3
    assert pool.calls[0][2]["candidate_limit"] == 7
    assert pool.calls[0][2]["archive_exceptions"] is False
    assert pool.calls[0][2]["confidence_threshold"] == 0.91
    assert pool.delete.await_args_list[-1].args == ("arq:result:reflection_dream:org-123",)
    assert_recent_job_indexed(pool, "reflection_dream:org-123")
