"""Shared SurrealDB schema bootstrap helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from sibyl_core.utils.log_safety import fingerprint_text

log = structlog.get_logger()


def split_statements(sql: str) -> list[str]:
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


def is_duplicate_unique_index_error(statement: str, error: Exception) -> bool:
    if " UNIQUE" not in statement.upper():
        return False
    return "already contains" in str(error).lower()


def is_missing_table_error(error: Exception) -> bool:
    message = str(error).lower()
    return "the table" in message and "does not exist" in message


async def execute_schema_statement(
    execute_query: Callable[[str], Awaitable[object]],
    statement: str,
    *,
    scope: str,
    group_id: str | None = None,
) -> None:
    try:
        await execute_query(statement)
    except Exception as exc:
        if not is_duplicate_unique_index_error(statement, exc):
            raise
        fields = {
            "schema_scope": scope,
            "statement_hash": fingerprint_text(statement),
            "error_hash": fingerprint_text(str(exc)),
            "error_type": type(exc).__name__,
        }
        if group_id:
            fields["group_id"] = group_id
        log.warning("surreal_schema_unique_index_skipped", **fields)
