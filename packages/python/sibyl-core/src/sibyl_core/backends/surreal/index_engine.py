"""Rebuild a database's indexes once when it predates the SurrealDB 4 server it now runs on.

A 4.x server converts a 3.x datastore without rewriting its indexes, and the
converted indexes are not all sound: a datetime B-tree keeps the 3.x key order,
so a range count over it comes back wrong and the planner stops using it for
ORDER BY. ``REBUILD INDEX`` on every index of the database puts it right.

Each database decides for itself, inside its own schema bootstrap and under
that database's schema lease. On a 4.x server a database whose schema_version
record exists without ``index_engine = 4`` predates the server: every index is
rebuilt, checked, and only then is the record marked. A database the
bootstrap creates on a 4.x server carries the mark from its first record, and
a 3.x server or the embedded engine is never touched. Every failure of this
step, from the first INFO FOR DB to the mark, raises IndexReadinessError and
leaves the database unmarked, so the next bootstrap tries again and nothing
serves it in the meantime.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog

from sibyl_core.backends.surreal.schema_version import (
    ensure_schema_version_table,
    schema_version_record_id,
    schema_version_record_query,
)
from sibyl_core.backends.surreal.url_schemes import surreal_url_scheme

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.schema_ownership import SchemaOwnership

log = structlog.get_logger()

# Bumped only if a later SurrealDB major again needs every index rebuilt.
INDEX_ENGINE = 4
INDEX_ENGINE_FIELD = "index_engine"

# Written by the schema bootstrap itself before it knows what the database
# holds, so their presence says nothing about the database's age.
_BOOKKEEPING_TABLES = frozenset({"schema_version", "schema_lease"})
_BUILT = frozenset({"ready", "built"})


class SurrealExecute(Protocol):
    def __call__(self, statement: str, /, **params: object) -> Awaitable[object]: ...


class LeaseableClient(Protocol):
    _url: str

    @property
    def namespace(self) -> str: ...

    @property
    def database(self) -> str: ...

    def execute_query(self, statement: str, /, **params: object) -> Awaitable[object]: ...

    def schema_lease_executor(self) -> Any: ...


class IndexReadinessError(RuntimeError):
    """A database's indexes could not be shown built by its server; it stays unmarked and must not serve."""


@asynccontextmanager
async def index_readiness_step(where: str) -> AsyncGenerator[None]:
    """Turn any failure while establishing index readiness into IndexReadinessError.

    The step spans the INFO FOR DB it decides on, the record reads, the lease,
    the inventory, every REBUILD, the status checks and the mark. Callers fail
    closed on this one type in every environment.
    """
    try:
        yield
    except IndexReadinessError:
        raise
    except Exception as exc:
        msg = f"index readiness for {where} could not be established: {exc}"
        raise IndexReadinessError(msg) from exc


def index_engine_of(info: object) -> int | None:
    """INDEX_ENGINE when INFO FOR DB came from a 4.x server, which alone reports a compatibility level."""
    return INDEX_ENGINE if isinstance(_mapping(info).get("compatibility"), int) else None


def holds_sibyl_tables(info: object) -> bool:
    """Whether INFO FOR DB lists any table beyond the bootstrap's own bookkeeping."""
    return any(name not in _BOOKKEEPING_TABLES for name in _mapping(_mapping(info).get("tables")))


def index_engine_current(recorded: object) -> bool:
    return isinstance(recorded, int) and recorded >= INDEX_ENGINE


async def recorded_index_engine(
    read: SurrealExecute, *, name: str
) -> tuple[int | None, int | None]:
    """(the schema_version record's version, its index_engine) in one read; (None, None) without one.

    Answers no rows rather than failing where the table does not exist yet.
    """
    record = _first(await read(schema_version_record_query(name, f"version, {INDEX_ENGINE_FIELD}")))
    if record is None:
        return None, None
    version, recorded = record.get("version"), record.get(INDEX_ENGINE_FIELD)
    return (
        int(version) if isinstance(version, int | float) else 0,
        recorded if isinstance(recorded, int) else None,
    )


async def settle_database_indexes(
    client: LeaseableClient,
    *,
    name: str,
    info: object,
    ownership: SchemaOwnership | None = None,
) -> int | None:
    """Bring one database's indexes up to its server's engine, first thing in its bootstrap.

    ``info`` is the INFO FOR DB the bootstrap read before writing anything.
    Returns the value a schema_version record this bootstrap creates must
    carry, or None. A caller already holding the database's schema lease
    passes it; otherwise the lease is taken only when a rebuild is due and the
    record is read again under it, so concurrent bootstraps rebuild once.
    """
    engine = index_engine_of(info)
    if engine is None:
        return None
    version, recorded = await recorded_index_engine(
        ownership.read if ownership is not None else client.execute_query, name=name
    )
    if index_engine_current(recorded):
        return None
    if not version and not holds_sibyl_tables(info):
        # Nothing of Sibyl's here yet: this bootstrap builds every index.
        return engine
    _refuse_http(client)
    if ownership is not None:
        await _rebuild_and_mark(ownership, client=client, name=name)
    else:
        from sibyl_core.backends.surreal.schema import hold_schema_ownership

        async with hold_schema_ownership(client) as owned:
            _, recorded = await recorded_index_engine(owned.read, name=name)
            if not index_engine_current(recorded):
                await _rebuild_and_mark(owned, client=client, name=name)
    return None


