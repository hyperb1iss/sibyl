"""SurrealDB schema bootstrap for Sibyl's knowledge graph."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import structlog

from sibyl_core.backends.surreal.schema_helpers import execute_schema_statement, split_statements
from sibyl_core.backends.surreal.schema_version import (
    GRAPH_SCHEMA_CURRENT_VERSION,
    SCHEMA_VERSION_TABLE,
    ConcurrentIndexDefinition,
    SchemaMigration,
    _validate_identifier,
    apply_schema_migrations,
    ensure_schema_version_table,
    get_schema_embedding_dimension,
    get_schema_version,
    rebuild_index_concurrently,
    record_schema_version,
)
from sibyl_core.config import core_config
from sibyl_core.models.entities import EntityType

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.protocols import SchemaDriver

logger = structlog.get_logger()


def _object_mapping(value: object) -> Mapping[object, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[object, object], value)


def _index_names_from_info(value: object) -> list[str]:
    info_map = _object_mapping(value)
    if info_map is None:
        return []
    indexes = _object_mapping(info_map.get("indexes"))
    if indexes is None:
        return []
    return [str(index_name) for index_name in indexes]


# Graph node embeddings are configured separately from the OpenAI chunk embedder.
# Default is 1024-dim; override via SIBYL_GRAPH_EMBEDDING_DIMENSIONS.
EMBEDDING_DIM = core_config.graph_embedding_dimensions
HNSW_EFC = core_config.graph_hnsw_efc
HNSW_M = core_config.graph_hnsw_m
_GRAPH_ENTITY_TYPE_VALUES = tuple(entity_type.value for entity_type in EntityType)
_EMBEDDED_SURREAL_SCHEMES = ("memory://", "surrealkv://", "rocksdb://", "file://")


def _surql_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(f"'{value}'" for value in values) + "]"


ANALYZER_DEFINITIONS = """
DEFINE ANALYZER IF NOT EXISTS name_analyzer
    TOKENIZERS blank, class
    FILTERS lowercase, ascii, snowball(english);

DEFINE ANALYZER IF NOT EXISTS content_analyzer
    TOKENIZERS blank, class
    FILTERS lowercase, ascii, snowball(english);
"""


NODE_DEFINITIONS = f"""
DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
ALTER TABLE IF EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS name ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS entity_type ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS summary ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS description ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS content ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS labels ON entity TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS attributes ON entity TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS group_id ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_by ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS modified_by ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS revision ON entity TYPE option<int> DEFAULT 1;
DEFINE FIELD IF NOT EXISTS last_recalled_at ON entity TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_used_at ON entity TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS retrieval_count ON entity TYPE option<int> DEFAULT 0;
DEFINE FIELD IF NOT EXISTS citation_count ON entity TYPE option<int> DEFAULT 0;
DEFINE FIELD IF NOT EXISTS misled_count ON entity TYPE option<int> DEFAULT 0;
DEFINE FIELD IF NOT EXISTS project_id ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS epic_id ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS parent_task_id ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS task_id ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS status ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS priority ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS complexity ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS feature ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS tags ON entity TYPE option<array<string>>;
DEFINE FIELD IF NOT EXISTS name_embedding ON entity TYPE option<array<float, {EMBEDDING_DIM}>>;

DEFINE INDEX IF NOT EXISTS idx_entity_uuid ON entity FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_entity_type ON entity FIELDS entity_type;
DEFINE INDEX IF NOT EXISTS idx_entity_labels ON entity FIELDS labels;
DEFINE INDEX IF NOT EXISTS idx_entity_project ON entity FIELDS project_id;
DEFINE INDEX IF NOT EXISTS idx_entity_parent_task ON entity FIELDS parent_task_id;
DEFINE INDEX IF NOT EXISTS idx_entity_task ON entity FIELDS task_id;
DEFINE INDEX IF NOT EXISTS idx_entity_status ON entity FIELDS status;
DEFINE INDEX IF NOT EXISTS idx_entity_priority ON entity FIELDS priority;
DEFINE INDEX IF NOT EXISTS idx_entity_updated ON entity FIELDS updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_last_recalled ON entity FIELDS last_recalled_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_last_used ON entity FIELDS last_used_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_type_updated ON entity FIELDS entity_type, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_type_project_updated ON entity FIELDS entity_type, project_id, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_type_parent_task_updated ON entity FIELDS entity_type, parent_task_id, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_type_status_updated ON entity FIELDS entity_type, status, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_type_project_status ON entity FIELDS entity_type, project_id, status;
DEFINE INDEX IF NOT EXISTS idx_entity_name_ft ON entity FIELDS name FULLTEXT ANALYZER name_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_summary_ft ON entity FIELDS summary FULLTEXT ANALYZER content_analyzer BM25;
REMOVE INDEX IF EXISTS idx_entity_description_ft ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_content_ft ON TABLE entity;
UPDATE entity SET
    description = description ?? attributes.description,
    content = content ?? attributes.content
