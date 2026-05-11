"""Persistence adapters for the legacy graph runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sibyl.persistence.legacy.graph import (
        ActiveGraphStore,
        GraphQueryAdapter,
        GraphReadServiceAdapter,
        GraphWriteServiceAdapter,
        LegacyEntityStore,
        LegacyGraphQueryAdapter,
        LegacyGraphStore,
        LegacyKnowledgeReadAdapter,
        LegacyKnowledgeWriteAdapter,
        LegacyRelationshipStore,
        LegacySearchIndex,
        LegacyTaskRuntime,
        TaskGraphRuntime,
        ensure_graph_indexes,
        ensure_legacy_graph_indexes,
        execute_debug_query,
        execute_legacy_debug_query,
        get_entity_graph_runtime,
        get_graph_query_adapter,
        get_graph_stats_payload,
        get_knowledge_read_adapter,
        get_legacy_graph_stats_payload,
        get_legacy_knowledge_read_adapter,
        get_task_graph_runtime,
        graph_stats_payload,
        reset_graph_runtime,
        reset_legacy_graph_runtime,
    )

__all__ = [
    "ActiveGraphStore",
    "GraphQueryAdapter",
    "GraphReadServiceAdapter",
    "GraphWriteServiceAdapter",
    "LegacyEntityStore",
    "LegacyGraphQueryAdapter",
    "LegacyGraphStore",
    "LegacyKnowledgeReadAdapter",
    "LegacyKnowledgeWriteAdapter",
    "LegacyRelationshipStore",
    "LegacySearchIndex",
    "LegacyTaskRuntime",
    "TaskGraphRuntime",
    "ensure_graph_indexes",
    "ensure_legacy_graph_indexes",
    "execute_debug_query",
    "execute_legacy_debug_query",
    "get_entity_graph_runtime",
    "get_graph_query_adapter",
    "get_graph_stats_payload",
    "get_knowledge_read_adapter",
    "get_legacy_graph_stats_payload",
    "get_legacy_knowledge_read_adapter",
    "get_task_graph_runtime",
    "graph_stats_payload",
    "reset_graph_runtime",
    "reset_legacy_graph_runtime",
]

_GRAPH_EXPORTS = set(__all__)


def __getattr__(name: str) -> Any:
    if name in _GRAPH_EXPORTS:
        from sibyl.persistence.legacy import graph

        return getattr(graph, name)
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
