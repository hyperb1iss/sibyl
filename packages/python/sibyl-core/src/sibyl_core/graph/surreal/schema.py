"""Compatibility shim for the legacy `sibyl_core.graph.surreal.schema` path."""

from sibyl_core.backends.surreal.schema import (
    ANALYZER_DEFINITIONS,
    EDGE_DEFINITIONS,
    EMBEDDING_DIM,
    GRAPH_EDGES,
    GRAPH_TABLES,
    NODE_DEFINITIONS,
    bootstrap_schema,
    drop_all_indexes,
)

__all__ = [
    "ANALYZER_DEFINITIONS",
    "EDGE_DEFINITIONS",
    "EMBEDDING_DIM",
    "GRAPH_EDGES",
    "GRAPH_TABLES",
    "NODE_DEFINITIONS",
    "bootstrap_schema",
    "drop_all_indexes",
]

