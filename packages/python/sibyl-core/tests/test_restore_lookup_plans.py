"""A graph restore reads the destination through operands the planner can index.

SurrealDB 4.0 plans a statement before it runs. A table filter is served from
an index only when its operand is a literal or a client parameter; a closure
argument, a FOR variable or a block LET makes it a scan of the whole table,
once per restored row, and a restore batch then costs more with every row the
destination already holds.
"""

import os
import re
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal.in_clumps import IN_CLUMP
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM, bootstrap_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.tools.admin import create_backup, restore_backup
from tests.test_archive_restore_batching import inventory, record_requests
from tests.test_graph_archive_atomicity import STAMP, episode
from tests.test_source_integrity_archive import runtime as runtime

ENTITIES = 40
RELATIONSHIPS = 36
TABLES = ("entity", "relates_to", "episode", "mentions", "source_states", "memory_derivations")
# A table named as a statement's source, and its filter up to the end of the
# statement or subquery. Restore filters hold no nested call.
_TABLE_FILTER = re.compile(rf"\b({'|'.join(TABLES)})\b([^;()]*?)\bWHERE\b([^;()]*)")
_VARIABLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_IN_LIST = re.compile(r"\bIN\s+\$([A-Za-z_][A-Za-z0-9_]*)")
# One parenthesized read of a table, as the clumped lookups and the
# uniqueness probes write it.
_TABLE_READ = re.compile(
    rf"\((SELECT (?:[^()]|\([^()]*\))*? FROM (?:{'|'.join(TABLES)})\b[^()]*\bWHERE\b[^()]*)\)"
)


@pytest.fixture(params=["embedded", "live"])
async def destination(request, runtime) -> AsyncIterator[tuple[GraphRuntime, bool]]:
    """An embedded destination, then a SurrealDB server when
    SIBYL_LIVE_SURREAL_TESTS=1 and SIBYL_SURREAL_URL name one."""
    from uuid import uuid4

    live = request.param == "live"
    if not live:
        client = SurrealGraphClient(
            group_id=runtime.client.group_id, url="memory://", database="plans_" + uuid4().hex
        )
    else:
        if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
            pytest.skip("live SurrealDB tests are disabled")
        url = os.environ.get("SIBYL_SURREAL_URL", "")
        if not url or is_embedded_surreal_url(url):
            pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
        client = SurrealGraphClient(
            group_id=runtime.client.group_id,
            url=url,
            username=os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
            password=os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
            namespace_prefix="verify_",
            database="graph",
            pool_size=2,
        )
    try:
        await bootstrap_schema(client)
        yield (
            GraphRuntime(
                client=client,
                entity_manager=EntityManager(client, group_id=client.group_id),
                relationship_manager=RelationshipManager(client, group_id=client.group_id),
            ),
            live,
        )
    finally:
        if live:
            await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


async def seed(runtime) -> None:
    """More rows than one clump holds, with every companion kind and a derivation."""
    organization = runtime.client.group_id
    await runtime.entity_manager.create_direct_bulk(
        [
            Entity(
                id=f"entity-{index:02d}",
                entity_type=EntityType.TOPIC,
                name=f"Entity {index}",
                embedding=[float(index + 1)] + [0.0] * (EMBEDDING_DIM - 1),
            )
            for index in range(ENTITIES)
        ]
    )
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id=f"edge-{index:02d}",
                source_id=f"entity-{index:02d}",
                target_id=f"entity-{index + 1:02d}",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for index in range(RELATIONSHIPS)
        ]
    )
    for index in range(3):
        await episode(runtime.client, f"episode-{index}")
    await runtime.client.execute_query(
        "FOR $row IN $rows { CREATE memory_derivations CONTENT $row; };",
        rows=[
            {
                "organization_id": organization,
                "target_kind": "graph_entity",
                "target_id": f"entity-{index:02d}",
                "body_sha256": "a" * 64,
                "principal_id": "owner",
                "authority_ceiling": {},
                "observations": [
                    {
                        "source": {
                            "organization_id": organization,
                            "kind": "graph_entity",
                            "id": f"entity-{index + 1:02d}",
                        },
                        "generation": 1,
                        "content_sha256": "b" * 64,
                        "revision": 1,
                        "durable": True,
                    }
                ],
                "active": True,
            }
            for index in range(2)
        ],
    )


