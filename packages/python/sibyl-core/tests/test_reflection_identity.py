"""Reflection evidence isolation against an actual isolated embedded database."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.auth.memory_policy import memory_metadata_read_allowed
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.memory_pipeline.lifecycle import graph_metadata_recallable
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_common import normalize_graph_records
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_reflection import (
    persist_reflection_candidate,
    promote_raw_memory,
    promote_reflection_candidate_review,
)
from sibyl_core.services.memory_sharing import share_memory
from sibyl_core.services.surreal_content import (
    get_raw_memory,
    remember_raw_memory,
    remember_reflection_candidate_review,
)
from sibyl_core.tools.reflect import reflect_memory
from sibyl_core.tools.search import graph_entity_to_search_result


@pytest.fixture
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GraphRuntime]:
    client = SurrealGraphClient(group_id=f"identity-{uuid4().hex}", url="memory://")
    try:
        await prepare_graph_schema(client)
        runtime = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=client.group_id),
            relationship_manager=RelationshipManager(client, group_id=client.group_id),
        )
        for project in ("project_a", "project_b"):
            await runtime.entity_manager.create_direct(
                Entity(id=project, entity_type=EntityType.PROJECT, name=project)
            )
        monkeypatch.setattr(
            "sibyl_core.services.memory_reflection.get_surreal_graph_runtime",
            AsyncMock(return_value=runtime),
        )
        monkeypatch.setattr(
            "sibyl_core.tools.reflect._load_reflection_decision_memories",
            AsyncMock(return_value=[]),
        )
        yield runtime
    finally:
        await client.close()


@pytest.fixture
async def content_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        yield
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("first_project", "second_project", "second_principal"),
    [
        (None, None, "user_a"),
        (None, None, "user_b"),
        (None, "project_b", "user_b"),
        ("project_a", "project_b", "user_b"),
    ],
)
async def test_sources_keep_original_content_scope_and_edges(
    runtime: GraphRuntime,
    first_project: str | None,
    second_project: str | None,
    second_principal: str,
) -> None:
    first_text = "We decided to preserve the original source evidence forever."
    second_text = "We decided to publish an unrelated purple terminal theme."
    first = await reflect_memory(
        first_text,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project=first_project,
        accessible_projects={first_project} if first_project else set(),
        persist=True,
    )
    source_before = await runtime.entity_manager.get(str(first.source_id))
    second = await reflect_memory(
        second_text,
        organization_id=runtime.client.group_id,
        principal_id=second_principal,
        project=second_project,
        accessible_projects={second_project} if second_project else set(),
        persist=True,
    )
    assert first.persisted_count == second.persisted_count == 1
    assert first.source_id != second.source_id
    assert await runtime.entity_manager.get(str(first.source_id)) == source_before
    assert (await runtime.entity_manager.get(str(second.source_id))).content == second_text
    edges = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE source_id = $source AND target_id = $target;",
        source=first.candidates[0].persisted_id,
        target=first.source_id,
    )
    assert len(edges) == 1


@pytest.mark.parametrize("project", [None, "project_b"])
async def test_candidate_identity_isolates_content_and_owner(
    runtime: GraphRuntime,
    project: str | None,
) -> None:
    candidate = ReflectionCandidate(
        kind="decision",
        title="Retention policy",
        content="Keep all records.",
        reason="test",
        confidence=1.0,
    )
    first = await persist_reflection_candidate(
        candidate=candidate,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    original = await runtime.entity_manager.get(str(first.response.id))
    second = await persist_reflection_candidate(
        candidate=replace(candidate, content="Delete old records."),
        organization_id=runtime.client.group_id,
        principal_id="user_b",
        project=project,
        accessible_projects={project} if project else set(),
    )
    assert first.response.success and second.response.success
    assert first.response.id != second.response.id
    assert await runtime.entity_manager.get(str(first.response.id)) == original


def candidate() -> ReflectionCandidate:
    return ReflectionCandidate(
        kind="decision",
        title="Retention policy",
        content="Keep all records.",
        reason="test",
        confidence=1.0,
    )


@pytest.mark.parametrize("changed", ["content", "principal", "project", "source", "domain"])
async def test_each_identity_boundary_is_independent(runtime: GraphRuntime, changed: str) -> None:
    original_candidate = replace(candidate(), content="a" * 150 + "first")
    args = dict(
        candidate=original_candidate, organization_id=runtime.client.group_id, principal_id="user_a"
    )
    first = await persist_reflection_candidate(**args)
    if changed == "content":
        args["candidate"] = replace(original_candidate, content="a" * 150 + "second")
    elif changed == "principal":
        args["principal_id"] = "user_b"
    elif changed == "project":
        args.update(project="project_b", accessible_projects={"project_b"})
    elif changed == "source":
        args["candidate"] = replace(original_candidate, raw_source_ids=["raw_source_b"])
    else:
        args["domain"] = "new-domain"
    second = await persist_reflection_candidate(**args)
    assert first.response.success and second.response.success
    assert first.response.id != second.response.id


async def test_exact_retry_replays_receipt_without_rewriting_evidence(
    runtime: GraphRuntime,
) -> None:
    args = dict(
        candidate=candidate(), organization_id=runtime.client.group_id, principal_id="user_a"
    )
    first = await persist_reflection_candidate(**args)
    before = await runtime.entity_manager.get(str(first.response.id))
    second = await persist_reflection_candidate(**args)
    assert second.response.success and second.response.id == first.response.id
    assert first.metadata["publication_outcome"] == "created"
    assert second.metadata["publication_outcome"] == "replayed"
    assert first.metadata["invalidation_details_available"] is True
    assert second.metadata["invalidation_details_available"] is False
    assert second.metadata["promotion_state"] == first.metadata["promotion_state"]
    assert "invalidated_source_ids" not in second.metadata
    assert await runtime.entity_manager.get(str(first.response.id)) == before


async def test_source_order_and_extraction_ids_do_not_change_identity(
    runtime: GraphRuntime,
) -> None:
    original = replace(candidate(), raw_source_ids=["source_b", "source_a"])
    args = dict(organization_id=runtime.client.group_id, principal_id="user_a")
    first = await persist_reflection_candidate(candidate=original, **args)
    second = await persist_reflection_candidate(
        candidate=replace(
            original,
            raw_source_ids=["source_a", "source_b"],
            metadata={"extraction_id": "unrelated-run"},
        ),
        **args,
    )
    assert first.response.id == second.response.id
    assert second.metadata["publication_outcome"] == "replayed"


@pytest.mark.parametrize("state", ["archived", "superseded", "deleted"])
async def test_retired_candidate_retry_preserves_lifecycle(
    runtime: GraphRuntime, state: str
) -> None:
    args = dict(
        candidate=candidate(), organization_id=runtime.client.group_id, principal_id="user_a"
    )
    first = await persist_reflection_candidate(**args)
    await runtime.entity_manager.update(
        str(first.response.id), {"metadata": {"lifecycle_state": state}}
    )
    before = await runtime.entity_manager.get(str(first.response.id))
    result = await persist_reflection_candidate(**args)
    assert not result.response.success
    assert result.response.id == first.response.id
    assert result.metadata["publication_outcome"] == "retired"
    assert await runtime.entity_manager.get(str(first.response.id)) == before


async def test_retired_source_prevents_new_candidate_publication(runtime: GraphRuntime) -> None:
    from sibyl_core.services.memory_reflection import persist_reflection_source

    content = "We decided to preserve evidence before publishing decisions."
    result = await persist_reflection_source(
        title="Session reflection",
        content=content,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    await runtime.entity_manager.update(
        str(result.response.id), {"metadata": {"lifecycle_state": "deleted"}}
    )
    before = await runtime.entity_manager.get(str(result.response.id))
    pack = await reflect_memory(
        content, organization_id=runtime.client.group_id, principal_id="user_a", persist=True
    )
    assert pack.persisted_count == 0
    assert pack.candidates[0].metadata["promotion_state"] == "denied"
    assert await runtime.entity_manager.get(str(result.response.id)) == before
    assert (
        await runtime.client.execute_query(
            "SELECT VALUE uuid FROM entity WHERE entity_type = 'decision';"
        )
        == []
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"content": "Corrupted evidence"},
        {"metadata": {"principal_id": "user_b"}},
        {
            "metadata": {
                "memory_scope": "project",
                "project_id": "project_b",
                "scope_key": "project_b",
            }
        },
        {"metadata": {"raw_source_ids": ["forged"]}},
    ],
)
async def test_conflict_checks_actual_row_fields(runtime: GraphRuntime, updates: dict) -> None:
    args = dict(
        candidate=candidate(), organization_id=runtime.client.group_id, principal_id="user_a"
    )
    first = await persist_reflection_candidate(**args)
    await runtime.entity_manager.update(str(first.response.id), updates)
    before = await runtime.entity_manager.get(str(first.response.id))
    with pytest.raises(ValueError, match="reflection identity conflict"):
        await persist_reflection_candidate(**args)
    assert await runtime.entity_manager.get(str(first.response.id)) == before


async def test_concurrent_same_identity_is_inserted_once(runtime: GraphRuntime) -> None:
    # Embedded connections serialize engine access. Concurrent callers still
    # exercise the insert-versus-existing and publication revision races.
    args = dict(
        candidate=candidate(), organization_id=runtime.client.group_id, principal_id="user_a"
    )
    results = await asyncio.gather(*(persist_reflection_candidate(**args) for _ in range(6)))
    assert all(result.response.success for result in results)
    assert len({result.response.id for result in results}) == 1
    rows = await runtime.client.execute_query(
        "SELECT * FROM entity WHERE entity_type = 'decision';"
    )
    assert len(rows) == 1
    assert rows[0]["attributes"]["reflection_publication"]["state"] == "complete"


async def test_interrupted_publication_resumes_missing_edges(
    runtime: GraphRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Interrupted(BaseException):
        pass

    async def interrupt(_relationships):
        raise Interrupted()

    args = dict(
        candidate=candidate(),
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project="project_a",
        accessible_projects={"project_a"},
    )
    real_create = runtime.relationship_manager.create_bulk
    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", interrupt)
    with pytest.raises(Interrupted):
        await persist_reflection_candidate(**args)
    rows = await runtime.client.execute_query(
        "SELECT * FROM entity WHERE entity_type = 'decision';"
    )
    assert len(rows) == 1
    assert rows[0]["attributes"]["reflection_publication"]["state"] == "pending"
    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", real_create)
    result = await persist_reflection_candidate(**args)
    assert result.response.success and result.response.id == rows[0]["uuid"]
    assert result.metadata["publication_outcome"] == "resumed"
    assert result.metadata["promotion_state"] == "complete"
    edges = await runtime.client.execute_query(
        "SELECT * FROM relates_to WHERE source_id = $source;", source=result.response.id
    )
    assert len(edges) == 1


@pytest.mark.parametrize("mode", ["review", "raw", "share"])
async def test_public_promotion_and_share_replay_preserve_raw_lifecycle(
    runtime: GraphRuntime,
    content_store: None,
    mode: str,
) -> None:
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="session-one",
        title="Retention policy",
        raw_content="Keep all records.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[raw.id],
        )

    async def publish(project="project_a"):
        common = dict(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            accessible_projects={project},
        )
        if mode == "share":
            result = await share_memory(
                source_ids=[raw.id], target_scope="project", target_scope_key=project, **common
            )
            assert len(result.promotions) == 1
            return result.promotions[0]
        common.update(promote_to_scope="project", promote_to_scope_key=project)
        if mode == "review":
            return await promote_reflection_candidate_review(candidate_id=raw.id, **common)
        return await promote_raw_memory(raw_memory_id=raw.id, **common)

    first = await publish()
    assert first.success
    original = await runtime.entity_manager.get(str(first.promoted_id))
    raw_before = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
    second = await publish()
    assert second.success and second.promoted_id == first.promoted_id
    assert second.metadata["publication_outcome"] == "replayed"
    assert await runtime.entity_manager.get(str(first.promoted_id)) == original
    assert (
        await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
        == raw_before
    )
    another_scope = await publish("project_b")
    if mode == "share":
        assert another_scope.success and another_scope.promoted_id != first.promoted_id
    else:
        assert not another_scope.success
        assert another_scope.reason == "candidate_already_promoted"
    assert await runtime.entity_manager.get(str(first.promoted_id)) == original


@pytest.mark.parametrize("mode", ["review", "raw", "share"])
async def test_partial_promotion_is_retryable_before_raw_review_transition(
    runtime: GraphRuntime,
    content_store: None,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="session-one",
        title="Retention policy",
        raw_content="Keep all records.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[raw.id],
        )

    async def publish():
        common = dict(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            accessible_projects={"project_a"},
        )
        if mode == "share":
            result = await share_memory(
                source_ids=[raw.id], target_scope="project", target_scope_key="project_a", **common
            )
            return result.promotions[0]
        common.update(promote_to_scope="project", promote_to_scope_key="project_a")
        if mode == "review":
            return await promote_reflection_candidate_review(candidate_id=raw.id, **common)
        return await promote_raw_memory(raw_memory_id=raw.id, **common)

    raw_before = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
    real_create = runtime.relationship_manager.create_bulk
    monkeypatch.setattr(
        runtime.relationship_manager,
        "create_bulk",
        AsyncMock(side_effect=lambda items: (0, len(items))),
    )
    first = await publish()
    assert not first.success and first.reason == "promotion_incomplete"
    assert first.promoted_id
    pending = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
    if mode == "share":
        assert pending == raw_before
    else:
        assert pending.review_state == raw_before.review_state
        assert pending.raw_content == raw_before.raw_content
        assert pending.principal_id == raw_before.principal_id
        assert pending.metadata["promoted_entity_id"] == first.promoted_id
        assert pending.metadata["promotion_state"] == "pending"
    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", real_create)
    second = await publish()
    assert second.success and second.promoted_id == first.promoted_id
    assert second.metadata["publication_outcome"] == "resumed"


async def test_legacy_evidence_is_not_remapped(runtime: GraphRuntime) -> None:
    legacy = Entity(
        id="session_bb302e9ccf16",
        entity_type=EntityType.SESSION,
        name="Session reflection",
        content="Legacy archive source",
        organization_id=runtime.client.group_id,
    )
    await runtime.entity_manager.create_direct(legacy)
    before = await runtime.entity_manager.get(legacy.id)
    result = await reflect_memory(
        "We decided to keep a new source separate from historical evidence.",
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        persist=True,
    )
    assert result.source_id.startswith("session_v2_")
    assert await runtime.entity_manager.get(legacy.id) == before


@pytest.mark.parametrize("declared", ["one_missing", "all_missing", "foreign_organization"])
async def test_review_promotion_requires_every_declared_source_before_reservation(
    runtime: GraphRuntime,
    content_store: None,
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
) -> None:
    from sibyl_core.services import memory_reflection

    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="complete-source",
        title="Present evidence",
        raw_content="The first source exists and is recallable.",
        embedding_provider=None,
    )
    missing_id = "missing-capture"
    if declared == "foreign_organization":
        foreign = await remember_raw_memory(
            organization_id="other-organization",
            principal_id="user_a",
            source_id="foreign-source",
            title="Foreign evidence",
            raw_content="This source exists only in another organization.",
            embedding_provider=None,
        )
        missing_id = foreign.id
    source_ids = [missing_id] if declared == "all_missing" else [source.id, missing_id]
    review = await remember_reflection_candidate_review(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        candidate=candidate(),
        raw_source_ids=source_ids,
    )
    reserve = AsyncMock(wraps=memory_reflection._reserve_promotion)
    persist = AsyncMock(wraps=memory_reflection.persist_reflection_candidate)
    links = AsyncMock(wraps=runtime.relationship_manager.create_bulk)
    monkeypatch.setattr(memory_reflection, "_reserve_promotion", reserve)
    monkeypatch.setattr(memory_reflection, "persist_reflection_candidate", persist)
    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", links)

    result = await promote_reflection_candidate_review(
        candidate_id=review.id,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        promote_to_scope="project",
        promote_to_scope_key="project_a",
        accessible_projects={"project_a"},
    )

    assert not result.success
    assert result.reason == "source_not_recallable"
    assert result.promoted_id is None
    assert result.raw_source_ids == source_ids
    reserve.assert_not_awaited()
    persist.assert_not_awaited()
    links.assert_not_awaited()
    assert (
        await get_raw_memory(organization_id=runtime.client.group_id, memory_id=review.id) == review
    )
    assert (
        await runtime.client.execute_query(
            "SELECT VALUE uuid FROM entity WHERE entity_type != 'project';"
        )
        == []
    )


@pytest.mark.parametrize("declared", ["empty", "duplicate"])
async def test_review_source_completeness_preserves_self_fallback_and_duplicates(
    runtime: GraphRuntime, content_store: None, declared: str
) -> None:
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="complete-source",
        title="Present evidence",
        raw_content="The declared evidence is complete.",
        embedding_provider=None,
    )
    review = await remember_reflection_candidate_review(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        candidate=candidate(),
        raw_source_ids=[] if declared == "empty" else [source.id, source.id],
    )
    result = await promote_reflection_candidate_review(
        candidate_id=review.id,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        promote_to_scope="project",
        promote_to_scope_key="project_a",
        accessible_projects={"project_a"},
    )
    assert result.success
    assert result.raw_source_ids == [review.id if declared == "empty" else source.id]


@pytest.mark.parametrize("scope", ["private", "project"])
async def test_unretained_reflection_input_can_be_reviewed_promoted_and_replayed(
    runtime: GraphRuntime, content_store: None, scope: str
) -> None:
    from sibyl_core.services.reflection import ephemeral_reflection_source_id

    content = "We decided to preserve explicit input provenance without retaining the source."
    project = "project_a" if scope == "project" else None
    pack = await reflect_memory(
        content,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project=project,
        accessible_projects={"project_a"},
        memory_scope=scope,
        scope_key=project,
        persist=True,
        persist_source=False,
        persist_review=True,
    )
    assert pack.source_id is None
    assert pack.persisted_count == 1
    anchor = ephemeral_reflection_source_id(content)
    review_id = pack.candidates[0].persisted_id
    review = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=review_id)
    assert review.source_id == anchor
    assert review.metadata["raw_source_ids"] == [anchor]
    assert await get_raw_memory(organization_id=runtime.client.group_id, memory_id=anchor) is None
    kwargs = {
        "candidate_id": review_id,
        "organization_id": runtime.client.group_id,
        "principal_id": "user_a",
        "promote_to_scope": scope,
        "promote_to_scope_key": project,
        "accessible_projects": {"project_a"},
    }
    result = await promote_reflection_candidate_review(**kwargs)
    assert result.success
    assert result.raw_source_ids == [anchor]
    entity = await runtime.entity_manager.get(str(result.promoted_id))
    assert entity.metadata["raw_source_ids"] == [anchor]
    assert entity.source_file == anchor
    assert entity.metadata["reflection_identity"]["source_ids"] == [anchor]
    assert entity.metadata["reflection_identity"]["primary_source_id"] == anchor
    replay = await promote_reflection_candidate_review(**kwargs)
    assert replay.success
    assert replay.promoted_id == result.promoted_id
    assert replay.raw_source_ids == [anchor]
    assert await runtime.entity_manager.get(str(result.promoted_id)) == entity


async def test_source_conflict_cannot_publish_orphan_candidates(runtime: GraphRuntime) -> None:
    from sibyl_core.services.memory_reflection import persist_reflection_source

    content = "We decided to preserve evidence before publishing decisions."
    source = await persist_reflection_source(
        title="Session reflection",
        content=content,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
    )
    await runtime.entity_manager.update(str(source.response.id), {"content": "Changed externally"})
    with pytest.raises(ValueError, match="reflection identity conflict"):
        await reflect_memory(
            content, organization_id=runtime.client.group_id, principal_id="user_a", persist=True
        )
    assert (
        await runtime.client.execute_query(
            "SELECT VALUE uuid FROM entity WHERE entity_type = 'decision';"
        )
        == []
    )


async def test_revocation_during_publication_is_not_reactivated(
    runtime: GraphRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_create = runtime.relationship_manager.create_bulk

    async def revoke(relationships):
        entity_id = relationships[0].source_id
        await runtime.entity_manager.update(entity_id, {"metadata": {"lifecycle_state": "deleted"}})
        return await real_create(relationships)

    monkeypatch.setattr(runtime.relationship_manager, "create_bulk", revoke)
    result = await persist_reflection_candidate(
        candidate=candidate(),
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project="project_a",
        accessible_projects={"project_a"},
    )
    assert not result.response.success
    assert result.metadata["publication_outcome"] == "retired"
    stored = await runtime.entity_manager.get(str(result.response.id))
    assert stored.metadata["lifecycle_state"] == "deleted"
    assert stored.metadata["reflection_publication"]["state"] == "pending"


@pytest.mark.parametrize("mode", ["raw", "review"])
async def test_second_promotion_does_not_strand_first_copy(
    runtime, content_store, monkeypatch, mode
):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="session-one",
        title="Retention policy",
        raw_content="Keep all records.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[raw.id],
        )
    common = dict(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        accessible_projects={"project_a", "project_b"},
        promote_to_scope="project",
    )

    async def publish(project):
        if mode == "review":
            return await promote_reflection_candidate_review(
                candidate_id=raw.id, promote_to_scope_key=project, **common
            )
        return await promote_raw_memory(
            raw_memory_id=raw.id, promote_to_scope_key=project, **common
        )

    first = await publish("project_a")
    second = await publish("project_b")
    assert first.success and not second.success
    assert second.reason == "candidate_already_promoted"
    correction = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=raw.id,
        principal_id="user_a",
        action="delete",
        accessible_projects={"project_a", "project_b"},
    )
    assert correction.applied
    first_row = await runtime.entity_manager.get(first.promoted_id)
    assert not graph_metadata_recallable(first_row.metadata), {
        "mode": mode,
        "first_copy_state": first_row.metadata.get("lifecycle_state"),
        "source_promoted_id": (
            await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
        ).metadata.get("promoted_entity_id"),
    }


@pytest.mark.parametrize("mode", ["raw", "review"])
async def test_partial_promotion_source_deletion_retires_inserted_row(
    runtime, content_store, monkeypatch, mode
):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="session-partial",
        title="Partial publication evidence",
        raw_content="Keep the underlying evidence authoritative.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[raw.id],
        )
    monkeypatch.setattr(
        runtime.relationship_manager,
        "create_bulk",
        AsyncMock(side_effect=lambda items: (0, len(items))),
    )
    common = dict(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        accessible_projects={"project_a"},
        promote_to_scope="project",
        promote_to_scope_key="project_a",
    )
    if mode == "review":
        result = await promote_reflection_candidate_review(candidate_id=raw.id, **common)
    else:
        result = await promote_raw_memory(raw_memory_id=raw.id, **common)
    assert not result.success and result.reason == "promotion_incomplete"
    before = await runtime.entity_manager.get(result.promoted_id)
    assert before.metadata["reflection_publication"]["state"] == "partial"
    correction = await apply_memory_correction(
        organization_id=runtime.client.group_id,
        source_id=raw.id,
        principal_id="user_a",
        action="delete",
        accessible_projects={"project_a"},
    )
    assert correction.applied
    row = await runtime.entity_manager.get(result.promoted_id)
    source = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
    assert not graph_metadata_recallable(row.metadata), {
        "mode": mode,
        "graph_state": row.metadata.get("lifecycle_state"),
        "publication_state": row.metadata["reflection_publication"]["state"],
        "source_promoted_id": source.metadata.get("promoted_entity_id"),
        "source_state": source.metadata.get("lifecycle_state"),
    }


async def test_restored_v2_uuid_at_random_record_id_replays(runtime):
    args = dict(
        candidate=candidate(), organization_id=runtime.client.group_id, principal_id="user_a"
    )
    first = await persist_reflection_candidate(**args)
    saved = await runtime.entity_manager.get(first.response.id)
    await runtime.entity_manager.delete(first.response.id)
    assert await runtime.entity_manager.create_direct_bulk([saved]) == [saved.id]
    rows = normalize_graph_records(
        await runtime.client.execute_query(
            "SELECT * FROM entity WHERE uuid = $uuid;", uuid=saved.id
        )
    )
    assert len(rows) == 1
    restored_record_id = str(rows[0]["record_id"])
    assert restored_record_id != f"entity:{saved.id}"
    restored = await runtime.entity_manager.get(saved.id)
    replay = await persist_reflection_candidate(**args)
    assert replay.response.success and replay.response.id == saved.id
    assert replay.metadata["publication_outcome"] == "replayed"
    assert await runtime.entity_manager.get(saved.id) == restored
    rows_after = normalize_graph_records(
        await runtime.client.execute_query(
            "SELECT * FROM entity WHERE uuid = $uuid;", uuid=saved.id
        )
    )
    assert len(rows_after) == 1
    assert str(rows_after[0]["record_id"]) == restored_record_id


async def test_project_receipt_does_not_reveal_private_invalidation_uuid(runtime, content_store):
    private_source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="private-session-alias",
        title="Private source",
        raw_content="Private evidence invisible to another project member.",
        embedding_provider=None,
    )
    proposal = replace(
        candidate(),
        metadata={"contradiction_source_ids": ["private-session-alias"]},
    )
    result = await persist_reflection_candidate(
        candidate=proposal,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project="project_a",
        accessible_projects={"project_a"},
    )
    assert result.response.success
    row = await runtime.entity_manager.get(result.response.id)
    reader_args = dict(
        principal_id="user_b", private_scope_granted=True, accessible_projects={"project_a"}
    )
    assert memory_metadata_read_allowed(row.metadata, **reader_args)
    assert not memory_metadata_read_allowed(
        {"memory_scope": "private", "principal_id": "user_a", "scope_key": "user_a"},
        **reader_args,
    )
    visible_result = graph_entity_to_search_result(
        row,
        organization_id=runtime.client.group_id,
        principal_id="user_b",
        score=1.0,
        policy_reason="project_scope_read_allowed",
    )
    assert result.metadata["invalidated_source_ids"] == [private_source.id]
    assert result.metadata["invalidation_details_available"] is True
    receipt = row.metadata["reflection_publication"]["receipt"]
    assert receipt["invalidation_details_available"] is False
    assert "invalidated_source_ids" not in receipt
    assert "invalidation_errors" not in receipt
    assert private_source.id not in json.dumps(visible_result.metadata, default=str)
    assert private_source.id not in json.dumps(row.model_dump(), default=str)
    replay = await persist_reflection_candidate(
        candidate=proposal,
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        project="project_a",
        accessible_projects={"project_a"},
    )
    assert replay.response.success
    assert replay.metadata["publication_outcome"] == "replayed"
    assert replay.metadata["invalidation_details_available"] is False
    assert private_source.id not in json.dumps(replay.metadata, default=str)


@pytest.mark.parametrize("mode", ["raw", "review"])
@pytest.mark.parametrize("boundary", ["before_insert", "final_save", "input_before_insert"])
async def test_source_correction_during_promotion_cannot_publish_live_evidence(
    runtime,
    content_store,
    monkeypatch,
    mode,
    boundary,
):
    from sibyl_core.services import memory_reflection as reflection

    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="corrected-during-publication",
        title="Original evidence",
        raw_content="The source remains authoritative.",
        embedding_provider=None,
    )
    raw = source
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[source.id],
        )
    correction_id = source.id if boundary == "input_before_insert" else raw.id

    async def correct():
        result = await apply_memory_correction(
            organization_id=runtime.client.group_id,
            source_id=correction_id,
            principal_id="user_a",
            action="delete",
            accessible_projects={"project_a"},
        )
        assert result.applied

    if boundary == "final_save":
        save = reflection.save_raw_memory

        async def correcting_save(memory, **kwargs):
            if memory.review_state == "promoted":
                await correct()
            return await save(memory, **kwargs)

        monkeypatch.setattr(reflection, "save_raw_memory", correcting_save)
    else:
        insert = runtime.entity_manager.create_direct_if_absent

        async def correcting_insert(entity):
            await correct()
            return await insert(entity)

        monkeypatch.setattr(runtime.entity_manager, "create_direct_if_absent", correcting_insert)
    common = dict(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        accessible_projects={"project_a"},
        promote_to_scope="project",
        promote_to_scope_key="project_a",
    )
    if mode == "review":
        result = await promote_reflection_candidate_review(candidate_id=raw.id, **common)
    else:
        result = await promote_raw_memory(raw_memory_id=raw.id, **common)
    assert not result.success and result.reason == "retired"
    row = await runtime.entity_manager.get(result.promoted_id)
    assert not graph_metadata_recallable(row.metadata)
    corrected = await get_raw_memory(
        organization_id=runtime.client.group_id, memory_id=correction_id
    )
    assert corrected.metadata["lifecycle_state"] == "deleted"
    retry = await (
        promote_reflection_candidate_review(candidate_id=raw.id, **common)
        if mode == "review"
        else promote_raw_memory(raw_memory_id=raw.id, **common)
    )
    assert not retry.success
    assert not graph_metadata_recallable((await runtime.entity_manager.get(row.id)).metadata)


@pytest.mark.parametrize("mode", ["raw", "review"])
@pytest.mark.parametrize("different_targets", [False, True])
async def test_concurrent_source_promotion_reserves_one_identity(
    runtime,
    content_store,
    mode,
    different_targets,
):
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="concurrent-promotion",
        title="Stable source",
        raw_content="One source has one authoritative promotion target.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[raw.id],
        )

    async def publish(project):
        common = dict(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            accessible_projects={"project_a", "project_b"},
            promote_to_scope="project",
            promote_to_scope_key=project,
        )
        if mode == "review":
            return await promote_reflection_candidate_review(candidate_id=raw.id, **common)
        return await promote_raw_memory(raw_memory_id=raw.id, **common)

    results = await asyncio.gather(
        publish("project_a"), publish("project_b" if different_targets else "project_a")
    )
    successful = [result for result in results if result.success]
    assert len(successful) == (1 if different_targets else 2)
    if different_targets:
        denied = next(result for result in results if not result.success)
        assert denied.reason == "candidate_already_promoted"
    assert len({result.promoted_id for result in successful}) == 1
    saved = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=raw.id)
    assert saved.review_state == "promoted"
    assert saved.metadata["promoted_entity_id"] == successful[0].promoted_id
    rows = await runtime.client.execute_query(
        "SELECT VALUE uuid FROM entity WHERE entity_type != 'project';"
    )
    assert rows == [successful[0].promoted_id]


@pytest.mark.parametrize("mode", ["raw", "review"])
async def test_same_scope_changed_domain_cannot_retarget_promotion(runtime, content_store, mode):
    raw = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="domain-sensitive",
        title="Stable source",
        raw_content="Preserve target identity.",
        embedding_provider=None,
    )
    if mode == "review":
        raw = await remember_reflection_candidate_review(
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            candidate=candidate(),
            raw_source_ids=[raw.id],
        )
    common = dict(
        organization_id=runtime.client.group_id, principal_id="user_a", promote_to_scope="private"
    )
    publish = promote_reflection_candidate_review if mode == "review" else promote_raw_memory
    common["candidate_id" if mode == "review" else "raw_memory_id"] = raw.id
    first = await publish(domain="original", **common)
    second = await publish(domain="changed", **common)
    assert first.success
    assert not second.success and second.reason == "candidate_already_promoted"


async def test_publication_request_replaces_removed_nested_keys(runtime):
    original = replace(candidate(), metadata={"valid_at": "2026-01-01T00:00:00+00:00"})
    args = dict(organization_id=runtime.client.group_id, principal_id="user_a")
    first = await persist_reflection_candidate(candidate=original, **args)
    second = await persist_reflection_candidate(candidate=candidate(), **args)
    assert second.response.success and second.response.id == first.response.id
    row = await runtime.entity_manager.get(first.response.id)
    assert row.metadata["reflection_publication"]["request"]["invalidation_cutoff"] == {}
    replay = await persist_reflection_candidate(candidate=candidate(), **args)
    assert replay.metadata["publication_outcome"] == "replayed"


async def test_disappearance_after_publication_revision_conflict_is_controlled(
    runtime, monkeypatch
):
    from sibyl_core.errors import RevisionConflictError

    async def disappear(entity_id, updates, **kwargs):
        await runtime.entity_manager.delete(entity_id)
        raise RevisionConflictError(entity_id, expected_revision=1, actual_revision=2)

    monkeypatch.setattr(runtime.entity_manager, "update", disappear)
    with pytest.raises(RuntimeError, match="reflection evidence disappeared during publication"):
        await persist_reflection_candidate(
            candidate=candidate(),
            organization_id=runtime.client.group_id,
            principal_id="user_a",
        )
