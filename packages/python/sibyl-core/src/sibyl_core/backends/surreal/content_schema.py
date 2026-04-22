"""SurrealDB schema bootstrap for Sibyl content storage."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM, render_fulltext_compatible_sql

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.content_client import SurrealContentClient


CONTENT_TABLES = (
    "crawl_sources",
    "crawled_documents",
    "document_chunks",
    "raw_captures",
    "system_settings",
    "backup_settings",
    "backups",
)

_SCHEMA_DIR = Path(__file__).with_name("schemas") / "content"


def _load_schema_file(filename: str) -> str:
    return (_SCHEMA_DIR / filename).read_text(encoding="utf-8").format(
        EMBEDDING_DIM=EMBEDDING_DIM
    )


CONTENT_ANALYZER_DEFINITIONS = _load_schema_file("01_analyzers.surql")
CONTENT_SCHEMA_DEFINITIONS = _load_schema_file("10_tables.surql")


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buffer.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement.rstrip(";").strip():
                statements.append(statement)
            buffer = []
    trailing = "\n".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


async def bootstrap_content_schema(client: SurrealContentClient, *, reset: bool = False) -> None:
    if reset:
        for table in CONTENT_TABLES:
            await client.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    compatible_schema = render_fulltext_compatible_sql(
        CONTENT_SCHEMA_DEFINITIONS,
        url=getattr(client, "_url", ""),
    )
    for block in (CONTENT_ANALYZER_DEFINITIONS, compatible_schema):
        for statement in _split_statements(block):
            await client.execute_query(statement)


__all__ = [
    "CONTENT_ANALYZER_DEFINITIONS",
    "CONTENT_SCHEMA_DEFINITIONS",
    "CONTENT_TABLES",
    "bootstrap_content_schema",
]
