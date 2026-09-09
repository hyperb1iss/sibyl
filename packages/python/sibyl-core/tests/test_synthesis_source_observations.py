"""Stored synthesis must retire with the exact raw or graph evidence it read."""

from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.context import (
    ContextFacet,
    ContextIntent,
    ContextItem,
    ContextPack,
    ContextSection,
)
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.synthesis import (
    SynthesisOutline,
    SynthesisOutlineSection,
    SynthesisOutputType,
    SynthesisRequest,
    SynthesisRun,
    SynthesisRunStatus,
    SynthesisSourcePack,
    SynthesisSourceReference,
    SynthesisVerification,
    SynthesisVerificationStatus,
)
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory
from sibyl_core.services.synthesis import (
    draft_synthesis_artifact,
    materialize_synthesis_section_packs,
    remember_synthesis_artifact,
)
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def materialized_synthesis(
    runtime, source_id, source_type, revision, content, *, accessible_projects=None, extra_items=()
):
    verification = SynthesisVerification(SynthesisVerificationStatus.PASS, 1, 0)
    request = SynthesisRequest(goal="Document the deployment rule")
    run = SynthesisRun(
        run_id="observed-synthesis",
        status=SynthesisRunStatus.VERIFIED,
        request=request,
        outline=SynthesisOutline(
            "Deployment rule",
            SynthesisOutputType.DOCUMENTATION,
            None,
            sections=[SynthesisOutlineSection("evidence", "Evidence", "deployment", "deployment")],
        ),
        source_packs=[
            SynthesisSourcePack(
                section_id="evidence",
                title="Evidence",
                query="deployment rule",
                source_ids=[source_id],
                sources=[
                    SynthesisSourceReference(
                        id=source_id,
                        type=source_type,
                        name="Deployment rule",
                        content_preview=content,
                        metadata={"revision": revision},
                    )
                ],
            )
        ],
        verification=verification,
    )
    item = ContextItem(
        id=f"raw_memory:{source_id}" if source_type == "raw_memory" else source_id,
        type=source_type,
        name="Deployment rule",
        content=content,
        score=1,
        facet=ContextFacet.DECISIONS,
        reason="source evidence",
        source_revision=revision,
        metadata={"source_id": source_id, "memory_scope": "private", "principal_id": "user_a"},
    )
    context = ContextPack(
        goal="deployment",
        intent=ContextIntent.RESEARCH,
        query="deployment",
        domain=None,
        project=None,
        sections=[ContextSection(ContextFacet.DECISIONS, "Evidence", [item, *extra_items])],
        total_items=1,
    )
    return await materialize_synthesis_section_packs(
        run,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        allowed_memory_scope_keys=None,
        accessible_projects=accessible_projects,
        context_fn=AsyncMock(return_value=context),
        source_loader=load_authorized_source_snapshot,
    )


async def remember_observed_synthesis(runtime, source_id, source_type, revision, content):
    run = await materialized_synthesis(runtime, source_id, source_type, revision, content)
    artifact = draft_synthesis_artifact(run)
    return await remember_synthesis_artifact(
        artifact,
        run,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )


async def recalled_ids(runtime):
    rows = await recall_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        query="deployment",
    )
    return {row.id for row in rows}


@pytest.fixture(autouse=True)
def disable_embeddings_and_bind_runtime(monkeypatch, runtime):
    monkeypatch.setattr(
        "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
        lambda: None,
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )


async def test_synthesis_retires_when_observed_raw_source_is_hidden(runtime, content_store):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="deployment-rule",
        raw_content="Deployment requires the blue approval.",
        embedding_provider=None,
    )
    artifact = await remember_observed_synthesis(
        runtime,
        source.id,
        "raw_memory",
        source.revision,
        source.raw_content,
    )
    assert artifact.remembered_memory_id in await recalled_ids(runtime)
    await apply_memory_correction(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id=source.id,
        action="hide",
    )
    assert source.id not in await recalled_ids(runtime)
    assert artifact.remembered_memory_id not in await recalled_ids(runtime)


