"""Shared Surreal content client lifecycle and scoped query primitives."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import cast
from weakref import WeakKeyDictionary

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal import records as _surreal_records
from sibyl_core.backends.surreal.schema_embedding_states import CONTENT_SCHEMA_RECORD_NAME
from sibyl_core.backends.surreal.schema_version import schema_version_record_id
from sibyl_core.config import settings
from sibyl_core.runtime_ports import RuntimePortUnavailable, get_content_port
from sibyl_core.services import content_models as models

CONTENT_KNN_EF_FLOOR = 40

DEFAULT_BATCH_SIZE = 128

# SurrealDB 3.x unions one index lookup per value of an `IN` list only up to
# 32 values; a longer list is planned as a full table scan. Statements whose
# only usable index is the single-column uuid index batch at the planner's
# cap. Statements that lead with an organization-prefixed composite keep the
# default: the planner walks that prefix and filters the list, so the cap would
# only multiply round trips.
ID_LOOKUP_BATCH_SIZE = 32

DIRECT_SEARCH_QUERY_TIMEOUT_SECONDS = 3.0

LIFECYCLE_FILTER_OVERFETCH_FACTOR = 4

normalize_record = _surreal_records.normalize_record

normalize_records = _surreal_records.normalize_records


@dataclass(slots=True)
class _SharedContentClientState:
    client: SurrealContentClient | None = None


_shared_content_client_state = _SharedContentClientState()

_shared_content_client_lock = asyncio.Lock()

# The highest content schema version each client has read. Versions only
# move up, so a version once read vouches for every requirement at or below
# it for the life of the client, and the scheduled passes stop re-reading the
# version record for every organization every minute.
_schema_versions: WeakKeyDictionary[SurrealContentClient, int] = WeakKeyDictionary()

_UPSERT_RECORD = {
    "crawl_sources": (
        "UPSERT crawl_sources CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id;"
    ),
    "crawled_documents": (
        "UPSERT crawled_documents CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id;"
    ),
    "raw_captures": (
        "UPSERT raw_captures CONTENT $record "
        "WHERE uuid = $uuid AND organization_id = $organization_id;"
    ),
}


def build_surreal_content_client() -> SurrealContentClient:
    return SurrealContentClient(
        url=settings.require_serviceable_surreal_url(),
        username=settings.surreal_username,
        password=settings.surreal_password.get_secret_value(),
        token=settings.surreal_token.get_secret_value(),
        pool_size=settings.surreal_client_pool_size("content"),
    )


async def get_shared_surreal_content_client() -> SurrealContentClient:
    try:
        content_port = get_content_port()
    except RuntimePortUnavailable:
        pass
    else:
        return await content_port.get_shared_client()

    if _shared_content_client_state.client is not None:
        return _shared_content_client_state.client

    async with _shared_content_client_lock:
        if _shared_content_client_state.client is None:
            _shared_content_client_state.client = build_surreal_content_client()
        return _shared_content_client_state.client


async def close_shared_surreal_content_client() -> None:
    async with _shared_content_client_lock:
        client = _shared_content_client_state.client
        _shared_content_client_state.client = None
        if client is not None:
            await client.close()


@asynccontextmanager
async def surreal_content_client() -> AsyncIterator[SurrealContentClient]:
    yield await get_shared_surreal_content_client()


# One error detector for every reply shape, the RPC error envelope included.
query_error = _surreal_records.query_error


async def select_many(
    client: SurrealContentClient, query: str, **params: object
) -> list[models.SurrealRecord]:
    result = await client.execute_query(query, **params)
    error = query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return normalize_records(result)


async def content_schema_version_at_least(client: SurrealContentClient, required: int) -> bool:
    """Whether the content schema has reached ``required``, asking the database only until it has.

    Reads the version record by id, which a namespace without the version
    table answers with no rows rather than an error.
    """
    try:
        known = _schema_versions.get(client)
    except TypeError:
        # A stand-in client that cannot be weakly referenced is asked every time.
        known = None
    if known is not None and known >= required:
        return True
    rows = await select_many(
        client, f"SELECT version FROM [{schema_version_record_id(CONTENT_SCHEMA_RECORD_NAME)}];"
    )
    version = rows[0].get("version") if rows else None
    if not isinstance(version, int | float):
        return False
    with suppress(TypeError):
        _schema_versions[client] = max(int(version), known or 0)
    return version >= required


def normalize_raw_statement_records(
    result: object, *, statement_index: int
) -> list[models.SurrealRecord]:
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        statements = payload.get("result")
        if (
            "status" not in payload
            and isinstance(statements, list)
            and statements
            and all(isinstance(statement, dict) for statement in statements)
        ):
            return normalize_records(statements[statement_index])
    return normalize_records(result)


async def select_many_raw(
    client: SurrealContentClient,
    query: str,
    **params: object,
) -> list[models.SurrealRecord]:
    execute_query_raw = getattr(client, "execute_query_raw", None)
    if callable(execute_query_raw):
        result = await cast("models.RawExecuteQuery", execute_query_raw)(query, **params)
    else:
        result = await client.execute_query(query, **params)
    error = query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return normalize_raw_statement_records(result, statement_index=-1)


async def select_one(
    client: SurrealContentClient, query: str, **params: object
) -> models.SurrealRecord | None:
    rows = await select_many(client, query, **params)
    return rows[0] if rows else None


async def replace_record(
    client: SurrealContentClient,
    table: str,
    *,
    uuid: str,
    record: models.SurrealRecord,
) -> models.SurrealRecord:
    organization_id = record.get("organization_id")
    if organization_id is None:
        raise RuntimeError(f"{table} record {uuid} requires organization_id")
    if table == "raw_captures":
        from sibyl_core.services.content_raw_persistence import replace_raw_memory_records_bulk

        return (await replace_raw_memory_records_bulk(client, [record]))[0]
    rows = await select_many(
        client,
        _UPSERT_RECORD[table],
        uuid=uuid,
        organization_id=str(organization_id),
        record=record,
    )
    if rows:
        return rows[0]
    try:
        rows = await select_many(client, f"CREATE {table} CONTENT $record;", record=record)
    except Exception as exc:
        rows = await select_many(
            client,
            _UPSERT_RECORD[table],
            uuid=uuid,
            organization_id=str(organization_id),
            record=record,
        )
        if rows:
            return rows[0]
        raise RuntimeError(f"failed to persist {table} record {uuid}") from exc
    if rows:
        return rows[0]
    raise RuntimeError(f"failed to persist {table} record {uuid}")


def value_batches(
    values: Iterable[str], *, batch_size: int = DEFAULT_BATCH_SIZE
) -> list[list[str]]:
    batch: list[str] = []
    batches: list[list[str]] = []
    for value in values:
        batch.append(value)
        if len(batch) >= batch_size:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)
    return batches
