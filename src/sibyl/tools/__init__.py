"""MCP tool implementations.

Provides all tool functions for the Conventions MCP Server:
- search: Semantic search across wisdom, patterns, and templates
- lookup: Direct access to rules, guides, and specific templates
- discovery: Listing and exploration of available knowledge
- mutation: Adding new knowledge to the graph
- admin: Maintenance and diagnostic tools
"""

from sibyl.tools.admin import (
    HealthStatus,
    RebuildResult,
    SyncResult,
    get_stats,
    health_check,
    mark_server_started,
    rebuild_indices,
    sync_wisdom_docs,
)
from sibyl.tools.discovery import (
    EntitySummary,
    ListResult,
    RelatedEntity,
    get_related,
    list_patterns,
    list_rules,
    list_templates,
    list_topics,
)
from sibyl.tools.lookup import (
    LanguageGuide,
    RuleResult,
    TemplateResult,
    get_language_guide,
    get_sacred_rules,
    get_template,
)
from sibyl.tools.mutation import (
    MutationResult,
    add_learning,
    record_debugging_victory,
)
from sibyl.tools.search import (
    SearchResult,
    find_solution,
    search_patterns,
    search_templates,
    search_wisdom,
)

__all__ = [
    # Admin
    "HealthStatus",
    "RebuildResult",
    "SyncResult",
    "get_stats",
    "health_check",
    "mark_server_started",
    "rebuild_indices",
    "sync_wisdom_docs",
    # Discovery
    "EntitySummary",
    "ListResult",
    "RelatedEntity",
    "get_related",
    "list_patterns",
    "list_rules",
    "list_templates",
    "list_topics",
    # Lookup
    "LanguageGuide",
    "RuleResult",
    "TemplateResult",
    "get_language_guide",
    "get_sacred_rules",
    "get_template",
    # Mutation
    "MutationResult",
    "add_learning",
    "record_debugging_victory",
    # Search
    "SearchResult",
    "find_solution",
    "search_patterns",
    "search_templates",
    "search_wisdom",
]