async def test_synthesis_does_not_revive_after_graph_source_delete_recreate(runtime, content_store):
    source = Entity(
        id="deployment-rule",
        entity_type=EntityType.DECISION,
        name="Deployment rule",
        description="Deployment requires the blue approval.",
        metadata={"memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(source)
    observed = await runtime.entity_manager.get(source.id)
    artifact = await remember_observed_synthesis(
        runtime,
        observed.id,
        "decision",
        observed.revision,
        observed.description,
    )
    assert artifact.remembered_memory_id in await recalled_ids(runtime)
    assert await runtime.entity_manager.delete(source.id)
    await runtime.entity_manager.create_direct(
        source.model_copy(
            update={
                "description": "Deployment requires the green approval.",
            }
        )
    )
    replacement = await runtime.entity_manager.get(source.id)
    assert replacement.description != observed.description
    assert artifact.remembered_memory_id not in await recalled_ids(runtime)


async def test_synthesis_rejects_source_changed_after_materialization(runtime, content_store):
    from sibyl_core.services.source_observations import SourceUnavailableError

    source = Entity(
        id="materialization-race",
        entity_type=EntityType.DECISION,
        name="Deployment rule",
        content="Blue approval",
        metadata={"memory_scope": "private", "principal_id": "user_a"},
    )
    await runtime.entity_manager.create_direct(source)
    current = await runtime.entity_manager.get(source.id)
    run = await materialized_synthesis(
        runtime, current.id, "decision", current.revision, "Stale search preview"
    )
    assert run.source_packs[0].sources[0].content_preview == "Blue approval"
    assert run.source_packs[0].sources[0].observation is not None
    await runtime.client.execute_query(
        "UPDATE entity SET content='Green approval' WHERE uuid=$uuid;", uuid=source.id
    )
    with pytest.raises(SourceUnavailableError):
        await remember_synthesis_artifact(
            draft_synthesis_artifact(run),
            run,
            organization_id=runtime.client.group_id,
            principal_id="user_a",
        )


async def test_synthesis_target_bookkeeping_preserves_association_but_recreate_retires_it(
    runtime, content_store
):
    from sibyl_core.services import content_client
    from sibyl_core.services.memory_derivations import load_raw_derivation

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="target-retirement-source",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    artifact = await remember_observed_synthesis(
        runtime, source.id, "raw_memory", source.revision, source.raw_content
    )
    target_id = artifact.remembered_memory_id
    association = await load_raw_derivation(runtime.client.group_id, target_id)
    assert association is not None and association["active"] is True
    assert target_id in await recalled_ids(runtime)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET retrieval_count += 1, revision += 1 WHERE uuid=$uuid;",
            uuid=target_id,
        )
        assert await load_raw_derivation(runtime.client.group_id, target_id) == association
        assert target_id in await recalled_ids(runtime)
        rows = await client.execute_query(
            "SELECT * FROM raw_captures WHERE uuid=$uuid;", uuid=target_id
        )
        saved = {key: value for key, value in rows[0].items() if key != "id"}
        with pytest.raises(Exception, match="reject"):
            await client.execute_query(
                "RETURN { DELETE raw_captures WHERE uuid=$uuid; THROW 'reject'; };",
                uuid=target_id,
            )
        assert await load_raw_derivation(runtime.client.group_id, target_id) == association
        assert target_id in await recalled_ids(runtime)
        await client.execute_query("DELETE raw_captures WHERE uuid=$uuid;", uuid=target_id)
        retired = await load_raw_derivation(runtime.client.group_id, target_id)
        assert retired is not None and retired["active"] is False
        await client.execute_query("CREATE raw_captures CONTENT $saved;", saved=saved)
    assert target_id not in await recalled_ids(runtime)
    assert await load_raw_derivation(runtime.client.group_id, target_id) == retired


async def test_private_synthesis_requires_current_source_project_membership(runtime, content_store):
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    source = Entity(
        id="project-deployment-rule",
        entity_type=EntityType.DECISION,
        name="Deployment rule",
        content="Deployment requires the project approval.",
        metadata={"memory_scope": "project", "scope_key": "project_a", "project_id": "project_a"},
    )
    await runtime.entity_manager.create_direct(source)
    current = await runtime.entity_manager.get(source.id)
    run = await materialized_synthesis(
        runtime,
        source.id,
        "decision",
        current.revision,
        current.content,
        accessible_projects={"project_a"},
    )
    artifact = await remember_synthesis_artifact(
        draft_synthesis_artifact(run),
        run,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )

    async def recall(authority):
        return {
            row.id
            for row in await recall_raw_memory(
                organization_id=runtime.client.group_id,
                principal_id="user_a",
                query="deployment",
                source_authority=authority,
            )
        }

    granted = SourceReadAuthority("user_a", projects=frozenset({"project_a"}))
    assert artifact.remembered_memory_id in await recall(granted)
    assert artifact.remembered_memory_id not in await recall(SourceReadAuthority("user_a"))
    restricted = SourceReadAuthority(
        "user_a", projects=frozenset({"project_a"}), scope_keys=frozenset({"private"})
    )
    assert artifact.remembered_memory_id not in await recall(restricted)
    with pytest.raises(ValueError, match="principal"):
        await recall(SourceReadAuthority("user_b", projects=frozenset({"project_a"})))


async def test_materialization_keeps_raw_and_graph_with_same_source_alias(runtime, content_store):
    from sibyl_core.memory_pipeline.observations import SourceKind

    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="shared-citation",
        raw_content="Raw deployment evidence.",
        embedding_provider=None,
    )
    await runtime.entity_manager.create_direct(
        Entity(
            id=raw.id,
            entity_type=EntityType.DECISION,
            name="Graph deployment evidence",
            content="Graph deployment evidence.",
            metadata={"memory_scope": "private", "principal_id": "user_a"},
        )
    )
    graph_item = ContextItem(
        id=raw.id,
        type="decision",
        name="Graph preview",
        content="Stale graph preview",
        score=1,
        facet=ContextFacet.DECISIONS,
        reason="evidence",
        metadata={"source_id": raw.id, "memory_scope": "private", "principal_id": "user_a"},
    )
    run = await materialized_synthesis(
        runtime,
        raw.id,
        "raw_memory",
        raw.revision,
        raw.raw_content,
        extra_items=[graph_item],
    )
    sources = run.source_packs[0].sources
    assert len(sources) == 2
    assert {source.id for source in sources} == {f"raw_capture:{raw.id}", f"graph_entity:{raw.id}"}
    assert {source.observation.source.kind for source in sources} == {
        SourceKind.RAW_CAPTURE,
        SourceKind.GRAPH_ENTITY,
    }
    assert {source.content_preview for source in sources} == {
        "Raw deployment evidence.",
        "Graph deployment evidence.",
    }


async def test_materialization_rejects_stale_ancestor_of_derived_raw(runtime, content_store):
    from sibyl_core.services.surreal_content import get_raw_memory

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="ancestral-deployment-rule",
        raw_content="Deployment requires blue approval.",
        embedding_provider=None,
    )
    artifact = await remember_observed_synthesis(
        runtime,
        source.id,
        "raw_memory",
        source.revision,
        source.raw_content,
    )
    derived = await get_raw_memory(
        memory_id=artifact.remembered_memory_id, organization_id=runtime.client.group_id
    )
    assert derived is not None
    await apply_memory_correction(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id=source.id,
        action="hide",
    )
    run = await materialized_synthesis(
        runtime,
        derived.id,
        "raw_memory",
        derived.revision,
        derived.raw_content,
    )
    assert not run.source_packs[0].sources
    assert run.source_packs[0].hidden_count == 1
