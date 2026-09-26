"""Vector lanes stand aside while almost no stored vector is in the query's model."""

from __future__ import annotations

from typing import Any

import pytest

from sibyl_core.services import embedding_lane_readiness as readiness_module
from sibyl_core.services.embedding_lane_readiness import (
    LANE_MIN_IN_MODEL_FRACTION,
    LANE_READY_ADOPTING,
    LANE_READY_COMPLETE,
    LANE_READY_CONVERTING,
    LANE_READY_UNKNOWN,
    LANE_SKIPPED_SPARSE,
    LANE_SKIPPED_SWITCHED,
    judge_lane_readiness,
    vector_lane_readiness,
)

_OLD = {"provider": "openai", "model": "text-embedding-3-small", "dimensions": 1536}
_NEW = {"provider": "bedrock", "model": "cohere.embed-v4", "dimensions": 1536}
_NEW_STAMP = {**_NEW, "text_version": 2, "input_kind_sensitive": True, "stamp_version": 2}


def _receipt(space: dict[str, Any], in_model: int | None, pending: int | None) -> dict[str, Any]:
    return {"status": "partial", "space": space, "in_model": in_model, "pending": pending}


@pytest.mark.parametrize(
    ("state", "run", "reason", "admit_unstamped"),
    [
        # Nothing recorded and nothing to vouch for the unstamped vectors:
        # the lane runs on stamped vectors only.
        ({}, True, LANE_READY_UNKNOWN, False),
        ({"complete_metadata": _NEW_STAMP}, True, LANE_READY_COMPLETE, False),
        ({"complete_metadata": _OLD}, False, LANE_SKIPPED_SWITCHED, False),
        # A pass that was skipped before it could count anything leaves the
        # switch reading in force.
        (
            {"complete_metadata": _OLD, "last_run": _receipt(_NEW, None, None)},
            False,
            LANE_SKIPPED_SWITCHED,
            False,
        ),
        (
            {"complete_metadata": _OLD, "last_run": _receipt(_NEW, 3, 97)},
            False,
            LANE_SKIPPED_SPARSE,
            False,
        ),
        (
            {"complete_metadata": _OLD, "last_run": _receipt(_NEW, 40, 60)},
            True,
            LANE_READY_CONVERTING,
            False,
        ),
        # Unstamped vectors judged another model's: a switch.
        ({"legacy_decision": "reembed"}, False, LANE_SKIPPED_SWITCHED, False),
        (
            {"legacy_decision": "reembed", "last_run": _receipt(_NEW, 1_000, 99_000)},
            False,
            LANE_SKIPPED_SPARSE,
            False,
        ),
        # A never-switched upgrade part way through adoption: the unstamped
        # vectors are the query's model and the lane reads them.
        (
            {
                "legacy_decision": "adopt",
                "legacy_metadata": _NEW_STAMP,
                "last_run": _receipt(_NEW, 1_000, 99_000),
            },
            True,
            LANE_READY_ADOPTING,
            True,
        ),
        # Adopted as a model the query no longer uses: a later switch.
        (
            {"legacy_decision": "adopt", "legacy_metadata": _OLD},
            False,
            LANE_SKIPPED_SWITCHED,
            False,
        ),
        ({"legacy_decision": "none"}, True, LANE_READY_UNKNOWN, True),
        # Unclassified: the plane's own pre-upgrade stamps stand in.
        ({"legacy_evidence": {"stamps": [_NEW]}}, True, LANE_READY_ADOPTING, True),
        ({"legacy_evidence": {"stamps": [_NEW, _OLD]}}, False, LANE_SKIPPED_SWITCHED, False),
        # A receipt for some other model says nothing about this one.
        ({"last_run": _receipt(_OLD, 0, 100)}, True, LANE_READY_UNKNOWN, False),
    ],
)
def test_lane_readiness_follows_the_planes_sweep_state(
    state: dict[str, Any], run: bool, reason: str, admit_unstamped: bool
) -> None:
    verdict = judge_lane_readiness(state, _NEW_STAMP)

    assert (verdict.run, verdict.reason, verdict.admit_unstamped) == (
        run,
        reason,
        admit_unstamped,
    )


def test_a_plane_that_never_switched_never_skips() -> None:
    """At 100,000 unstamped vectors adoption can report 1% stamped; the lane still runs."""
    progress = {"last_run": _receipt(_NEW, 1_000, 99_000)}
    adopting = {**progress, "legacy_decision": "adopt", "legacy_metadata": _NEW_STAMP}
    matching = {**progress, "legacy_evidence": {"stamps": [_NEW]}}
    silent = dict(progress)

    for state in (adopting, matching, silent):
        assert judge_lane_readiness(state, _NEW_STAMP).run, state
    # Unstamped vectors count wherever the evidence already says adopt.
    assert judge_lane_readiness(adopting, _NEW_STAMP).admit_unstamped
    assert judge_lane_readiness(matching, _NEW_STAMP).admit_unstamped
    assert not judge_lane_readiness(silent, _NEW_STAMP).admit_unstamped