WHERE description = NONE OR content = NONE;

DEFINE INDEX IF NOT EXISTS idx_entity_description_text_ft ON entity FIELDS description FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_content_text_ft ON entity FIELDS content FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_embedding ON entity FIELDS name_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC {HNSW_EFC} M {HNSW_M};

DEFINE TABLE IF NOT EXISTS episode SCHEMAFULL;
ALTER TABLE IF EXISTS episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS name ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS source ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS source_description ON episode TYPE option<string>;
DEFINE FIELD IF NOT EXISTS content ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS labels ON episode TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS group_id ON episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON episode TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS valid_at ON episode TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS entity_edges ON episode TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS project_id ON episode TYPE option<string>;

DEFINE INDEX IF NOT EXISTS idx_episode_uuid ON episode FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_episode_created ON episode FIELDS created_at;
DEFINE INDEX IF NOT EXISTS idx_episode_content_ft ON episode FIELDS content FULLTEXT ANALYZER content_analyzer BM25;
"""


RELATION_EDGE_CLEANUP_DEFINITIONS = """
DELETE FROM relates_to
WHERE in NOT IN (SELECT VALUE id FROM entity)
   OR out NOT IN (SELECT VALUE id FROM entity);

UPDATE relates_to SET
    episodes = episodes ?? [],
    attributes = attributes ?? {}
WHERE episodes = NONE OR attributes = NONE;

DELETE FROM mentions
WHERE in NOT IN (SELECT VALUE id FROM episode)
   OR out NOT IN (SELECT VALUE id FROM entity);
"""


EDGE_DEFINITIONS = f"""
DEFINE TABLE OVERWRITE relates_to SCHEMAFULL TYPE RELATION IN entity OUT entity ENFORCED;
ALTER TABLE IF EXISTS relates_to SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS name ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS fact ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS fact_embedding ON relates_to TYPE option<array<float, {EMBEDDING_DIM}>>;
DEFINE FIELD IF NOT EXISTS group_id ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON relates_to TYPE option<string>;
DEFINE FIELD IF NOT EXISTS target_id ON relates_to TYPE option<string>;
DEFINE FIELD IF NOT EXISTS episodes ON relates_to TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS attributes ON relates_to TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS created_at ON relates_to TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expired_at ON relates_to TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS valid_at ON relates_to TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS invalid_at ON relates_to TYPE option<datetime>;

DEFINE INDEX IF NOT EXISTS idx_relates_uuid ON relates_to FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_relates_source ON relates_to FIELDS source_id;
DEFINE INDEX IF NOT EXISTS idx_relates_target ON relates_to FIELDS target_id;
DEFINE INDEX IF NOT EXISTS idx_relates_name_source ON relates_to FIELDS name, source_id;
DEFINE INDEX IF NOT EXISTS idx_relates_name_target ON relates_to FIELDS name, target_id;
DEFINE INDEX IF NOT EXISTS idx_relates_source_target_name ON relates_to FIELDS source_id, target_id, name;
DEFINE INDEX IF NOT EXISTS idx_relates_source_created ON relates_to FIELDS source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_target_created ON relates_to FIELDS target_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_created ON relates_to FIELDS created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_fact_ft ON relates_to FIELDS fact FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_relates_fact_embedding ON relates_to FIELDS fact_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC {HNSW_EFC} M {HNSW_M};

DEFINE TABLE OVERWRITE mentions SCHEMAFULL TYPE RELATION IN episode OUT entity ENFORCED;
ALTER TABLE IF EXISTS mentions SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON mentions TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON mentions TYPE string;
DEFINE FIELD IF NOT EXISTS source_id ON mentions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS target_id ON mentions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON mentions TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mentions_uuid ON mentions FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_mentions_source ON mentions FIELDS source_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_target ON mentions FIELDS target_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_source_created ON mentions FIELDS source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_mentions_target_created ON mentions FIELDS target_id, created_at, uuid;
"""


ENTITY_DENORMALIZATION_MAINTENANCE_DEFINITIONS = """
UPDATE entity SET
    description = description ?? attributes.description,
    content = content ?? attributes.content
WHERE description = NONE OR content = NONE;
"""


RELATION_ENDPOINT_SCHEMA_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS source_id ON mentions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS target_id ON mentions TYPE option<string>;

DEFINE INDEX IF NOT EXISTS idx_relates_source_created ON relates_to FIELDS source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_target_created ON relates_to FIELDS target_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_mentions_source ON mentions FIELDS source_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_target ON mentions FIELDS target_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_source_created ON mentions FIELDS source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_mentions_target_created ON mentions FIELDS target_id, created_at, uuid;
"""


