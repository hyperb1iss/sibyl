"""Lazy compatibility surface for Sibyl MCP tools."""

from typing import Any

from sibyl_core.tools.helpers import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    VALID_ENTITY_TYPES,
    _auto_discover_links,
    _build_entity_metadata,
    _generate_id,
    _get_field,
    _serialize_enum,
    auto_tag_task,
    get_project_tags,
)
from sibyl_core.tools.reflect import (
    reflection_pack_to_dict,
    reflection_pack_to_markdown,
)
from sibyl_core.tools.responses import (
    AddResponse,
    DependencyNode,
    EntitySummary,
    ExploreResponse,
    RelatedEntity,
    SearchResponse,
    SearchResult,
)


async def add(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.add import add as _add

    return await _add(*args, **kwargs)


async def search(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.search import search as _search

    return await _search(*args, **kwargs)


async def explore(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.explore import explore as _explore

    return await _explore(*args, **kwargs)


async def get_health(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.health import get_health as _get_health

    return await _get_health(*args, **kwargs)


async def get_stats(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.health import get_stats as _get_stats

    return await _get_stats(*args, **kwargs)


async def compile_context(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.context import compile_context as _compile_context

    return await _compile_context(*args, **kwargs)


def context_pack_to_dict(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.context import context_pack_to_dict as _context_pack_to_dict

    return _context_pack_to_dict(*args, **kwargs)


def context_pack_to_markdown(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.context import context_pack_to_markdown as _context_pack_to_markdown

    return _context_pack_to_markdown(*args, **kwargs)


async def reflect_memory(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.reflect import reflect_memory as _reflect_memory

    return await _reflect_memory(*args, **kwargs)


async def synthesis_plan(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.synthesis import synthesis_plan as _synthesis_plan

    return await _synthesis_plan(*args, **kwargs)


async def synthesis_draft(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.synthesis import synthesis_draft as _synthesis_draft

    return await _synthesis_draft(*args, **kwargs)


async def synthesis_verify(*args: Any, **kwargs: Any) -> Any:
    from sibyl_core.tools.synthesis import synthesis_verify as _synthesis_verify

    return await _synthesis_verify(*args, **kwargs)


__all__ = [
    # Helper functions (for internal use)
    "MAX_CONTENT_LENGTH",
    "MAX_TITLE_LENGTH",
    "VALID_ENTITY_TYPES",
    # Response types
    "AddResponse",
    "DependencyNode",
    "EntitySummary",
    "ExploreResponse",
    "RelatedEntity",
    "SearchResponse",
    "SearchResult",
    "_auto_discover_links",
    "_build_entity_metadata",
    "_generate_id",
    "_get_field",
    "_serialize_enum",
    # Main tools
    "add",
    "auto_tag_task",
    "compile_context",
    "context_pack_to_dict",
    "context_pack_to_markdown",
    "explore",
    # Health/stats
    "get_health",
    "get_project_tags",
    "get_stats",
    "reflect_memory",
    "reflection_pack_to_dict",
    "reflection_pack_to_markdown",
    "search",
    "synthesis_draft",
    "synthesis_plan",
    "synthesis_verify",
]
