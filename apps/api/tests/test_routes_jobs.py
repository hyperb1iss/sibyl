"""Tests for job route visibility helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from sibyl.api.routes.jobs import (
    _job_visible_to_org,
    cancel_job,
    jobs_health,
    list_jobs,
    trigger_consolidation,
    trigger_priority_decay,
    trigger_reflection_dream_cycle,
)


class TestJobVisibility:
    @pytest.mark.asyncio
    async def test_job_metadata_checks_org_without_deserializing_arguments(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        visible = SimpleNamespace(
            organization_id=str(org.id),
            function="project_memory_batch",
        )
        hidden = SimpleNamespace(
            organization_id="00000000-0000-0000-0000-000000000999",
            function="project_memory_batch",
        )

        assert await _job_visible_to_org(visible, org=org) is True
        assert await _job_visible_to_org(hidden, org=org) is False

    @pytest.mark.asyncio
    async def test_source_jobs_use_embedded_org_metadata_without_db_lookup(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        job = SimpleNamespace(
            function="crawl_source",
            args=("00000000-0000-0000-0000-000000000222",),
            kwargs={"organization_id": str(org.id)},
        )

        assert await _job_visible_to_org(job, org=org, session=session) is True
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_jobs_hide_other_org_metadata_without_db_lookup(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        job = SimpleNamespace(
            function="sync_source",
            args=("00000000-0000-0000-0000-000000000222",),
            kwargs={"organization_id": "00000000-0000-0000-0000-000000000999"},
        )

        assert await _job_visible_to_org(job, org=org, session=session) is False
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_source_jobs_fall_back_to_db_lookup(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        job = SimpleNamespace(
            function="crawl_source",
            args=("00000000-0000-0000-0000-000000000222",),
            kwargs=None,
        )

        with patch(
            "sibyl.api.routes.jobs.get_crawl_source_by_id",
            AsyncMock(return_value=SimpleNamespace(organization_id=org.id)),
        ) as get_source:
            assert await _job_visible_to_org(job, org=org, session=session) is True

        get_source.assert_awaited_once_with(
            session,
            source_id=UUID("00000000-0000-0000-0000-000000000222"),
        )

    @pytest.mark.asyncio
    async def test_maintenance_jobs_use_group_id_argument(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        visible_consolidation = SimpleNamespace(
            function="consolidate_org",
            args=(str(org.id),),
            kwargs=None,
        )
        visible_reflection = SimpleNamespace(
            function="run_reflection_dream_cycle",
            args=(str(org.id),),
            kwargs=None,
        )
        hidden = SimpleNamespace(
            function="priority_decay",
            args=("00000000-0000-0000-0000-000000000999",),
            kwargs=None,
        )

        assert await _job_visible_to_org(visible_consolidation, org=org, session=session) is True
        assert await _job_visible_to_org(visible_reflection, org=org, session=session) is True
        assert await _job_visible_to_org(hidden, org=org, session=session) is False
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_backfill_jobs_use_group_id_argument(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        session = AsyncMock()
        visible = SimpleNamespace(
            function="backfill_entity_embeddings",
            args=([{"id": "session_1"}], str(org.id)),
            kwargs=None,
        )
        hidden = SimpleNamespace(
            function="backfill_entity_embeddings",
            args=(
                [{"id": "session_1"}],
                "00000000-0000-0000-0000-000000000999",
            ),
            kwargs=None,
        )

        assert await _job_visible_to_org(visible, org=org, session=session) is True
        assert await _job_visible_to_org(hidden, org=org, session=session) is False
        session.get.assert_not_called()


class TestListJobsRoute:
    @pytest.mark.asyncio
    async def test_list_jobs_batches_legacy_source_visibility_checks(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        visible_legacy_source_id = UUID("00000000-0000-0000-0000-000000000222")
        embedded_visible_source_id = UUID("00000000-0000-0000-0000-000000000444")
        invisible_legacy_source_id = UUID("00000000-0000-0000-0000-000000000333")
        embedded_hidden_source_id = UUID("00000000-0000-0000-0000-000000000555")

        jobs = [
            SimpleNamespace(
                job_id="crawl:legacy-visible",
                function="crawl_source",
                status=SimpleNamespace(value="queued"),
                enqueue_time=None,
                start_time=None,
                finish_time=None,
                error=None,
                args=(str(visible_legacy_source_id),),
                kwargs=None,
            ),
            SimpleNamespace(
                job_id="sync:legacy-hidden",
                function="sync_source",
                status=SimpleNamespace(value="queued"),
                enqueue_time=None,
                start_time=None,
                finish_time=None,
                error=None,
                args=(str(invisible_legacy_source_id),),
                kwargs=None,
            ),
            SimpleNamespace(
                job_id="crawl:embedded-visible",
                function="crawl_source",
                status=SimpleNamespace(value="queued"),
                enqueue_time=None,
                start_time=None,
                finish_time=None,
                error=None,
                args=(str(embedded_visible_source_id),),
                kwargs={"organization_id": str(org.id)},
            ),
            SimpleNamespace(
                job_id="crawl:embedded-hidden",
                function="crawl_source",
                status=SimpleNamespace(value="queued"),
                enqueue_time=None,
                start_time=None,
                finish_time=None,
                error=None,
                args=(str(embedded_hidden_source_id),),
                kwargs={"organization_id": "00000000-0000-0000-0000-000000000999"},
            ),
            SimpleNamespace(
                job_id="other",
                function="create_entity",
                status=SimpleNamespace(value="queued"),
                enqueue_time=None,
                start_time=None,
                finish_time=None,
                error=None,
                args=(),
                kwargs=None,
            ),
        ]

        resolve_visible_source_ids = AsyncMock(return_value={visible_legacy_source_id})

        with (
            patch("sibyl.jobs.queue.list_jobs", AsyncMock(return_value=jobs)),
            patch(
                "sibyl.api.routes.jobs._resolve_visible_source_ids",
                resolve_visible_source_ids,
            ),
        ):
            response = await list_jobs(org=org)

        resolve_visible_source_ids.assert_awaited_once_with(jobs, org=org)
        assert [job["job_id"] for job in response["jobs"]] == [
            "crawl:legacy-visible",
            "crawl:embedded-visible",
        ]
        assert response["total"] == 2


class TestJobsHealthRoute:
    @pytest.mark.asyncio
    async def test_jobs_health_reports_coordination_backend(self) -> None:
        with patch(
            "sibyl.coordination.get_coordination_health",
            AsyncMock(
                return_value={
                    "status": "healthy",
                    "backend": "redis",
                    "durable": True,
                    "queue_healthy": True,
                    "worker_healthy": True,
                    "queue_depth": 2,
                }
            ),
        ):
            response = await jobs_health()

        assert response["status"] == "healthy"
        assert response["backend"] == "redis"
        assert response["durable"] is True
        assert response["queue_depth"] == 2


class TestCancelJobRoute:
    @pytest.mark.asyncio
    async def test_cancel_job_preserves_not_found_for_invisible_job(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))
        job = SimpleNamespace(job_id="crawl:source-123")

        with (
            patch("sibyl.jobs.queue.get_job_status", AsyncMock(return_value=job)),
            patch("sibyl.api.routes.jobs._job_visible_to_org", AsyncMock(return_value=False)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await cancel_job("crawl:source-123", org=org)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Job not found: crawl:source-123"


class TestMaintenanceTriggers:
    @pytest.mark.asyncio
    async def test_trigger_consolidation_enqueues_for_current_org(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with patch(
            "sibyl.jobs.queue.enqueue_consolidation",
            AsyncMock(return_value="consolidate:00000000-0000-0000-0000-000000000111"),
        ) as enqueue:
            response = await trigger_consolidation(org=org)

        enqueue.assert_awaited_once_with("00000000-0000-0000-0000-000000000111")
        assert response == {
            "job_id": "consolidate:00000000-0000-0000-0000-000000000111",
            "function": "consolidate_org",
            "status": "queued",
            "message": "Consolidation run queued",
        }

    @pytest.mark.asyncio
    async def test_trigger_priority_decay_enqueues_for_current_org(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with patch(
            "sibyl.jobs.queue.enqueue_priority_decay",
            AsyncMock(return_value="priority_decay:00000000-0000-0000-0000-000000000111"),
        ) as enqueue:
            response = await trigger_priority_decay(org=org)

        enqueue.assert_awaited_once_with("00000000-0000-0000-0000-000000000111")
        assert response == {
            "job_id": "priority_decay:00000000-0000-0000-0000-000000000111",
            "function": "priority_decay",
            "status": "queued",
            "message": "Forgetting sweep queued",
        }

    @pytest.mark.asyncio
    async def test_trigger_reflection_dream_enqueues_for_current_org(self) -> None:
        org = SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111"))

        with patch(
            "sibyl.jobs.queue.enqueue_reflection_dream_cycle",
            AsyncMock(return_value="reflection_dream:00000000-0000-0000-0000-000000000111"),
        ) as enqueue:
            response = await trigger_reflection_dream_cycle(
                dry_run=True,
                source_limit=2,
                candidate_limit=5,
                archive_exceptions=False,
                org=org,
            )

        enqueue.assert_awaited_once_with(
            "00000000-0000-0000-0000-000000000111",
            dry_run=True,
            source_limit=2,
            candidate_limit=5,
            archive_exceptions=False,
        )
        assert response == {
            "job_id": "reflection_dream:00000000-0000-0000-0000-000000000111",
            "function": "run_reflection_dream_cycle",
            "status": "queued",
            "message": "Reflection dream cycle queued",
        }