RELATION_ENDPOINT_BACKFILL_DEFINITIONS = """
UPDATE relates_to SET
    source_id = in.uuid,
    target_id = out.uuid
WHERE source_id = NONE
    OR target_id = NONE
    OR source_id != in.uuid
    OR target_id != out.uuid;

UPDATE mentions SET
    source_id = in.uuid,
    target_id = out.uuid
WHERE source_id = NONE
    OR target_id = NONE
    OR source_id != in.uuid
    OR target_id != out.uuid;
"""


ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS parent_task_id ON entity TYPE option<string>;
DEFINE FIELD OVERWRITE updated_at ON entity TYPE option<datetime>;
UPDATE (
    SELECT VALUE id
    FROM entity
    WHERE type::is::string(updated_at)
        AND string::is::datetime(updated_at)
) SET updated_at = type::datetime(updated_at);
UPDATE entity SET updated_at = NONE
    WHERE updated_at != NONE
        AND !type::is::datetime(updated_at);
DEFINE INDEX OVERWRITE idx_entity_group_updated
    ON entity FIELDS group_id, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_group_type_updated
    ON entity FIELDS group_id, entity_type, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_group_type_project_updated
    ON entity FIELDS group_id, entity_type, project_id, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_group_type_epic_updated
    ON entity FIELDS group_id, entity_type, epic_id, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_group_type_parent_task_updated
    ON entity FIELDS group_id, entity_type, parent_task_id, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_group_type_status_updated
    ON entity FIELDS group_id, entity_type, status, updated_at, created_at, uuid CONCURRENTLY;
"""


PARENT_TASK_CANONICALIZATION_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS parent_task_id ON entity TYPE option<string>;

UPDATE entity SET parent_task_id = epic_id
WHERE entity_type = 'task'
    AND (parent_task_id = NONE OR parent_task_id = '')
    AND epic_id != NONE
    AND epic_id != '';

UPDATE entity SET parent_task_id = attributes.parent_task_id
WHERE entity_type = 'task'
    AND (parent_task_id = NONE OR parent_task_id = '')
    AND attributes.parent_task_id != NONE
    AND attributes.parent_task_id != '';

UPDATE entity SET parent_task_id = attributes.epic_id
WHERE entity_type = 'task'
    AND (parent_task_id = NONE OR parent_task_id = '')
    AND attributes.epic_id != NONE
    AND attributes.epic_id != '';
"""


GRAPH_INDEX_PRUNE_DEFINITIONS = """
REMOVE INDEX IF EXISTS idx_entity_group ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_epic ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_updated ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_updated ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_project_updated ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_epic_updated ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_parent_task_updated ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_status_updated ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_epic_status ON TABLE entity;
REMOVE INDEX IF EXISTS idx_entity_group_type_project_status ON TABLE entity;
REMOVE INDEX IF EXISTS idx_episode_group ON TABLE episode;
REMOVE INDEX IF EXISTS idx_relates_group ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_source ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_target ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_name_source ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_name_target ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_source_target_name ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_source_created ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_target_created ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_relates_group_created ON TABLE relates_to;
REMOVE INDEX IF EXISTS idx_mentions_group ON TABLE mentions;
REMOVE INDEX IF EXISTS idx_mentions_group_source ON TABLE mentions;
REMOVE INDEX IF EXISTS idx_mentions_group_target ON TABLE mentions;
REMOVE INDEX IF EXISTS idx_mentions_group_source_created ON TABLE mentions;
REMOVE INDEX IF EXISTS idx_mentions_group_target_created ON TABLE mentions;

DEFINE INDEX OVERWRITE idx_entity_updated
    ON entity FIELDS updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_type_updated
    ON entity FIELDS entity_type, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_type_project_updated
    ON entity FIELDS entity_type, project_id, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_type_parent_task_updated
    ON entity FIELDS entity_type, parent_task_id, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_type_status_updated
    ON entity FIELDS entity_type, status, updated_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_entity_type_project_status
    ON entity FIELDS entity_type, project_id, status CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_source
    ON relates_to FIELDS source_id CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_target
    ON relates_to FIELDS target_id CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_name_source
    ON relates_to FIELDS name, source_id CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_name_target
    ON relates_to FIELDS name, target_id CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_source_target_name
    ON relates_to FIELDS source_id, target_id, name CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_source_created
    ON relates_to FIELDS source_id, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_target_created
    ON relates_to FIELDS target_id, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_relates_created
    ON relates_to FIELDS created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_mentions_source
    ON mentions FIELDS source_id CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_mentions_target
    ON mentions FIELDS target_id CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_mentions_source_created
    ON mentions FIELDS source_id, created_at, uuid CONCURRENTLY;
DEFINE INDEX OVERWRITE idx_mentions_target_created
    ON mentions FIELDS target_id, created_at, uuid CONCURRENTLY;
"""


GRAPH_ENUM_ASSERTION_DEFINITIONS = f"""
DEFINE FIELD OVERWRITE entity_type ON entity TYPE string
    ASSERT $value IN {_surql_string_array(_GRAPH_ENTITY_TYPE_VALUES)};
"""


