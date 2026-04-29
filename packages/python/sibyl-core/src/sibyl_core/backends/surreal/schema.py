"""SurrealDB schema bootstrap for Sibyl's knowledge graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.schema_helpers import execute_schema_statement, split_statements
from sibyl_core.config import core_config

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.driver import SurrealDriver


# Graph node embeddings (entity/community/relationship facts) come from Graphiti's
# embedder, which is configured separately from the OpenAI chunk embedder. Default
# is 1024-dim; override via SIBYL_GRAPH_EMBEDDING_DIMENSIONS.
EMBEDDING_DIM = core_config.graph_embedding_dimensions
_EMBEDDED_SURREAL_SCHEMES = ("memory://", "surrealkv://")

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
DEFINE FIELD IF NOT EXISTS labels ON entity TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS attributes ON entity TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS group_id ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS project_id ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS epic_id ON entity TYPE option<string>;
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
DEFINE INDEX IF NOT EXISTS idx_entity_task ON entity FIELDS task_id;
DEFINE INDEX IF NOT EXISTS idx_entity_status ON entity FIELDS status;
DEFINE INDEX IF NOT EXISTS idx_entity_priority ON entity FIELDS priority;
DEFINE INDEX IF NOT EXISTS idx_entity_name_ft ON entity FIELDS name FULLTEXT ANALYZER name_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_summary_ft ON entity FIELDS summary FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_description_ft ON entity FIELDS attributes.description FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_content_ft ON entity FIELDS attributes.content FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_embedding ON entity FIELDS name_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;

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
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32;

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


EDGE_DEFINITIONS = f"""
DEFINE TABLE IF NOT EXISTS relates_to TYPE RELATION IN entity OUT entity SCHEMAFULL;
ALTER TABLE IF EXISTS relates_to SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS name ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS fact ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS fact_embedding ON relates_to TYPE option<array<float, {EMBEDDING_DIM}>>;
DEFINE FIELD IF NOT EXISTS group_id ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS episodes ON relates_to TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS attributes ON relates_to TYPE object FLEXIBLE DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS created_at ON relates_to TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expired_at ON relates_to TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS valid_at ON relates_to TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS invalid_at ON relates_to TYPE option<datetime>;

DEFINE INDEX IF NOT EXISTS idx_relates_uuid ON relates_to FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_relates_group ON relates_to FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_relates_fact_ft ON relates_to FIELDS fact FULLTEXT ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_relates_fact_embedding ON relates_to FIELDS fact_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;

DEFINE TABLE IF NOT EXISTS mentions TYPE RELATION IN episode OUT entity SCHEMAFULL;
ALTER TABLE IF EXISTS mentions SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON mentions TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON mentions TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON mentions TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mentions_uuid ON mentions FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_mentions_group ON mentions FIELDS group_id;

DEFINE TABLE IF NOT EXISTS has_episode TYPE RELATION IN saga OUT episode SCHEMAFULL;
ALTER TABLE IF EXISTS has_episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON has_episode TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON has_episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON has_episode TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_has_ep_uuid ON has_episode FIELDS uuid UNIQUE;

DEFINE TABLE IF NOT EXISTS next_episode TYPE RELATION IN episode OUT episode SCHEMAFULL;
ALTER TABLE IF EXISTS next_episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON next_episode TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON next_episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON next_episode TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_next_ep_uuid ON next_episode FIELDS uuid UNIQUE;

DEFINE TABLE IF NOT EXISTS has_member TYPE RELATION IN community OUT entity | community SCHEMAFULL;
ALTER TABLE IF EXISTS has_member SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON has_member TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON has_member TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON has_member TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_has_member_uuid ON has_member FIELDS uuid UNIQUE;
"""


GRAPH_TABLES = ("entity", "episode", "community", "saga")
GRAPH_EDGES = ("relates_to", "mentions", "has_episode", "next_episode", "has_member")


def render_fulltext_compatible_sql(sql: str, *, url: str) -> str:
    fulltext_keyword = "SEARCH" if url.startswith(_EMBEDDED_SURREAL_SCHEMES) else "FULLTEXT"
    return sql.replace("FULLTEXT ANALYZER", f"{fulltext_keyword} ANALYZER")


async def bootstrap_schema(driver: SurrealDriver, *, reset: bool = False) -> None:
    if not driver.group_id:
        msg = "bootstrap_schema requires driver.clone(group_id) first"
        raise ValueError(msg)

    if reset:
        for table in (*GRAPH_EDGES, *GRAPH_TABLES):
            await driver.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    compatible_blocks = (
        ANALYZER_DEFINITIONS,
        render_fulltext_compatible_sql(NODE_DEFINITIONS, url=driver._url),
        render_fulltext_compatible_sql(EDGE_DEFINITIONS, url=driver._url),
    )
    for block in compatible_blocks:
        for statement in split_statements(block):
            await execute_schema_statement(
                driver.execute_query,
                statement,
                scope="graph",
                group_id=driver.group_id,
            )


async def drop_all_indexes(driver: SurrealDriver) -> None:
    if not driver.group_id:
        return

    for table in (*GRAPH_TABLES, *GRAPH_EDGES):
        info = await driver.execute_query(f"INFO FOR TABLE {table};")
        indexes: dict[str, object] = {}
        if isinstance(info, dict):
            indexes = info.get("indexes", {}) or {}
        elif isinstance(info, list) and info and isinstance(info[0], dict):
            indexes = info[0].get("indexes", {}) or {}

        for index_name in indexes:
            await driver.execute_query(f"REMOVE INDEX IF EXISTS {index_name} ON TABLE {table};")


__all__ = [
    "ANALYZER_DEFINITIONS",
    "EDGE_DEFINITIONS",
    "EMBEDDING_DIM",
    "GRAPH_EDGES",
    "GRAPH_TABLES",
    "NODE_DEFINITIONS",
    "bootstrap_schema",
    "drop_all_indexes",
    "render_fulltext_compatible_sql",
]
