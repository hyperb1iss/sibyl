"""Registered traversal must retain actual source proof and audience."""

from dataclasses import asdict

import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from tests.test_entity_membership_reads import (
    membership_entities as membership_entities,  # noqa: PLC0414
    reader_token,
)
from tests.test_public_membership_adapters import (
    public_membership_runtime as public_membership_runtime,  # noqa: PLC0414
)


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["session", "key_only"])
async def test_registered_traversal_checks_retained_source_audience(membership_entities, condition):
    from uuid import uuid4

    from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

    runtime = membership_entities
    token = await reader_token(runtime, condition)
    runtime.active_token["value"] = token
    allowed = condition == "session"
    graph = await get_surreal_graph_runtime(runtime.org)
    far = Entity(id=str(uuid4()), entity_type=EntityType.NOTE, name="Independent far row")
    await graph.entity_manager.create_direct(far)
    await graph.relationship_manager.create(
        Relationship(
            id=str(uuid4()),
            source_id=runtime.passage_id,
            target_id=far.id,
            relationship_type=RelationshipType.RELATED_TO,
        )
    )
    before = await graph.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    result = await runtime.mcp.call_tool(
        "expand_neighbors", {"entity_ids": [runtime.anchor_id], "depth": 2, "limit": 50}
    )
    assert not result.is_error, result
    ids = {row["id"] for row in result.structured_content["neighbors"]}
    targets = {runtime.parent_id, runtime.passage_id, far.id}
    assert (targets <= ids) if allowed else not (targets & ids), result
    seed = await runtime.mcp.call_tool(
        "expand_neighbors", {"entity_ids": [runtime.passage_id], "depth": 2}
    )
    assert not seed.is_error, seed
    assert bool(seed.structured_content["origins"]) is allowed
    if not allowed:
        assert runtime.passage_id in seed.structured_content["unresolved"]
    if allowed:
        result = await runtime.mcp.call_tool("fetch_slice", {"entity_id": runtime.passage_id})
        assert not result.is_error, result
    else:
        with pytest.raises(UnexpectedToolError) as denied:
            await runtime.mcp.call_tool("fetch_slice", {"entity_id": runtime.passage_id})
        assert isinstance(denied.value.__cause__, KeyError)
    assert before == await graph.client.execute_query("SELECT * FROM entity ORDER BY uuid;")


@pytest.fixture
async def operational_traversal(public_membership_runtime, monkeypatch):
    from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    runtime = public_membership_runtime
    runtime.graph = await get_surreal_graph_runtime(runtime.org)
    await runtime.graph.entity_manager.create_direct(
        Entity(id=runtime.project, entity_type=EntityType.PROJECT, name="Traversal project")
    )

    async def source_authority(organization_id, principal_id):
        if organization_id != runtime.org:
            return None
        ctx = await runtime.resolver.resolve({"sub": principal_id, "org": organization_id})
        if ctx is None or ctx.org_role is None:
            return None
        return SourceReadAuthority(
            principal_id,
            projects=frozenset(await list_accessible_project_graph_ids(ctx)),
            teams=ctx.accessible_teams,
            delegations=ctx.accessible_delegations,
        )

    monkeypatch.setattr("sibyl_core.runtime_ports._source_authority_resolver", source_authority)
    runtime.authority = await source_authority(runtime.org, runtime.owner)
    runtime.active_token["value"] = await reader_token(runtime, "session")
    return runtime