async def database_needs_rebuild(read: SurrealExecute, *, name: str) -> object | None:
    """The bootstrap's rule, for code that reaches a database without bootstrapping it.

    Returns the INFO FOR DB that shows a rebuild is due (a 4.x server, and a
    schema_version record without the mark or tables without a record), or
    None. A marked database costs one record read; anything else one more
    INFO FOR DB. A namespace or database that does not exist yet holds nothing.
    """
    try:
        version, recorded = await recorded_index_engine(read, name=name)
    except Exception as exc:
        if "does not exist" in str(exc).lower():
            return None
        raise
    if index_engine_current(recorded):
        return None
    info = await read("INFO FOR DB;")
    if index_engine_of(info) is None:
        return None
    return info if version or holds_sibyl_tables(info) else None


async def ensure_database_indexes(client: LeaseableClient, *, name: str) -> bool:
    """For a process that uses auth or content without running their bootstrap.

    Returns whether a rebuild ran; raises IndexReadinessError on any failure.
    """
    async with index_readiness_step(f"{client.namespace}/{client.database}"):
        info = await database_needs_rebuild(client.execute_query, name=name)
        if info is None:
            return False
        await settle_database_indexes(client, name=name, info=info)
        return True


def _refuse_http(client: LeaseableClient) -> None:
    # The SDK's HTTP transport gives every request 30 s in total, and a 4.0
    # REBUILD can wait up to 60 s for older write transactions before it
    # starts. Every shipped configuration speaks WebSocket.
    if surreal_url_scheme(client._url) in {"http", "https"}:
        msg = (
            f"{client.namespace}/{client.database} needs its indexes rebuilt after the move to "
            "SurrealDB 4, and a rebuild can outlast the 30 s HTTP request limit. Start once "
            "with SIBYL_SURREAL_URL set to the server's ws:// (or wss://) /rpc endpoint."
        )
        raise IndexReadinessError(msg)


async def rebuild_database_indexes(
    read: SurrealExecute,
    *,
    heartbeat: Callable[[], Awaitable[None]],
) -> int:
    """REBUILD every index of the database, then prove each is still defined and built.

    Each index is its own statement, outside any transaction, as SurrealDB asks
    for an index with many records. 4.0 makes a rebuild wait up to 60 s for
    older write transactions anywhere in the datastore, then go ahead; a failed
    rebuild keeps the definition with status ``error`` (a UNIQUE index whose
    rows collide refuses writes until fixed). The check after the loop catches
    both a definition that went missing and one that is not built.
    """
    inventory = await _index_inventory(read)
    for table, index in inventory:
        await heartbeat()
        try:
            await read(f"REBUILD INDEX {_ident(index)} ON {_ident(table)};")
        except Exception as exc:
            msg = f"REBUILD INDEX {index} ON {table} failed: {exc}"
            raise IndexReadinessError(msg) from exc
    present = set(await _index_inventory(read))
    for table, index in inventory:
        if (table, index) not in present:
            msg = f"index {index} on {table} is no longer defined after its rebuild"
            raise IndexReadinessError(msg)
        building = _mapping(
            _mapping(await read(f"INFO FOR INDEX {_ident(index)} ON {_ident(table)};")).get(
                "building"
            )
        )
        status = building.get("status")
        if status is not None and status not in _BUILT:
            msg = f"index {index} on {table} is {status} after its rebuild: {building.get('error')}"
            raise IndexReadinessError(msg)
    return len(inventory)


async def _rebuild_and_mark(
    ownership: SchemaOwnership,
    *,
    client: LeaseableClient,
    name: str,
) -> None:
    started = time.monotonic()
    where = {"namespace": client.namespace, "database": client.database}
    log.warning(
        "surreal_index_rebuild_started",
        reason="database written before its SurrealDB 4 server",
        **where,
    )
    count = await rebuild_database_indexes(ownership.read, heartbeat=ownership.heartbeat)
    # Completion is persisted before the lease is released, fenced by it: an
    # owner that lost the lease cannot mark. A database with tables but no
    # record gets one at version 0, which its migrations then carry forward.
    await ensure_schema_version_table(ownership.mutate)
    await ownership.mutate(
        f"UPSERT {schema_version_record_id(name)} SET name = $name, "
        "version = version ?? 0, migrations = migrations ?? [], "
        f"{INDEX_ENGINE_FIELD} = $index_engine, "
        "created_at = created_at ?? time::now(), updated_at = time::now();",
        name=name,
        index_engine=INDEX_ENGINE,
    )
    log.warning(
        "surreal_index_rebuild_finished",
        indexes=count,
        seconds=round(time.monotonic() - started, 1),
        **where,
    )


async def _index_inventory(read: SurrealExecute) -> list[tuple[str, str]]:
    tables = _mapping(_mapping(await read("INFO FOR DB;")).get("tables"))
    inventory: list[tuple[str, str]] = []
    for table in sorted(tables):
        table_info = _mapping(await read(f"INFO FOR TABLE {_ident(table)};"))
        inventory.extend((table, index) for index in sorted(_mapping(table_info.get("indexes"))))
    return inventory


def _ident(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, list) and value:
        value = value[0]
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _first(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return cast("Mapping[str, object]", value[0])
    return None


__all__ = [
    "INDEX_ENGINE",
    "INDEX_ENGINE_FIELD",
    "IndexReadinessError",
    "database_needs_rebuild",
    "ensure_database_indexes",
    "holds_sibyl_tables",
    "index_engine_current",
    "index_engine_of",
    "index_readiness_step",
    "rebuild_database_indexes",
    "recorded_index_engine",
    "settle_database_indexes",
]
