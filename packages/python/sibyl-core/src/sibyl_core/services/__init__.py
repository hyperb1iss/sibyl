"""Backend-agnostic service contracts."""

from sibyl_core.services.contracts import KnowledgeReadService, KnowledgeWriteService
from sibyl_core.services.legacy_graph import (
    LegacyGraphRuntime,
    execute_legacy_graph_query,
    get_legacy_graph_client,
    get_legacy_graph_runtime,
)

__all__ = [
    "KnowledgeReadService",
    "KnowledgeWriteService",
    "LegacyGraphRuntime",
    "execute_legacy_graph_query",
    "get_legacy_graph_client",
    "get_legacy_graph_runtime",
]
