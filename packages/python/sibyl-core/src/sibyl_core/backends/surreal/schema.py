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
DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE option<string>;
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
DEFINE INDEX IF NOT EXISTS idx_entity_group ON entity FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_entity_type ON entity FIELDS entity_type;
DEFINE INDEX IF NOT EXISTS idx_entity_labels ON entity FIELDS labels;
DEFINE INDEX IF NOT EXISTS idx_entity_project ON entity FIELDS project_id;
DEFINE INDEX IF NOT EXISTS idx_entity_epic ON entity FIELDS epic_id;
DEFINE INDEX IF NOT EXISTS idx_entity_parent_task ON entity FIELDS parent_task_id;
DEFINE INDEX IF NOT EXISTS idx_entity_task ON entity FIELDS task_id;
DEFINE INDEX IF NOT EXISTS idx_entity_status ON entity FIELDS status;
DEFINE INDEX IF NOT EXISTS idx_entity_priority ON entity FIELDS priority;
DEFINE INDEX IF NOT EXISTS idx_entity_group_updated ON entity FIELDS group_id, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_updated ON entity FIELDS group_id, entity_type, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_project_updated ON entity FIELDS group_id, entity_type, project_id, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_epic_updated ON entity FIELDS group_id, entity_type, epic_id, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_parent_task_updated ON entity FIELDS group_id, entity_type, parent_task_id, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_status_updated ON entity FIELDS group_id, entity_type, status, updated_at, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_epic_status ON entity FIELDS group_id, entity_type, epic_id, status;
DEFINE INDEX IF NOT EXISTS idx_entity_group_type_project_status ON entity FIELDS group_id, entity_type, project_id, status;
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
DEFINE INDEX IF NOT EXISTS idx_episode_group ON episode FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_episode_created ON episode FIELDS created_at;
DEFINE INDEX IF NOT EXISTS idx_episode_content_ft ON episode FIELDS content FULLTEXT ANALYZER content_analyzer BM25;

DEFINE TABLE IF NOT EXISTS community SCHEMAFULL;
ALTER TABLE IF EXISTS community SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON community TYPE string;
DEFINE FIELD IF NOT EXISTS name ON community TYPE string;
DEFINE FIELD IF NOT EXISTS summary ON community TYPE option<string>;
DEFINE FIELD IF NOT EXISTS labels ON community TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS group_id ON community TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON community TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS name_embedding ON community TYPE option<array<float, {EMBEDDING_DIM}>>;

DEFINE INDEX IF NOT EXISTS idx_community_uuid ON community FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_community_group ON community FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_community_name_ft ON community FIELDS name FULLTEXT ANALYZER name_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_community_summary_ft ON community FIELDS summary FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_community_embedding ON community FIELDS name_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC {HNSW_EFC} M {HNSW_M};

DEFINE TABLE IF NOT EXISTS saga SCHEMAFULL;
ALTER TABLE IF EXISTS saga SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON saga TYPE string;
DEFINE FIELD IF NOT EXISTS name ON saga TYPE string;
DEFINE FIELD IF NOT EXISTS labels ON saga TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS group_id ON saga TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON saga TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_saga_uuid ON saga FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_saga_group ON saga FIELDS group_id;
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

DELETE FROM has_episode
WHERE in NOT IN (SELECT VALUE id FROM saga)
   OR out NOT IN (SELECT VALUE id FROM episode);

DELETE FROM next_episode
WHERE in NOT IN (SELECT VALUE id FROM episode)
   OR out NOT IN (SELECT VALUE id FROM episode);

DELETE FROM has_member
WHERE in NOT IN (SELECT VALUE id FROM community)
   OR (
       out NOT IN (SELECT VALUE id FROM entity)
       AND out NOT IN (SELECT VALUE id FROM community)
   );
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
DEFINE INDEX IF NOT EXISTS idx_relates_group ON relates_to FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_relates_group_source ON relates_to FIELDS group_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_relates_group_target ON relates_to FIELDS group_id, target_id;
DEFINE INDEX IF NOT EXISTS idx_relates_group_name_source ON relates_to FIELDS group_id, name, source_id;
DEFINE INDEX IF NOT EXISTS idx_relates_group_name_target ON relates_to FIELDS group_id, name, target_id;
DEFINE INDEX IF NOT EXISTS idx_relates_group_source_target_name ON relates_to FIELDS group_id, source_id, target_id, name;
DEFINE INDEX IF NOT EXISTS idx_relates_group_source_created ON relates_to FIELDS group_id, source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_group_target_created ON relates_to FIELDS group_id, target_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_group_created ON relates_to FIELDS group_id, created_at, uuid;
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
DEFINE INDEX IF NOT EXISTS idx_mentions_group ON mentions FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_source ON mentions FIELDS group_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_target ON mentions FIELDS group_id, target_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_source_created ON mentions FIELDS group_id, source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_target_created ON mentions FIELDS group_id, target_id, created_at, uuid;

