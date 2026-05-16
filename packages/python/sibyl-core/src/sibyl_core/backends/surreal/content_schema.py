"""SurrealDB schema bootstrap for Sibyl content storage."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.schema import render_fulltext_compatible_sql
from sibyl_core.backends.surreal.schema_helpers import execute_schema_statement, split_statements
from sibyl_core.config import core_config

# Document chunks use the OpenAI embedder dimension (text-embedding-3-small = 1536),
# which differs from the graph node embedder dimension. Keep them as separate
# constants so a graph dim change can't silently break content search and vice versa.
EMBEDDING_DIM = core_config.embedding_dimensions

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.content_client import SurrealContentClient


CONTENT_TABLES = (
    "crawl_sources",
    "crawled_documents",
    "document_chunks",
    "raw_captures",
    "api_idempotency_records",
    "source_imports",
    "system_settings",
    "telemetry_rollups",
    "backup_settings",
    "backups",
)

_SCHEMA_DIR = Path(__file__).with_name("schemas") / "content"


def _load_schema_file(filename: str) -> str:
    return (_SCHEMA_DIR / filename).read_text(encoding="utf-8").format(EMBEDDING_DIM=EMBEDDING_DIM)


CONTENT_ANALYZER_DEFINITIONS = _load_schema_file("01_analyzers.surql")
CONTENT_SCHEMA_DEFINITIONS = _load_schema_file("10_tables.surql")


async def bootstrap_content_schema(client: SurrealContentClient, *, reset: bool = False) -> None:
    if reset:
        for table in CONTENT_TABLES:
            await client.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    compatible_schema = render_fulltext_compatible_sql(
        CONTENT_SCHEMA_DEFINITIONS,
        url=getattr(client, "_url", ""),
    )
    for block in (CONTENT_ANALYZER_DEFINITIONS, compatible_schema):
        for statement in split_statements(block):
            await execute_schema_statement(client.execute_query, statement, scope="content")


__all__ = [
    "CONTENT_ANALYZER_DEFINITIONS",
    "CONTENT_SCHEMA_DEFINITIONS",
    "CONTENT_TABLES",
    "bootstrap_content_schema",
]
