"""Native persisted membership facts govern entity, capture, and MCP readers."""

from uuid import UUID, uuid4

import httpx
import pytest

from sibyl.api.routes import entities
from sibyl.auth.jwt import create_access_token
from sibyl.persistence.content_runtime import get_content_read_session_dependency
from sibyl.persistence.surreal.auth_runtime.api_keys import create_api_key_for_user
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
from tests.test_public_membership_adapters import (
    public_membership_runtime as public_membership_runtime,  # noqa: PLC0414
)


@pytest.fixture(params=["team", "delegated"])
async def membership_entities(public_membership_runtime, request, monkeypatch):
    runtime = public_membership_runtime
    scope = request.param
    scope_key = runtime.team if scope == "team" else runtime.delegation
    memory = runtime.memories[scope]
    graph = await get_surreal_graph_runtime(runtime.org)
    parent_id, passage_id, anchor_id = [str(uuid4()) for _ in range(3)]
    rows = [
        Entity(
            id=parent_id,
            entity_type=EntityType.NOTE,
            name=f"scopeproof {scope} canonical",
            content=memory.raw_content,
            metadata={
                "raw_memory_id": memory.id,
                "memory_scope": scope,
                "scope_key": scope_key,
                "principal_id": runtime.owner,
            },
        ),
        Entity(
            id=passage_id,
            entity_type=EntityType.PASSAGE,
            name=f"scopeproof {scope} legacy span",
            content=memory.raw_content,
            metadata={
                "projection_kind": "passage",
                "parent_entity_id": parent_id,
                "source_entity_id": parent_id,
                "memory_scope": "project",
                "scope_key": runtime.project,
                "project_id": runtime.project,
                "passage_index": 0,
                "passage_total": 1,
            },
        ),
        Entity(
            id=anchor_id,
            entity_type=EntityType.NOTE,
            name="scopeproof ordinary resource",
            content="Independent organization resource",
        ),
    ]
    await graph.entity_manager.create_direct_bulk(rows)
    for source, target, relationship_type in (
        (anchor_id, parent_id, RelationshipType.RELATED_TO),
        (anchor_id, passage_id, RelationshipType.RELATED_TO),
        (passage_id, parent_id, RelationshipType.PART_OF),
    ):
        await graph.relationship_manager.create(
            Relationship(
                id=str(uuid4()),
                source_id=source,
                target_id=target,
                relationship_type=relationship_type,
            )
        )
    # Both readers satisfy the passage's own project policy. Only retained
    # source audience authority distinguishes the outsider controls.
    await runtime.auth.execute_query(
        "CREATE project_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": runtime.org,
            "project_id": runtime.project,
            "user_id": runtime.peer,
            "role": "project_viewer",
        },
    )
    spaces = {"delegated": runtime.delegation}
    for name, key in (("team", runtime.team), ("project", runtime.project)):
        space = str(uuid4())
        await runtime.auth.execute_query(
            "CREATE memory_spaces CONTENT $record;",
            record={
                "uuid": space,
                "organization_id": runtime.org,
                "memory_scope": name,
                "scope_key": key,
                "created_by_user_id": runtime.owner,
            },
        )
        spaces[name] = space

    async def content_session():
        yield runtime.content

    async def native_content():
        return runtime.content

    monkeypatch.setattr(
        "sibyl.persistence.surreal.content.get_shared_surreal_content_client", native_content
    )
    runtime.app.include_router(entities.router, prefix="/api")
    runtime.app.dependency_overrides[get_content_read_session_dependency] = content_session
    runtime.scope = scope
    runtime.scope_key = scope_key
    runtime.spaces = spaces
    runtime.parent_id = parent_id
    runtime.passage_id = passage_id
    runtime.anchor_id = anchor_id
    runtime.raw_id = memory.id
    return runtime


