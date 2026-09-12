from unittest.mock import AsyncMock

import pytest

from sibyl_core.retrieval._search_lifecycle import _apply_supersession_gate
from sibyl_core.retrieval._search_plan import RetrievalSignal, build_context_retrieval_plan
from tests.test_graph_edge_ancestry import candidate
from tests.test_operational_projection import authority as authority
from tests.test_operational_projection import capture
from tests.test_operational_projection import content_store as content_store
from tests.test_operational_projection import runtime as runtime


@pytest.fixture(autouse=True)
def relationship_authority(monkeypatch, authority):
    monkeypatch.setattr(
        "sibyl_core.services.operational_relationships.get_source_authority_resolver",
        lambda: AsyncMock(return_value=authority),
    )


async def edge_results(runtime, edge):
    plan = build_context_retrieval_plan(
        query="inspect",
        organization_id=runtime.client.group_id,
        facets=[],
        facet_types={},
        principal_id="user_a",
        project="project_a",
        accessible_projects=["project_a"],
    )
    lists, _ = await _apply_supersession_gate(
        client=runtime.client,
        group_id=runtime.client.group_id,
        source_lists=[(RetrievalSignal.EDGE_FULLTEXT, [candidate(edge)])],
        plan=plan,
    )
    return lists[0][1]