async def operational_source(runtime, *, outcome="failed", revision=None):
    from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
    from sibyl_core.models.experience import OperationalExperience
    from sibyl_core.services.content_raw_persistence import remember_raw_memory
    from sibyl_core.services.operational_capture import OperationalSourceWrite, canonical_experience
    from sibyl_core.services.operational_projection import load_operational_projection_source

    tree = "RootWebArea 'Read proof'\n" + "\n".join(
        "  StaticText 'Evidence " + str(index) + " " + ("text " * 18) + "'" for index in range(64)
    )
    payload = OperationalExperience.model_validate(
        {
            "source_id": "native-traversal",
            "project_id": runtime.project,
            "goal": "Read retained evidence",
            "outcome": outcome,
            "observations": [
                {
                    "id": "state",
                    "ordinal": 0,
                    "evidence": [
                        {
                            "id": "tree",
                            "content": tree,
                            "content_type": "text/plain; profile=accessibility-tree",
                        }
                    ],
                }
            ],
        }
    )
    memory = await remember_raw_memory(
        organization_id=runtime.org,
        principal_id=runtime.owner,
        source_id=payload.source_id,
        raw_content=canonical_experience(payload),
        memory_scope="project",
        scope_key=runtime.project,
        metadata={"project_id": runtime.project},
        capture_surface="operational_experience",
        embedding_provider=None,
        operational_write=OperationalSourceWrite(
            runtime.org, payload.source_id, runtime.owner, runtime.project, revision
        ),
    )
    source = await load_operational_projection_source(
        SourceIdentity(runtime.org, SourceKind.RAW_CAPTURE, memory.id), runtime.authority
    )
    return memory, source


async def registered_neighbors(runtime, origin, *, relationship="DERIVED_FROM", depth=1):
    result = await runtime.mcp.call_tool(
        "expand_neighbors",
        {
            "entity_ids": [origin],
            "relationship_types": [relationship],
            "depth": depth,
            "limit": 24,
        },
    )
    assert not result.is_error, result
    return {row["id"] for row in result.structured_content["neighbors"]}


async def registered_spans(runtime, parent):
    result = await runtime.mcp.call_tool("fetch_slice", {"entity_id": parent, "window": 64})
    assert not result.is_error, result
    return {row["id"] for row in result.structured_content["passages"]}


@pytest.mark.asyncio
async def test_operational_traversal_edge_generation(operational_traversal):
    from sibyl_core.models.entities import EntityType, RelationshipType
    from sibyl_core.services.graph_read_availability import (
        available_graph_entities,
        available_graph_relationships,
    )

    runtime = operational_traversal
    memory, source = await operational_source(runtime)
    old = await runtime.graph.entity_manager.publish_operational_entities(source)
    await runtime.graph.relationship_manager.publish_operational_relationships(source)
    passages = [row for row in old.entities if row.entity_type is EntityType.PASSAGE]
    ids = {row.id for row in passages}
    assert len(ids) > 1
    parent_id = passages[0].metadata["parent_entity_id"]
    edges = [
        edge.id
        for edge in old.relationships
        if edge.relationship_type is RelationshipType.DERIVED_FROM and edge.target_id == parent_id
    ]
    assert ids <= await registered_neighbors(runtime, parent_id)
    assert ids == await registered_spans(runtime, parent_id)
    _, fresh_source = await operational_source(runtime, outcome="unknown", revision=memory.revision)
    fresh = await runtime.graph.entity_manager.publish_operational_entities(fresh_source)
    assert old.manifest.content_hash != fresh.manifest.content_hash
    assert set(
        await available_graph_entities(runtime.org, [parent_id, *ids], runtime=runtime.graph)
    ) == {parent_id, *ids}
    assert not await available_graph_relationships(runtime.org, edges, runtime=runtime.graph)
    assert not ids & await registered_neighbors(runtime, parent_id)
    # An unsliced parent may still be readable, but old edge-selected spans
    # cannot be represented as a current window.
    assert not ids & await registered_spans(runtime, parent_id)
    await runtime.graph.relationship_manager.publish_operational_relationships(fresh_source)
    assert ids <= await registered_neighbors(runtime, parent_id)
    assert ids == await registered_spans(runtime, parent_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["promote", "share"])
