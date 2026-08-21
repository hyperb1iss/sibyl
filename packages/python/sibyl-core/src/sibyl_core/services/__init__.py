"""Backend-agnostic service contracts."""

from importlib import import_module
from typing import Any

from sibyl_core.services.contracts import KnowledgeReadService, KnowledgeWriteService

_EXPORTS = {
    "GraphRuntime": ("sibyl_core.services.graph_runtime", "GraphRuntime"),
    "count_entities_by_type": ("sibyl_core.services.graph_runtime", "count_entities_by_type"),
    "execute_graph_query": ("sibyl_core.services.graph_runtime", "execute_graph_query"),
    "get_graph_client": ("sibyl_core.services.graph_runtime", "get_graph_client"),
    "get_graph_runtime": ("sibyl_core.services.graph_runtime", "get_graph_runtime"),
}

__all__ = [
    "GraphRuntime",
    "KnowledgeReadService",
    "KnowledgeWriteService",
    "count_entities_by_type",
    "execute_graph_query",
    "get_graph_client",
    "get_graph_runtime",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
