"""Typed observations preserve evidence identity and current source authority."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.source_observations import (
    GraphSourceSnapshot,
    SourceUnavailableError,
    graph_evidence,
    load_source_observation,
    observe_graph_snapshot,
    observe_raw_capture,
)
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_reflection_identity import content_store as content_store

OWNER = SourceReadAuthority(principal_id="owner")


def raw():
    return RawMemory(
        id="same-id",
        organization_id="org",
        principal_id="owner",
        source_id="source",
        raw_content="Deployment requires blue approval.",
        revision=3,
        observed_revision=3,
    )


def graph_snapshot():
    entity = Entity(
        id="same-id",
        organization_id="org",
        name="Approval",
        entity_type=EntityType.DECISION,
        content="Deployment requires blue approval.",
        revision=3,
        observed_revision=3,
        metadata={"memory_scope": "private", "principal_id": "owner"},
    )
    observation = SourceObservation(
        SourceIdentity("org", SourceKind.GRAPH_ENTITY, entity.id),
        2,
        graph_evidence(entity),
        3,
        True,
    )
    return GraphSourceSnapshot(entity, observation)


def test_equal_string_ids_do_not_conflate_raw_and_graph_sources():
    raw_observation = observe_raw_capture(raw(), OWNER)
    graph = graph_snapshot()
    assert raw_observation.source.key != graph.observation.source.key
    assert not raw_observation.same_evidence(graph.observation)
    assert not raw_observation.durable


def test_source_keys_do_not_alias_separator_containing_components():
    first = SourceIdentity("org:a", SourceKind.RAW_CAPTURE, "b")
    second = SourceIdentity("org", SourceKind.RAW_CAPTURE, "a:b")
    assert first.key != second.key


@pytest.mark.parametrize("generation", [True, -1, "2", None])
def test_invalid_generation_cannot_be_observed(generation):
    with pytest.raises(ValueError):
        replace(graph_snapshot().observation, generation=generation)


def test_bookkeeping_revision_is_not_a_new_raw_evidence_generation():
    source = raw()
    before = observe_raw_capture(source, OWNER)
    after = observe_raw_capture(replace(source, revision=4, observed_revision=4), OWNER)
    assert before.same_evidence(after)
    changed = observe_raw_capture(replace(source, raw_content="Use green approval."), OWNER)
    assert not before.same_evidence(changed)


def test_raw_compatibility_uses_existing_correction_generation():
    source = replace(
        raw(), metadata={"correction_history": [{"action": "revise", "prior_revision": 1}]}
    )
    assert observe_raw_capture(source, OWNER).generation == 2


@pytest.mark.parametrize(
    "authority",
    [SourceReadAuthority("other"), SourceReadAuthority("owner", scope_keys=frozenset())],
)
def test_raw_and_graph_authority_have_identical_denial(authority):
    snapshot = graph_snapshot()
    for call in (
        lambda: observe_raw_capture(raw(), authority),
        lambda: observe_graph_snapshot(snapshot, snapshot.observation.source, authority),
    ):
        with pytest.raises(SourceUnavailableError, match=r"^Source observation is unavailable\.$"):
            call()


@pytest.mark.parametrize(
    "damage", ["hash", "revision", "organization", "deleted", "nondurable", "content"]
)
def test_graph_state_must_bind_the_loaded_entity(damage):
    snapshot = graph_snapshot()
    source = snapshot.observation.source
    if damage == "hash":
        snapshot = replace(
            snapshot, observation=replace(snapshot.observation, content_sha256="0" * 64)
        )
    elif damage == "revision":
        snapshot = replace(snapshot, observation=replace(snapshot.observation, revision=2))
    elif damage == "organization":
        snapshot = replace(
            snapshot, entity=snapshot.entity.model_copy(update={"organization_id": "foreign"})
        )
    elif damage == "deleted":
        snapshot = replace(snapshot, deleted=True)
    elif damage == "nondurable":
        snapshot = replace(snapshot, observation=replace(snapshot.observation, durable=False))
    else:
        snapshot = replace(
            snapshot, entity=snapshot.entity.model_copy(update={"content": "Different evidence"})
        )
    with pytest.raises(SourceUnavailableError):
        observe_graph_snapshot(snapshot, source, OWNER)


async def test_graph_loader_requires_durable_store_snapshot():
    snapshot = graph_snapshot()
    with pytest.raises(SourceUnavailableError):
        await load_source_observation(snapshot.observation.source, OWNER, organization_id="org")
    loaded = await load_source_observation(
        snapshot.observation.source,
        OWNER,
        organization_id="org",
        graph_loader=AsyncMock(return_value=snapshot),
    )
    assert loaded == snapshot.observation


async def test_raw_loader_reads_actual_capture_and_denies_foreign_org(content_store):
    source = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="observed",
        raw_content="Blue approval",
        embedding_provider=None,
    )
    identity = SourceIdentity("org", SourceKind.RAW_CAPTURE, source.id)
    observation = await load_source_observation(identity, OWNER, organization_id="org")
    assert observation.source == identity
    assert observation.revision == source.revision
    with pytest.raises(SourceUnavailableError):
        await load_source_observation(
            replace(identity, organization_id="other"), OWNER, organization_id="org"
        )


@pytest.mark.parametrize("revision", [None, True, 0])
def test_unversioned_raw_snapshot_cannot_claim_observed_evidence(revision):
    with pytest.raises(SourceUnavailableError):
        observe_raw_capture(replace(raw(), observed_revision=revision), OWNER)


def test_raw_legacy_checkpoint_keeps_generation_across_bookkeeping():
    history = [{"action": "revise"}]
    source = replace(
        raw(),
        metadata={"correction_history": history},
        legacy_content_checkpoint={"entries": history, "observed_revision": 2},
    )
    observed = observe_raw_capture(source, OWNER)
    assert observed.generation == 2
    assert observed.same_evidence(
        observe_raw_capture(
            replace(source, revision=4, observed_revision=4),
            OWNER,
        )
    )


@pytest.mark.parametrize("member", [False, True])
def test_project_entity_requires_its_own_membership(member):
    entity = Entity(
        id="restricted-project",
        organization_id="org",
        entity_type=EntityType.PROJECT,
        name="Restricted project",
        content="Project evidence",
        metadata={},
        revision=2,
        observed_revision=2,
    )
    observation = SourceObservation(
        SourceIdentity("org", SourceKind.GRAPH_ENTITY, entity.id),
        1,
        graph_evidence(entity),
        2,
        True,
    )
    snapshot = GraphSourceSnapshot(entity, observation)
    authority = SourceReadAuthority(
        "reader",
        projects=frozenset({entity.id}) if member else frozenset(),
    )
    if member:
        assert observe_graph_snapshot(snapshot, observation.source, authority) == observation
    else:
        with pytest.raises(SourceUnavailableError):
            observe_graph_snapshot(snapshot, observation.source, authority)


@pytest.mark.parametrize("kind", list(SourceKind))
async def test_foreign_operation_org_is_denied_before_any_store_read(monkeypatch, kind):
    raw_loader = AsyncMock()
    graph_loader = AsyncMock()
    monkeypatch.setattr(
        "sibyl_core.services.content_raw_persistence.get_raw_memory",
        raw_loader,
    )
    with pytest.raises(SourceUnavailableError):
        await load_source_observation(
            SourceIdentity("foreign", kind, "same-id"),
            OWNER,
            organization_id="trusted-operation-org",
            graph_loader=graph_loader,
        )
    raw_loader.assert_not_awaited()
    graph_loader.assert_not_awaited()
