"""A graph restore that would collide on any UNIQUE index is refused before it writes.

The cases come from the indexes a freshly bootstrapped graph store defines, so
an index added later joins automatically, and fails here until it has a source
row below.
"""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.source_archive_store import unique_indexes
from sibyl_core.tools.admin import create_backup, restore_backup
from tests.test_source_integrity_archive import destination as destination
from tests.test_source_integrity_archive import runtime as runtime

# The fields each restored row replaces a destination row by. A destination row
# sharing all of them is the row being replaced, so an index over them cannot
# collide. An entity replaces the row found by organization and uuid; a ledger
# state is upserted at its own derived record key.
IDENTITY: dict[str, tuple[str, ...]] = {
    "entity": ("group_id", "uuid"),
    "source_states": (),
    "memory_derivations": ("organization_id", "target_kind", "target_id"),
    "episode": ("uuid",),
    "relates_to": ("uuid",),
    "mentions": ("uuid",),
}


def _discover() -> list[tuple[str, str, tuple[str, ...]]]:
    async def run():
        client = SurrealGraphClient(group_id=f"discover-{uuid4().hex}", url="memory://")
        try:
            await prepare_graph_schema(client)
            return [
                (table, name, tuple(fields))
                for table in sorted(IDENTITY)
                for name, fields in await unique_indexes(client.execute_query, table)
            ]
        finally:
            await client.close()

    return asyncio.run(run())


def pytest_generate_tests(metafunc):
    if "unique_index" in metafunc.fixturenames:
        indexes = _discover()
        assert indexes, "no UNIQUE index discovered on the tables a graph restore writes"
        metafunc.parametrize(
            "unique_index", indexes, ids=[f"{table}.{name}" for table, name, _ in indexes]
        )


async def _fingerprint(client) -> str:
    info = await client.execute_query("INFO FOR DB;")
    tables = sorted(name for name in info["tables"] if not name.startswith("schema_"))
    snapshot = ", ".join(f"`{table}`: (SELECT * FROM `{table}` ORDER BY id)" for table in tables)
    return await client.execute_query(f"RETURN crypto::sha256(type::string({{{snapshot}}}));")


async def test_merge_refuses_every_unique_index_collision(
    runtime, destination, monkeypatch, unique_index
):
    table, name, fields = unique_index
    identity = IDENTITY[table]
    if identity and set(identity) <= set(fields):
        pytest.skip(f"{name} covers the row's identity: a match is the row being replaced")
    org = runtime.client.group_id
    for index in range(3):
        await runtime.entity_manager.create_direct(
            Entity(id=f"incoming-{index}", entity_type=EntityType.TOPIC, name=f"Incoming {index}")
        )
    # A pending source derives its lifecycle repair key from its uuid, so every
    # entity index has a key to collide on.
    await runtime.client.execute_query(
        "UPDATE entity SET attributes.source_validation_pending = true WHERE uuid = 'incoming-0';"
    )
    source_rows = {
        "entity": "SELECT * OMIT id FROM entity WHERE uuid = 'incoming-0';",
        "source_states": "SELECT * OMIT id FROM source_states WHERE source_id = 'incoming-0';",
    }
    assert table in source_rows, f"add a source row for {table} so {name} is covered"
    row = (await runtime.client.execute_query(source_rows[table]))[0]
    # The same unique key under another identity, at another record key.
    colliding = dict(row)
    for column in identity:
        if column not in fields:
            colliding[column] = "other-" + str(uuid4())
    created = await destination.client.execute_query(
        f"CREATE {table} CONTENT $record;",
        record=colliding,
    )
    stored = created[0] if isinstance(created, list) else created
    if any(stored.get(column) != row.get(column) for column in fields):
        pytest.skip(f"{name} is computed from the row's identity, so it collides only with it")
    backup = await create_backup(organization_id=org)
    assert backup.success, backup.message
    monkeypatch.setattr(
        "sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=destination)
    )
    before = await _fingerprint(destination.client)

    result = await restore_backup(backup.backup_data, organization_id=org)

    assert not result.success
    # The colliding row may share more than one index; any of them refuses it.
    assert any("on unique index" in error for error in result.errors), result.errors
    assert await _fingerprint(destination.client) == before