async def archive_of(runtime):
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.message
    backup.backup_data.mentions = [
        {
            "uuid": f"mention-{index}",
            "source_id": f"episode-{index}",
            "target_id": f"entity-{index:02d}",
            "created_at": STAMP,
        }
        for index in range(3)
    ]
    backup.backup_data.mention_count = 3
    return backup.backup_data


def table_filters(sent: list[tuple[str, dict]]) -> list[tuple[str, str, dict]]:
    """Every filter a request applies to a table: the table, the filter, the request's params."""
    return [
        (match.group(1), " ".join(match.group(3).split()), params)
        for query, params in sent
        for match in _TABLE_FILTER.finditer(query)
    ]


async def test_restore_filters_tables_on_client_parameters_only(runtime, destination, monkeypatch):
    target, live = destination
    await seed(runtime)
    archive = await archive_of(runtime)
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=target))
    sent = record_requests(monkeypatch, target.client)
    organization = runtime.client.group_id

    # Into an empty destination, then over the rows the first pass restored,
    # so both the create and the replace branch of every companion write run.
    fresh = await restore_backup(archive, organization_id=organization)
    assert fresh.success, fresh.errors
    assert fresh.entities_restored >= ENTITIES
    assert fresh.relationships_restored == RELATIONSHIPS
    assert (fresh.episodes_restored, fresh.mentions_restored) == (3, 3)
    again = await restore_backup(archive, organization_id=organization, skip_existing=False)
    assert again.success, again.errors
    assert (again.relationships_restored, again.mentions_restored) == (RELATIONSHIPS, 3)

    filters = table_filters(sent)
    assert {table for table, _, _ in filters} >= {
        "entity",
        "relates_to",
        "episode",
        "mentions",
        "memory_derivations",
    }
    for table, clause, params in filters:
        unknown = [name for name in _VARIABLE.findall(clause) if name not in params]
        assert not unknown, (
            f"{table} is filtered on ${unknown[0]}, not a client parameter: {clause}"
        )
        for name in _IN_LIST.findall(clause):
            assert len(params[name]) <= IN_CLUMP, (table, clause)

    expected = await inventory(runtime.client)
    restored = await inventory(target.client)
    expected["mentions"] = [{"uuid": f"mention-{index}"} for index in range(3)]
    assert restored == expected
    associations = await target.client.execute_query(
        "SELECT VALUE target_id FROM memory_derivations ORDER BY target_id;"
    )
    assert associations == ["entity-00", "entity-01"]

    if not live:
        return
    # On a server, each of those reads is served from an index. A read with
    # nothing to look up plans as a constant, so each is explained with the
    # first parameters that gave it values.
    examples: dict[str, dict] = {}
    for query, params in sent:
        for match in _TABLE_READ.finditer(query):
            read = match.group(1)
            names = set(_VARIABLE.findall(read))
            if all(params[name] != [] for name in names):
                examples.setdefault(read, {name: params[name] for name in names})
    assert len(examples) >= 8
    assert {table for table in TABLES if any(f" FROM {table} " in read for read in examples)} == {
        *TABLES
    }
    for read, params in sorted(examples.items()):
        plan = str(await target.client.execute_query(read + " EXPLAIN", **params))
        assert "TableScan" not in plan, (read, plan[:400])
        assert "IndexScan" in plan, (read, plan[:400])


async def test_ledger_state_off_its_derived_key_refuses_before_any_write(
    runtime, destination, monkeypatch
):
    """A ledger state is read at the record key every writer files it under.

    A state row filed anywhere else is not read, so the uniqueness preflight
    has to refuse the restore before anything is written.
    """
    target, _ = destination
    await seed(runtime)
    archive = await archive_of(runtime)
    organization = runtime.client.group_id
    await target.client.execute_query(
        "CREATE source_states:stray CONTENT { organization_id: $organization, "
        "source_kind: 'graph_entity', source_id: 'entity-00', generation: 7, revision: 1, "
        "deleted: false, incarnation: 'stray' };",
        organization=organization,
    )
    before = await inventory(target.client)
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=target))

    result = await restore_backup(archive, organization_id=organization)

    assert not result.success
    assert any("source_state_identity" in error for error in result.errors), result.errors
    assert await inventory(target.client) == before