ENTITY_ACTOR_ATTRIBUTION_DEFINITIONS = """
DEFINE FIELD IF NOT EXISTS created_by ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS modified_by ON entity TYPE option<string>;
UPDATE entity SET created_by = attributes.created_by
WHERE created_by = NONE
    AND attributes.created_by != NONE
    AND type::is::string(attributes.created_by)
    AND attributes.created_by != '';
UPDATE entity SET modified_by = attributes.modified_by
WHERE modified_by = NONE
    AND attributes.modified_by != NONE
    AND type::is::string(attributes.modified_by)
    AND attributes.modified_by != '';
"""

# Pending migrations may leave these optional; the terminal repair restores strict ints.
ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS = """
DEFINE FIELD OVERWRITE revision ON entity TYPE option<int> DEFAULT 1;
DEFINE FIELD OVERWRITE retrieval_count ON entity TYPE option<int> DEFAULT 0;
DEFINE FIELD OVERWRITE citation_count ON entity TYPE option<int> DEFAULT 0;
DEFINE FIELD OVERWRITE misled_count ON entity TYPE option<int> DEFAULT 0;
"""

ENTITY_USAGE_SIGNAL_DEFINITIONS = f"""
{ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS}
DEFINE FIELD IF NOT EXISTS last_recalled_at ON entity TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_used_at ON entity TYPE option<datetime>;
UPDATE entity SET
    retrieval_count = retrieval_count ?? attributes.retrieval_count ?? 0,
    citation_count = citation_count ?? attributes.citation_count ?? 0
WHERE retrieval_count = NONE OR citation_count = NONE;
DEFINE FIELD OVERWRITE retrieval_count ON entity TYPE int DEFAULT 0;
DEFINE FIELD OVERWRITE citation_count ON entity TYPE int DEFAULT 0;
DEFINE INDEX IF NOT EXISTS idx_entity_last_recalled
    ON entity FIELDS last_recalled_at, created_at, uuid CONCURRENTLY;
DEFINE INDEX IF NOT EXISTS idx_entity_last_used
    ON entity FIELDS last_used_at, created_at, uuid CONCURRENTLY;
UPDATE entity SET last_recalled_at = attributes.last_recalled_at
WHERE last_recalled_at = NONE
    AND attributes.last_recalled_at != NONE;
UPDATE entity SET last_used_at = attributes.last_used_at
WHERE last_used_at = NONE
    AND attributes.last_used_at != NONE;
UPDATE entity SET retrieval_count = attributes.retrieval_count ?? 0
WHERE retrieval_count = 0
    AND attributes.retrieval_count != NONE;
UPDATE entity SET citation_count = attributes.citation_count ?? 0
WHERE citation_count = 0
    AND attributes.citation_count != NONE;
"""

MEMORY_QUALITY_METADATA_MIGRATION_DEFINITIONS = """
UPDATE entity SET attributes.importance = attributes.memory_importance
WHERE type::is::number(attributes.importance) = false
    AND type::is::number(attributes.memory_importance);
UPDATE entity SET attributes.importance = attributes.retention_importance
WHERE type::is::number(attributes.retention_importance);
UPDATE entity SET attributes.confidence = attributes.share_confidence
WHERE type::is::number(attributes.confidence) = false
    AND type::is::number(attributes.share_confidence);
UPDATE entity SET attributes.confidence = attributes.projection_confidence
WHERE type::is::number(attributes.projection_confidence);
UPDATE entity SET attributes.confidence = attributes.reflection_confidence
WHERE type::is::number(attributes.reflection_confidence);
UPDATE entity SET attributes.confidence = attributes.promotion_confidence
WHERE type::is::number(attributes.promotion_confidence);
UPDATE entity SET
    attributes.retention_importance = attributes.importance,
    attributes.memory_importance = attributes.importance
WHERE type::is::number(attributes.importance);
UPDATE entity SET
    attributes.promotion_confidence = attributes.confidence,
    attributes.reflection_confidence = attributes.confidence,
    attributes.projection_confidence = attributes.confidence,
    attributes.share_confidence = attributes.confidence
WHERE type::is::number(attributes.confidence);
UPDATE relates_to SET attributes.importance = attributes.memory_importance
WHERE type::is::number(attributes.importance) = false
    AND type::is::number(attributes.memory_importance);
UPDATE relates_to SET attributes.importance = attributes.retention_importance
WHERE type::is::number(attributes.retention_importance);
UPDATE relates_to SET attributes.confidence = attributes.share_confidence
WHERE type::is::number(attributes.confidence) = false
    AND type::is::number(attributes.share_confidence);
UPDATE relates_to SET attributes.confidence = attributes.projection_confidence
WHERE type::is::number(attributes.projection_confidence);
UPDATE relates_to SET attributes.confidence = attributes.reflection_confidence
WHERE type::is::number(attributes.reflection_confidence);
UPDATE relates_to SET attributes.confidence = attributes.promotion_confidence
WHERE type::is::number(attributes.promotion_confidence);
UPDATE relates_to SET
    attributes.retention_importance = attributes.importance,
    attributes.memory_importance = attributes.importance
WHERE type::is::number(attributes.importance);
UPDATE relates_to SET
    attributes.promotion_confidence = attributes.confidence,
    attributes.reflection_confidence = attributes.confidence,
    attributes.projection_confidence = attributes.confidence,
    attributes.share_confidence = attributes.confidence
WHERE type::is::number(attributes.confidence);
"""

