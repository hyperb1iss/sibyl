"""Install apps/api runtime implementations for sibyl-core ports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

from sibyl_core.models import CrawlStatus, SourceType
from sibyl_core.runtime_ports import (
    install_audit_port,
    install_content_port,
    install_graph_link_port,
    install_queue_port,
)


def _normalize_pattern_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item]
    return [str(value)]


class ApiQueuePort:
    async def enqueue_create_entity(
        self,
        *,
        entity_id: str,
        entity_data: Mapping[str, Any],
        entity_type: str,
        group_id: str,
        relationships: Sequence[Mapping[str, Any]] | None,
        auto_link_params: Mapping[str, Any],
        generate_embeddings: bool = True,
    ) -> str:
        from sibyl.jobs.queue import enqueue_create_entity

        return await enqueue_create_entity(
            entity_id=entity_id,
            entity_data=dict(entity_data),
            entity_type=entity_type,
            group_id=group_id,
            relationships=[dict(relationship) for relationship in relationships or ()] or None,
            auto_link_params=dict(auto_link_params),
            generate_embeddings=generate_embeddings,
        )

    async def enqueue_update_task(
        self,
        entity_id: str,
        updates: Mapping[str, Any],
        organization_id: str,
    ) -> str:
        from sibyl.jobs.queue import enqueue_update_task

        return await enqueue_update_task(entity_id, dict(updates), organization_id)

    async def enqueue_create_learning_episode(
        self,
        task_data: Mapping[str, Any],
        organization_id: str,
        *,
        policy_context: Mapping[str, Any],
    ) -> str:
        from sibyl.jobs.queue import enqueue_create_learning_episode

        return await enqueue_create_learning_episode(
            dict(task_data),
            organization_id,
            policy_context=dict(policy_context),
        )

    async def enqueue_create_learning_procedure(
        self,
        task_data: Mapping[str, Any],
        organization_id: str,
        *,
        policy_context: Mapping[str, Any],
    ) -> str:
        from sibyl.jobs.queue import enqueue_create_learning_procedure

        return await enqueue_create_learning_procedure(
            dict(task_data),
            organization_id,
            policy_context=dict(policy_context),
        )

    async def enqueue_crawl(
        self,
        source_id: str,
        *,
        organization_id: str,
        max_pages: int,
        max_depth: int,
        generate_embeddings: bool,
        force: bool,
    ) -> str:
        from sibyl.jobs.queue import enqueue_crawl

        return await enqueue_crawl(
            source_id,
            organization_id=organization_id,
            max_pages=max_pages,
            max_depth=max_depth,
            generate_embeddings=generate_embeddings,
            force=force,
        )

    async def enqueue_sync(self, source_id: str, *, organization_id: str) -> str:
        from sibyl.jobs.queue import enqueue_sync

        return await enqueue_sync(source_id, organization_id=organization_id)


class ApiContentPort:
    def read_session(self) -> AbstractAsyncContextManager[Any]:
        from sibyl.persistence.content_runtime import get_content_read_session

        return get_content_read_session()

    async def create_or_get_crawl_source(
        self,
        *,
        url: str,
        depth: int,
        data: Mapping[str, object],
        organization_id: str,
    ) -> tuple[str, bool]:
        from sibyl.persistence.content_runtime import (
            create_crawl_source_record,
            list_sources_for_graph_linking,
        )

        normalized_url = url.rstrip("/")
        source_name = str(data.get("name") or normalized_url.split("//")[-1].split("/")[0])
        source_type = str(data.get("source_type") or "website").lower()
        org_uuid = UUID(organization_id)

        try:
            source_type_enum = SourceType(source_type)
        except ValueError:
            source_type_enum = SourceType.WEBSITE

        include_patterns = _normalize_pattern_list(
            data.get("include_patterns") or data.get("patterns")
        )
        exclude_patterns = _normalize_pattern_list(
            data.get("exclude_patterns") or data.get("exclude")
        )

        async with self.read_session() as session:
            sources = await list_sources_for_graph_linking(
                session,
                organization_id=org_uuid,
                source_id=None,
            )
            for source in sources:
                if source.url.rstrip("/") == normalized_url:
                    return str(source.id), False

            source = await create_crawl_source_record(
                session,
                name=source_name,
                url=normalized_url,
                organization_id=org_uuid,
                source_type=source_type_enum,
                description=str(data["description"]) if data.get("description") else None,
                crawl_depth=max(0, min(int(depth), 10)),
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
            )
            return str(source.id), True

    async def crawl_source_exists(self, *, source_id: str, organization_id: str) -> bool:
        from sibyl.persistence.content_runtime import get_org_crawl_source

        async with self.read_session() as session:
            source = await get_org_crawl_source(
                session,
                source_id=UUID(source_id),
                organization_id=UUID(organization_id),
            )
            return source is not None

    async def list_crawl_source_ids(self, *, organization_id: str) -> list[str]:
        from sibyl.persistence.content_runtime import list_sources_for_graph_linking

        async with self.read_session() as session:
            sources = await list_sources_for_graph_linking(
                session,
                organization_id=UUID(organization_id),
                source_id=None,
            )
            return [str(source.id) for source in sources]

    async def mark_crawl_pending(
        self,
        *,
        source_id: str,
        organization_id: str,
        job_id: str,
    ) -> None:
        from sibyl.persistence.content_runtime import get_org_crawl_source, save_crawl_source_record

        async with self.read_session() as session:
            source = await get_org_crawl_source(
                session,
                source_id=UUID(source_id),
                organization_id=UUID(organization_id),
            )
            if source is None:
                return
            source.current_job_id = job_id
            source.crawl_status = CrawlStatus.PENDING
            source.last_error = None
            await save_crawl_source_record(session, source=source)

    async def list_unlinked_document_chunks(
        self,
        *,
        organization_id: str,
        source_id: str | None,
        limit: int,
    ) -> list[Any]:
        from sibyl.persistence.content_runtime import (
            list_sources_for_graph_linking,
            list_unlinked_source_chunks,
        )

        requested_source_id = UUID(source_id) if source_id else None
        async with self.read_session() as session:
            sources = await list_sources_for_graph_linking(
                session,
                organization_id=UUID(organization_id),
                source_id=requested_source_id,
            )
            chunks: list[Any] = []
            for source in sources:
                remaining = limit - len(chunks)
                if remaining <= 0:
                    break
                chunks.extend(
                    await list_unlinked_source_chunks(
                        session,
                        source_id=source.id,
                        limit=remaining,
                    )
                )
            return chunks


class ApiGraphLinkPort:
    async def process_chunks(
        self,
        *,
        graph_client: Any,
        organization_id: str,
        chunks: Sequence[Any],
        source_name: str,
        create_new_entities: bool,
    ) -> Any:
        from sibyl.crawler.graph_integration import GraphIntegrationService

        integration = GraphIntegrationService(
            graph_client,
            organization_id,
            create_new_entities=create_new_entities,
        )
        return await integration.process_chunks(list(chunks), source_name=source_name)


class ApiAuditPort:
    async def log_memory_audit_event(self, **kwargs: Any) -> None:
        from sibyl.persistence.auth_runtime import log_memory_audit_event

        await log_memory_audit_event(**kwargs)


def install_core_runtime_ports() -> None:
    install_queue_port(ApiQueuePort())
    install_content_port(ApiContentPort())
    install_graph_link_port(ApiGraphLinkPort())
    install_audit_port(ApiAuditPort())


__all__ = ["install_core_runtime_ports"]
