from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import sibyl.coordination.broker as broker_module
from sibyl.config import settings
from sibyl.coordination._local.broker import LOCAL_BROKER_ERROR, LocalQueueBroker
from sibyl.coordination._local.events import LocalEventBus
from sibyl.coordination._local.locks import LocalLockManager
from sibyl.coordination._local.pending import LocalPendingRegistry
from sibyl.coordination.broker import JobInfo, JobStatus


@pytest.mark.asyncio
async def test_local_event_bus_fans_out_to_subscribers() -> None:
    bus = LocalEventBus()
    subscriber = AsyncMock()

    await bus.connect()
    await bus.subscribe(subscriber)
    await bus.publish("entity_updated", {"id": "entity_123"}, "org_456")

    subscriber.assert_awaited_once_with("entity_updated", {"id": "entity_123"}, "org_456")


@pytest.mark.asyncio
async def test_local_lock_manager_serializes_same_entity() -> None:
    manager = LocalLockManager()
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first_worker() -> None:
        async with manager.lock("org_123", "entity_456"):
            order.append("first_start")
            entered_first.set()
            await release_first.wait()
            order.append("first_end")

    async def second_worker() -> None:
        await entered_first.wait()
        async with manager.lock("org_123", "entity_456"):
            order.append("second")

    first_task = asyncio.create_task(first_worker())
    second_task = asyncio.create_task(second_worker())

    await entered_first.wait()
    await asyncio.sleep(0)
    assert order == ["first_start"]

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first_start", "first_end", "second"]


@pytest.mark.asyncio
async def test_local_pending_registry_expires_pending_state_and_operations() -> None:
    registry = LocalPendingRegistry()

    await registry.mark_pending("task_123", "create_entity:task_123", "task", "org_456")
    await registry.queue_pending_operation(
        "task_123",
        "add_note",
        {"content": "Test note"},
        "user_789",
    )

    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    registry._pending_expires["task_123"] = expired_at
    registry._operations_expires["task_123"] = expired_at

    assert await registry.is_pending("task_123") is None
    assert await registry.get_pending_operations("task_123") == []


@pytest.mark.asyncio
async def test_local_queue_broker_reports_degraded_health() -> None:
    broker = LocalQueueBroker()

    assert await broker.health() == {
        "status": "degraded",
        "error": LOCAL_BROKER_ERROR,
        "queue_healthy": False,
        "worker_healthy": False,
        "queue_depth": 0,
    }


