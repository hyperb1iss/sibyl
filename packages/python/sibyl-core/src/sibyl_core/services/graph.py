"""Public native graph service contract.

Implementation lives in one-way domain modules. Internal callers should import
the canonical owner directly; this module exists only for the documented public
surface.
"""

from sibyl_core.services.graph_client import (
    SurrealGraphClient,
    close_graph_clients,
    get_surreal_graph_client,
    mark_graph_schema_dirty,
    prepare_graph_schema,
)
from sibyl_core.services.graph_common import (
    normalize_graph_records as normalize_records,
)
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_entity_store import (
    CLEAR_MEMORY_SCOPE,
    MAX_ENTITY_CONTENT_CHARS,
    heal_entity_metadata_snapshots,
)
from sibyl_core.services.graph_records import (
    entity_from_surreal_row,
    relationship_from_surreal_row,
)
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime, get_surreal_graph_runtime

__all__ = [
    "CLEAR_MEMORY_SCOPE",
    "MAX_ENTITY_CONTENT_CHARS",
    "EntityManager",
    "GraphRuntime",
    "RelationshipManager",
    "SurrealGraphClient",
    "close_graph_clients",
    "entity_from_surreal_row",
    "get_surreal_graph_client",
    "get_surreal_graph_runtime",
    "heal_entity_metadata_snapshots",
    "mark_graph_schema_dirty",
    "normalize_records",
    "prepare_graph_schema",
    "relationship_from_surreal_row",
]
