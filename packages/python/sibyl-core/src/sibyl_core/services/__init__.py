"""Backend-agnostic service contracts."""

from sibyl_core.services.contracts import KnowledgeReadService, KnowledgeWriteService
from sibyl_core.services.graph_runtime import (
    ActiveGraphRuntime,
    count_entities_by_type,
    execute_graph_query,
    get_graph_client,
    get_graph_runtime,
)

__all__ = [
    "ActiveGraphRuntime",
    "KnowledgeReadService",
    "KnowledgeWriteService",
    "count_entities_by_type",
    "execute_graph_query",
    "get_graph_client",
    "get_graph_runtime",
]