@pytest.mark.asyncio
async def test_local_queue_broker_executes_local_jobs_and_reports_health() -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def crawl_source(
        ctx: dict[str, object],
        source_id: str,
        *,
        organization_id: str | None = None,
        max_pages: int = 100,
        max_depth: int = 3,
        generate_embeddings: bool = True,
    ) -> dict[str, object]:
        calls.append(
            (
                "crawl_source",
                (source_id,),
                {
                    "organization_id": organization_id,
                    "max_pages": max_pages,
                    "max_depth": max_depth,
                    "generate_embeddings": generate_embeddings,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"source_id": source_id, "ok": True}

    async def create_entity(
        ctx: dict[str, object],
        entity_data: dict[str, object],
        entity_type: str,
        group_id: str,
        relationships: list[dict[str, object]] | None = None,
        auto_link_params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            (
                "create_entity",
                (entity_data, entity_type, group_id),
                {
                    "relationships": relationships,
                    "auto_link_params": auto_link_params,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"entity_id": entity_data["id"], "ok": True}

    async def update_task(
        ctx: dict[str, object],
        task_id: str,
        updates: dict[str, object],
        group_id: str,
        *,
        epic_id: str | None = None,
        new_status: str | None = None,
        add_depends_on: list[str] | None = None,
        remove_depends_on: list[str] | None = None,
    ) -> dict[str, object]:
        calls.append(
            (
                "update_task",
                (task_id, updates, group_id),
                {
                    "epic_id": epic_id,
                    "new_status": new_status,
                    "add_depends_on": add_depends_on,
                    "remove_depends_on": remove_depends_on,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"task_id": task_id, "ok": True}

    async def run_backup(
        ctx: dict[str, object],
        organization_id: str,
        *,
        include_database_dump: bool = True,
        include_graph: bool = True,
        backup_id: str | None = None,
    ) -> dict[str, object]:
        calls.append(
            (
                "run_backup",
                (organization_id,),
                {
                    "include_database_dump": include_database_dump,
                    "include_graph": include_graph,
                    "backup_id": backup_id,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"backup_id": backup_id, "ok": True}

    async def consolidate_org(
        ctx: dict[str, object],
        group_id: str,
        similarity_threshold: float = 0.90,
        max_merges_per_run: int = 50,
    ) -> dict[str, object]:
        calls.append(
            (
                "consolidate_org",
                (group_id,),
                {
                    "similarity_threshold": similarity_threshold,
                    "max_merges_per_run": max_merges_per_run,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"group_id": group_id, "ok": True}

    async def priority_decay(
        ctx: dict[str, object],
        group_id: str,
        *,
        min_age_days: int = 180,
        max_archives_per_run: int = 100,
    ) -> dict[str, object]:
        calls.append(
            (
                "priority_decay",
                (group_id,),
                {
                    "min_age_days": min_age_days,
                    "max_archives_per_run": max_archives_per_run,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"group_id": group_id, "archived": max_archives_per_run}

    async def run_reflection_dream_cycle(
        ctx: dict[str, object],
        group_id: str,
        *,
        dry_run: bool = False,
        source_limit: int = 20,
        candidate_limit: int = 50,
        archive_exceptions: bool = True,
        confidence_threshold: float | None = None,
    ) -> dict[str, object]:
        calls.append(
            (
                "run_reflection_dream_cycle",
                (group_id,),
                {
                    "dry_run": dry_run,
                    "source_limit": source_limit,
                    "candidate_limit": candidate_limit,
                    "archive_exceptions": archive_exceptions,
                    "confidence_threshold": confidence_threshold,
                    "ctx_has_start_time": "start_time" in ctx,
                },
            )
        )
        return {"group_id": group_id, "dry_run": dry_run, "ok": True}

    broker = LocalQueueBroker(
        functions={
            "crawl_source": crawl_source,
            "create_entity": create_entity,
            "update_task": update_task,
            "run_backup": run_backup,
            "consolidate_org": consolidate_org,
            "priority_decay": priority_decay,
            "run_reflection_dream_cycle": run_reflection_dream_cycle,
        },
        max_concurrency=1,
        result_ttl_seconds=60,
    )

    await broker.startup()
    health = await broker.health()
    assert health["status"] == "healthy"
    assert health["queue_healthy"] is True
    assert health["worker_healthy"] is True

    with patch("sibyl.jobs.pending.mark_pending", AsyncMock()) as mark_pending:
        crawl_job_id = await broker.enqueue_crawl("source_123", organization_id="org_456")
        entity_job_id = await broker.enqueue_create_entity(
            "entity_123",
            {"id": "entity_123", "name": "Entity"},
            "task",
            "org_456",
        )
        task_job_id = await broker.enqueue_update_task(
            "task_123",
            {"status": "doing"},
            "org_456",
            new_status="doing",
        )
        backup_job_id = await broker.enqueue_backup("org_456", backup_id="backup_123")
        consolidation_job_id = await broker.enqueue_consolidation("org_456")
        priority_decay_job_id = await broker.enqueue_priority_decay(
            "org_456",
            min_age_days=90,
            max_archives_per_run=12,
        )
        reflection_job_id = await broker.enqueue_reflection_dream_cycle(
            "org_456",
            dry_run=True,
            source_limit=3,
            candidate_limit=7,
            archive_exceptions=False,
            confidence_threshold=0.91,
        )

        mark_pending.assert_awaited_once_with(
            "entity_123",
            entity_job_id,
            "task",
            "org_456",
        )

    crawl_info = await _wait_for_job_status(broker, crawl_job_id, JobStatus.COMPLETE)
    entity_info = await _wait_for_job_status(broker, entity_job_id, JobStatus.COMPLETE)
    task_info = await _wait_for_job_status(broker, task_job_id, JobStatus.COMPLETE)
    backup_info = await _wait_for_job_status(broker, backup_job_id, JobStatus.COMPLETE)
    consolidation_info = await _wait_for_job_status(
        broker,
        consolidation_job_id,
        JobStatus.COMPLETE,
    )
    priority_decay_info = await _wait_for_job_status(
        broker,
        priority_decay_job_id,
        JobStatus.COMPLETE,
    )
    reflection_info = await _wait_for_job_status(broker, reflection_job_id, JobStatus.COMPLETE)

    assert crawl_info.result == {"source_id": "source_123", "ok": True}
    assert entity_info.result == {"entity_id": "entity_123", "ok": True}
    assert task_info.result == {"task_id": "task_123", "ok": True}
    assert backup_info.result == {"backup_id": "backup_123", "ok": True}
    assert consolidation_info.result == {"group_id": "org_456", "ok": True}
    assert priority_decay_info.result == {"group_id": "org_456", "archived": 12}
    assert reflection_info.result == {"group_id": "org_456", "dry_run": True, "ok": True}
    assert [call[0] for call in calls] == [
        "crawl_source",
        "create_entity",
        "update_task",
        "run_backup",
        "consolidate_org",
        "priority_decay",
        "run_reflection_dream_cycle",
    ]
    assert all(call[2]["ctx_has_start_time"] is True for call in calls)

    await broker.shutdown()


@pytest.mark.asyncio
async def test_local_queue_broker_force_reruns_completed_job() -> None:
    calls: list[str] = []

    async def crawl_source(
        _ctx: dict[str, object],
        source_id: str,
        *,
        organization_id: str | None = None,
        max_pages: int = 100,
        max_depth: int = 3,
        generate_embeddings: bool = True,
    ) -> dict[str, object]:
        del organization_id, max_pages, max_depth, generate_embeddings
        calls.append(source_id)
        return {"source_id": source_id, "calls": len(calls)}

    broker = LocalQueueBroker(
        functions={"crawl_source": crawl_source},
        max_concurrency=1,
        result_ttl_seconds=60,
    )

    await broker.startup()

    first_job_id = await broker.enqueue_crawl("source_123")
    first_info = await _wait_for_job_status(broker, first_job_id, JobStatus.COMPLETE)
    same_job_id = await broker.enqueue_crawl("source_123")
    same_info = await _wait_for_job_status(broker, same_job_id, JobStatus.COMPLETE)
    forced_job_id = await broker.enqueue_crawl("source_123", force=True)
    forced_info = await _wait_for_job_status(broker, forced_job_id, JobStatus.COMPLETE)

    assert first_job_id == "crawl:source_123"
    assert same_job_id == first_job_id
    assert forced_job_id == first_job_id
    assert first_info.result == {"source_id": "source_123", "calls": 1}
    assert same_info.result == {"source_id": "source_123", "calls": 1}
    assert forced_info.result == {"source_id": "source_123", "calls": 2}
    assert calls == ["source_123", "source_123"]

    await broker.shutdown()


@pytest.mark.asyncio
async def test_local_queue_broker_cancels_queued_jobs_and_best_effort_running_jobs() -> None:
    release = asyncio.Event()

    async def sync_source(
        _ctx: dict[str, object],
        source_id: str,
        *,
        _organization_id: str | None = None,
    ) -> dict[str, object]:
        await release.wait()
        return {"source_id": source_id, "ok": True}

    broker = LocalQueueBroker(
        functions={"sync_source": sync_source},
        max_concurrency=1,
        result_ttl_seconds=60,
    )

    await broker.startup()

    running_job_id = await broker.enqueue_sync("source_running")
    await _wait_for_job_status(broker, running_job_id, JobStatus.IN_PROGRESS)
    queued_job_id = await broker.enqueue_sync("source_queued")

    assert await broker.cancel_job(queued_job_id) is True
    queued_info = await broker.get_job_status(queued_job_id)
    assert queued_info.status == JobStatus.NOT_FOUND

    assert await broker.cancel_job(running_job_id) is False
    running_info = await _wait_for_job_status(broker, running_job_id, JobStatus.NOT_FOUND)
    assert running_info.status == JobStatus.NOT_FOUND

    await broker.shutdown()


@pytest.mark.asyncio
async def test_local_queue_broker_preserves_learning_policy_context() -> None:
    calls: list[dict[str, object]] = []

    async def create_learning_episode(
        _ctx: dict[str, object],
        task_data: dict[str, object],
        group_id: str,
        *,
        policy_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append(
            {
                "task_data": task_data,
                "group_id": group_id,
                "policy_context": policy_context,
            }
        )
        return {"ok": True}

    broker = LocalQueueBroker(
        functions={"create_learning_episode": create_learning_episode},
        max_concurrency=1,
        result_ttl_seconds=60,
    )
    policy_context = {
        "actor_user_id": "user-1",
        "organization_id": "org-1",
        "memory_space": "project",
        "scope_key": "project-1",
    }

    await broker.startup()
    job_id = await broker.enqueue_create_learning_episode(
        {"id": "task-1", "learnings": "keep queue payload intact"},
        "org-1",
        policy_context=policy_context,
    )
    info = await _wait_for_job_status(broker, job_id, JobStatus.COMPLETE)
    await broker.shutdown()

    assert info.result == {"ok": True}
    assert calls == [
        {
            "task_data": {"id": "task-1", "learnings": "keep queue payload intact"},
            "group_id": "org-1",
            "policy_context": policy_context,
        }
    ]


@pytest.mark.parametrize("configured_backend", ["auto", "local"])
def test_surreal_queue_backend_uses_local_broker(
    monkeypatch: pytest.MonkeyPatch,
    configured_backend: str,
) -> None:
    monkeypatch.setattr(settings, "store", "surreal")
    monkeypatch.setattr(settings, "coordination_backend", configured_backend)
    broker_module._broker = None
    broker_module._broker_backend = None

    try:
        assert broker_module.get_queue_backend() == "local"
        assert isinstance(broker_module.get_broker(), LocalQueueBroker)
    finally:
        broker_module._broker = None
        broker_module._broker_backend = None


async def _wait_for_job_status(
    broker: LocalQueueBroker,
    job_id: str,
    expected_status: JobStatus,
) -> JobInfo:
    for _ in range(100):
        info = await broker.get_job_status(job_id)
        if info.status == expected_status:
            return info
        await asyncio.sleep(0.01)

    pytest.fail(f"Timed out waiting for {job_id} to reach {expected_status.value}")
