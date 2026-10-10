"""Statements a SurrealDB server refuses outright or quietly answers wrong.

SurrealDB refuses an ORDER BY on a field missing from a non-star projection
at parse time ("Missing order idiom"), on 3.2 servers and on the embedded
engine alike, so each read here projects every field it orders by. The
cleanup tests pin deletes whose IN list lands on a field of a UNIQUE index
key other than its last. Every test runs on the embedded engine and, when
SIBYL_LIVE_SURREAL_TESTS=1 names a server, on that server too.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sibyl.persistence import auth_archive
from sibyl.persistence.surreal import organization_runtime
from sibyl.persistence.surreal.auth_runtime import _common as auth_common, projects
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import graph_community_clusters
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, normalize_records
from sibyl_core.services.graph_client import prepare_graph_schema
from sibyl_core.tools import admin

CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}


def engine_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


@pytest.fixture(params=["embedded", "live"])
async def auth(request, monkeypatch) -> AsyncIterator[SurrealAuthClient]:
    client = SurrealAuthClient(
        url=engine_url(request.param),
        namespace=f"verify_query_shapes_{uuid4().hex}",
        **CREDENTIALS,
    )
    await bootstrap_auth_schema(client)

    @asynccontextmanager
    async def auth_scope():
        yield client

    monkeypatch.setattr(auth_common, "surreal_auth_client_scope", auth_scope)
    try:
        yield client
    finally:
        if request.param == "live":
            with suppress(Exception):
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


@pytest.fixture(params=["embedded", "live"])
async def graph(request) -> AsyncIterator[SimpleNamespace]:
    group_id = str(uuid4())
    client = SurrealGraphClient(
        group_id=group_id,
        url=engine_url(request.param),
        namespace_prefix="verify_query_shapes_",
        database="graph",
        **CREDENTIALS,
    )
    try:
        await prepare_graph_schema(client)
        yield SimpleNamespace(
            group_id=group_id,
            client=client,
            manager=EntityManager(client, group_id=group_id),
        )
    finally:
        if request.param == "live":
            with suppress(Exception):
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
        await client.close()


async def _create_entity(
    graph: SimpleNamespace,
    *,
    entity_type: EntityType,
    name: str,
    metadata: dict[str, object] | None = None,
) -> str:
    entity_id = f"{entity_type.value}-{uuid4().hex[:12]}"
    await graph.manager.create_direct(
        Entity(
            id=entity_id,
            entity_type=entity_type,
            name=name,
            description=name,
            organization_id=graph.group_id,
            metadata=metadata or {},
        )
    )
    return entity_id


async def test_team_list_reads_each_team_with_its_memory_space(auth) -> None:
    organization_id = uuid4()
    creator = uuid4()
    first = await projects.create_team_record(
        organization_id=organization_id, created_by_user_id=creator, name="Platform"
    )
    second = await projects.create_team_record(
        organization_id=organization_id, created_by_user_id=creator, name="Research"
    )

    teams = await projects.list_team_records(organization_id=organization_id)

    assert [team.id for team in teams] == [first.id, second.id]
    assert {team.id: team.memory_space_id for team in teams} == {
        first.id: first.memory_space_id,
        second.id: second.memory_space_id,
    }
    assert all(isinstance(team.memory_space_id, UUID) for team in teams)


async def test_denormalized_field_backfill_scans_the_organization(graph, monkeypatch) -> None:
    task_id = await _create_entity(graph, entity_type=EntityType.TASK, name="Backfill me")
    # An older row: the filter column lives only in attributes.metadata.
    await graph.client.execute_query(
        "UPDATE entity SET project_id = NONE, attributes.metadata = $metadata WHERE uuid = $uuid;",
        uuid=task_id,
        metadata={"project_id": "project-backfill"},
    )

    async def runtime(_group_id: str) -> SimpleNamespace:
        return SimpleNamespace(client=graph.client, entity_manager=graph.manager)

    monkeypatch.setattr(admin, "get_graph_runtime", runtime)
    result = await admin.backfill_denormalized_fields(organization_id=graph.group_id)

    assert result.errors == []
    assert result.success
    assert result.entities_updated == 1
    rows = normalize_records(
        await graph.client.execute_query(
            "SELECT project_id FROM entity WHERE uuid = $uuid;", uuid=task_id
        )
    )
    assert rows == [{"project_id": "project-backfill"}]


async def test_type_clusters_sample_members_on_the_native_path(graph) -> None:
    older = await _create_entity(graph, entity_type=EntityType.PATTERN, name="older")
    newer = await _create_entity(graph, entity_type=EntityType.PATTERN, name="newer")
    await graph.client.execute_query(
        "UPDATE entity SET updated_at = time::now() - 1h WHERE uuid = $uuid;", uuid=older
    )

    clusters = await graph_community_clusters._native_type_based_clusters(
        graph.client, graph.group_id
    )

    assert clusters is not None
    by_type = {cluster.dominant_type: cluster for cluster in clusters}
    assert by_type[EntityType.PATTERN.value].member_ids == [newer, older]


# SurrealDB's write planner (3.2.4, 3.2.5 and 3.3.0) can serve an UPDATE,
# DELETE or UPSERT, and any SELECT nested inside one, from a UNIQUE index, and
# a UNIQUE index walked with an IN list on any field of its key but the last
# hands back no rows (surrealdb/surrealdb#7534). On UNIQUE (a, b, c) both
# `a IN $list` and `a = $x AND b IN $list` match nothing; the write silently
# does nothing, and an UPSERT inserts a new row instead of updating. Which
# index the planner takes follows index names and, when a non-unique index
# competes, can differ from one namespace to the next.
#
# The shapes that always match: equality on every key field before the last
# (an IN list on the last one is fine), a list of record ids (UPDATE $ids,
# DELETE $ids), or one statement per value. The deletes below use none of
# those. They stay correct only because each IN field also carries a
# single-field index whose name sorts first, or because the list arrives as
# a subquery the planner cannot put on an index. Renaming or dropping that
# index, or binding the list as a parameter, turns the cleanup into a silent
# no-op.


async def _seed_api_keys(auth: SurrealAuthClient, organization_id: UUID) -> list[str]:
    now = datetime.now(UTC)
    key_ids = [str(uuid4()) for _ in range(2)]
    await auth.execute_query(
        "INSERT INTO api_keys $keys; "
        "INSERT INTO api_key_project_scopes $project_scopes; "
        "INSERT INTO api_key_memory_space_scopes $space_scopes;",
        keys=[
            {
                "uuid": key_id,
                "organization_id": str(organization_id),
                "user_id": str(uuid4()),
                "name": "cleanup",
                "key_prefix": f"sk_{key_id[:8]}",
                "key_salt": "salt",
                "key_hash": key_id,
                "created_at": now,
                "updated_at": now,
            }
            for key_id in key_ids
        ],
        project_scopes=[
            {
                "uuid": str(uuid4()),
                "api_key_id": key_id,
                "project_id": str(uuid4()),
                "created_at": now,
            }
            for key_id in key_ids
        ],
        space_scopes=[
            {
                "uuid": str(uuid4()),
                "api_key_id": key_id,
                "memory_space_id": str(uuid4()),
                "created_at": now,
            }
            for key_id in key_ids
        ],
    )
    return key_ids


async def _remaining_children(
    auth: SurrealAuthClient, *, team_ids: list[str], key_ids: list[str]
) -> dict[str, object]:
    rows = normalize_records(
        await auth.execute_query(
            """
            RETURN {
                members: (SELECT VALUE uuid FROM team_members
                    WHERE team_id = $teams[0] OR team_id = $teams[1]),
                project_scopes: (SELECT VALUE uuid FROM api_key_project_scopes
                    WHERE api_key_id = $keys[0] OR api_key_id = $keys[1]),
                space_scopes: (SELECT VALUE uuid FROM api_key_memory_space_scopes
                    WHERE api_key_id = $keys[0] OR api_key_id = $keys[1]),
            };
            """,
            teams=team_ids,
            keys=key_ids,
        )
    )
    return rows[0]


@pytest.mark.parametrize("cleanup", ["delete_org", "restore_org_archive"])
async def test_org_cleanup_deletes_children_listed_by_parent_id(auth, cleanup) -> None:
    organization_id = uuid4()
    teams = [
        await projects.create_team_record(
            organization_id=organization_id, created_by_user_id=uuid4(), name=name
        )
        for name in ("Platform", "Research")
    ]
    team_ids = [str(team.id) for team in teams]
    key_ids = await _seed_api_keys(auth, organization_id)
    seeded = await _remaining_children(auth, team_ids=team_ids, key_ids=key_ids)
    assert {name: len(rows) for name, rows in seeded.items()} == {
        "members": 2,
        "project_scopes": 2,
        "space_scopes": 2,
    }

    if cleanup == "delete_org":
        await organization_runtime._delete_org_auth_child_records(
            auth, organization_id=organization_id
        )
    else:
        await auth_archive._clean_auth_archive_rows(auth, str(organization_id))

    assert await _remaining_children(auth, team_ids=team_ids, key_ids=key_ids) == {
        "members": [],
        "project_scopes": [],
        "space_scopes": [],
    }


async def test_team_delete_removes_its_memory_space_members(auth) -> None:
    organization_id = uuid4()
    team = await projects.create_team_record(
        organization_id=organization_id, created_by_user_id=uuid4(), name="Platform"
    )
    now = datetime.now(UTC)
    await auth.execute_query(
        "INSERT INTO memory_space_members $rows;",
        rows=[
            {
                "uuid": str(uuid4()),
                "organization_id": str(organization_id),
                "space_id": str(team.memory_space_id),
                "principal_type": "user",
                "principal_id": str(uuid4()),
                "role": "member",
                "created_by_user_id": str(uuid4()),
                "created_at": now,
                "updated_at": now,
            }
            for _ in range(2)
        ],
    )

    async def members() -> list[object]:
        rows = normalize_records(
            await auth.execute_query(
                "SELECT uuid FROM memory_space_members WHERE space_id = $space_id;",
                space_id=str(team.memory_space_id),
            )
        )
        return [row["uuid"] for row in rows]

    # The two seeded rows plus the creator's own membership.
    assert len(await members()) == 3
    assert await projects.delete_team_record(organization_id=organization_id, team_ref=str(team.id))
    assert await members() == []