DEFINE TABLE OVERWRITE has_episode SCHEMAFULL TYPE RELATION IN saga OUT episode ENFORCED;
ALTER TABLE IF EXISTS has_episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON has_episode TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON has_episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON has_episode TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_has_ep_uuid ON has_episode FIELDS uuid UNIQUE;

DEFINE TABLE OVERWRITE next_episode SCHEMAFULL TYPE RELATION IN episode OUT episode ENFORCED;
ALTER TABLE IF EXISTS next_episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON next_episode TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON next_episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON next_episode TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_next_ep_uuid ON next_episode FIELDS uuid UNIQUE;

DEFINE TABLE OVERWRITE has_member SCHEMAFULL TYPE RELATION IN community OUT entity | community ENFORCED;
ALTER TABLE IF EXISTS has_member SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON has_member TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON has_member TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON has_member TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_has_member_uuid ON has_member FIELDS uuid UNIQUE;
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

DEFINE INDEX IF NOT EXISTS idx_relates_group_source_created ON relates_to FIELDS group_id, source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_relates_group_target_created ON relates_to FIELDS group_id, target_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_source ON mentions FIELDS group_id, source_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_target ON mentions FIELDS group_id, target_id;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_source_created ON mentions FIELDS group_id, source_id, created_at, uuid;
DEFINE INDEX IF NOT EXISTS idx_mentions_group_target_created ON mentions FIELDS group_id, target_id, created_at, uuid;
"""


RELATION_ENDPOINT_BACKFILL_DEFINITIONS = """
UPDATE relates_to SET
    source_id = source_id ?? in.uuid,
    target_id = target_id ?? out.uuid
WHERE source_id = NONE OR target_id = NONE;

UPDATE mentions SET
    source_id = source_id ?? in.uuid,
    target_id = target_id ?? out.uuid
WHERE source_id = NONE OR target_id = NONE;
"""


CURRENT_SCHEMA_MAINTENANCE_DEFINITIONS = (
    ENTITY_DENORMALIZATION_MAINTENANCE_DEFINITIONS + RELATION_ENDPOINT_BACKFILL_DEFINITIONS
)


GRAPH_TABLES = ("entity", "episode", "community", "saga")
GRAPH_EDGES = ("relates_to", "mentions", "has_episode", "next_episode", "has_member")
GRAPH_SCHEMA_MIGRATIONS = (
    SchemaMigration(
        version=2,
        name="graph_schema_bootstrap",
    ),
    SchemaMigration(
        version=GRAPH_SCHEMA_CURRENT_VERSION,
        name="relation_endpoint_denormalization",
        statements=tuple(
            split_statements(
                RELATION_ENDPOINT_SCHEMA_DEFINITIONS + "\n" + RELATION_ENDPOINT_BACKFILL_DEFINITIONS
            )
        ),
    ),
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
        table="community", field="name_embedding", index="idx_community_embedding"
    ),
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


def render_fulltext_compatible_sql(sql: str, *, url: str) -> str:
    if url.startswith(("memory://", "surrealkv://")):
        return sql.replace("FULLTEXT ANALYZER", "SEARCH ANALYZER")
    return sql


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
        for table in (*GRAPH_EDGES, *GRAPH_TABLES, SCHEMA_VERSION_TABLE):
            await driver.execute_query(f"REMOVE TABLE IF EXISTS {table};")
    else:
        await ensure_schema_version_table(driver.execute_query, group_id=driver.group_id)
        current_version = await get_schema_version(driver.execute_query)
        if current_version >= GRAPH_SCHEMA_CURRENT_VERSION:
            if not force:
                await _reconcile_embedding_dimension(driver)
                return
        elif current_version > 0 and not force:
            await apply_schema_migrations(
                driver.execute_query,
                GRAPH_SCHEMA_MIGRATIONS,
                group_id=driver.group_id,
            )
            await _reconcile_embedding_dimension(driver)
            return

    compatible_blocks = (
        ANALYZER_DEFINITIONS,
        render_fulltext_compatible_sql(NODE_DEFINITIONS, url=driver._url),
        RELATION_EDGE_CLEANUP_DEFINITIONS,
        render_fulltext_compatible_sql(EDGE_DEFINITIONS, url=driver._url),
    )
    for block in compatible_blocks:
        await _execute_graph_schema_block(
            driver,
            block,
            ignore_missing_relation_tables=block == RELATION_EDGE_CLEANUP_DEFINITIONS,
        )
    await apply_schema_migrations(
        driver.execute_query,
        GRAPH_SCHEMA_MIGRATIONS,
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
    "EDGE_DEFINITIONS",
    "EMBEDDING_DIM",
    "EMBEDDING_VECTOR_FIELDS",
    "GRAPH_EDGES",
    "GRAPH_SCHEMA_MIGRATIONS",
    "GRAPH_TABLES",
    "NODE_DEFINITIONS",
    "RELATION_EDGE_CLEANUP_DEFINITIONS",
    "EmbeddingVectorField",
    "bootstrap_schema",
    "drop_all_indexes",
    "rebuild_embedding_indexes_for_dimension",
    "render_fulltext_compatible_sql",
]
