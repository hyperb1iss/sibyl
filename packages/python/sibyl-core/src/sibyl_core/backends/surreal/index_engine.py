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
a 3.x server or the embedded engine is never touched. A failed or incomplete
rebuild raises and leaves the database unmarked, so the next bootstrap tries
again and nothing serves it in the meantime.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog

from sibyl_core.backends.surreal.schema_version import (
    schema_version_record_id,
    schema_version_record_query,
)

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


class IndexRebuildFailed(RuntimeError):
    """A database's indexes could not all be rebuilt; it stays unmarked and must not serve."""


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
    if ownership is not None:
        await _rebuild_and_mark(ownership, client=client, name=name, recorded=version is not None)
    else:
        from sibyl_core.backends.surreal.schema import hold_schema_ownership

        async with hold_schema_ownership(client) as owned:
            version, recorded = await recorded_index_engine(owned.read, name=name)
            if index_engine_current(recorded):
                return None
            await _rebuild_and_mark(owned, client=client, name=name, recorded=version is not None)
    # Tables without a record yet: the mark goes on the record this bootstrap
    # creates, after the rebuild above.
    return None if version is not None else engine


async def ensure_database_indexes(client: LeaseableClient, *, name: str) -> bool:
    """For a process that uses a database without running its bootstrap: rebuild if it is due.

    Returns whether a rebuild ran. A database without a schema_version record
    is left to the bootstrap that will create it.
    """
    version, recorded = await recorded_index_engine(client.execute_query, name=name)
    if version is None or index_engine_current(recorded):
        return False
    info = await client.execute_query("INFO FOR DB;")
    if index_engine_of(info) is None:
        return False
    await settle_database_indexes(client, name=name, info=info)
    return True


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
            raise IndexRebuildFailed(msg) from exc
    present = set(await _index_inventory(read))
    for table, index in inventory:
        if (table, index) not in present:
            msg = f"index {index} on {table} is no longer defined after its rebuild"
            raise IndexRebuildFailed(msg)
        building = _mapping(
            _mapping(await read(f"INFO FOR INDEX {_ident(index)} ON {_ident(table)};")).get(
                "building"
            )
        )
        status = building.get("status")
        if status is not None and status not in _BUILT:
            msg = f"index {index} on {table} is {status} after its rebuild: {building.get('error')}"
            raise IndexRebuildFailed(msg)
    return len(inventory)


async def _rebuild_and_mark(
    ownership: SchemaOwnership,
    *,
    client: LeaseableClient,
    name: str,
    recorded: bool,
) -> None:
    started = time.monotonic()
    where = {"namespace": client.namespace, "database": client.database}
    log.warning(
        "surreal_index_rebuild_started",
        reason="database written before its SurrealDB 4 server",
        **where,
    )
    count = await rebuild_database_indexes(ownership.read, heartbeat=ownership.heartbeat)
    if recorded:
        # Fenced by the lease: an owner that lost it cannot mark the database.
        # A record from before this field needs its definition first.
        await ownership.mutate(
            f"DEFINE FIELD IF NOT EXISTS {INDEX_ENGINE_FIELD} ON schema_version "
            "TYPE option<int>;\n"
            f"UPDATE {schema_version_record_id(name)} "
            f"SET {INDEX_ENGINE_FIELD} = $index_engine, updated_at = time::now();",
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
    "IndexRebuildFailed",
    "ensure_database_indexes",
    "holds_sibyl_tables",
    "index_engine_current",
    "index_engine_of",
    "rebuild_database_indexes",
    "recorded_index_engine",
    "settle_database_indexes",
]
