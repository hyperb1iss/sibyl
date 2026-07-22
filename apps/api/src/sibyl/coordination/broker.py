"""Job broker protocols and backend resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from arq.connections import RedisSettings

from sibyl.coordination import get_coordination_backend

RECENT_JOB_INDEX_KEY = "sibyl:jobs:recent"
RECENT_JOB_INDEX_LIMIT = 1000
QueueBackend = Literal["local", "redis"]

_JOB_ORG_ARGUMENT_INDEX = {
    "create_entity": 2,
    "update_entity": 3,
    "backfill_entity_embeddings": 1,
    "project_memory_batch": 1,
    "extract_memory_entities": 1,
    "distill_operational_experience_notes": 1,
    "consolidate_org": 0,
    "priority_decay": 0,
    "run_reflection_dream_cycle": 0,
}


def job_organization_id(
    function: str,
    args: tuple[Any, ...] | list[Any],
    kwargs: dict[str, Any],
) -> str | None:
    if function in {"crawl_source", "sync_source"}:
        organization_id = kwargs.get("organization_id")
        return str(organization_id) if organization_id is not None else None
    index = _JOB_ORG_ARGUMENT_INDEX.get(function)
    if index is None or index >= len(args):
        return None
    return str(args[index])


class JobStatus(StrEnum):
    """Job status enum matching arq statuses."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    NOT_FOUND = "not_found"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"


@dataclass
class JobInfo:
    """Information about a job."""

    job_id: str
    function: str
    status: JobStatus
    enqueue_time: datetime | None = None
    start_time: datetime | None = None
    finish_time: datetime | None = None
    result: Any = None
    error: str | None = None
    organization_id: str | None = None
    args: tuple[Any, ...] | None = None
    kwargs: dict[str, Any] | None = None


def memory_projection_job_id(
    sources_data: list[dict[str, Any]],
    group_id: str,
    *,
    created_source_ids: list[str] | None = None,
) -> str:
    source_ids = list(created_source_ids or [])
    if not source_ids:
        source_ids = [str(source.get("id") or "") for source in sources_data]
    digest = sha256("|".join([group_id, *source_ids]).encode()).hexdigest()[:16]
    return f"project_memory:{digest}"


def memory_extraction_job_id(
    sources_data: list[dict[str, Any]],
    group_id: str,
    *,
    created_source_ids: list[str] | None = None,
) -> str:
    source_ids = list(created_source_ids or [])
    if not source_ids:
        source_ids = [str(source.get("id") or "") for source in sources_data]
    digest = sha256("|".join([group_id, *source_ids]).encode()).hexdigest()[:16]
    return f"extract_memory:{digest}"