ENTITY_REVISION_MIGRATION_DEFINITIONS = """
DEFINE FIELD OVERWRITE revision ON entity TYPE option<int> DEFAULT 1;
UPDATE entity SET revision = 1 WHERE revision = NONE OR revision < 1;
DEFINE FIELD OVERWRITE revision ON entity TYPE int DEFAULT 1 ASSERT $value >= 1;
"""

ENTITY_MISLED_USAGE_SIGNAL_DEFINITIONS = """
DEFINE FIELD OVERWRITE misled_count ON entity TYPE option<int> DEFAULT 0;
UPDATE entity SET misled_count = misled_count ?? attributes.misled_count ?? 0
WHERE misled_count = NONE;
DEFINE FIELD OVERWRITE misled_count ON entity TYPE int DEFAULT 0;
UPDATE entity SET misled_count = attributes.misled_count ?? 0
WHERE misled_count = 0
    AND attributes.misled_count != NONE;
"""

ENTITY_REQUIRED_FIELD_REPAIR_DEFINITIONS = f"""
{ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS}
UPDATE entity SET revision = 1 WHERE revision = NONE OR revision < 1;
UPDATE entity SET
    retrieval_count = retrieval_count ?? attributes.retrieval_count ?? 0,
    citation_count = citation_count ?? attributes.citation_count ?? 0,
    misled_count = misled_count ?? attributes.misled_count ?? 0
WHERE retrieval_count = NONE OR citation_count = NONE OR misled_count = NONE;
UPDATE entity SET retrieval_count = attributes.retrieval_count ?? 0
WHERE retrieval_count = 0
    AND attributes.retrieval_count != NONE;
UPDATE entity SET citation_count = attributes.citation_count ?? 0
WHERE citation_count = 0
    AND attributes.citation_count != NONE;
UPDATE entity SET misled_count = attributes.misled_count ?? 0
WHERE misled_count = 0
    AND attributes.misled_count != NONE;
DEFINE FIELD OVERWRITE revision ON entity TYPE int DEFAULT 1 ASSERT $value >= 1;
DEFINE FIELD OVERWRITE retrieval_count ON entity TYPE int DEFAULT 0;
DEFINE FIELD OVERWRITE citation_count ON entity TYPE int DEFAULT 0;
DEFINE FIELD OVERWRITE misled_count ON entity TYPE int DEFAULT 0;
"""


CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS = ENTITY_DENORMALIZATION_MAINTENANCE_DEFINITIONS


GRAPH_TABLES = ("entity", "episode")
GRAPH_EDGES = ("relates_to", "mentions")
REMOVED_GRAPH_TABLES = ("community", "saga")
REMOVED_GRAPH_EDGES = ("has_episode", "next_episode", "has_member")
REMOVED_GRAPH_OBJECTS = (*REMOVED_GRAPH_EDGES, *REMOVED_GRAPH_TABLES)
DEAD_GRAPH_OBJECT_REMOVAL_DEFINITIONS = "\n".join(
    f"REMOVE TABLE IF EXISTS {table};" for table in REMOVED_GRAPH_OBJECTS
)
GRAPH_SCHEMA_MIGRATIONS = (
    SchemaMigration(
        version=2,
        name="graph_schema_bootstrap",
    ),
    SchemaMigration(
        version=3,
        name="relation_endpoint_denormalization",
        statements=tuple(
            split_statements(
                RELATION_ENDPOINT_SCHEMA_DEFINITIONS + "\n" + RELATION_ENDPOINT_BACKFILL_DEFINITIONS
            )
        ),
    ),
    SchemaMigration(
        version=4,
        name="drop_dead_graph_objects",
        statements=tuple(split_statements(DEAD_GRAPH_OBJECT_REMOVAL_DEFINITIONS)),
    ),
    SchemaMigration(
        version=5,
        name="entity_updated_at_datetime",
        statements=tuple(split_statements(ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS)),
    ),
    SchemaMigration(
        version=6,
        name="relation_endpoint_mirror_backfill",
        statements=tuple(split_statements(RELATION_ENDPOINT_BACKFILL_DEFINITIONS)),
    ),
    SchemaMigration(
        version=7,
        name="graph_index_prune",
        statements=tuple(
            split_statements(
                PARENT_TASK_CANONICALIZATION_DEFINITIONS + "\n" + GRAPH_INDEX_PRUNE_DEFINITIONS
            )
        ),
    ),
    SchemaMigration(
        version=8,
        name="graph_enum_assertions",
        statements=tuple(split_statements(GRAPH_ENUM_ASSERTION_DEFINITIONS)),
    ),
    SchemaMigration(
        version=9,
        name="entity_actor_attribution",
        statements=tuple(split_statements(ENTITY_ACTOR_ATTRIBUTION_DEFINITIONS)),
    ),
    SchemaMigration(
        version=10,
        name="entity_usage_signals",
        statements=tuple(split_statements(ENTITY_USAGE_SIGNAL_DEFINITIONS)),
    ),
    SchemaMigration(
        version=11,
        name="memory_quality_metadata",
        statements=tuple(
            split_statements(
                ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS
                + "\n"
                + MEMORY_QUALITY_METADATA_MIGRATION_DEFINITIONS
            )
        ),
    ),
    SchemaMigration(
        version=12,
        name="entity_revision",
        statements=tuple(
            split_statements(
                ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS
                + "\n"
                + ENTITY_REVISION_MIGRATION_DEFINITIONS
            )
        ),
    ),
    SchemaMigration(
        version=13,
        name="entity_misled_usage_signal",
        statements=tuple(
            split_statements(
                ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS
                + "\n"
                + ENTITY_MISLED_USAGE_SIGNAL_DEFINITIONS
            )
        ),
    ),
    SchemaMigration(
        version=14,
        name="entity_required_field_repair",
        statements=tuple(split_statements(ENTITY_REQUIRED_FIELD_REPAIR_DEFINITIONS)),
    ),
)


