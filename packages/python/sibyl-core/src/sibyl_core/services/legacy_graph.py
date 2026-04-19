"""Compatibility exports for callers still using the legacy service module."""

from sibyl_core.services.graph_runtime import (
    ActiveGraphRuntime,
    LegacyGraphRuntime,
    count_entities_by_type,
    execute_graph_query,
    execute_legacy_graph_query,
    get_graph_client,
    get_graph_runtime,
    get_legacy_graph_client,
    get_legacy_graph_runtime,
)

__all__ = [
    "ActiveGraphRuntime",
    "LegacyGraphRuntime",
    "count_entities_by_type",
    "execute_graph_query",
    "execute_legacy_graph_query",
    "get_graph_client",
    "get_graph_runtime",
    "get_legacy_graph_client",
    "get_legacy_graph_runtime",
]