async def test_modern_traversal_fresh_authority_and_retirement(
    operational_traversal, monkeypatch, mode
):
    from uuid import uuid4

    from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
    from sibyl_core.services.memory_correction import apply_memory_correction
    from sibyl_core.services.memory_reflection import promote_raw_memory
    from sibyl_core.services.memory_sharing import share_memory

    runtime = operational_traversal
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
    common = {
        "organization_id": runtime.org,
        "principal_id": runtime.owner,
        "accessible_projects": {runtime.project},
        "writable_projects": {runtime.project},
    }
    memory = runtime.memories["private"]
    if mode == "promote":
        result = await promote_raw_memory(
            raw_memory_id=memory.id,
            promote_to_scope="project",
            promote_to_scope_key=runtime.project,
            **common,
        )
    else:
        shared = await share_memory(
            source_ids=[memory.id],
            target_scope="project",
            target_scope_key=runtime.project,
            **common,
        )
        assert shared.applied, asdict(shared)
        result = shared.promotions[0]
    assert result.success, asdict(result)
    target = await runtime.graph.entity_manager.get(result.promoted_id)
    assert target.derivation_required
    anchor, far = [
        Entity(id=str(uuid4()), entity_type=EntityType.NOTE, name=name)
        for name in ("Independent anchor", "Independent far row")
    ]
    await runtime.graph.entity_manager.create_direct_bulk([anchor, far])
    await runtime.graph.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id=str(uuid4()),
                source_id=anchor.id,
                target_id=target.id,
                relationship_type=RelationshipType.RELATED_TO,
            ),
            Relationship(
                id=str(uuid4()),
                source_id=target.id,
                target_id=far.id,
                relationship_type=RelationshipType.RELATED_TO,
            ),
        ]
    )
    runtime.active_token["value"] = await reader_token(runtime, "absent")
    expected = {target.id, far.id}
    assert expected <= await registered_neighbors(
        runtime, anchor.id, relationship="RELATED_TO", depth=2
    )
    assert target.id in await registered_spans(runtime, target.id)
    await runtime.auth.execute_query(
        "DELETE organization_members WHERE organization_id=$org AND user_id=$user;",
        org=runtime.org,
        user=runtime.owner,
    )
    assert not expected & await registered_neighbors(
        runtime, anchor.id, relationship="RELATED_TO", depth=2
    )
    seed = await runtime.mcp.call_tool("expand_neighbors", {"entity_ids": [target.id]})
    assert not seed.is_error, seed
    assert not seed.structured_content["origins"]
    assert target.id in seed.structured_content["unresolved"]
    with pytest.raises(UnexpectedToolError) as denied:
        await runtime.mcp.call_tool("fetch_slice", {"entity_id": target.id})
    assert isinstance(denied.value.__cause__, KeyError)
    await runtime.auth.execute_query(
        "CREATE organization_members CONTENT $record;",
        record={
            "uuid": str(uuid4()),
            "organization_id": runtime.org,
            "user_id": runtime.owner,
            "role": "member",
        },
    )
    assert expected <= await registered_neighbors(
        runtime, anchor.id, relationship="RELATED_TO", depth=2
    )
    # The canonical mutation succeeds while graph stamping fails, leaving a
    # stored stale body that the next read must reject without repairing it.
    manager_type = type(runtime.graph.entity_manager)
    original = manager_type.update

    async def stamp_outage(manager, *args, **kwargs):
        if manager._group_id == runtime.org:
            raise RuntimeError("Owned projection stamp outage")
        return await original(manager, *args, **kwargs)

    monkeypatch.setattr(manager_type, "update", stamp_outage)
    corrected = await apply_memory_correction(
        organization_id=runtime.org,
        source_id=memory.id,
        principal_id=runtime.owner,
        action="delete",
    )
    monkeypatch.setattr(manager_type, "update", original)
    assert corrected.applied, corrected
    before = await runtime.graph.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    assert not expected & await registered_neighbors(
        runtime, anchor.id, relationship="RELATED_TO", depth=2
    )
    with pytest.raises(UnexpectedToolError) as denied:
        await runtime.mcp.call_tool("fetch_slice", {"entity_id": target.id})
    assert isinstance(denied.value.__cause__, KeyError)
    assert before == await runtime.graph.client.execute_query("SELECT * FROM entity ORDER BY uuid;")
    assert (await runtime.graph.entity_manager.get(target.id)).content == target.content