def _graph_schema_migrations(*, url: str) -> tuple[SchemaMigration, ...]:
    return tuple(
        SchemaMigration(
            version=migration.version,
            name=migration.name,
            statements=tuple(
                render_surreal_compatible_sql(statement, url=url)
                for statement in migration.statements
            ),
        )
        for migration in GRAPH_SCHEMA_MIGRATIONS
    )


@dataclass(frozen=True, slots=True)
class EmbeddingVectorField:
    """A vector field whose dimension is baked into both its type and its HNSW index."""

    table: str
    field: str
    index: str

    def field_redefinition(self, dimension: int) -> str:
        return (
            f"DEFINE FIELD OVERWRITE {self.field} ON {self.table} "
            f"TYPE option<array<float, {dimension}>>;"
        )

    def clear_statement(self) -> str:
        return f"UPDATE {self.table} SET {self.field} = NONE WHERE {self.field} != NONE;"

    def index_definition(self, dimension: int) -> ConcurrentIndexDefinition:
        return ConcurrentIndexDefinition(
            name=self.index,
            table=self.table,
            definition=(
                f"DEFINE INDEX {self.index} ON {self.table} FIELDS {self.field} "
                f"HNSW DIMENSION {dimension} DIST COSINE TYPE F32 EFC {HNSW_EFC} M {HNSW_M}"
            ),
        )


EMBEDDING_VECTOR_FIELDS = (
    EmbeddingVectorField(table="entity", field="name_embedding", index="idx_entity_embedding"),
    EmbeddingVectorField(
        table="relates_to", field="fact_embedding", index="idx_relates_fact_embedding"
    ),
)


async def rebuild_embedding_indexes_for_dimension(
    driver: SchemaDriver,
    *,
    dimension: int,
) -> None:
    """Resize every HNSW vector field/index to ``dimension`` and record the new size.

    Old-dimension vectors violate the resized ``array<float, N>`` field constraint, so each
    field is cleared before its type is redefined; embeddings regenerate on the next write
    (write-time dimension validation keeps only correctly-sized vectors from then on).
    """
    if not driver.group_id:
        msg = "rebuild_embedding_indexes_for_dimension requires driver.clone(group_id) first"
        raise ValueError(msg)

    for vector_field in EMBEDDING_VECTOR_FIELDS:
        await execute_schema_statement(
            driver.execute_query,
            vector_field.clear_statement(),
            scope="graph_embedding_dimension_rebuild",
            group_id=driver.group_id,
        )
        await execute_schema_statement(
            driver.execute_query,
            vector_field.field_redefinition(dimension),
            scope="graph_embedding_dimension_rebuild",
            group_id=driver.group_id,
        )
        await rebuild_index_concurrently(
            driver.execute_query,
            vector_field.index_definition(dimension),
        )

    await record_schema_version(
        driver.execute_query,
        version=GRAPH_SCHEMA_CURRENT_VERSION,
        migrations=list(GRAPH_SCHEMA_MIGRATIONS),
        embedding_dimension=dimension,
    )
    logger.info(
        "surreal_schema_embedding_dimension_rebuilt",
        group_id=driver.group_id,
        embedding_dimension=dimension,
    )


def _store_supports_concurrent_rebuild(url: str) -> bool:
    return not url.startswith(("memory://", "surrealkv://"))


