"""Sibyl tool package exports."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AddResponse": ("sibyl_core.tools.responses", "AddResponse"),
    "ConflictWarning": ("sibyl_core.tools.responses", "ConflictWarning"),
    "EntitySummary": ("sibyl_core.tools.responses", "EntitySummary"),
    "ExploreResponse": ("sibyl_core.tools.responses", "ExploreResponse"),
    "HealthStatus": ("sibyl_core.tools.admin", "HealthStatus"),
    "RebuildResult": ("sibyl_core.tools.admin", "RebuildResult"),
    "RelatedEntity": ("sibyl_core.tools.responses", "RelatedEntity"),
    "SearchResponse": ("sibyl_core.tools.responses", "SearchResponse"),
    "SearchResult": ("sibyl_core.tools.responses", "SearchResult"),
    "TemporalEdge": ("sibyl_core.tools.responses", "TemporalEdge"),
    "TemporalResponse": ("sibyl_core.tools.responses", "TemporalResponse"),
    "add": ("sibyl_core.tools.core", "add"),
    "compile_context": ("sibyl_core.tools.core", "compile_context"),
    "context_pack_to_dict": ("sibyl_core.tools.core", "context_pack_to_dict"),
    "context_pack_to_markdown": ("sibyl_core.tools.core", "context_pack_to_markdown"),
    "detect_conflicts": ("sibyl_core.tools.conflicts", "detect_conflicts"),
    "explore": ("sibyl_core.tools.core", "explore"),
    "find_similar_entities": ("sibyl_core.tools.conflicts", "find_similar_entities"),
    "find_temporal_conflicts": ("sibyl_core.tools.temporal", "find_conflicts"),
    "get_entity_history": ("sibyl_core.tools.temporal", "get_entity_history"),
    "get_health": ("sibyl_core.tools.core", "get_health"),
    "get_stats": ("sibyl_core.tools.admin", "get_stats"),
    "get_unified_stats": ("sibyl_core.tools.core", "get_stats"),
    "health_check": ("sibyl_core.tools.admin", "health_check"),
    "mark_server_started": ("sibyl_core.tools.admin", "mark_server_started"),
    "rebuild_indices": ("sibyl_core.tools.admin", "rebuild_indices"),
    "search": ("sibyl_core.tools.core", "search"),
    "synthesis_draft": ("sibyl_core.tools.core", "synthesis_draft"),
    "synthesis_plan": ("sibyl_core.tools.core", "synthesis_plan"),
    "synthesis_verify": ("sibyl_core.tools.core", "synthesis_verify"),
    "temporal_query": ("sibyl_core.tools.temporal", "temporal_query"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