@pytest.mark.asyncio
async def test_fetch_slice_refreshes_source_after_discovery(operational_traversal, monkeypatch):

    from sibyl_core.services.memory_correction import apply_memory_correction
    from sibyl_core.services.memory_reflection import promote_raw_memory

    runtime = operational_traversal
    memory = runtime.memories["private"]
    promoted = await promote_raw_memory(
        raw_memory_id=memory.id,
        organization_id=runtime.org,
        principal_id=runtime.owner,
        promote_to_scope="project",
        promote_to_scope_key=runtime.project,
        accessible_projects={runtime.project},
        writable_projects={runtime.project},
    )
    assert promoted.success, asdict(promoted)
    target = await runtime.graph.entity_manager.get(promoted.promoted_id)
    assert target.id in await registered_spans(runtime, target.id)
    relationship_type = type(runtime.graph.relationship_manager)
    original_discovery = relationship_type.get_related_entities
    manager_type = type(runtime.graph.entity_manager)
    original_update = manager_type.update
    corrected_after_discovery = []

    async def stamp_outage(manager, *args, **kwargs):
        if manager._group_id == runtime.org:
            raise RuntimeError("Owned projection stamp outage")
        return await original_update(manager, *args, **kwargs)

    async def discovery(manager, *args, **kwargs):
        rows = await original_discovery(manager, *args, **kwargs)
        if manager._group_id != runtime.org:
            return rows
        monkeypatch.setattr(manager_type, "update", stamp_outage)
        corrected = await apply_memory_correction(
            organization_id=runtime.org,
            source_id=memory.id,
            principal_id=runtime.owner,
            action="delete",
        )
        monkeypatch.setattr(manager_type, "update", original_update)
        assert corrected.applied, corrected
        corrected_after_discovery.append(corrected)
        return rows

    monkeypatch.setattr(relationship_type, "get_related_entities", discovery)
    with pytest.raises(UnexpectedToolError) as denied:
        await runtime.mcp.call_tool("fetch_slice", {"entity_id": target.id})
    assert isinstance(denied.value.__cause__, KeyError)
    assert len(corrected_after_discovery) == 1
    assert (await runtime.graph.entity_manager.get(target.id)).content == target.content


@pytest.mark.asyncio
async def test_expand_neighbors_refreshes_source_after_hydration(
    operational_traversal, monkeypatch
):
    from sibyl_core.models.entities import EntityType
    from sibyl_core.services.memory_correction import apply_memory_correction

    runtime = operational_traversal
    memory, source = await operational_source(runtime)
    projection = await runtime.graph.entity_manager.publish_operational_entities(source)
    await runtime.graph.relationship_manager.publish_operational_relationships(source)
    passages = [row for row in projection.entities if row.entity_type is EntityType.PASSAGE]
    passage_ids = {row.id for row in passages}
    parent_id = passages[0].metadata["parent_entity_id"]
    assert passage_ids <= await registered_neighbors(runtime, parent_id)
    original_query = runtime.graph.client.execute_query
    corrected_after_hydration = []

    async def after_hydration(query, **params):
        rows = await original_query(query, **params)
        if (
            not corrected_after_hydration
            and "SELECT * FROM entity WHERE" in query
            and "uuid IN $uuids" in query
            and passage_ids & set(params.get("uuids", ()))
        ):
            corrected = await apply_memory_correction(
                organization_id=runtime.org,
                source_id=memory.id,
                principal_id=runtime.owner,
                action="delete",
                accessible_projects={runtime.project},
                writable_projects={runtime.project},
            )
            assert corrected.applied, corrected
            corrected_after_hydration.append(corrected)
        return rows

    monkeypatch.setattr(runtime.graph.client, "execute_query", after_hydration)
    assert not passage_ids & await registered_neighbors(runtime, parent_id)
    assert len(corrected_after_hydration) == 1
    assert corrected_after_hydration[0].updated_memory.revision > memory.revision
    retained = await runtime.graph.entity_manager.get_many(list(passage_ids))
    assert {row.id: row.content for row in retained} == {row.id: row.content for row in passages}