async def test_operational_relationship_adopts_exact_pending_legacy_after_reload(
    runtime, content_store, authority
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships
    from sibyl_core.services.operational_projection import load_operational_projection_source
    from tests.test_operational_legacy import legacy

    _, source = await capture(runtime, authority)
    original = await legacy(runtime, source)
    await runtime.entity_manager.publish_operational_entities(source)
    resumed = await load_operational_projection_source(source.observation.source, authority)
    ids = await runtime.relationship_manager.publish_operational_relationships(resumed)
    assert set(ids) == set(original.projection.manifest.relationship_ids)
    current = await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    assert set(current) == set(ids)
    before = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
    assert set(
        await runtime.relationship_manager.publish_operational_relationships(resumed)
    ) == set(ids)
    assert before == await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")


async def test_operational_relationship_adoption_fences_edge_mutation_after_proof(
    runtime, content_store, authority, monkeypatch
):
    from sibyl_core.services.operational_projection import OperationalProjectionSource
    from tests.test_operational_legacy import legacy

    _, source = await capture(runtime, authority)
    original = await legacy(runtime, source)
    await runtime.entity_manager.publish_operational_entities(source)
    real = OperationalProjectionSource.legacy_adoption_proof
    edge_id = original.projection.relationships[0].id

    async def changed_after_proof(self, client):
        proof = await real(self, client)
        await client.execute_query(
            "UPDATE relates_to SET fact='changed after adoption proof' WHERE uuid=$id;", id=edge_id
        )
        return proof

    monkeypatch.setattr(OperationalProjectionSource, "legacy_adoption_proof", changed_after_proof)
    with pytest.raises(
        Exception, match="operational relationship source changed before publication"
    ):
        await runtime.relationship_manager.publish_operational_relationships(source)
    rows = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
    assert rows
    assert all(row.get("operational_derivation_required") is not True for row in rows)
    assert (
        next(row for row in rows if row["uuid"] == edge_id)["fact"]
        == "changed after adoption proof"
    )


async def test_operational_edge_old_generation_cannot_rejoin_updated_endpoints(
    runtime, content_store, authority
):
    memory, old = await capture(runtime, authority)
    old_projection = await runtime.entity_manager.publish_operational_entities(old)
    await runtime.relationship_manager.publish_operational_relationships(old)
    assert any(
        [bool(await edge_results(runtime, edge)) for edge in old_projection.relationships]
    ), "Original published edges must be available before source revision"
    _, new = await capture(runtime, authority, outcome="unknown", revision=memory.revision)
    new_projection = await runtime.entity_manager.publish_operational_entities(new)
    current_ids = set(new_projection.manifest.entity_ids)
    old_edges = [
        e
        for e in old_projection.relationships
        if e.source_id in current_ids and e.target_id in current_ids
    ]
    assert old_edges
    assert old_projection.manifest.content_hash != new_projection.manifest.content_hash
    exposed = []
    for edge in old_edges:
        if await edge_results(runtime, edge):
            exposed.append(edge.id)
    assert not exposed, "Old relationship generation remains available after endpoint reprojection"

    await runtime.relationship_manager.publish_operational_relationships(new)
    assert any([bool(await edge_results(runtime, edge)) for edge in new_projection.relationships])


async def test_operational_relationship_archive_retains_binding(
    runtime, content_store, authority, monkeypatch
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships
    from sibyl_core.tools import admin

    monkeypatch.setattr(admin, "get_graph_runtime", AsyncMock(return_value=runtime))
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    ids = [r.id for r in projection.relationships]
    assert len(
        await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    ) == len(ids)
    backup = await admin.create_backup(organization_id=runtime.client.group_id)
    assert backup.success, backup.error
    result = await admin.restore_backup(
        backup.backup_data, organization_id=runtime.client.group_id, clean=True, skip_existing=False
    )
    assert result.success, result.error
    assert len(
        await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    ) == len(ids)


@pytest.mark.parametrize("mutation", ["body", "move", "binding", "marker"])
async def test_operational_relationship_generic_writer_cannot_replace(
    runtime, content_store, authority, mutation
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    edge = projection.relationships[0]
    before = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
    changes = {
        "body": {"metadata": {**edge.metadata, "fact": "caller replacement"}},
        "move": {"target_id": projection.entities[-1].id},
        "binding": {"operational_source_binding": {"version": 1}},
        "marker": {"operational_derivation_required": True},
    }
    with pytest.raises(Exception, match=r"[Oo]perational|protected"):
        await runtime.relationship_manager.create(edge.model_copy(update=changes[mutation]))
    assert before == await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")


async def test_operational_relationship_unbound_legacy_is_not_current(
    runtime, content_store, authority
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.create_direct_bulk(projection.relationships)
    ids = [edge.id for edge in projection.relationships]
    assert not await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)


async def test_operational_relationship_ordinary_and_eval_links_remain_available(
    runtime, content_store, authority
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    edges = [
        projection.relationships[0].model_copy(
            update={
                "id": f"ordinary_{category}",
                "metadata": {"category": category, "fact": "A normal relationship"},
            }
        )
        for category in ("ordinary", "eval")
    ]
    await runtime.relationship_manager.create_direct_bulk(edges)
    result = await available_graph_relationships(
        runtime.client.group_id, [e.id for e in edges], runtime=runtime
    )
    assert set(result) == {e.id for e in edges}


async def test_operational_relationship_same_payload_new_incarnation_denied(
    runtime, content_store, authority
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    ids = [e.id for e in projection.relationships]
    assert await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    endpoint = projection.relationships[0].source_id
    await runtime.client.execute_query(
        "UPDATE source_states SET incarnation=type::string(rand::uuid()) WHERE source_kind='graph_entity' AND source_id=$id;",
        id=endpoint,
    )
    result = await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    affected = {e.id for e in projection.relationships if endpoint in (e.source_id, e.target_id)}
    assert not affected.intersection(result)


async def test_operational_relationship_body_tamper_denied(runtime, content_store, authority):
    from sibyl_core.services.graph_read_availability import available_graph_relationships

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    edge = projection.relationships[0]
    await runtime.client.execute_query(
        "UPDATE relates_to SET fact='replacement' WHERE uuid=$id;", id=edge.id
    )
    assert not await available_graph_relationships(
        runtime.client.group_id, [edge.id], runtime=runtime
    )


@pytest.mark.parametrize("raced", [False, True])
@pytest.mark.parametrize("mutation", ["entity", "association"])
async def test_operational_relationship_native_late_source_race(
    runtime, content_store, authority, monkeypatch, raced, mutation
):
    import asyncio
    import os

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("native multi-connection transaction control")
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    endpoint = projection.relationships[0].source_id
    execute = runtime.client.execute_query
    intercepted = False

    async def delayed(query, **kwargs):
        nonlocal intercepted
        if "operational relationship source changed before publication" not in query or intercepted:
            return await execute(query, **kwargs)
        intercepted = True
        pending = asyncio.create_task(
            execute(
                query.replace(
                    "LET $source_states_to_fence = $states;",
                    "SLEEP 1s; LET $source_states_to_fence = $states;",
                    1,
                ),
                **kwargs,
            )
        )
        try:
            if raced:
                await asyncio.sleep(0.25)
                query = (
                    "UPDATE entity SET content='concurrent replacement' WHERE uuid=$id RETURN AFTER;"
                    if mutation == "entity"
                    else "UPDATE memory_derivations SET active=false WHERE target_id=$id RETURN AFTER;"
                )
                changed = await execute(query, id=endpoint)
                assert len(changed) == 1
            return await pending
        finally:
            if not pending.done():
                await pending

    monkeypatch.setattr(runtime.client, "execute_query", delayed)
    if raced:
        with pytest.raises(Exception, match=r"operational relationship source changed|transaction"):
            await runtime.relationship_manager.publish_operational_relationships(source)
        assert not await execute("SELECT * FROM relates_to;")
        if mutation == "entity":
            row = await runtime.entity_manager.get(endpoint)
            assert row.content == "concurrent replacement"
        else:
            rows = await execute(
                "SELECT * FROM memory_derivations WHERE target_id=$id;", id=endpoint
            )
            assert len(rows) == 1 and rows[0]["active"] is False
    else:
        assert await runtime.relationship_manager.publish_operational_relationships(source)
        assert len(await execute("SELECT * FROM relates_to;")) == len(projection.relationships)
    assert intercepted


@pytest.mark.parametrize("raced", [False, True])
async def test_operational_relationship_completion_witness_fences_edges(
    runtime, content_store, authority, raced
):
    import asyncio
    import os

    from sibyl_core.services.operational_relationships import (
        _SNAPSHOT,
        OPERATIONAL_SNAPSHOT_WRITE_WITNESS,
        _snapshot,
        operational_relationship_inventory_current,
    )

    if not os.environ.get("SIBYL_COHORT_NATIVE_URL"):
        pytest.skip("native multi-connection completion transaction control")
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    ids = list(projection.manifest.entity_ids)
    edge_ids = [edge.id for edge in projection.relationships]
    snapshot = await _snapshot(
        runtime.client, organization_id=runtime.client.group_id, ids=ids, relationship_ids=edge_ids
    )
    assert await operational_relationship_inventory_current(source, snapshot)
    execute = runtime.client.execute_query
    pending = asyncio.create_task(
        execute(
            "RETURN {"
            + _SNAPSHOT
            + """
        IF $operational_snapshot_fingerprint != $fingerprint {
            THROW 'completion support changed';
        };
        SLEEP 1s;
        """
            + OPERATIONAL_SNAPSHOT_WRITE_WITNESS
            + """
        UPDATE entity SET attributes.completion_witness_test=true WHERE uuid=$manifest;
        RETURN true; };
        """,
            org=runtime.client.group_id,
            ids=ids,
            relationship_ids=edge_ids,
            fingerprint=snapshot["fingerprint"],
            manifest=projection.manifest.manifest_entity_id,
        )
    )
    try:
        if raced:
            await asyncio.sleep(0.25)
            changed = await execute(
                "UPDATE relates_to SET fact='concurrent edge' WHERE uuid=$id RETURN AFTER;",
                id=edge_ids[0],
            )
            assert len(changed) == 1
            with pytest.raises(Exception, match=r"completion support changed|transaction"):
                await pending
        else:
            await pending
    finally:
        if not pending.done():
            await pending
    manifest = (
        await execute(
            "SELECT * FROM entity WHERE uuid=$id;", id=projection.manifest.manifest_entity_id
        )
    )[0]
    assert bool(manifest["attributes"].get("completion_witness_test")) is not raced
    if raced:
        edge = (await execute("SELECT * FROM relates_to WHERE uuid=$id;", id=edge_ids[0]))[0]
        assert edge["fact"] == "concurrent edge"
    else:
        edges = await execute("SELECT * FROM relates_to;")
        assert all(edge["attributes"].get("operational_write_witness") for edge in edges)


async def test_operational_relationship_cached_views_do_not_rejoin_old_edges(
    runtime, content_store, authority
):
    from sibyl_core.services import graph_community_clusters as clusters
    from sibyl_core.services import graph_community_hierarchy as hierarchy
    from sibyl_core.services import graph_community_snapshot as snapshots

    memory, source = await capture(runtime, authority)
    original = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    snapshots.GRAPH_SNAPSHOT_CACHE.clear()
    clusters.CLUSTER_CACHE.clear()
    hierarchy.HIERARCHICAL_CACHE.clear()
    hierarchy.GRAPH_LOD_CACHE.clear()
    reader = {"principal_id": "user_a", "accessible_projects": {"project_a"}}
    before = await snapshots._get_visible_graph_snapshot(
        runtime.client, runtime.client.group_id, **reader
    )
    assert {e.id for e in original.relationships} == {e.id for e in before.relationships}
    await clusters.get_clusters_for_visualization(runtime.client, runtime.client.group_id, **reader)
    warm = await hierarchy.get_hierarchical_graph(runtime.client, runtime.client.group_id, **reader)
    assert warm.total_edges > 0
    _, changed = await capture(runtime, authority, outcome="unknown", revision=memory.revision)
    current = await runtime.entity_manager.publish_operational_entities(changed)
    after = await snapshots._get_visible_graph_snapshot(
        runtime.client, runtime.client.group_id, **reader
    )
    assert set(current.manifest.entity_ids).intersection(after.entity_by_id)
    assert not after.relationships
    cold_edges = await hierarchy.get_hierarchical_graph(
        runtime.client, runtime.client.group_id, **reader
    )
    assert cold_edges.total_edges == 0
    await runtime.relationship_manager.publish_operational_relationships(changed)
    restored = await snapshots._get_visible_graph_snapshot(
        runtime.client, runtime.client.group_id, **reader
    )
    assert restored.relationships


async def test_operational_relationship_schema_upgrade_retains_ordinary_edges(
    runtime, content_store, authority
):
    from sibyl_core.backends.surreal import schema
    from sibyl_core.services.graph_read_availability import available_graph_relationships

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    edge = projection.relationships[0].model_copy(
        update={"id": "pre_upgrade", "metadata": {"fact": "ordinary old edge"}}
    )
    await runtime.relationship_manager.create(edge)
    execute = runtime.client.execute_query
    await execute("REMOVE FIELD operational_derivation_required ON relates_to;")
    await execute("REMOVE FIELD operational_source_binding ON relates_to;")
    await execute("REMOVE FIELD validation_write_witness ON memory_derivations;")
    await execute("UPDATE schema_version:graph SET version=27;")
    before = await execute("SELECT * FROM relates_to WHERE uuid=$id;", id=edge.id)
    await schema.bootstrap_schema(runtime.client)
    assert await execute("SELECT VALUE version FROM schema_version:graph;") == [
        schema.GRAPH_SCHEMA_CURRENT_VERSION
    ]
    info = await execute("INFO FOR TABLE relates_to;")
    assert "operational_source_binding" in info["fields"]
    associations = await execute("INFO FOR TABLE memory_derivations;")
    assert "validation_write_witness" in associations["fields"]
    after = await execute("SELECT * FROM relates_to WHERE uuid=$id;", id=edge.id)
    assert before == after
    assert set(
        await available_graph_relationships(runtime.client.group_id, [edge.id], runtime=runtime)
    ) == {edge.id}
    await runtime.relationship_manager.publish_operational_relationships(source)
    assert await available_graph_relationships(
        runtime.client.group_id, [projection.relationships[0].id], runtime=runtime
    )


@pytest.mark.parametrize("revocation", ["source", "membership"])
async def test_operational_relationship_revocation_denies_read_and_publish(
    runtime, content_store, authority, monkeypatch, revocation
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships
    from sibyl_core.services.memory_correction import apply_memory_correction
    from sibyl_core.services.memory_source_validation import SourceReadAuthority
    from sibyl_core.services.source_observations import SourceUnavailableError

    memory, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    ids = [e.id for e in projection.relationships]
    if revocation == "source":
        correction = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            source_id=memory.id,
            action="hide",
            accessible_projects={"project_a"},
            writable_projects={"project_a"},
        )
        assert correction.applied, correction
    else:
        for module in ("operational_projection", "graph_derivations", "operational_relationships"):
            monkeypatch.setattr(
                f"sibyl_core.services.{module}.get_source_authority_resolver",
                lambda: AsyncMock(return_value=SourceReadAuthority("user_a")),
            )
    assert not await available_graph_relationships(runtime.client.group_id, ids, runtime=runtime)
    with pytest.raises(SourceUnavailableError):
        await runtime.relationship_manager.publish_operational_relationships(source)


async def test_operational_relationship_completion_requires_exact_inventory(
    runtime, content_store, authority
):
    from copy import deepcopy

    from sibyl_core.services.operational_relationships import (
        _snapshot,
        operational_relationship_inventory_current,
    )

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    snapshot = await _snapshot(
        runtime.client,
        organization_id=runtime.client.group_id,
        ids=projection.manifest.entity_ids,
        relationship_ids=[r.id for r in projection.relationships],
    )
    assert await operational_relationship_inventory_current(source, snapshot)
    missing = deepcopy(snapshot)
    missing["relationships"].pop()
    assert not await operational_relationship_inventory_current(source, missing)
    changed = deepcopy(snapshot)
    changed["relationships"][0]["fact"] = "unsupported replacement"
    assert not await operational_relationship_inventory_current(source, changed)
    foreign = deepcopy(snapshot)
    foreign["relationships"][0]["operational_source_binding"]["source"]["incarnation"] = (
        "another-incarnation"
    )
    assert not await operational_relationship_inventory_current(source, foreign)
    endpoint = projection.relationships[0].source_id
    for field, value in (
        ("active", False),
        ("principal_id", "another-principal"),
        ("organization_id", "another-org"),
        ("target_kind", "raw_capture"),
        ("target_id", "another-target"),
        ("authority_ceiling", {}),
    ):
        invalid = deepcopy(snapshot)
        association = next(row for row in invalid["associations"] if row["target_id"] == endpoint)
        association[field] = value
        assert not await operational_relationship_inventory_current(source, invalid), field
    await runtime.client.execute_query(
        "UPDATE memory_derivations SET active=false WHERE target_id=$id;", id=endpoint
    )
    inactive = await _snapshot(
        runtime.client,
        organization_id=runtime.client.group_id,
        ids=projection.manifest.entity_ids,
        relationship_ids=[r.id for r in projection.relationships],
    )
    assert not await operational_relationship_inventory_current(source, inactive)


async def test_operational_relationship_exact_replay_preserves_rows(
    runtime, content_store, authority
):
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    expected = [r.id for r in projection.relationships]
    assert await runtime.relationship_manager.publish_operational_relationships(source) == expected
    before = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
    assert await runtime.relationship_manager.publish_operational_relationships(source) == expected
    assert before == await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")


@pytest.mark.parametrize("method", ["delete", "delete_bulk", "delete_between"])
async def test_operational_relationship_retraction_retains_protected_identity(
    runtime, content_store, authority, method
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships
    from sibyl_core.services.source_observations import SourceUnavailableError

    memory, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    edge = projection.relationships[0]
    ordinary = edge.model_copy(
        update={"id": "ordinary-delete", "metadata": {"fact": "ordinary link"}}
    )
    await runtime.relationship_manager.create(ordinary)
    before = (
        await runtime.client.execute_query("SELECT * FROM relates_to WHERE uuid=$id;", id=edge.id)
    )[0]

    async def remove():
        manager = runtime.relationship_manager
        if method == "delete":
            return sum([await manager.delete(edge.id), await manager.delete(ordinary.id)])
        if method == "delete_bulk":
            return await manager.delete_bulk([edge.id, ordinary.id, edge.id])
        return await manager.delete_between(edge.source_id, edge.target_id, edge.relationship_type)

    assert await remove() == 2
    assert await remove() == 0
    rows = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid IN $ids;", ids=[edge.id, ordinary.id]
    )
    assert len(rows) == 1
    retired = rows[0]
    assert retired["uuid"] == edge.id
    assert retired["operational_derivation_required"] is True
    assert retired["operational_source_binding"] == before["operational_source_binding"]
    assert retired["invalid_at"] and retired["expired_at"]
    assert not await available_graph_relationships(
        runtime.client.group_id, [edge.id], runtime=runtime
    )
    with pytest.raises(Exception, match="protected operational relationship"):
        await runtime.relationship_manager.create(
            edge.model_copy(update={"metadata": {"fact": "converted ordinary"}})
        )
    with pytest.raises(SourceUnavailableError):
        await runtime.relationship_manager.publish_operational_relationships(source)
    assert (
        retired
        == (
            await runtime.client.execute_query(
                "SELECT * FROM relates_to WHERE uuid=$id;", id=edge.id
            )
        )[0]
    )

    _, revised = await capture(
        runtime, authority, revision=memory.revision, metadata={"encoding_revision": 2}
    )
    await runtime.entity_manager.publish_operational_entities(revised)
    await runtime.relationship_manager.publish_operational_relationships(revised)
    assert await available_graph_relationships(runtime.client.group_id, [edge.id], runtime=runtime)


@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("active", [False, True])
async def test_operational_relationship_restore_respects_mode(
    runtime, content_store, authority, monkeypatch, clean, active
):
    from sibyl_core.services.graph_read_availability import available_graph_relationships
    from sibyl_core.tools import admin

    monkeypatch.setattr(admin, "get_graph_runtime", AsyncMock(return_value=runtime))
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    backup = await admin.create_backup(organization_id=runtime.client.group_id)
    assert backup.success
    edge = projection.relationships[0]
    if active:
        await runtime.client.execute_query(
            "UPDATE relates_to SET attributes.operational_write_witness='retained' WHERE uuid=$id;",
            id=edge.id,
        )
    else:
        assert await runtime.relationship_manager.delete(edge.id)
    before = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=edge.id
    )
    restored = await admin.restore_backup(
        backup.backup_data, organization_id=runtime.client.group_id, clean=clean, skip_existing=True
    )
    assert restored.success, restored
    after = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE uuid=$id;", id=edge.id
    )
    if clean:
        assert restored.relationships_skipped == 0
        assert after != before
        assert await available_graph_relationships(
            runtime.client.group_id, [edge.id], runtime=runtime
        )
    else:
        assert restored.relationships_skipped == len(projection.relationships)
        assert after == before
        current = await available_graph_relationships(
            runtime.client.group_id, [edge.id], runtime=runtime
        )
        assert bool(current) is active


@pytest.mark.parametrize("race", [False, True])
async def test_operational_relationship_eager_embedding_is_source_fenced(
    runtime, content_store, authority, race
):
    from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata

    calls = []
    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)

    class Spy(DeterministicEmbeddingProvider):
        async def embed_texts(self, texts, *, input_kind="document"):
            calls.append(list(texts))
            if race:
                await runtime.client.execute_query(
                    "UPDATE entity SET content='changed during embedding' WHERE uuid=$id;",
                    id=projection.relationships[0].source_id,
                )
            return await super().embed_texts(texts, input_kind=input_kind)

    runtime.relationship_manager._embedding_provider = Spy(
        EmbeddingMetadata("test", "one", EMBEDDING_DIM, "test", "bytes")
    )
    if race:
        with pytest.raises(Exception, match="operational relationship source changed"):
            await runtime.relationship_manager.publish_operational_relationships(
                source, generate_embeddings=True
            )
        assert calls
        assert not await runtime.client.execute_query("SELECT * FROM relates_to;")
    else:
        await runtime.relationship_manager.publish_operational_relationships(
            source, generate_embeddings=True
        )
        assert len(calls) == 1 and len(calls[0]) == len(projection.relationships)
        before = await runtime.client.execute_query("SELECT * FROM relates_to ORDER BY uuid;")
        assert all(len(row["fact_embedding"]) == EMBEDDING_DIM for row in before)
        await runtime.relationship_manager.publish_operational_relationships(
            source, generate_embeddings=True
        )
        await runtime.relationship_manager.publish_operational_relationships(source)
        assert len(calls) == 1
        assert before == await runtime.client.execute_query(
            "SELECT * FROM relates_to ORDER BY uuid;"
        )


@pytest.mark.parametrize("field", ["operational_write_witness", "fact"])
async def test_operational_publication_snapshot_distinguishes_bookkeeping(
    runtime, content_store, authority, monkeypatch, field
):
    from sibyl_core.services import operational_relationships as owner

    _, source = await capture(runtime, authority)
    await runtime.entity_manager.publish_operational_entities(source)
    ids = await runtime.relationship_manager.publish_operational_relationships(source)
    original = owner._snapshot

    async def changed_after_snapshot(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        column = (
            "attributes.operational_write_witness"
            if field == "operational_write_witness"
            else "fact"
        )
        await runtime.client.execute_query(
            f"UPDATE relates_to SET {column}='after snapshot' WHERE uuid=$id;", id=ids[0]
        )
        return snapshot

    monkeypatch.setattr(owner, "_snapshot", changed_after_snapshot)
    if field == "fact":
        with pytest.raises(Exception, match="source changed before publication"):
            await runtime.relationship_manager.publish_operational_relationships(source)
        rows = await runtime.client.execute_query(
            "SELECT fact FROM relates_to WHERE uuid=$id;", id=ids[0]
        )
        assert rows[0]["fact"] == "after snapshot"
    else:
        assert await runtime.relationship_manager.publish_operational_relationships(source) == ids


@pytest.mark.parametrize("mutation", ["witness", "body", "binding", "retired", "endpoint"])
async def test_operational_relationship_retrieval_ignores_only_witness(
    runtime, content_store, authority, monkeypatch, mutation
):
    from sibyl_core.retrieval import _search_lifecycle

    _, source = await capture(runtime, authority)
    projection = await runtime.entity_manager.publish_operational_entities(source)
    await runtime.relationship_manager.publish_operational_relationships(source)
    edge = projection.relationships[0]
    current = _search_lifecycle.available_graph_relationships

    async def changed(*args, **kwargs):
        updates = {
            "witness": "attributes.operational_write_witness='new-witness'",
            "body": "fact='changed evidence'",
            "binding": "operational_source_binding=NONE",
            "retired": "invalid_at=time::now()",
            "endpoint": "source_id='different-endpoint'",
        }
        await runtime.client.execute_query(
            "UPDATE relates_to SET " + updates[mutation] + " WHERE uuid=$id;",
            id=edge.id,
        )
        return await current(*args, **kwargs)

    monkeypatch.setattr(_search_lifecycle, "available_graph_relationships", changed)
    assert bool(await edge_results(runtime, edge)) is (mutation == "witness")
