"""Versioned schema helpers for SurrealDB bootstrap."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Protocol, cast

from sibyl_core.backends.surreal.schema_helpers import execute_schema_statement, split_statements

GRAPH_SCHEMA_CURRENT_VERSION = 15
GRAPH_SCHEMA_NAME = "graph"
SCHEMA_VERSION_TABLE = "schema_version"

SCHEMA_VERSION_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS schema_version SCHEMAFULL;
ALTER TABLE IF EXISTS schema_version SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name ON schema_version TYPE string;
DEFINE FIELD IF NOT EXISTS version ON schema_version TYPE int;
DEFINE FIELD IF NOT EXISTS embedding_dimension ON schema_version TYPE option<int>;
DEFINE FIELD IF NOT EXISTS migrations ON schema_version TYPE array<object> DEFAULT [];
DEFINE FIELD IF NOT EXISTS migrations.*.version ON schema_version TYPE int;
DEFINE FIELD IF NOT EXISTS migrations.*.name ON schema_version TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON schema_version TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON schema_version TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS idx_schema_version_name ON schema_version FIELDS name UNIQUE;
"""

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SurrealExecute(Protocol):
    def __call__(self, statement: str, /, **params: object) -> Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    version: int
    name: str
    statements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConcurrentIndexDefinition:
    name: str
    table: str
    definition: str


@dataclass(frozen=True, slots=True)
class IndexBuildStatus:
    status: str
    initial: int | None = None
    pending: int | None = None
    updated: int | None = None


async def ensure_schema_version_table(
    execute_query: SurrealExecute,
    *,
    group_id: str | None = None,
    scope: str = "schema_version",
) -> None:
    for statement in split_statements(SCHEMA_VERSION_DEFINITIONS):
        await execute_schema_statement(
            execute_query,
            statement,
            scope=scope,
            group_id=group_id,
        )


async def get_schema_version(
    execute_query: SurrealExecute,
    *,
    name: str = GRAPH_SCHEMA_NAME,
) -> int:
    result = await execute_query(
        "SELECT version FROM schema_version WHERE name = $name LIMIT 1;",
        name=name,
    )
    first = _first_record(result)
    if first is None:
        return 0
    raw_version = first.get("version")
    return int(raw_version) if isinstance(raw_version, int | float | str) else 0


async def record_schema_version(
    execute_query: SurrealExecute,
    *,
    version: int,
    migrations: Sequence[SchemaMigration],
    name: str = GRAPH_SCHEMA_NAME,
    embedding_dimension: int | None = None,
) -> None:
    record_id = schema_version_record_id(name)
    migration_payload = [
        {"version": migration.version, "name": migration.name} for migration in migrations
    ]
    await execute_query(
        f"""
        UPSERT {record_id} SET
            name = $name,
            version = $version,
            embedding_dimension = $embedding_dimension ?? embedding_dimension,
            migrations = $migrations,
            created_at = created_at ?? time::now(),
            updated_at = time::now();
        """,
        name=name,
        version=version,
        embedding_dimension=embedding_dimension,
        migrations=migration_payload,
    )


async def get_schema_embedding_dimension(
    execute_query: SurrealExecute,
    *,
    name: str = GRAPH_SCHEMA_NAME,
) -> int | None:
    result = await execute_query(
        "SELECT embedding_dimension FROM schema_version WHERE name = $name LIMIT 1;",
        name=name,
    )
    first = _first_record(result)
    if first is None:
        return None
    raw_dimension = first.get("embedding_dimension")
    return int(raw_dimension) if isinstance(raw_dimension, int | float | str) else None


