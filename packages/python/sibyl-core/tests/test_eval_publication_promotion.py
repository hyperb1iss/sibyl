"""Only the designated draft can cross promotion's unpublished boundary."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.services import content_models, memory_reflection
from sibyl_core.services import eval_publication as p
from sibyl_core.services.content_raw_persistence import (
    get_raw_memory,
    remember_reflection_candidate_review,
)
from sibyl_core.services.content_raw_recall import recall_raw_memory
from sibyl_core.services.graph_client import (
    SurrealGraphClient,
    mark_graph_schema_dirty,
    prepare_graph_schema,
)
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from tests.test_eval_consolidation import admitted_pair as admitted_pair
from tests.test_eval_publication import proposal as proposal
from tests.test_eval_receipts import evidence as evidence
from tests.test_reflection_identity import content_store as content_store


@pytest.fixture
async def runtime(monkeypatch):
    client = SurrealGraphClient(group_id="org", url="memory://")
    mark_graph_schema_dirty("org")
    await prepare_graph_schema(client)
    runtime = GraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id="org"),
        relationship_manager=RelationshipManager(client, group_id="org"),
    )
    monkeypatch.setattr(
        memory_reflection, "get_surreal_graph_runtime", AsyncMock(return_value=runtime)
    )
    try:
        yield runtime
    finally:
        await client.close()


async def test_designated_candidate_promotes_and_replays_without_early_recall(
    proposal, runtime, monkeypatch
):
    op, result = proposal
    stored = await p.store_consolidation(op, result)
    recall_params = dict(
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        query="Check the actual output",
    )
    assert stored.memory.id not in {
        memory.id for memory in await recall_raw_memory(**recall_params)
    }
    observed = []
    create = runtime.entity_manager.create_direct_if_absent

    async def watched(entity):
        observed.append(graph_metadata_recallable(entity.metadata))
        return await create(entity)

    monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", watched)
    first = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert first.success, first
    assert observed == [False]
    current = await get_raw_memory(organization_id=op.organization_id, memory_id=stored.memory.id)
    assert current.review_state == "promoted"
    assert content_models.raw_memory_recallable(current)
    assert current.id in {memory.id for memory in await recall_raw_memory(**recall_params)}
    assert graph_metadata_recallable((await runtime.entity_manager.get(first.promoted_id)).metadata)
    assert current.metadata["source_bindings"] == stored.memory.metadata["source_bindings"]
    replay = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert replay.success and replay.promoted_id == first.promoted_id
    assert (await p.get_stored_consolidation(op)).memory.review_state == "promoted"


async def test_another_unpublished_candidate_is_not_support(proposal, runtime):
    op, result = proposal
    first = await p.store_consolidation(op, result)
    second = await remember_reflection_candidate_review(
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        candidate=result.candidate,
        raw_source_ids=[first.memory.id],
        source_memories=[first.memory],
    )
    outcome = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=second.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert not outcome.success
    assert outcome.reason == "source_not_recallable"


async def test_designated_exception_does_not_waive_lifecycle(proposal):
    op, result = proposal
    stored = await p.store_consolidation(op, result)
    archived = replace(stored.memory, review_state="archived")
    assert not memory_reflection._publication_source_recallable(archived, archived.id)
    assert not memory_reflection._publication_source_recallable(stored.memory, "different")


async def test_interrupted_graph_publication_remains_hidden_and_resumes(
    proposal, runtime, monkeypatch
):
    op, result = proposal
    stored = await p.store_consolidation(op, result)

    class Interrupted(BaseException):
        pass

    create = memory_reflection._write_promotion_relationships

    async def stop(*_args):
        raise Interrupted()

    monkeypatch.setattr(memory_reflection, "_write_promotion_relationships", stop)
    kwargs = dict(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    with pytest.raises(Interrupted):
        await memory_reflection.promote_reflection_candidate_review(**kwargs)
    current = await get_raw_memory(organization_id=op.organization_id, memory_id=stored.memory.id)
    graph = await runtime.entity_manager.get(current.metadata["promoted_entity_id"])
    assert not content_models.raw_memory_recallable(current)
    assert not graph_metadata_recallable(graph.metadata)
    monkeypatch.setattr(memory_reflection, "_write_promotion_relationships", create)
    resumed = await memory_reflection.promote_reflection_candidate_review(**kwargs)
    assert resumed.success, resumed
    assert graph_metadata_recallable(
        (await runtime.entity_manager.get(resumed.promoted_id)).metadata
    )


async def test_source_deleted_at_finalization_cannot_publish(proposal, runtime, monkeypatch):
    from sibyl_core.services import content_client

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    finalize = memory_reflection._mark_promotion_plan_promoted

    async def changed(**kwargs):
        async with content_client.surreal_content_client() as client:
            await client.execute_query(
                "DELETE raw_captures WHERE uuid = $id;",
                id=result.group.episodes[0].stored_sources[0].source_id,
            )
        return await finalize(**kwargs)

    monkeypatch.setattr(memory_reflection, "_mark_promotion_plan_promoted", changed)
    promoted = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert not promoted.success
    current = await get_raw_memory(organization_id=op.organization_id, memory_id=stored.memory.id)
    assert not content_models.raw_memory_recallable(current)


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE raw_captures SET metadata.eval_admission.receipt_sha256 = 'changed' WHERE uuid = $source_id;",
        "UPDATE raw_captures SET metadata.eval_admission.assignment_sha256 = 'changed' WHERE uuid = $source_id;",
        "UPDATE raw_captures SET metadata.eval_admission.admission_id = 'changed' WHERE uuid = $source_id;",
        "UPDATE raw_captures SET raw_content = 'changed' WHERE uuid = $source_id;",
        "UPDATE eval_attempts SET assignment_json = '{}' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET assignment_sha256 = 'changed' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET receipt_base64 = 'Y2hhbmdlZA==' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET receipt_sha256 = 'changed' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET outcome_sha256 = 'changed' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET transcript_sha256 = 'changed' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET episode_sha256 = 'changed' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET admitted_at = NONE WHERE capture_id = $source_id;",
    ],
)
@pytest.mark.parametrize("at_finalization", [False, True])
async def test_original_admission_survives_candidate_review_boundary(
    proposal, runtime, monkeypatch, query, at_finalization
):
    from sibyl_core.services import content_client

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    source_id = result.group.episodes[0].stored_sources[0].source_id
    async with content_client.surreal_content_client() as client:
        execute = client.execute_query
        raw_execute = client.execute_query_raw
        mutations = []
        if at_finalization:

            async def changed(sql, **params):
                if params.get("publication_operation_id") and params.get("source_observations"):
                    await execute(query, source_id=source_id)
                    mutations.append(True)
                return await raw_execute(sql, **params)

            monkeypatch.setattr(client, "execute_query_raw", changed)
        else:
            await execute(query, source_id=source_id)
        outcome = await memory_reflection.promote_reflection_candidate_review(
            candidate_id=stored.memory.id,
            organization_id=op.organization_id,
            principal_id=op.principal_id,
            promote_to_scope="private",
        )
    if at_finalization:
        assert mutations == [True]
    assert not outcome.success
    current = await get_raw_memory(organization_id=op.organization_id, memory_id=stored.memory.id)
    assert not content_models.raw_memory_recallable(current)
    graph_id = current.metadata.get("promoted_entity_id")
    if graph_id:
        assert not graph_metadata_recallable((await runtime.entity_manager.get(graph_id)).metadata)


@pytest.mark.parametrize("field,value", [("raw_source_ids", []), ("source_bindings", {})])
async def test_original_source_bindings_cannot_be_removed(proposal, runtime, field, value):
    from sibyl_core.services import content_client

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET metadata." + field + " = $value WHERE uuid = $id;",
            id=stored.memory.id,
            value=value,
        )
    outcome = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert not outcome.success


async def test_revision_change_at_finalization_returns_denial(proposal, runtime, monkeypatch):
    await test_original_admission_survives_candidate_review_boundary(
        proposal,
        runtime,
        monkeypatch,
        "UPDATE raw_captures SET revision += 1 WHERE uuid = $source_id;",
        True,
    )


@pytest.mark.parametrize(
    "query",
    [
        "DELETE raw_captures WHERE uuid = $source_id;",
        "UPDATE raw_captures SET deleted_at = time::now() WHERE uuid = $source_id;",
        "UPDATE raw_captures SET review_state = 'archived' WHERE uuid = $source_id;",
        "UPDATE raw_captures SET principal_id = 'other' WHERE uuid = $source_id;",
        "UPDATE raw_captures SET metadata.eval_admission.receipt_sha256 = 'changed' WHERE uuid = $source_id;",
        "UPDATE eval_attempts SET receipt_base64 = 'Y2hhbmdlZA==' WHERE capture_id = $source_id;",
        "UPDATE eval_attempts SET assignment_json = '{}' WHERE capture_id = $source_id;",
        "UPDATE raw_captures UNSET metadata.eval_consolidation WHERE uuid = $candidate_id; DELETE raw_captures WHERE uuid = $source_id;",
        "UPDATE raw_captures SET metadata.eval_consolidation = 'forged' WHERE uuid = $candidate_id; DELETE raw_captures WHERE uuid = $source_id;",
    ],
)
async def test_published_sources_are_current_in_actual_raw_and_graph_retrieval(
    proposal, runtime, monkeypatch, query
):
    from sibyl_core.models.context import ContextFacet
    from sibyl_core.retrieval import _search_database
    from sibyl_core.retrieval.search import build_context_retrieval_plan, context_search
    from sibyl_core.services import content_client

    op, result = proposal
    stored = await p.store_consolidation(op, result)
    promoted = await memory_reflection.promote_reflection_candidate_review(
        candidate_id=stored.memory.id,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        promote_to_scope="private",
    )
    assert promoted.success
    graph = await runtime.entity_manager.get(promoted.promoted_id)
    assert graph.metadata["source_bindings"][stored.memory.id] >= 1
    monkeypatch.setattr(
        _search_database, "get_surreal_graph_runtime", AsyncMock(return_value=runtime)
    )
    plan = build_context_retrieval_plan(
        query="actual output",
        project=None,
        accessible_projects=None,
        organization_id=op.organization_id,
        principal_id=op.principal_id,
        facets=[ContextFacet.PROCEDURES],
        facet_types={ContextFacet.PROCEDURES: ["procedure"]},
        limit=10,
    )

    async def no_raw(**_kwargs):
        return []

    async def read():
        raw = await recall_raw_memory(
            organization_id=op.organization_id,
            principal_id=op.principal_id,
            query="actual output",
            limit=100,
        )
        response = await context_search(
            plan=plan,
            types=["procedure"],
            facet=ContextFacet.PROCEDURES,
            raw_memory_recall_fn=no_raw,
        )
        return {memory.id for memory in raw}, {item.id for item in response.results}

    raw_ids, graph_ids = await read()
    assert stored.memory.id in raw_ids
    assert promoted.promoted_id in graph_ids
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            query,
            source_id=result.group.episodes[0].stored_sources[0].source_id,
            candidate_id=stored.memory.id,
        )
    if "$candidate_id" in query:
        metadata = dict(graph.metadata)
        metadata.pop("source_bindings", None)
        if "UNSET" in query:
            metadata.pop("eval_consolidation", None)
        else:
            metadata["eval_consolidation"] = "forged"
        await runtime.entity_manager.update(graph.id, {"metadata": metadata})
    raw_ids, graph_ids = await read()
    assert stored.memory.id not in raw_ids
    assert promoted.promoted_id not in graph_ids
