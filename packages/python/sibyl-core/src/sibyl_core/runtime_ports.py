"""Runtime ports installed by host applications around sibyl-core."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol


class RuntimePortUnavailable(RuntimeError):
    """Raised when a host-only runtime operation has no installed port."""


class QueuePort(Protocol):
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
    ) -> str: ...

    async def enqueue_entity_embedding_backfill(
        self,
        *,
        entities_data: Sequence[Mapping[str, Any]],
        group_id: str,
        relationships: Sequence[Mapping[str, Any]] | None,
    ) -> str: ...

    async def enqueue_update_task(
        self,
        entity_id: str,
        updates: Mapping[str, Any],
        organization_id: str,
    ) -> str: ...

    async def enqueue_create_learning_episode(
        self,
        task_data: Mapping[str, Any],
        organization_id: str,
        *,
        policy_context: Mapping[str, Any],
    ) -> str: ...

    async def enqueue_create_learning_procedure(
        self,
        task_data: Mapping[str, Any],
        organization_id: str,
        *,
        policy_context: Mapping[str, Any],
    ) -> str: ...

    async def enqueue_crawl(
        self,
        source_id: str,
        *,
        organization_id: str,
        max_pages: int,
        max_depth: int,
        generate_embeddings: bool,
        force: bool,
    ) -> str: ...

    async def enqueue_sync(self, source_id: str, *, organization_id: str) -> str: ...


class ContentPort(Protocol):
    def read_session(self) -> AbstractAsyncContextManager[Any]: ...

    async def create_or_get_crawl_source(
        self,
        *,
        url: str,
        depth: int,
        data: Mapping[str, object],
        organization_id: str,
    ) -> tuple[str, bool]: ...

    async def crawl_source_exists(self, *, source_id: str, organization_id: str) -> bool: ...

    async def list_crawl_source_ids(self, *, organization_id: str) -> list[str]: ...

    async def mark_crawl_pending(
        self,
        *,
        source_id: str,
        organization_id: str,
        job_id: str,
    ) -> None: ...

    async def list_unlinked_document_chunks(
        self,
        *,
        organization_id: str,
        source_id: str | None,
        limit: int,
    ) -> list[Any]: ...


class GraphLinkPort(Protocol):
    async def process_chunks(
        self,
        *,
        graph_client: Any,
        organization_id: str,
        chunks: Sequence[Any],
        source_name: str,
        create_new_entities: bool,
    ) -> Any: ...


class AuditPort(Protocol):
    async def log_memory_audit_event(
        self,
        *,
        action: str,
        user_id: str | None,
        organization_id: str,
        request: Any,
        memory_scope: str | None,
        scope_key: str | None,
        project_id: str | None,
        source_surface: str,
        source_ids: Sequence[str],
        policy_allowed: bool,
        policy_reason: str,
        details: Mapping[str, Any],
    ) -> None: ...


class _NoAuditPort:
    async def log_memory_audit_event(self, **_kwargs: Any) -> None:
        return None


_queue_port: QueuePort | None = None
_content_port: ContentPort | None = None
_graph_link_port: GraphLinkPort | None = None
_audit_port: AuditPort = _NoAuditPort()


def install_queue_port(port: QueuePort) -> None:
    global _queue_port
    _queue_port = port


def install_content_port(port: ContentPort) -> None:
    global _content_port
    _content_port = port


def install_graph_link_port(port: GraphLinkPort) -> None:
    global _graph_link_port
    _graph_link_port = port


def install_audit_port(port: AuditPort) -> None:
    global _audit_port
    _audit_port = port


def reset_runtime_ports() -> None:
    global _audit_port, _content_port, _graph_link_port, _queue_port
    _queue_port = None
    _content_port = None
    _graph_link_port = None
    _audit_port = _NoAuditPort()


def get_queue_port() -> QueuePort:
    if _queue_port is None:
        raise RuntimePortUnavailable("queue runtime port is not installed")
    return _queue_port


def get_content_port() -> ContentPort:
    if _content_port is None:
        raise RuntimePortUnavailable("content runtime port is not installed")
    return _content_port


def get_graph_link_port() -> GraphLinkPort:
    if _graph_link_port is None:
        raise RuntimePortUnavailable("graph-link runtime port is not installed")
    return _graph_link_port


def get_audit_port() -> AuditPort:
    return _audit_port


__all__ = [
    "AuditPort",
    "ContentPort",
    "GraphLinkPort",
    "QueuePort",
    "RuntimePortUnavailable",
    "get_audit_port",
    "get_content_port",
    "get_graph_link_port",
    "get_queue_port",
    "install_audit_port",
    "install_content_port",
    "install_graph_link_port",
    "install_queue_port",
    "reset_runtime_ports",
]