async def reader_token(runtime, condition):
    principal = runtime.peer if condition in {"absent", "key_only"} else runtime.owner
    if condition in {"session", "absent"}:
        # Auth snapshots must ignore claimed memberships, even in signed tokens.
        return create_access_token(
            user_id=UUID(principal),
            organization_id=UUID(runtime.org),
            extra_claims={
                "accessible_teams": [runtime.team],
                "accessible_delegations": [runtime.delegation],
            },
        )
    allowed_scope = "project" if condition == "wrong_key" else runtime.scope
    space_ids = {runtime.spaces["project"], runtime.spaces[allowed_scope]}
    _key, token = await create_api_key_for_user(
        organization_id=UUID(runtime.org),
        user_id=UUID(principal),
        name=f"{condition} fixture credential",
        live=False,
        scopes=["mcp", "api:read"],
        memory_space_ids=[UUID(space) for space in space_ids],
        expires_at=None,
        request=None,
    )
    return token


async def assert_public_entity_reads(runtime, token, *, allowed):
    targets = {runtime.parent_id, runtime.passage_id}
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=runtime.base, headers=headers) as client:
        for entity_id in targets:
            for include_summary, related_limit in ((False, 0), (True, 0), (True, 10)):
                response = await client.get(
                    f"/api/entities/{entity_id}",
                    params={"include_summary": include_summary, "related_limit": related_limit},
                )
                assert response.status_code == (200 if allowed else 404), response.text
        for params in ({"page_size": 200}, {"page_size": 200, "search": "scopeproof"}):
            response = await client.get("/api/entities", params=params)
            assert response.status_code == 200, response.text
            ids = {row["id"] for row in response.json()["entities"]}
            assert (targets <= ids) if allowed else not (targets & ids), response.text
            assert runtime.anchor_id in ids
        response = await client.get(
            f"/api/entities/{runtime.anchor_id}",
            params={"include_summary": False, "related_limit": 50},
        )
        assert response.status_code == 200, response.text
        ids = {row["id"] for row in (response.json()["related"] or [])}
        assert (targets <= ids) if allowed else not (targets & ids), response.text
        response = await client.get(f"/api/entities/captures/{runtime.raw_id}")
        assert response.status_code == (200 if allowed else 404), response.text
        response = await client.get("/api/entities/captures", params={"limit": 200})
        assert response.status_code == 200, response.text
        ids = {row["id"] for row in response.json()["captures"]}
        assert (runtime.raw_id in ids) is allowed

    runtime.active_token["value"] = token
    for args in (
        {"mode": "list", "types": ["note", "passage"], "limit": 200},
        {"mode": "related", "entity_id": runtime.anchor_id, "limit": 200},
    ):
        result = await runtime.mcp.call_tool("explore", args)
        assert not result.is_error, result
        ids = {row["id"] for row in result.structured_content["entities"]}
        assert (targets <= ids) if allowed else not (targets & ids), result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition", ["session", "matching_key", "wrong_key", "absent", "key_only"]
)
async def test_entity_and_capture_membership_authority(membership_entities, condition):
    runtime = membership_entities
    token = await reader_token(runtime, condition)
    await assert_public_entity_reads(
        runtime, token, allowed=condition in {"session", "matching_key"}
    )


@pytest.mark.asyncio
async def test_entity_membership_revocation_with_retained_key(membership_entities):
    runtime = membership_entities
    token = await reader_token(runtime, "matching_key")
    await assert_public_entity_reads(runtime, token, allowed=True)
    if runtime.scope == "team":
        await runtime.auth.execute_query(
            "DELETE team_members WHERE team_id=$team AND user_id=$user;",
            team=runtime.team,
            user=runtime.owner,
        )
    else:
        await runtime.auth.execute_query(
            "DELETE memory_space_members WHERE space_id=$space AND principal_id=$user;",
            space=runtime.delegation,
            user=runtime.owner,
        )
    claims = {"sub": runtime.owner, "org": runtime.org}
    ctx = await runtime.resolver.resolve(claims)
    facts = ctx.accessible_teams if runtime.scope == "team" else ctx.accessible_delegations
    assert runtime.scope_key not in facts
    await assert_public_entity_reads(runtime, token, allowed=False)