async def apply_schema_migrations(
    execute_query: SurrealExecute,
    migrations: Sequence[SchemaMigration],
    *,
    name: str = GRAPH_SCHEMA_NAME,
    group_id: str | None = None,
    scope: str = "schema_migration",
) -> list[SchemaMigration]:
    await ensure_schema_version_table(
        execute_query,
        group_id=group_id,
        scope=f"{scope}_version",
    )
    current_version = await get_schema_version(execute_query, name=name)
    applied: list[SchemaMigration] = []
    sorted_migrations = sorted(migrations, key=lambda item: item.version)
    for migration in sorted_migrations:
        if migration.version <= current_version:
            continue
        for statement in migration.statements:
            await execute_schema_statement(
                execute_query,
                statement,
                scope=scope,
                group_id=group_id,
            )
        applied.append(migration)
        migration_history = [
            item for item in sorted_migrations if item.version <= migration.version
        ]
        await record_schema_version(
            execute_query,
            version=migration.version,
            migrations=migration_history,
            name=name,
        )
    return applied


async def rebuild_index_concurrently(
    execute_query: SurrealExecute,
    definition: ConcurrentIndexDefinition,
) -> None:
    _validate_identifier(definition.name)
    _validate_identifier(definition.table)
    await execute_query(f"REMOVE INDEX IF EXISTS {definition.name} ON TABLE {definition.table};")
    await execute_query(_with_concurrently(definition.definition))


async def get_index_build_status(
    execute_query: SurrealExecute,
    *,
    name: str,
    table: str,
) -> IndexBuildStatus | None:
    _validate_identifier(name)
    _validate_identifier(table)
    result = await execute_query(f"INFO FOR INDEX {name} ON {table};")
    record = _first_record(result)
    if record is None:
        return None
    building = record.get("building")
    if not isinstance(building, Mapping):
        return None
    building_map = cast(Mapping[object, object], building)
    status = str(building_map.get("status") or "unknown")
    return IndexBuildStatus(
        status=status,
        initial=_optional_int(building_map.get("initial")),
        pending=_optional_int(building_map.get("pending")),
        updated=_optional_int(building_map.get("updated")),
    )


async def wait_for_index_ready(
    execute_query: SurrealExecute,
    *,
    name: str,
    table: str,
    timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 1.0,
) -> IndexBuildStatus | None:
    deadline = monotonic() + timeout_seconds
    last_status: IndexBuildStatus | None = None
    while monotonic() < deadline:
        status = await get_index_build_status(execute_query, name=name, table=table)
        if status is None or status.status in {"ready", "built"}:
            return status
        if status.status == "error":
            msg = f"index {name} on {table} failed to build"
            raise RuntimeError(msg)
        last_status = status
        await asyncio.sleep(poll_interval_seconds)
    msg = f"timed out waiting for index {name} on {table}: {last_status}"
    raise TimeoutError(msg)


def _with_concurrently(definition: str) -> str:
    body = definition.strip().rstrip(";").strip()
    if " CONCURRENTLY" in f" {body.upper()} ":
        return f"{body};"
    upper = body.upper()
    if upper.endswith(" DEFER"):
        return f"{body[:-6].rstrip()} CONCURRENTLY DEFER;"
    return f"{body} CONCURRENTLY;"


def _first_record(result: object) -> Mapping[str, object] | None:
    if isinstance(result, Mapping):
        return cast(Mapping[str, object], result)
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, Mapping):
            return cast(Mapping[str, object], first)
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _validate_identifier(value: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value):
        msg = f"invalid SurrealDB identifier: {value!r}"
        raise ValueError(msg)


def schema_version_record_id(name: str) -> str:
    _validate_identifier(name)
    return f"{SCHEMA_VERSION_TABLE}:{name}"


__all__ = [
    "GRAPH_SCHEMA_CURRENT_VERSION",
    "GRAPH_SCHEMA_NAME",
    "SCHEMA_VERSION_DEFINITIONS",
    "SCHEMA_VERSION_TABLE",
    "ConcurrentIndexDefinition",
    "IndexBuildStatus",
    "SchemaMigration",
    "apply_schema_migrations",
    "ensure_schema_version_table",
    "get_index_build_status",
    "get_schema_embedding_dimension",
    "get_schema_version",
    "rebuild_index_concurrently",
    "record_schema_version",
    "schema_version_record_id",
    "wait_for_index_ready",
]
