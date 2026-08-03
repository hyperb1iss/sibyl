"""The exact-match arm: firing, inertness, scope authorization, fusion behavior."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval import search as search_module
from sibyl_core.retrieval.candidates import RetrievalCandidate
from sibyl_core.retrieval.search import (
    RetrievalSignal,
    build_context_retrieval_plan,
)

PROBE_KEY = "ERR_CONN_RESET_0x7f31"
PROBE_QUERY = f"why does {PROBE_KEY} keep firing"
# The discriminating property: the body never contains the key, so no lexical or
# dense lane can reach this row. Only the writer's declaration can.
BODY_WITHOUT_THE_KEY = "The socket dropped mid-handshake and the retry loop gave up."


class _RecordingClient:
    """A Surreal stand-in for the exact-key read, recording the query it got.

    Matching mirrors CONTAINSANY against an index on the array's elements: the
    row comes back when any element of its stored key list is among the bound
    probes. Verified against a live 3.2.3 server, which is the only reason this
    stand-in can be trusted to mean the same thing the server does.
    """

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> list[dict[str, object]]:
        self.queries.append((query, dict(params)))
        raw_probes = params.get("probe_keys")
        if not isinstance(raw_probes, list | tuple):
            return list(self.rows)
        probes = {str(probe) for probe in raw_probes}
        return [
            dict(row)
            for row in self.rows
            if probes & {str(key) for key in row.get("retrieval_keys_normalized") or ()}
        ]


def _plan(
    *,
    principal_id: str = "user-alice",
    query: str = PROBE_QUERY,
    project: str | None = "project_123",
) -> search_module.RetrievalPlan:
    return build_context_retrieval_plan(
        query=query,
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["episode", "note"]},
        principal_id=principal_id,
        project=project,
        accessible_projects={project} if project else None,
    )


async def _captured_metadata(
    *,
    principal_id: str = "user-alice",
    retrieval_keys: list[str] | None = None,
    memory_scope: str = "private",
    scope_key: str | None = None,
) -> dict[str, object]:
    """Run a real capture and hand back the metadata the graph row would carry."""

    captured: dict[str, object] = {}

    async def remember_raw_memory(_request: MemoryCaptureRequest) -> Mapping[str, object]:
        return {"id": "raw_1"}

    async def create_graph_entity(
        _request: MemoryCaptureRequest,
        metadata: Mapping[str, object],
    ) -> Mapping[str, object]:
        captured.update(metadata)
        return {"id": "note_1"}

    await MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    ).capture(
        MemoryCaptureRequest(
            title="Connection resets on startup",
            content=BODY_WITHOUT_THE_KEY,
            entity_type="note",
            metadata={"project_id": "project_123"},
            retrieval_keys=[PROBE_KEY] if retrieval_keys is None else retrieval_keys,
            memory_scope=memory_scope,
            scope_key=scope_key,
            principal_id=principal_id,
        )
    )
    return captured


def _row(
    metadata: Mapping[str, object],
    *,
    normalized_keys: list[str] | None = None,
) -> dict[str, object]:
    return {
        "uuid": "note_1",
        "name": "Connection resets on startup",
        "content": BODY_WITHOUT_THE_KEY,
        "group_id": "org-123",
        "project_id": "project_123",
        "entity_type": "note",
        "retrieval_keys": list(metadata.get("retrieval_keys") or ()),
        "retrieval_keys_normalized": normalized_keys or [PROBE_KEY.casefold()],
        "attributes": dict(metadata),
    }


# ---------------------------------------------------------------------------
# Capture stamps the declaration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_stamps_declared_keys_onto_the_graph_metadata() -> None:
    metadata = await _captured_metadata()

    assert metadata["retrieval_keys"] == [PROBE_KEY]


@pytest.mark.asyncio
async def test_capture_omits_the_field_when_no_key_is_declared() -> None:
    metadata = await _captured_metadata(retrieval_keys=[])

    assert "retrieval_keys" not in metadata


@pytest.mark.asyncio
async def test_capture_refuses_an_invalid_declaration_before_the_raw_write() -> None:
    """An invalid key list must not strand a verbatim record with no projection."""

    raw_writes: list[str] = []

    async def remember_raw_memory(_request: MemoryCaptureRequest) -> Mapping[str, object]:
        raw_writes.append("written")
        return {"id": "raw_1"}

    async def create_graph_entity(
        _request: MemoryCaptureRequest,
        _metadata: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {"id": "note_1"}

    service = MemoryCaptureService(
        remember_raw_memory=remember_raw_memory,
        create_graph_entity=create_graph_entity,
    )
    with pytest.raises(ValueError, match="at most 16 retrieval keys"):
        await service.capture(
            MemoryCaptureRequest(
                title="Too many keys",
                content="Body",
                retrieval_keys=[f"key_{index}" for index in range(17)],
                principal_id="user-alice",
            )
        )

    assert raw_writes == []


# ---------------------------------------------------------------------------
# The lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arm_finds_a_row_whose_body_never_contains_the_query() -> None:
    metadata = await _captured_metadata()
    client = _RecordingClient([_row(metadata)])
    plan = _plan()

    candidates = await search_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=search_module._search_filter_for_plan(plan),
        limit=8,
        probe_tokens=(PROBE_KEY.casefold(),),
    )

    assert [candidate.id for candidate in candidates] == ["note_1"]
    assert PROBE_KEY.casefold() not in candidates[0].content.casefold()
    assert candidates[0].metadata["matched_retrieval_keys"] == [PROBE_KEY.casefold()]
    assert candidates[0].retrieval_signals == (RetrievalSignal.EXACT_KEY.value,)


@pytest.mark.asyncio
async def test_arm_reads_the_indexed_column_with_one_set_membership_query() -> None:
    """The read shape and the index definition are one contract, verified live.

    On SurrealDB 3.2.3 this same CONTAINSANY is index-served only because the
    index is defined on the array's elements (`retrieval_keys_normalized.*`). An
    index on the bare array field turns it into a full table scan, and turns a
    bare equality into zero rows unless the WHERE clause happens to carry a
    second predicate. Both halves are pinned: the query here, the index
    definition in test_surreal_schema_syntax.py.
    """

    client = _RecordingClient()
    plan = _plan()

    await search_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=search_module._search_filter_for_plan(plan),
        limit=8,
        probe_tokens=("err_conn_reset_0x7f31", "search.py"),
    )

    assert len(client.queries) == 1
    query, params = client.queries[0]
    assert "retrieval_keys_normalized CONTAINSANY $probe_keys" in query
    assert "retrieval_keys_normalized =" not in query
    assert "retrieval_keys_normalized IN" not in query
    assert params["probe_keys"] == ["err_conn_reset_0x7f31", "search.py"]
    assert params["group_id"] == "org-123"


@pytest.mark.asyncio
async def test_arm_issues_no_read_without_a_probe() -> None:
    client = _RecordingClient([{"uuid": "should-not-be-read"}])
    plan = _plan(query="how do we handle authentication")

    candidates = await search_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=search_module._search_filter_for_plan(plan),
        limit=8,
        probe_tokens=(),
    )

    assert candidates == []
    assert client.queries == []


@pytest.mark.asyncio
async def test_arm_drops_a_row_whose_stored_keys_do_not_actually_match() -> None:
    """The index is a filter, not the decision: overlap is confirmed in Python."""

    metadata = await _captured_metadata()
    client = _RecordingClient([_row(metadata, normalized_keys=["some_other_key"])])
    plan = _plan()

    candidates = await search_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=search_module._search_filter_for_plan(plan),
        limit=8,
        probe_tokens=(PROBE_KEY.casefold(),),
    )

    assert candidates == []


@pytest.mark.asyncio
async def test_arm_ranks_more_matched_keys_above_fewer() -> None:
    metadata = await _captured_metadata()
    two_of_two = _row(metadata, normalized_keys=["key_a", "key_b"]) | {"uuid": "both"}
    one_of_two = _row(metadata, normalized_keys=["key_a"]) | {"uuid": "one"}
    client = _RecordingClient([one_of_two, two_of_two])
    plan = _plan()

    candidates = await search_module._exact_key_candidates(
        client=client,
        plan=plan,
        search_filter=search_module._search_filter_for_plan(plan),
        limit=8,
        probe_tokens=("key_a", "key_b"),
    )

    assert [candidate.id for candidate in candidates] == ["both", "one"]
    assert candidates[0].score == 1.0
    assert candidates[1].score == 0.5


# ---------------------------------------------------------------------------
# Scope authorization, both directions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_key_hit_reaches_its_owner() -> None:
    metadata = await _captured_metadata(principal_id="user-alice")
    candidate = search_module._candidate_from_node_record(
        _row(metadata),
        signal=RetrievalSignal.EXACT_KEY,
        score=1.0,
    )

    assert search_module._candidate_allowed(
        candidate,
        plan=_plan(principal_id="user-alice"),
        requested_types=set(),
        facet=None,
    )


@pytest.mark.asyncio
async def test_exact_key_hit_is_denied_to_a_project_co_member() -> None:
    """A declared key is not a scope override: the private row stays private."""

    metadata = await _captured_metadata(principal_id="user-alice")
    candidate = search_module._candidate_from_node_record(
        _row(metadata),
        signal=RetrievalSignal.EXACT_KEY,
        score=1.0,
    )

    assert not search_module._candidate_allowed(
        candidate,
        plan=_plan(principal_id="user-bob"),
        requested_types=set(),
        facet=None,
    )


@pytest.mark.asyncio
async def test_project_scoped_exact_key_hit_reaches_a_co_member() -> None:
    metadata = await _captured_metadata(
        principal_id="user-alice",
        memory_scope="project",
        scope_key="project_123",
    )
    candidate = search_module._candidate_from_node_record(
        _row(metadata),
        signal=RetrievalSignal.EXACT_KEY,
        score=1.0,
    )

    assert search_module._candidate_allowed(
        candidate,
        plan=_plan(principal_id="user-bob"),
        requested_types=set(),
        facet=None,
    )


@pytest.mark.asyncio
async def test_project_scoped_exact_key_hit_is_denied_outside_the_project() -> None:
    metadata = await _captured_metadata(
        principal_id="user-alice",
        memory_scope="project",
        scope_key="project_123",
    )
    candidate = search_module._candidate_from_node_record(
        _row(metadata),
        signal=RetrievalSignal.EXACT_KEY,
        score=1.0,
    )

    assert not search_module._candidate_allowed(
        candidate,
        plan=_plan(principal_id="user-bob", project="project_other"),
        requested_types=set(),
        facet=None,
    )


# ---------------------------------------------------------------------------
# Fusion behavior
# ---------------------------------------------------------------------------


def _candidate(
    candidate_id: str,
    *,
    retrieval_keys: list[str] | None = None,
    matched: list[str] | None = None,
    project_id: str = "project_123",
) -> RetrievalCandidate:
    metadata: dict[str, Any] = {}
    if retrieval_keys is not None:
        metadata["retrieval_keys"] = retrieval_keys
    if matched is not None:
        metadata["matched_retrieval_keys"] = matched
    return RetrievalCandidate(
        id=candidate_id,
        type="note",
        name=candidate_id,
        content=BODY_WITHOUT_THE_KEY,
        score=1.0,
        source=None,
        metadata=metadata,
        project_id=project_id,
    )


def test_exact_key_hit_outranks_an_equally_ranked_lexical_hit() -> None:
    plan = _plan()
    exact = _candidate("exact", retrieval_keys=[PROBE_KEY], matched=[PROBE_KEY.casefold()])
    lexical = _candidate("lexical")

    ranked = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [lexical]),
            (RetrievalSignal.EXACT_KEY, [exact]),
        ],
        plan=plan,
        limit=2,
    )

    assert [candidate.id for candidate, _score, _meta in ranked] == ["exact", "lexical"]
    exact_metadata = ranked[0][2]
    assert exact_metadata["exact_key_boost"] == plan.weights.exact_key_boost
    assert exact_metadata["matched_retrieval_keys"] == [PROBE_KEY.casefold()]
    assert "exact_key_boost" not in ranked[1][2]


def test_exact_key_boost_matches_the_direct_raw_source_boost() -> None:
    """The stated derivation, pinned: a declared channel, not an inferred one."""

    plan = _plan()

    assert plan.weights.exact_key_boost == plan.weights.direct_raw_source_boost


def test_exact_key_boost_applies_once_to_a_candidate_found_by_two_lanes() -> None:
    plan = _plan()
    both = _candidate("both", retrieval_keys=[PROBE_KEY], matched=[PROBE_KEY.casefold()])

    ranked = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [both]),
            (RetrievalSignal.EXACT_KEY, [both]),
        ],
        plan=plan,
        limit=2,
    )

    assert len(ranked) == 1
    assert ranked[0][2]["exact_key_boost"] == plan.weights.exact_key_boost


def test_matched_keys_survive_a_row_that_another_lane_found_first() -> None:
    """Regression: candidates_by_id keeps whichever lane saw the row first.

    A live probe caught this. Full-text can reach a row through a sub-token of
    the identifier, so the same uuid arrives from two lanes with two candidate
    instances, and only the exact-key instance carries the match list. Reading
    the match list off the surviving instance reported an empty one.
    """

    plan = _plan()
    from_fulltext = _candidate("shared")
    from_key = _candidate("shared", retrieval_keys=[PROBE_KEY], matched=[PROBE_KEY.casefold()])

    ranked = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [from_fulltext]),
            (RetrievalSignal.EXACT_KEY, [from_key]),
        ],
        plan=plan,
        limit=2,
    )

    assert len(ranked) == 1
    assert ranked[0][2]["matched_retrieval_keys"] == [PROBE_KEY.casefold()]


def test_a_candidate_found_only_by_the_key_is_not_demoted_as_vector_only() -> None:
    accessible_projects = {f"project_{index}" for index in range(20)}
    plan = build_context_retrieval_plan(
        query=PROBE_QUERY,
        organization_id="org-123",
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["note"]},
        principal_id="user-alice",
        project="project_0",
        accessible_projects=accessible_projects,
    )
    exact = _candidate(
        "exact",
        retrieval_keys=[PROBE_KEY],
        matched=[PROBE_KEY.casefold()],
        project_id="project_0",
    )

    ranked = search_module._fuse_candidates(
        [(RetrievalSignal.EXACT_KEY, [exact])],
        plan=plan,
        limit=1,
    )

    assert "vector_only_demoted" not in ranked[0][2]
    assert "graph_expansion_only_demoted" not in ranked[0][2]


def test_an_inert_lane_changes_no_score_and_no_metadata() -> None:
    """Inertness is a structural claim, not an observation about one query.

    rrf_merge drops empty lists before it assigns weights, so an unfired
    exact-key lane cannot shift another candidate's fused score. Pinned here
    because the alternative failure is silent: a query with no identifier would
    rank differently than it did before keys existed.
    """

    plan = _plan()
    lexical = _candidate("lexical")
    vector = _candidate("vector")

    without_lane = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [lexical]),
            (RetrievalSignal.NODE_VECTOR, [vector]),
        ],
        plan=plan,
        limit=10,
    )
    with_inert_lane = search_module._fuse_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [lexical]),
            (RetrievalSignal.EXACT_KEY, []),
            (RetrievalSignal.NODE_VECTOR, [vector]),
        ],
        plan=plan,
        limit=10,
    )

    assert [(candidate.id, score) for candidate, score, _meta in without_lane] == [
        (candidate.id, score) for candidate, score, _meta in with_inert_lane
    ]
    assert [meta for _candidate, _score, meta in without_lane] == [
        meta for _candidate, _score, meta in with_inert_lane
    ]


def test_matched_keys_join_the_coverage_text() -> None:
    """Without this the coverage re-rank buries a hit whose body lacks the token."""

    exact = _candidate("exact", retrieval_keys=[PROBE_KEY])

    text = search_module._candidate_query_text(exact, matched_keys=(PROBE_KEY.casefold(),))

    assert PROBE_KEY.casefold() in text


def test_declared_but_unmatched_keys_stay_out_of_the_coverage_text() -> None:
    """A declared key is not a ranking lever on queries it never matched.

    Folding a row's whole declared list into its coverage text moved scores on
    prose queries the arm never fired for, which would let a writer buy a
    permanent coverage lift with sixteen keys of prose keywords.
    """

    keyed = _candidate("keyed", retrieval_keys=["connection pooling handbook"])

    assert search_module._candidate_query_text(keyed) == f"keyed {BODY_WITHOUT_THE_KEY}".lower()
    assert "handbook" not in search_module._candidate_query_text(keyed)


def test_a_candidate_without_keys_has_unchanged_coverage_text() -> None:
    plain = _candidate("plain")

    assert search_module._candidate_query_text(plain) == f"plain {BODY_WITHOUT_THE_KEY}".lower()


def test_coverage_rerank_ignores_keys_the_query_did_not_match() -> None:
    """The end-to-end version of the same property, through the real re-ranker."""

    keyed = _candidate("keyed", retrieval_keys=["connection pooling handbook"])
    rival = _candidate("rival")
    fused: list[tuple[RetrievalCandidate, float, dict[str, Any]]] = [
        (keyed, 1.0, {"sources": [RetrievalSignal.NODE_FULLTEXT.value]}),
        (rival, 0.9, {"sources": [RetrievalSignal.NODE_FULLTEXT.value]}),
    ]

    reranked = search_module._apply_query_coverage_to_fused(
        "how do we handle connection pooling",
        fused,
        temporal_target=None,
    )
    scores = {candidate.id: score for candidate, score, _meta in reranked}

    stripped: list[tuple[RetrievalCandidate, float, dict[str, Any]]] = [
        (_candidate("keyed"), 1.0, {"sources": [RetrievalSignal.NODE_FULLTEXT.value]}),
        (_candidate("rival"), 0.9, {"sources": [RetrievalSignal.NODE_FULLTEXT.value]}),
    ]
    baseline = search_module._apply_query_coverage_to_fused(
        "how do we handle connection pooling",
        stripped,
        temporal_target=None,
    )

    assert scores == {candidate.id: score for candidate, score, _meta in baseline}


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def test_receipt_reports_the_arm_inert_without_a_probe() -> None:
    metadata = search_module._exact_key_receipt_metadata(probe_tokens=(), candidates=[])

    assert metadata == {"exact_key_probe_fired": False}


def test_receipt_reports_probe_tokens_and_hit_count() -> None:
    metadata = search_module._exact_key_receipt_metadata(
        probe_tokens=("err_conn_reset_0x7f31",),
        candidates=[_candidate("exact")],
    )

    assert metadata == {
        "exact_key_probe_fired": True,
        "exact_key_probe_tokens": ["err_conn_reset_0x7f31"],
        "exact_key_hit_count": 1,
    }


def test_receipt_counts_authorized_hits_only() -> None:
    """A pre-filter count is an existence oracle for a caller-supplied string.

    An unauthorized caller gets no rows, so the count must not tell them that
    exactly one memory in the organization declares the key they guessed.
    """

    denied = search_module._exact_key_receipt_metadata(
        probe_tokens=("err_conn_reset_0x7f31",),
        candidates=[],
    )

    assert denied == {
        "exact_key_probe_fired": True,
        "exact_key_probe_tokens": ["err_conn_reset_0x7f31"],
        "exact_key_hit_count": 0,
    }


def test_search_result_surfaces_the_boost_and_the_matched_keys() -> None:
    plan = _plan()
    exact = _candidate("exact", retrieval_keys=[PROBE_KEY], matched=[PROBE_KEY.casefold()])
    ranked = search_module._fuse_candidates(
        [(RetrievalSignal.EXACT_KEY, [exact])],
        plan=plan,
        limit=1,
    )
    candidate, score, fusion_metadata = ranked[0]

    result = search_module._search_result_from_candidate(
        candidate,
        score=score,
        fusion_metadata=fusion_metadata,
        include_content=True,
    )

    assert result.metadata["exact_key_boost"] == plan.weights.exact_key_boost
    assert result.metadata["matched_retrieval_keys"] == [PROBE_KEY.casefold()]
    assert result.metadata["retrieval_signals"] == [RetrievalSignal.EXACT_KEY.value]
