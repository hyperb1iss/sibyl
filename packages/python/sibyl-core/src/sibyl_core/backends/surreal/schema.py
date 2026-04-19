"""SurrealDB schema bootstrap for Sibyl's knowledge graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.driver import SurrealDriver


EMBEDDING_DIM = 1536

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
DEFINE FIELD IF NOT EXISTS uuid ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS name ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS entity_type ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS summary ON entity TYPE option<string>;
DEFINE FIELD IF NOT EXISTS labels ON entity TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS attributes ON entity FLEXIBLE TYPE object DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS group_id ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS name_embedding ON entity TYPE option<array<float, {EMBEDDING_DIM}>>;

DEFINE INDEX IF NOT EXISTS idx_entity_uuid ON entity FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_entity_group ON entity FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_entity_type ON entity FIELDS entity_type;
DEFINE INDEX IF NOT EXISTS idx_entity_labels ON entity FIELDS labels;
DEFINE INDEX IF NOT EXISTS idx_entity_name_ft ON entity FIELDS name
    SEARCH ANALYZER name_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_entity_embedding ON entity FIELDS name_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;

DEFINE TABLE IF NOT EXISTS episode SCHEMAFULL;
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
DEFINE INDEX IF NOT EXISTS idx_episode_content_ft ON episode FIELDS content
    SEARCH ANALYZER content_analyzer BM25;

DEFINE TABLE IF NOT EXISTS community SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON community TYPE string;
DEFINE FIELD IF NOT EXISTS name ON community TYPE string;
DEFINE FIELD IF NOT EXISTS summary ON community TYPE option<string>;
DEFINE FIELD IF NOT EXISTS labels ON community TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS group_id ON community TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON community TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS name_embedding ON community TYPE option<array<float, {EMBEDDING_DIM}>>;

DEFINE INDEX IF NOT EXISTS idx_community_uuid ON community FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_community_group ON community FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_community_embedding ON community FIELDS name_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32;

DEFINE TABLE IF NOT EXISTS saga SCHEMAFULL;
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
DEFINE FIELD IF NOT EXISTS uuid ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS name ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS fact ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS fact_embedding ON relates_to TYPE option<array<float, {EMBEDDING_DIM}>>;
DEFINE FIELD IF NOT EXISTS group_id ON relates_to TYPE string;
DEFINE FIELD IF NOT EXISTS episodes ON relates_to TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS attributes ON relates_to FLEXIBLE TYPE object DEFAULT {{}};
DEFINE FIELD IF NOT EXISTS created_at ON relates_to TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expired_at ON relates_to TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS valid_at ON relates_to TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS invalid_at ON relates_to TYPE option<datetime>;

DEFINE INDEX IF NOT EXISTS idx_relates_uuid ON relates_to FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_relates_group ON relates_to FIELDS group_id;
DEFINE INDEX IF NOT EXISTS idx_relates_fact_ft ON relates_to FIELDS fact
    SEARCH ANALYZER content_analyzer BM25;
DEFINE INDEX IF NOT EXISTS idx_relates_fact_embedding ON relates_to FIELDS fact_embedding
    HNSW DIMENSION {EMBEDDING_DIM} DIST COSINE TYPE F32 EFC 150 M 12;

DEFINE TABLE IF NOT EXISTS mentions TYPE RELATION IN episode OUT entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON mentions TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON mentions TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON mentions TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mentions_uuid ON mentions FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_mentions_group ON mentions FIELDS group_id;

DEFINE TABLE IF NOT EXISTS has_episode TYPE RELATION IN saga OUT episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON has_episode TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON has_episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON has_episode TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_has_ep_uuid ON has_episode FIELDS uuid UNIQUE;

DEFINE TABLE IF NOT EXISTS next_episode TYPE RELATION IN episode OUT episode SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON next_episode TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON next_episode TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON next_episode TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_next_ep_uuid ON next_episode FIELDS uuid UNIQUE;

DEFINE TABLE IF NOT EXISTS has_member TYPE RELATION IN community OUT entity | community SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON has_member TYPE string;
DEFINE FIELD IF NOT EXISTS group_id ON has_member TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON has_member TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_has_member_uuid ON has_member FIELDS uuid UNIQUE;
"""


GRAPH_TABLES = ("entity", "episode", "community", "saga")
GRAPH_EDGES = ("relates_to", "mentions", "has_episode", "next_episode", "has_member")


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buffer.append(raw_line)
        if line.endswith(";"):
            stmt = "\n".join(buffer).strip()
            if stmt.rstrip(";").strip():
                statements.append(stmt)
            buffer = []
    trailing = "\n".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


async def bootstrap_schema(driver: SurrealDriver, *, reset: bool = False) -> None:
    if not driver.group_id:
        msg = "bootstrap_schema requires driver.clone(group_id) first"
        raise ValueError(msg)

    if reset:
        for table in (*GRAPH_EDGES, *GRAPH_TABLES):
            await driver.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    for block in (ANALYZER_DEFINITIONS, NODE_DEFINITIONS, EDGE_DEFINITIONS):
        for statement in _split_statements(block):
            await driver.execute_query(statement)


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
]

