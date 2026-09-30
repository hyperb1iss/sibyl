"""Authorized reflection reuse shares pure work without retaining source authority."""

from collections import Counter
from dataclasses import replace

import pytest

from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.content_raw_persistence import save_raw_memory
from sibyl_core.services.reflection_validation import prepare_stored_reflection
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.tasks import ordinary_projection as projection
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import (
    OriginalValidationEvidence,
    prepare_reflection_validation,
)
from tests.test_ordinary_cohort import content_store as content_store
from tests.test_ordinary_packet_correction import create_candidate
from tests.test_ordinary_projection_reuse import controller, projected, track_projections


@pytest.fixture
async def stored_reflection(content_store, monkeypatch):
    parent, resolver, _ = await create_candidate(content_store, monkeypatch, complete=True)
    return parent, resolver


async def test_authorized_reflection_reuses_one_projection_per_source_with_identical_bytes(
    stored_reflection, monkeypatch
):
    parent, resolver = stored_reflection
    calls = track_projections(monkeypatch)
    args = ("org", "owner", parent.memory.id, resolver)
    # Disabling lookup exercises the existing reconstruction path for comparison.
    with monkeypatch.context() as cold:
        cold.setattr(projection.ProjectionReuse, "_read", lambda self, key: None)
        expected = await prepare_stored_reflection(*args)
    source_ids = [source.id for source in parent.sources]
    assert Counter(calls) == Counter(dict.fromkeys(source_ids, 6))
    calls.clear()
    actual = await prepare_stored_reflection(*args)
    assert Counter(calls) == Counter(source_ids)
    assert actual.prepared.payload_json == expected.prepared.payload_json
    assert actual.prepared.prompt == expected.prepared.prompt
    assert actual.prepared.input_sha256 == expected.prepared.input_sha256
    assert actual.evidence == expected.evidence
    assert actual.source_bindings == expected.source_bindings
    assert actual.snapshot_sha256 == expected.snapshot_sha256
    assert actual.origin_dependencies == expected.origin_dependencies
    assert actual.publication_policy_sha256 == expected.publication_policy_sha256


async def test_independent_preparations_reparse_and_reauthorize(stored_reflection, monkeypatch):
    parent, resolver = stored_reflection
    calls = track_projections(monkeypatch)
    args = ("org", "owner", parent.memory.id, resolver)
    resolver.reset_mock()
    first = await prepare_stored_reflection(*args)
    first_authorizations = resolver.await_count
    assert first_authorizations > 0
    assert isinstance(first.evidence, projection.OrdinaryEvidenceProjection)
    expected_citations = dict(first.evidence.citations)
    first.evidence.citations.clear()
    second = await prepare_stored_reflection(*args)
    assert resolver.await_count > first_authorizations
    assert Counter(calls) == Counter(dict.fromkeys((source.id for source in parent.sources), 2))
    assert isinstance(second.evidence, projection.OrdinaryEvidenceProjection)
    assert second.evidence.citations == expected_citations
    assert second.prepared == first.prepared


@pytest.mark.parametrize("change", ["authorization", "deleted_source", "source_revision"])
async def test_prior_success_cannot_hide_changed_source_authority(
    stored_reflection, content_store, change
):
    parent, resolver = stored_reflection
    args = ("org", "owner", parent.memory.id, resolver)
    await prepare_stored_reflection(*args)
    source = parent.sources[0]
    if change == "authorization":
        resolver.return_value = None
    elif change == "deleted_source":
        await content_store.execute_query(
            "DELETE raw_captures WHERE organization_id=$org AND uuid=$id;", org="org", id=source.id
        )
    else:
        await save_raw_memory(
            replace(source, title="Changed source revision with unchanged bytes"),
            expected_revision=source.revision,
        )
    with pytest.raises(SourceUnavailableError):
        await prepare_stored_reflection(*args)


@pytest.mark.parametrize("damage", ["payload", "citations", "binding"])
def test_warm_critic_projection_still_rejects_forged_supplied_views(damage):
    sources = [controller("left"), controller("right")]
    reuse = projection.ProjectionReuse()
    valid = projected(sources, reuse)
    candidate = ReflectionCandidate(
        "pattern",
        "Retained report",
        "Preserve the reported goal.",
        "inference",
        0.5,
        raw_source_ids=[source.episode_id for source in sources],
    )
    evidence = [
        OriginalValidationEvidence(source.episode_id, source.artifact, "c" * 64, "reported")
        for source in sources
    ]
    changes = {
        "payload": {"payload_json": "{}"},
        "citations": {"citations": {}},
        "binding": {"binding_json": canonical({**valid.binding, "projection": {}})},
    }
    arguments = {
        "parent_operation_id": "a" * 64,
        "parent_candidate_sha256": "b" * 64,
        "evidence": evidence,
        "citations": valid.citations,
    }
    expected = prepare_reflection_validation(candidate, **arguments, projection=valid)
    with pytest.raises(ValueError, match="projection"):
        prepare_reflection_validation(
            candidate,
            **arguments,
            projection=replace(valid, **changes[damage]),
            projection_reuse=reuse,
        )
    actual = prepare_reflection_validation(
        candidate,
        **arguments,
        projection=valid,
        projection_reuse=reuse,
    )
    assert actual == expected
