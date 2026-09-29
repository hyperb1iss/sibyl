"""Indexed raw evidence corpora maintained with canonical capture mutations."""

from __future__ import annotations

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_helpers import split_statements
from sibyl_core.backends.surreal.schema_version import SurrealExecute

RAW_LEXICAL_TABLES = ("raw_lexical_originals", "raw_lexical_reflections")
RAW_LEXICAL_STATE_TABLE = "raw_lexical_states"
_BACKFILL_BATCH_SIZE = 128
RAW_LEXICAL_READ_FIELDS = (
    "uuid",
    "revision",
    "organization_id",
    "source_id",
    "principal_id",
    "memory_scope",
    "scope_key",
    "agent_id",
    "project_id",
    "review_state",
    "entity_id",
    "entity_type",
    "title",
    "raw_content",
    "tags",
    "metadata",
    "provenance",
    "capture_surface",
    "created_by_user_id",
    "captured_at",
    "deleted_at",
    "purge_after",
    "last_recalled_at",
    "last_used_at",
    "retrieval_count",
    "citation_count",
    "misled_count",
    "created_at",
)


def _sync_body() -> str:
    # The unindexed state is written even for a draft or purge. Backfill and
    # every canonical event therefore conflict on the same key, including
    # races where neither indexed corpus contained a row yet.
    projection = ", ".join(f"{field}: $source.{field}" for field in RAW_LEXICAL_READ_FIELDS)
    return """
    LET $key = crypto::sha256(type::string($capture_id));
    LET $original = type::record(string::concat('raw_lexical_originals:', $key));
    LET $reflection = type::record(string::concat('raw_lexical_reflections:', $key));
    LET $fence = type::record(string::concat('raw_lexical_states:', $key));
    LET $state = (SELECT * FROM $fence)[0];
    UPSERT $fence SET organization_id = $source.organization_id ?? $state.organization_id,
        generation = ($state.generation ?? 0) + 1, deleted = $deleted;
    LET $candidate = string::lowercase(string::trim(type::string(
        $source.capture_surface ?? $source.metadata.capture_surface ?? '')))
        = 'reflection_candidate';
    IF $deleted OR ($candidate AND $source.review_state != 'promoted') {
        DELETE $original;
        DELETE $reflection;
    } ELSE {
        LET $row = {record_id: $capture_id, __READ_FIELDS__};
        IF $candidate {
            DELETE $original;
            UPSERT $reflection CONTENT $row;
        } ELSE {
            DELETE $reflection;
            UPSERT $original CONTENT $row;
        };
    };
""".replace("__READ_FIELDS__", projection)


def raw_lexical_definitions(raw_schema: str) -> str:
    """Mirror read columns and policy metadata without copying capture vectors.

    Sharing canonical field declarations keeps filtering before LIMIT
    identical in each lane. These rows are disposable indexes; record_id
    names the canonical capture, never a new source identity.
    """
    fields = [
        statement
        for statement in split_statements(raw_schema)
        if statement.startswith("DEFINE FIELD IF NOT EXISTS ")
        and " ON raw_captures " in statement
        and statement.split()[5] in RAW_LEXICAL_READ_FIELDS
    ]
    definitions = []
    for table in RAW_LEXICAL_TABLES:
        definitions.extend(
            [
                f"DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL;",
                f"ALTER TABLE IF EXISTS {table} SCHEMAFULL;",
                f"ALTER TABLE IF EXISTS {table} PERMISSIONS NONE;",
                *(field.replace(" ON raw_captures ", f" ON {table} ") for field in fields),
                f"DEFINE FIELD IF NOT EXISTS record_id ON {table} TYPE record<raw_captures>;",
                f"DEFINE INDEX IF NOT EXISTS idx_{table}_uuid ON {table} FIELDS uuid UNIQUE;",
                f"DEFINE INDEX IF NOT EXISTS idx_{table}_title_ft ON {table} "
                "FIELDS title FULLTEXT ANALYZER title_analyzer BM25 HIGHLIGHTS;",
                f"DEFINE INDEX IF NOT EXISTS idx_{table}_content_ft ON {table} "
                "FIELDS raw_content FULLTEXT ANALYZER content_analyzer BM25 HIGHLIGHTS;",
            ]
        )
    definitions.extend(
        [
            "DEFINE TABLE IF NOT EXISTS raw_lexical_states SCHEMAFULL;",
            "ALTER TABLE IF EXISTS raw_lexical_states SCHEMAFULL;",
            "ALTER TABLE IF EXISTS raw_lexical_states PERMISSIONS NONE;",
            "DEFINE FIELD IF NOT EXISTS organization_id ON raw_lexical_states TYPE string;",
            "DEFINE FIELD IF NOT EXISTS generation ON raw_lexical_states TYPE int;",
            "DEFINE FIELD IF NOT EXISTS deleted ON raw_lexical_states TYPE bool;",
            "DEFINE EVENT IF NOT EXISTS maintain_raw_lexical ON raw_captures WHEN true THEN {"
            " LET $source = IF $event = 'DELETE' THEN $before ELSE $after END;"
            " LET $capture_id = $source.id;"
            " LET $deleted = $event = 'DELETE';" + " ".join(_sync_body().split()) + "};",
        ]
    )
    return "\n".join(definitions)


async def migrate_raw_lexical(execute_query: SurrealExecute) -> None:
    """Backfill current records without changing canonical content or revisions.

    The event is installed first. Each batch rereads captures in the same
    transaction that writes indexed copies and conflict fences. A concurrent
    mutation aborts the batch instead of publishing stale content; schema
    version advancement waits for the entire idempotent backfill.
    """
    cursor: str | None = None
    while True:
        predicate = " WHERE id > type::record($cursor)" if cursor is not None else ""
        rows = normalize_records(
            await execute_query(
                "SELECT id, type::string(id) AS capture_record_id FROM raw_captures"
                + predicate
                + " ORDER BY id LIMIT $batch_size;",
                cursor=cursor,
                batch_size=_BACKFILL_BATCH_SIZE,
            )
        )
        if not rows:
            return
        record_ids = [str(row["capture_record_id"]) for row in rows]
        await execute_query(
            "BEGIN TRANSACTION; FOR $record_id IN $record_ids {"
            " LET $capture_id = type::record($record_id);"
            " LET $source = (SELECT * FROM $capture_id)[0];"
            " LET $deleted = $source = NONE;" + _sync_body() + "}; COMMIT TRANSACTION;",
            record_ids=record_ids,
        )
        cursor = record_ids[-1]