async def _reconcile_embedding_dimension(driver: SchemaDriver) -> None:
    recorded = await get_schema_embedding_dimension(driver.execute_query)
    if recorded is None:
        await record_schema_version(
            driver.execute_query,
            version=GRAPH_SCHEMA_CURRENT_VERSION,
            migrations=list(GRAPH_SCHEMA_MIGRATIONS),
            embedding_dimension=EMBEDDING_DIM,
        )
        return
    if recorded == EMBEDDING_DIM:
        return
    if not _store_supports_concurrent_rebuild(driver._url):
        logger.warning(
            "surreal_schema_embedding_dimension_rebuild_skipped",
            group_id=driver.group_id,
            recorded_dimension=recorded,
            configured_dimension=EMBEDDING_DIM,
            reason="embedded_store",
        )
        return
    logger.warning(
        "surreal_schema_embedding_dimension_drift",
        group_id=driver.group_id,
        recorded_dimension=recorded,
        configured_dimension=EMBEDDING_DIM,
    )
    await rebuild_embedding_indexes_for_dimension(driver, dimension=EMBEDDING_DIM)


def _is_relation_cleanup_statement(statement: str) -> bool:
    normalized = statement.lstrip().lower()
    return any(
        normalized.startswith(f"delete from {table}") or normalized.startswith(f"update {table}")
        for table in GRAPH_EDGES
    )


def _is_missing_table_error(error: Exception) -> bool:
    message = str(error).lower()
    return "the table" in message and "does not exist" in message


def _coerce_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _first_count_value(value: object) -> object:
    value_map = _object_mapping(value)
    if value_map is not None:
        if "result" in value_map:
            return _first_count_value(value_map.get("result"))
        count = value_map.get("count")
        if count is not None:
            return count
        return value_map.get("cnt", 0)
    if isinstance(value, list):
        for item in value:
            count = _first_count_value(item)
            if count is not None:
                return count
    return None


async def _dead_graph_object_count(driver: SchemaDriver, table: str) -> int:
    _validate_identifier(table)
    try:
        result = await driver.execute_query(f"SELECT count() AS count FROM {table} GROUP ALL;")
    except Exception as exc:
        if _is_missing_table_error(exc):
            return 0
        raise
    return _coerce_count(_first_count_value(result))


async def _ensure_removed_graph_objects_empty(driver: SchemaDriver) -> None:
    occupied: dict[str, int] = {}
    for table in REMOVED_GRAPH_OBJECTS:
        count = await _dead_graph_object_count(driver, table)
        if count:
            occupied[table] = count

    if occupied:
        summary = ", ".join(f"{table}={count}" for table, count in occupied.items())
        msg = (
            "Dead graph objects still contain rows; export or clear them before "
            f"graph schema v{GRAPH_SCHEMA_CURRENT_VERSION} migration: {summary}"
        )
        raise RuntimeError(msg)


async def _assert_graph_migrations_safe(
    driver: SchemaDriver,
    *,
    current_version: int,
) -> None:
    if current_version >= 8:
        return

    invalid_entity_type = await _first_invalid_graph_enum_value(
        driver,
        table="entity",
        field="entity_type",
        allowed=_GRAPH_ENTITY_TYPE_VALUES,
    )
    if invalid_entity_type is not None:
        raise RuntimeError(
            "Cannot migrate entity.entity_type enum assertion: "
            f"invalid existing value {invalid_entity_type!r}"
        )


