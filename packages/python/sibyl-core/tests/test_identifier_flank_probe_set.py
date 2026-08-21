"""The A2 identifier-flank probe set, run against the detector and the arm.

The fixture is the standing regression probe the 1.2 ranking gate references, so
it is checked here rather than only read by an offline harness: a change to the
detector or the arm that breaks the flank fails in CI instead of in a leaderboard
run weeks later.

Each firing case asserts the discriminating property in both directions. The
memory's body must not contain the queried token, so a hit cannot be explained
by full-text or dense similarity, and the arm must nonetheless return it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sibyl_core.memory_pipeline.retrieval_keys import (
    normalize_retrieval_keys,
    retrieval_key_match_form,
)
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval import _search_candidates as candidate_module
from sibyl_core.retrieval import _search_fusion as fusion_module
from sibyl_core.retrieval import _search_plan as plan_module
from sibyl_core.retrieval import _search_sources as source_module
from sibyl_core.retrieval import search as search_module
from sibyl_core.retrieval.identifier_query import identifier_probe_tokens
from sibyl_core.retrieval.search import RetrievalSignal, build_context_retrieval_plan

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "identifier_flank" / "a2_probe_set.json"


def load_probe_set() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


PROBE_SET = load_probe_set()
CASES: list[dict[str, Any]] = PROBE_SET["cases"]
FIRING_CASES = [case for case in CASES if case["expect_fire"]]
INERT_CASES = [case for case in CASES if not case["expect_fire"]]


def _case_ids(cases: list[dict[str, Any]]) -> list[str]:
    return [str(case["id"]) for case in cases]


class _KeyIndexClient:
    """A Surreal stand-in for the exact-key read against one seeded row.

    Mirrors CONTAINSANY over an index on the array's elements: the row comes
    back when any element of its stored key list is among the bound probes.
    """

    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.reads = 0

    async def execute_query(self, _query: str, **params: object) -> list[dict[str, object]]:
        self.reads += 1
        raw_probes = params.get("probe_keys")
        if not isinstance(raw_probes, list | tuple):
            return []
        probes = {str(probe) for probe in raw_probes}
        stored = {str(key) for key in self.row.get("retrieval_keys_normalized") or ()}
        return [dict(self.row)] if probes & stored else []


def _plan(query: str) -> search_module.RetrievalPlan:
    return build_context_retrieval_plan(
        query=query,
        organization_id="org-a2",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id="user-a2",
        project=None,
        accessible_projects=None,
    )


def _row_for(case: dict[str, Any]) -> dict[str, object]:
    memory = case["memory"]
    display, match = normalize_retrieval_keys(memory["retrieval_keys"])
    return {
        "uuid": f"note_{case['id']}",
        "name": memory["title"],
        "content": memory["body"],
        "entity_type": "note",
        "group_id": "org-a2",
        "retrieval_keys": display,
        "retrieval_keys_normalized": match,
        "attributes": {
            "memory_scope": "private",
            "principal_id": "user-a2",
            "retrieval_keys": display,
        },
    }


# ---------------------------------------------------------------------------
# The fixture's own claims
# ---------------------------------------------------------------------------


def test_probe_set_is_sized_for_a_standing_gate() -> None:
    assert 10 <= len(CASES) <= 20
    assert len(FIRING_CASES) >= 8
    assert len(INERT_CASES) >= 5
    assert len(_case_ids(CASES)) == len(set(_case_ids(CASES)))


@pytest.mark.parametrize("case", CASES, ids=_case_ids(CASES))
def test_detector_agrees_with_the_declared_expectation(case: dict[str, Any]) -> None:
    probes = identifier_probe_tokens(case["query"])

    assert list(probes) == list(case["expected_probe_tokens"])
    assert bool(probes) is case["expect_fire"]


@pytest.mark.parametrize("case", FIRING_CASES, ids=_case_ids(FIRING_CASES))
def test_firing_case_is_discriminating(case: dict[str, Any]) -> None:
    """The body must not contain the token, or the case proves nothing."""

    body = case["memory"]["body"].casefold()
    title = case["memory"]["title"].casefold()
    for token in case["expected_probe_tokens"]:
        assert token not in body
        assert token not in title


@pytest.mark.parametrize("case", FIRING_CASES, ids=_case_ids(FIRING_CASES))
def test_firing_case_declares_a_key_that_matches_its_probe(case: dict[str, Any]) -> None:
    _display, match = normalize_retrieval_keys(case["memory"]["retrieval_keys"])

    assert set(case["expected_probe_tokens"]) & set(match)


# ---------------------------------------------------------------------------
# The arm, end to end over the fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FIRING_CASES, ids=_case_ids(FIRING_CASES))
async def test_arm_returns_the_expected_memory(case: dict[str, Any]) -> None:
    plan = _plan(case["query"])
    client = _KeyIndexClient(_row_for(case))

    candidates = await source_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=plan_module._search_filter_for_plan(plan),
        limit=plan.candidate_limits.exact_key,
        probe_tokens=identifier_probe_tokens(case["query"]),
    )

    assert [candidate.id for candidate in candidates] == [f"note_{case['id']}"]
    assert candidates[0].retrieval_signals == (RetrievalSignal.EXACT_KEY.value,)
    assert candidate_module._candidate_allowed(
        candidates[0],
        plan=plan,
        requested_types=set(),
        facet=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FIRING_CASES, ids=_case_ids(FIRING_CASES))
async def test_expected_memory_survives_fusion_against_a_lexical_rival(
    case: dict[str, Any],
) -> None:
    """A hit that ranks below prose noise is a hit nobody reads."""

    plan = _plan(case["query"])
    client = _KeyIndexClient(_row_for(case))
    exact = await source_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=plan_module._search_filter_for_plan(plan),
        limit=plan.candidate_limits.exact_key,
        probe_tokens=identifier_probe_tokens(case["query"]),
    )
    rival = candidate_module._candidate_from_node_record(
        {
            "uuid": "lexical_rival",
            "name": "Something the query words happen to touch",
            "content": case["query"],
            "entity_type": "note",
            "group_id": "org-a2",
            "attributes": {"memory_scope": "private", "principal_id": "user-a2"},
        },
        signal=RetrievalSignal.NODE_FULLTEXT,
        score=1.0,
    )

    ranked = fusion_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [rival]),
            (RetrievalSignal.EXACT_KEY, exact),
        ],
        plan=plan,
        limit=2,
    )

    assert ranked[0][0].id == f"note_{case['id']}"
    assert ranked[0][2]["exact_key_boost"] == plan.weights.exact_key_boost


@pytest.mark.asyncio
@pytest.mark.parametrize("case", INERT_CASES, ids=_case_ids(INERT_CASES))
async def test_inert_case_issues_no_read(case: dict[str, Any]) -> None:
    plan = _plan(case["query"])
    client = _KeyIndexClient({"uuid": "must-not-be-read"})

    candidates = await source_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=plan_module._search_filter_for_plan(plan),
        limit=plan.candidate_limits.exact_key,
        probe_tokens=identifier_probe_tokens(case["query"]),
    )

    assert candidates == []
    assert client.reads == 0
    assert search_module._exact_key_receipt_metadata(
        probe_tokens=identifier_probe_tokens(case["query"]),
        candidates=[],
    ) == {"exact_key_probe_fired": False}


def test_declared_keys_match_case_insensitively() -> None:
    case = next(item for item in FIRING_CASES if item["id"] == "case-insensitive-declaration")
    _display, match = normalize_retrieval_keys(case["memory"]["retrieval_keys"])

    assert match == [retrieval_key_match_form(case["memory"]["retrieval_keys"][0])]
    assert match == list(case["expected_probe_tokens"])