def operational_note_distillation_job_id(
    experience_data: dict[str, Any],
    group_id: str,
    *,
    content_hash: str,
) -> str:
    source_id = str(experience_data.get("source_id") or "")
    payload = json.dumps(
        {
            "group_id": group_id,
            "source_id": source_id,
            "content_hash": content_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(payload.encode()).hexdigest()[:16]
    return f"distill_operational_experience:{digest}"


def entity_embedding_job_id(
    entities_data: list[dict[str, Any]],
    group_id: str,
    *,
    relationships: list[dict[str, Any]] | None = None,
    completion_manifest: dict[str, Any] | None = None,
) -> str:
    manifest_metadata: dict[str, Any] = {}
    if isinstance(completion_manifest, dict):
        candidate_metadata = completion_manifest.get("metadata")
        if isinstance(candidate_metadata, dict):
            manifest_metadata = dict(candidate_metadata)
    entity_inputs = [
        {
            "id": str(entity.get("id") or ""),
            "entity_type": str(entity.get("entity_type") or ""),
            "name": str(entity.get("name") or entity.get("title") or ""),
            "description": str(entity.get("description") or ""),
            "content": str(entity.get("content") or ""),
            "summary": str(
                (entity.get("metadata") or {}).get("summary") or ""
                if isinstance(entity.get("metadata"), dict)
                else ""
            ),
        }
        for entity in entities_data
    ]
    relationship_inputs = [
        {
            "id": str(relationship.get("id") or ""),
            "source_id": str(relationship.get("source_id") or ""),
            "target_id": str(relationship.get("target_id") or ""),
            "relationship_type": str(
                relationship.get("relationship_type") or relationship.get("type") or ""
            ),
            "fact": str(
                (relationship.get("metadata") or {}).get("fact")
                if isinstance(relationship.get("metadata"), dict)
                else ""
            ),
        }
        for relationship in relationships or ()
    ]
    payload = json.dumps(
        {
            "group_id": group_id,
            "entities": sorted(entity_inputs, key=lambda item: item["id"]),
            "relationships": sorted(relationship_inputs, key=lambda item: item["id"]),
            "completion_manifest": {
                "id": str(completion_manifest.get("id") or ""),
                "content_hash": str(manifest_metadata.get("operational_content_hash") or ""),
            }
            if completion_manifest
            else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(payload.encode()).hexdigest()[:16]
    return f"embed_entities:{digest}"


def raw_promotion_job_id(
    organization_id: str,
    *,
    raw_memory_ids: list[str] | None = None,
) -> str:
    raw_ids = list(raw_memory_ids or [])
    digest_source = "|".join([organization_id, *raw_ids]) if raw_ids else organization_id
    digest = sha256(digest_source.encode()).hexdigest()[:16]
    return f"raw_promotion:{digest}"


def raw_capture_changefeed_job_id(organization_id: str) -> str:
    digest = sha256(organization_id.encode()).hexdigest()[:16]
    return f"raw_capture_changefeed:{digest}"


class QueueBroker(Protocol):
    """Backend contract for job queue coordination."""

    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def health(self) -> dict[str, Any]: ...

    def get_redis_settings(self) -> RedisSettings: ...

    async def get_pool(self) -> Any: ...

    async def close_pool(self) -> None: ...

    async def enqueue_crawl(
        self,
        source_id: str | UUID,
        *,
        organization_id: str | None = None,
        max_pages: int = 100,
        max_depth: int = 3,
        generate_embeddings: bool = True,
        force: bool = False,
    ) -> str: ...

    async def enqueue_sync(
        self,
        source_id: str | UUID,
        *,
        organization_id: str | None = None,
    ) -> str: ...

    async def enqueue_create_entity(
        self,
        entity_id: str,
        entity_data: dict[str, Any],
        entity_type: str,
        group_id: str,
        relationships: list[dict[str, Any]] | None = None,
        auto_link_params: dict[str, Any] | None = None,
        generate_embeddings: bool = True,
    ) -> str: ...

    async def enqueue_update_entity(
        self,
        entity_id: str,
        updates: dict[str, Any],
        entity_type: str,
        group_id: str,
    ) -> str: ...

    async def enqueue_memory_projection(
        self,
        sources_data: list[dict[str, Any]],
        group_id: str,
        *,
        created_source_ids: list[str] | None = None,
    ) -> str: ...

    async def enqueue_memory_extraction(
        self,
        sources_data: list[dict[str, Any]],
        group_id: str,
        *,
        created_source_ids: list[str] | None = None,
        max_entities_per_source: int = 4,
        max_source_chars: int = 12_000,
        max_concurrent: int = 2,
        max_tokens: int = 8192,
    ) -> str: ...

    async def enqueue_operational_note_distillation(
        self,
        experience_data: dict[str, Any],
        group_id: str,
        *,
        content_hash: str,
        created_by: str | None,
        max_tokens: int = 2_048,
    ) -> str: ...

    async def enqueue_entity_embedding_backfill(
        self,
        entities_data: list[dict[str, Any]],
        group_id: str,
        *,
        relationships: list[dict[str, Any]] | None = None,
        completion_manifest: dict[str, Any] | None = None,
    ) -> str: ...

    async def enqueue_create_learning_episode(
        self,
        task_data: dict[str, Any],
        group_id: str,
        *,
        policy_context: dict[str, Any] | None = None,
    ) -> str: ...

    async def enqueue_create_learning_procedure(
        self,
        task_data: dict[str, Any],
        group_id: str,
        *,
        policy_context: dict[str, Any] | None = None,
    ) -> str: ...

    async def enqueue_update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        group_id: str,
        epic_id: str | None = None,
        new_status: str | None = None,
        add_depends_on: list[str] | None = None,
        remove_depends_on: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> str: ...

    async def enqueue_source_import_drain(
        self,
        import_id: str,
        *,
        organization_id: str,
        principal_id: str,
        policy_context: dict[str, Any],
        batch_size: int | None = None,
        promotion_preview_approved: bool | None = None,
    ) -> str: ...

    async def enqueue_raw_promotion(
        self,
        organization_id: str,
        *,
        raw_memory_ids: list[str] | None = None,
        limit: int = 100,
        force: bool = False,
    ) -> str: ...

    async def enqueue_raw_capture_changefeed_poll(
        self,
        organization_id: str,
        *,
        limit: int = 100,
    ) -> str: ...

    async def enqueue_scheduled_job(self, function: str) -> str: ...

    async def get_job_status(self, job_id: str) -> JobInfo: ...

    async def list_jobs(self, *, function: str | None = None, limit: int = 50) -> list[JobInfo]: ...

    async def cancel_job(self, job_id: str) -> bool: ...

    async def enqueue_backup(
        self,
        organization_id: str,
        *,
        include_database_dump: bool = True,
        include_graph: bool = True,
        backup_id: str | None = None,
    ) -> str: ...

    async def enqueue_backup_cleanup(
        self,
        *,
        retention_days: int | None = None,
    ) -> str: ...

    async def enqueue_consolidation(
        self,
        group_id: str,
        *,
        similarity_threshold: float = 0.90,
        max_merges_per_run: int = 50,
    ) -> str: ...

    async def enqueue_priority_decay(
        self,
        group_id: str,
        *,
        min_age_days: int = 180,
        max_archives_per_run: int = 100,
    ) -> str: ...

    async def enqueue_reflection_dream_cycle(
        self,
        group_id: str,
        *,
        dry_run: bool = False,
        source_limit: int = 20,
        candidate_limit: int = 50,
        archive_exceptions: bool = True,
        confidence_threshold: float | None = None,
    ) -> str: ...


_broker: QueueBroker | None = None
_broker_backend: QueueBackend | None = None


def get_queue_backend() -> QueueBackend:
    """Return the queue backend used for job execution."""
    return get_coordination_backend()


def get_broker() -> QueueBroker:
    """Return the queue broker for the active coordination backend."""
    global _broker, _broker_backend  # noqa: PLW0603

    backend = get_queue_backend()
    if _broker is not None and _broker_backend == backend:
        return _broker

    broker: QueueBroker
    if backend == "redis":
        from sibyl.coordination._redis.broker import RedisQueueBroker

        broker = cast("QueueBroker", RedisQueueBroker())
    else:
        from sibyl.coordination._local.broker import LocalQueueBroker

        broker = cast("QueueBroker", LocalQueueBroker())

    _broker = broker
    _broker_backend = backend
    return broker