async def _first_invalid_graph_enum_value(
    driver: SchemaDriver,
    *,
    table: str,
    field: str,
    allowed: tuple[str, ...],
) -> str | None:
    _validate_identifier(table)
    _validate_identifier(field)
    try:
        result = await driver.execute_query(
            f"""
            SELECT {field}
            FROM {table}
            GROUP BY {field};
            """
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            return None
        raise
    rows = result if isinstance(result, list) else []
    allowed_values = set(allowed)
    for row in rows:
        row_map = _object_mapping(row)
        if row_map is None:
            continue
        value = row_map.get(field)
        if value in {None, ""}:
            return "" if value == "" else "NONE"
        normalized = str(value)
        if normalized not in allowed_values:
            return normalized
    return None


def render_fulltext_compatible_sql(sql: str, *, url: str) -> str:
    if url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        return sql.replace("FULLTEXT ANALYZER", "SEARCH ANALYZER")
    return sql


def render_surreal_compatible_sql(sql: str, *, url: str) -> str:
    rendered = render_fulltext_compatible_sql(sql, url=url)
    if not url.startswith(_EMBEDDED_SURREAL_SCHEMES):
        rendered = (
            rendered.replace("type::is::string", "type::is_string")
            .replace("type::is::number", "type::is_number")
            .replace("type::is::datetime", "type::is_datetime")
            .replace("string::is::datetime", "string::is_datetime")
        )
    return rendered


async def bootstrap_schema(
    driver: SchemaDriver,
    *,
    reset: bool = False,
    force: bool = False,
) -> None:
    if not driver.group_id:
        msg = "bootstrap_schema requires driver.clone(group_id) first"
        raise ValueError(msg)

    if reset:
        for table in (*GRAPH_EDGES, *GRAPH_TABLES, *REMOVED_GRAPH_EDGES, *REMOVED_GRAPH_TABLES):
            await driver.execute_query(f"REMOVE TABLE IF EXISTS {table};")
        await driver.execute_query(f"REMOVE TABLE IF EXISTS {SCHEMA_VERSION_TABLE};")
    else:
        await ensure_schema_version_table(driver.execute_query, group_id=driver.group_id)
        current_version = await get_schema_version(driver.execute_query)
        if current_version >= GRAPH_SCHEMA_CURRENT_VERSION:
            if not force:
                await _reconcile_embedding_dimension(driver)
                return
        elif current_version > 0 and not force:
            await _assert_graph_migrations_safe(driver, current_version=current_version)
            await _ensure_removed_graph_objects_empty(driver)
            await apply_schema_migrations(
                driver.execute_query,
                _graph_schema_migrations(url=driver._url),
                group_id=driver.group_id,
            )
            await _reconcile_embedding_dimension(driver)
            return
        await _ensure_removed_graph_objects_empty(driver)

    compatible_blocks = (
        ANALYZER_DEFINITIONS,
        render_surreal_compatible_sql(NODE_DEFINITIONS, url=driver._url),
        RELATION_EDGE_CLEANUP_DEFINITIONS,
        render_surreal_compatible_sql(EDGE_DEFINITIONS, url=driver._url),
    )
    for block in compatible_blocks:
        await _execute_graph_schema_block(
            driver,
            block,
            ignore_missing_relation_tables=block == RELATION_EDGE_CLEANUP_DEFINITIONS,
        )
    await apply_schema_migrations(
        driver.execute_query,
        _graph_schema_migrations(url=driver._url),
        group_id=driver.group_id,
    )
    await _reconcile_embedding_dimension(driver)


async def _execute_graph_schema_block(
    driver: SchemaDriver,
    block: str,
    *,
    ignore_missing_relation_tables: bool = False,
) -> bool:
    skipped_missing_relation_table = False
    for statement in split_statements(block):
        try:
            await execute_schema_statement(
                driver.execute_query,
                statement,
                scope="graph",
                group_id=driver.group_id,
            )
        except Exception as exc:
            if not (
                ignore_missing_relation_tables
                and _is_relation_cleanup_statement(statement)
                and _is_missing_table_error(exc)
            ):
                raise
            skipped_missing_relation_table = True
            logger.debug(
                "surreal_schema_relation_cleanup_skipped",
                group_id=driver.group_id,
                error_type=type(exc).__name__,
            )
    return skipped_missing_relation_table


async def drop_all_indexes(driver: SchemaDriver) -> None:
    if not driver.group_id:
        return

    for table in (*GRAPH_TABLES, *GRAPH_EDGES):
        info = await driver.execute_query(f"INFO FOR TABLE {table};")
        index_names = _index_names_from_info(info)
        if not index_names and isinstance(info, list) and info:
            index_names = _index_names_from_info(info[0])

        for index_name in index_names:
            _validate_identifier(index_name)
            await driver.execute_query(f"REMOVE INDEX IF EXISTS {index_name} ON TABLE {table};")


__all__ = [
    "ANALYZER_DEFINITIONS",
    "DEAD_GRAPH_OBJECT_REMOVAL_DEFINITIONS",
    "EDGE_DEFINITIONS",
    "EMBEDDING_DIM",
    "EMBEDDING_VECTOR_FIELDS",
    "ENTITY_ACTOR_ATTRIBUTION_DEFINITIONS",
    "ENTITY_MISLED_USAGE_SIGNAL_DEFINITIONS",
    "ENTITY_REQUIRED_FIELD_OPTIONAL_DEFINITIONS",
    "ENTITY_REQUIRED_FIELD_REPAIR_DEFINITIONS",
    "ENTITY_REVISION_MIGRATION_DEFINITIONS",
    "ENTITY_UPDATED_AT_DATETIME_MIGRATION_DEFINITIONS",
    "ENTITY_USAGE_SIGNAL_DEFINITIONS",
    "GRAPH_EDGES",
    "GRAPH_ENUM_ASSERTION_DEFINITIONS",
    "GRAPH_INDEX_PRUNE_DEFINITIONS",
    "GRAPH_SCHEMA_MIGRATIONS",
    "GRAPH_TABLES",
    "MEMORY_QUALITY_METADATA_MIGRATION_DEFINITIONS",
    "NODE_DEFINITIONS",
    "PARENT_TASK_CANONICALIZATION_DEFINITIONS",
    "RELATION_EDGE_CLEANUP_DEFINITIONS",
    "REMOVED_GRAPH_EDGES",
    "REMOVED_GRAPH_OBJECTS",
    "REMOVED_GRAPH_TABLES",
    "EmbeddingVectorField",
    "_graph_schema_migrations",
    "bootstrap_schema",
    "drop_all_indexes",
    "rebuild_embedding_indexes_for_dimension",
    "render_fulltext_compatible_sql",
    "render_surreal_compatible_sql",
]
