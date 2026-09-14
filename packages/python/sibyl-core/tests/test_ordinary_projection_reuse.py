"""Projection reuse preserves checks while sharing immutable preparation work."""

from dataclasses import replace

import pytest

from sibyl_core.services import ordinary_cohort as service
from sibyl_core.tasks import ordinary_projection as projection
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.ordinary_proposals import prepare_partial_proposal
from tests.test_episode_evidence import _episode
from tests.test_ordinary_proposals import cohort, episode


def controller(name):
    source = episode(name, text=canonical({**_episode(), "goal": name}))
    return source.model_copy(update={"episode_id": source.source.source_id})


def projected(sources, reuse=None):
    return projection.prepare_ordinary_projection(
        [(source.episode_id, source.artifact) for source in sources],
        [source.source for source in sources],
        reuse=reuse,
    )


def track_projections(monkeypatch):
    calls = []
    original = projection.project_episode

    def traced(identifier, artifact, *, prefix):
        calls.append(identifier)
        return original(identifier, artifact, prefix=prefix)

    monkeypatch.setattr(projection, "project_episode", traced)
    return calls


def test_one_projection_serves_preparation_and_both_budgets(monkeypatch):
    group = cohort(controller("a"), controller("b"))
    expected = service._prepare_cohort_input(group)
    expected_chars = service._cohort_input_chars(expected, 100, 200)
    calls = track_projections(monkeypatch)
    reuse = projection.ProjectionReuse()

    actual = service._prepare_cohort_input(group, projection_reuse=reuse)
    actual_chars = service._cohort_input_chars(actual, 100, 200, projection_reuse=reuse)

    assert actual == expected
    assert actual_chars == expected_chars
    assert calls == [source.episode_id for source in group.episodes]


def test_reuse_does_not_expose_mutable_citation_storage():
    sources = [controller("a"), controller("b")]
    reuse = projection.ProjectionReuse()
    first = projected(sources, reuse)
    expected = dict(first.citations)
    first.citations.clear()
    second = projected(sources, reuse)
    assert second.citations == expected
    second.citations.clear()
    assert projected(sources, reuse).citations == expected


@pytest.mark.parametrize("change", ["payload", "citations", "binding"])
def test_reuse_preserves_supplied_projection_validation(change):
    group = cohort(controller("a"), controller("b"))
    reuse = projection.ProjectionReuse()
    valid = projected(group.episodes, reuse)
    values = {
        "payload": {"payload_json": "{}"},
        "citations": {"citations": {}},
        "binding": {"binding_json": canonical({**valid.binding, "projection": {}})},
    }
    with pytest.raises(ValueError, match="projection"):
        prepare_partial_proposal(
            group, projection=replace(valid, **values[change]), projection_reuse=reuse
        )
    assert projected(group.episodes, reuse) == valid


def test_reuse_checks_current_bytes_before_lookup():
    source = controller("a")
    reuse = projection.ProjectionReuse()
    projected([source], reuse)
    changed = source.model_copy(update={"artifact": source.artifact + b" "})
    with pytest.raises(ValueError, match="source bytes differ"):
        projected([changed], reuse)


@pytest.mark.parametrize(
    "update", [{"incarnation": "replacement"}, {"generation": 2}, {"observed_revision": 2}]
)
def test_reuse_binds_every_source_observation(monkeypatch, update):
    source = controller("a")
    calls = track_projections(monkeypatch)
    reuse = projection.ProjectionReuse()
    original = projected([source], reuse)
    changed = source.model_copy(update={"source": source.source.model_copy(update=update)})
    actual = projected([changed], reuse)
    assert len(calls) == 2
    assert actual.binding_json != original.binding_json
    assert actual == projected([changed])


def test_reuse_retains_only_one_candidate_and_has_no_shared_state(monkeypatch):
    left, right = [controller("a")], [controller("b")]
    calls = track_projections(monkeypatch)
    reuse = projection.ProjectionReuse()
    original = projected(left, reuse)
    assert projected(left, reuse) == original
    assert len(calls) == 1
    projected(right, reuse)
    assert projected(left, reuse) == original
    assert len(calls) == 3
    assert projected(left, projection.ProjectionReuse()) == original
    assert len(calls) == 4