def test_unclassified_planes_follow_the_provisional_verdict() -> None:
    from sibyl_core.services.embedding_sweep import LegacyVectorBasis, LegacyVectorDecision

    reembed = (LegacyVectorDecision.REEMBED, LegacyVectorBasis.DEPLOYMENT_STAMPS_DIFFER)
    matched = (LegacyVectorDecision.ADOPT, LegacyVectorBasis.DEPLOYMENT_STAMPS_MATCH)
    unproven = (LegacyVectorDecision.ADOPT, LegacyVectorBasis.NO_PRIOR_EVIDENCE)

    # Other organizations' records show a switch the plane's own do not.
    assert not judge_lane_readiness({}, _NEW_STAMP, provisional=reembed).run
    assert judge_lane_readiness({}, _NEW_STAMP, provisional=matched).admit_unstamped
    unvouched = judge_lane_readiness({}, _NEW_STAMP, provisional=unproven)
    assert unvouched.run and not unvouched.admit_unstamped
    # The operator's policy reaches the plane's own snapshot too.
    assert not judge_lane_readiness({}, _NEW_STAMP, legacy_policy="reembed").run
    # Once a verdict is recorded, the provisional one no longer matters.
    assert judge_lane_readiness(
        {"legacy_decision": "adopt", "legacy_metadata": _NEW_STAMP},
        _NEW_STAMP,
        provisional=reembed,
    ).admit_unstamped


def test_threshold_sits_where_the_measured_lanes_answered_within_half_a_second() -> None:
    switched = {"complete_metadata": _OLD}
    at_threshold = judge_lane_readiness({**switched, "last_run": _receipt(_NEW, 5, 95)}, _NEW_STAMP)
    below = judge_lane_readiness({**switched, "last_run": _receipt(_NEW, 4, 96)}, _NEW_STAMP)

    assert LANE_MIN_IN_MODEL_FRACTION == 0.05
    assert at_threshold.run and at_threshold.in_model_fraction == pytest.approx(0.05)
    assert not below.run and below.in_model_fraction == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_lane_resumes_once_the_cached_state_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    state: dict[str, Any] = {"complete_metadata": _OLD}
    reads: list[str] = []

    async def execute(query: str, **params: object) -> list[dict[str, Any]]:
        reads.append(str(params["key"]))
        return [dict(state)]

    monkeypatch.setattr(readiness_module.time, "monotonic", lambda: clock[0])

    async def ask() -> bool:
        verdict = await vector_lane_readiness(
            plane="graph", organization_id="org-a", execute=execute, query_stamp=_NEW_STAMP
        )
        return verdict.run

    assert await ask() is False
    state = {"complete_metadata": _OLD, "last_run": _receipt(_NEW, 50, 50)}
    clock[0] += 10
    assert await ask() is False, "a fresh verdict is reused inside the refresh window"
    clock[0] += 25
    assert await ask() is True
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_unreadable_state_runs_the_lane() -> None:
    async def execute(query: str, **params: object) -> object:
        raise RuntimeError("table embedding_states does not exist")

    verdict = await vector_lane_readiness(
        plane="document_chunks",
        organization_id="org-b",
        execute=execute,
        query_stamp=_NEW_STAMP,
    )

    assert (verdict.run, verdict.reason) == (True, LANE_READY_UNKNOWN)


@pytest.mark.asyncio
async def test_retrieval_reports_the_skipped_lane_without_embedding_the_query() -> None:
    from sibyl_core.retrieval._search_plan import RetrievalPlan, SearchFilter
    from sibyl_core.retrieval._search_sources import _vector_candidate_sources_detailed

    class Provider:
        def __init__(self) -> None:
            self.calls = 0
            self.metadata = type("Meta", (), {"to_dict": staticmethod(lambda: _NEW_STAMP)})()

        async def embed_texts(self, texts: list[str], *, input_kind: str) -> list[list[float]]:
            self.calls += 1
            return [[0.0] * 1536 for _ in texts]

    class Client:
        async def execute_query(self, query: str, **params: object) -> list[dict[str, Any]]:
            return [{"complete_metadata": _OLD}]

    provider = Provider()
    plan = RetrievalPlan(
        query="where did the deploy notes go",
        organization_id="org-c",
        facets=(),
        facet_types={},
        scopes=(),
        denied_scopes=(),
    )

    fetch = await _vector_candidate_sources_detailed(
        client=Client(),
        plan=plan,
        search_filter=SearchFilter(),
        embedding_provider=provider,  # type: ignore[arg-type]
    )

    assert provider.calls == 0
    assert fetch.as_metadata()["vector_status"] == "vector_lane_model_switched"
    assert fetch.requested and not fetch.attempted and not fetch.degraded
