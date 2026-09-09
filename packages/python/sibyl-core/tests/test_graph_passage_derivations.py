from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.projection.passages import project_entity_passages
from sibyl_core.services.eval_publication_guards import unavailable_publication_ids
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.tools.reflect import reflect_memory
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import materialized_synthesis
from tests.test_synthesis_source_observations import runtime as runtime


async def published_passages(runtime, content_store, monkeypatch):
    org = runtime.client.group_id
    resolver = AsyncMock(return_value=SourceReadAuthority("user_a"))
    monkeypatch.setattr(
        "sibyl_core.services.graph_derivations.get_source_authority_resolver", lambda: resolver
    )
    body = "\n\n".join(
        f"Rule {i}: Deployment requires blue approval. "
        + ("Preserve the deployment evidence. " * 30)
        for i in range(12)
    )
    source = Entity(
        id="projection-source",
        entity_type=EntityType.SESSION,
        name="Original source",
        content=body,
        metadata={"memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(source)

    class Extractor:
        async def extract(self, request):
            return [
                ReflectionCandidate(
                    kind="decision",
                    title="Derived long rule",
                    content=request.content,
                    reason="retain rule",
                    confidence=1.0,
                    tags=[],
                )
            ]

    pack = await reflect_memory(
        body,
        organization_id=org,
        principal_id="user_a",
        existing_source_id=source.id,
        persist=True,
        persist_source=False,
        extractor=Extractor(),
    )
    assert pack.persisted_count == 1
    target = await runtime.entity_manager.get(pack.candidates[0].persisted_id)
    projection = await project_entity_passages(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        source=target,
        group_id=org,
        generate_embeddings=False,
    )
    assert projection.passages > 1 and not projection.errors
    passage = await runtime.entity_manager.get(projection.created_passages[0].id)
    return source, target, projection, passage


async def test_projected_passage_retains_graph_ancestry(runtime, content_store, monkeypatch):
    org = runtime.client.group_id
    source, target, projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    assert not await unavailable_publication_ids(
        org, {target.id: target.metadata, passage.id: passage.metadata}
    )
    await runtime.entity_manager.update(
        source.id, {"content": "The approval requirement was revoked."}
    )
    denied = await unavailable_publication_ids(
        org, {target.id: target.metadata, passage.id: passage.metadata}
    )
    assert target.id in denied
    materialized = await materialized_synthesis(
        runtime, passage.id, "passage", passage.revision, passage.content
    )
    retained = sum(len(pack.sources) for pack in materialized.source_packs)
    print(
        "INDEPENDENT_PROJECTION",
        {
            "passages": projection.passages,
            "target_denied": target.id in denied,
            "passage_denied": passage.id in denied,
            "materialized_source_count": retained,
        },
    )
    assert passage.id in denied and retained == 0, (
        "projected passage bypassed protected graph ancestor revocation and materialization"
    )


@pytest.mark.parametrize("mutation", ["remove_markers", "recreate_parent"])
async def test_passage_protected_parent_survives_mutable_marker_removal_and_recreation(
    runtime, content_store, monkeypatch, mutation
):
    source, target, _projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    if mutation == "remove_markers":
        await runtime.client.execute_query(
            "UPDATE entity UNSET attributes.source_entity_id, attributes.parent_entity_id, attributes.projection_kind WHERE uuid=$id;",
            id=passage.id,
        )
        await runtime.entity_manager.update(source.id, {"content": "Revoked original evidence."})
    else:
        rows = await runtime.client.execute_query(
            "SELECT * FROM entity WHERE uuid=$id;", id=target.id
        )
        saved = {key: value for key, value in rows[0].items() if key != "id"}
        await runtime.client.execute_query(
            "DELETE entity WHERE uuid=$id; CREATE entity CONTENT $row;", id=target.id, row=saved
        )
    assert passage.id in await unavailable_publication_ids(
        runtime.client.group_id, {passage.id: passage.metadata}
    )
    materialized = await materialized_synthesis(
        runtime, passage.id, "passage", passage.revision, passage.content
    )
    assert not any(pack.sources for pack in materialized.source_packs)


async def test_projection_parent_race_rolls_back_children_and_associations(
    runtime, content_store, monkeypatch
):
    _source, target, _projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    original = runtime.entity_manager.create_direct_bulk

    async def race(entities, **kwargs):
        await runtime.entity_manager.update(
            target.id, {"content": "Changed after passage planning."}
        )
        return await original(entities, **kwargs)

    monkeypatch.setattr(runtime.entity_manager, "create_direct_bulk", race)
    before = await runtime.client.execute_query(
        "SELECT * FROM memory_derivations WHERE target_id=$id;", id=passage.id
    )
    result = await project_entity_passages(
        entity_manager=runtime.entity_manager,
        relationship_manager=runtime.relationship_manager,
        source=target,
        group_id=runtime.client.group_id,
        generate_embeddings=False,
    )
    assert result.errors and result.passages == 0
    after = await runtime.client.execute_query(
        "SELECT * FROM memory_derivations WHERE target_id=$id;", id=passage.id
    )
    assert after == before
    assert (await runtime.entity_manager.get(passage.id)).content == passage.content


async def test_passage_reprojection_advances_changed_parent_epoch_only(
    runtime, content_store, monkeypatch
):
    from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
    from sibyl_core.services.source_state_store import load_source_snapshot

    _source, target, _projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )

    async def observe():
        return await load_source_snapshot(
            SourceIdentity(runtime.client.group_id, SourceKind.GRAPH_ENTITY, passage.id),
            organization_id=runtime.client.group_id,
            execute_query=runtime.client.execute_query,
        )

    first = await observe()

    async def reproject():
        result = await project_entity_passages(
            entity_manager=runtime.entity_manager,
            relationship_manager=runtime.relationship_manager,
            source=target,
            group_id=runtime.client.group_id,
            generate_embeddings=False,
        )
        assert result.passages and not result.errors

    await reproject()
    replay = await observe()
    assert replay.observation.same_evidence(first.observation)
    await runtime.client.execute_query(
        "UPDATE entity SET attributes.excluded_from_recall=true WHERE uuid=$id; UPDATE entity SET attributes.excluded_from_recall=false WHERE uuid=$id;",
        id=target.id,
    )
    assert (await observe()).observation.same_evidence(first.observation)
    await reproject()
    assert (await observe()).observation.generation > first.observation.generation
