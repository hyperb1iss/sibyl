"""The naive-strong arm: RRF rank math, tight packing, lanes, and non-interference.

The arm only means something if it is genuinely simpler than the machine and
the machine is genuinely unchanged when the arm is not selected. Both halves are
asserted here: the fusion tests pin the rank math the arm is allowed to have,
and the non-interference tests pin the surface it is not allowed to touch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata
from sibyl_core.models.context import ContextFacet
from sibyl_core.retrieval import naive as naive_module
from sibyl_core.retrieval import search as search_module
from sibyl_core.retrieval.candidates import CandidateKind, CandidateScope, RetrievalCandidate
from sibyl_core.retrieval.naive import (
    NAIVE_RRF_K,
    fuse_naive_candidates,
    naive_retrieval_plan,
    naive_search,
    pack_naive_results,
)
from sibyl_core.retrieval.search import (
    RetrievalPlan,
    RetrievalSignal,
    RetrievalWeights,
    build_context_retrieval_plan,
    context_search,
)
from sibyl_core.services.graph import (
    EntityManager,
    RelationshipManager,
    SurrealGraphClient,
    prepare_graph_schema,
)

ORG_ID = "org-naive-arm"
PROJECT_ID = "project_naive"
PRINCIPAL_ID = "user-naive"
CORPUS_QUERY = "deployment pipeline stale token"


def _candidate(
    candidate_id: str,
    *,
    score: float = 0.5,
    content: str = "body",
) -> RetrievalCandidate:
    return RetrievalCandidate(
        id=candidate_id,
        kind=CandidateKind.NODE,
        type="session",
        name=candidate_id,
        content=content,
        score=score,
        source=None,
        metadata={},
        scope=CandidateScope(),
    )


def _plan(
    *,
    query: str,
    limit: int = 10,
    organization_id: str = ORG_ID,
    project: str | None = None,
) -> RetrievalPlan:
    return build_context_retrieval_plan(
        query=query,
        organization_id=organization_id,
        facets=[ContextFacet.RECENT_MEMORY],
        facet_types={ContextFacet.RECENT_MEMORY: ["session"]},
        principal_id=PRINCIPAL_ID,
        project=project,
        accessible_projects={project} if project else None,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# RRF rank math
# ---------------------------------------------------------------------------


def test_single_lane_scores_are_exactly_the_rrf_series() -> None:
    """One lane, no weights: the score is 1/(k + rank) and nothing else."""

    lane = [_candidate("a"), _candidate("b"), _candidate("c")]

    fused = fuse_naive_candidates([(RetrievalSignal.NODE_FULLTEXT, lane)], limit=10)

    assert [candidate.id for candidate, _score, _metadata in fused] == ["a", "b", "c"]
    assert [round(score, 12) for _candidate, score, _metadata in fused] == [
        round(1.0 / (NAIVE_RRF_K + rank), 12) for rank in (1, 2, 3)
    ]


def test_the_arm_defaults_to_the_rrf_papers_k() -> None:
    assert NAIVE_RRF_K == 60.0


def test_agreement_across_lanes_sums_the_two_contributions() -> None:
    """A row both lanes returned scores the sum of its two rank contributions."""

    lexical = [_candidate("a"), _candidate("shared")]
    dense = [_candidate("shared"), _candidate("b")]

    fused = fuse_naive_candidates(
        [(RetrievalSignal.NODE_FULLTEXT, lexical), (RetrievalSignal.NODE_VECTOR, dense)],
        limit=10,
    )
    scores = {candidate.id: score for candidate, score, _metadata in fused}

    assert scores["shared"] == pytest.approx(1.0 / (NAIVE_RRF_K + 2) + 1.0 / (NAIVE_RRF_K + 1))
    assert scores["a"] == pytest.approx(1.0 / (NAIVE_RRF_K + 1))
    # Agreement beats a single lane's top rank: this is the whole reason the arm
    # fuses rather than concatenating.
    assert fused[0][0].id == "shared"


def test_lane_score_magnitudes_never_enter_the_ranking() -> None:
    """RRF is ordinal. Rescaling a lane's scores must not move a single result."""

    modest = [_candidate("a", score=0.51), _candidate("b", score=0.50)]
    dramatic = [_candidate("a", score=99.0), _candidate("b", score=0.001)]

    modest_fused = fuse_naive_candidates([(RetrievalSignal.NODE_FULLTEXT, modest)], limit=10)
    dramatic_fused = fuse_naive_candidates([(RetrievalSignal.NODE_FULLTEXT, dramatic)], limit=10)

    assert [(candidate.id, score) for candidate, score, _metadata in modest_fused] == [
        (candidate.id, score) for candidate, score, _metadata in dramatic_fused
    ]


def test_equal_ranks_in_disjoint_lanes_tie_exactly() -> None:
    """Two rows nothing distinguishes get identical scores, not near-identical."""

    fused = fuse_naive_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [_candidate("only_lexical")]),
            (RetrievalSignal.NODE_VECTOR, [_candidate("only_dense")]),
        ],
        limit=10,
    )
    scores = [score for _candidate, score, _metadata in fused]

    assert scores[0] == scores[1]
    assert {candidate.id for candidate, _score, _metadata in fused} == {
        "only_lexical",
        "only_dense",
    }


def test_lane_order_does_not_change_any_score() -> None:
    """No per-lane weights means the lane list is a set, not a priority order."""

    lexical = [_candidate("a"), _candidate("shared")]
    dense = [_candidate("shared"), _candidate("b")]

    forward = fuse_naive_candidates(
        [(RetrievalSignal.NODE_FULLTEXT, lexical), (RetrievalSignal.NODE_VECTOR, dense)],
        limit=10,
    )
    reversed_lanes = fuse_naive_candidates(
        [(RetrievalSignal.NODE_VECTOR, dense), (RetrievalSignal.NODE_FULLTEXT, lexical)],
        limit=10,
    )

    assert {candidate.id: score for candidate, score, _metadata in forward} == {
        candidate.id: score for candidate, score, _metadata in reversed_lanes
    }


def test_fusion_records_which_lanes_found_each_row() -> None:
    fused = fuse_naive_candidates(
        [
            (RetrievalSignal.NODE_FULLTEXT, [_candidate("a"), _candidate("shared")]),
            (RetrievalSignal.NODE_VECTOR, [_candidate("shared")]),
        ],
        limit=10,
    )
    metadata_by_id = {candidate.id: metadata for candidate, _score, metadata in fused}

    assert metadata_by_id["shared"]["sources"] == ["node_fulltext", "node_vector"]
    assert metadata_by_id["shared"]["ranks"] == {"node_fulltext": 2, "node_vector": 1}
    assert metadata_by_id["a"]["sources"] == ["node_fulltext"]


def test_fusion_truncates_to_the_requested_limit() -> None:
    lane = [_candidate(f"c{index}") for index in range(10)]

    fused = fuse_naive_candidates([(RetrievalSignal.NODE_FULLTEXT, lane)], limit=3)

    assert [candidate.id for candidate, _score, _metadata in fused] == ["c0", "c1", "c2"]


def test_empty_lanes_fuse_to_nothing() -> None:
    assert fuse_naive_candidates([(RetrievalSignal.NODE_FULLTEXT, [])], limit=5) == []
    assert fuse_naive_candidates([], limit=5) == []


# ---------------------------------------------------------------------------
# Tight pack
# ---------------------------------------------------------------------------


def _fused_for_pack(*sizes: int) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    return [
        (
            _candidate(f"c{index}", content="x" * size),
            1.0 / (index + 1),
            {"sources": ["node_fulltext"], "ranks": {}, "original_scores": {}},
        )
        for index, size in enumerate(sizes)
    ]


def test_pack_stops_before_crossing_the_char_budget() -> None:
    results, receipt = pack_naive_results(
        _fused_for_pack(100, 100, 100),
        include_content=True,
        char_budget=250,
    )

    assert [result.id for result in results] == ["c0", "c1"]
    assert receipt["naive_pack_chars_used"] == 200
    assert receipt["naive_pack_budget_exhausted"] is True


def test_pack_admits_a_single_item_wider_than_the_whole_budget() -> None:
    """An empty pack answers nothing, so the first item is always admitted."""

    results, receipt = pack_naive_results(
        _fused_for_pack(5_000),
        include_content=True,
        char_budget=100,
    )

    assert [result.id for result in results] == ["c0"]
    assert receipt["naive_pack_budget_exhausted"] is False


def test_pack_without_a_budget_keeps_every_fused_result() -> None:
    results, receipt = pack_naive_results(
        _fused_for_pack(100, 100, 100),
        include_content=True,
        char_budget=None,
    )

    assert len(results) == 3
    assert receipt["naive_pack_char_budget"] is None
    assert receipt["naive_pack_budget_exhausted"] is False


def test_pack_costs_nothing_per_item_when_content_is_withheld() -> None:
    results, receipt = pack_naive_results(
        _fused_for_pack(5_000, 5_000),
        include_content=False,
        char_budget=100,
    )

    assert len(results) == 2
    assert receipt["naive_pack_chars_used"] == 0


# ---------------------------------------------------------------------------
# The arm end to end, against the embedded engine
# ---------------------------------------------------------------------------


def _embedding_provider() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(
        EmbeddingMetadata(
            provider="deterministic",
            model="unit-test",
            dimensions=EMBEDDING_DIM,
            cache_namespace="naive-arm-test",
            tokenizer_estimate_method="utf8-byte-length",
        )
    )


async def _seed_corpus(client: SurrealGraphClient, provider: Any, *, group_id: str) -> None:
    """Three sessions: one the lexical lane finds, one the dense lane finds, one both.

    Row vectors are blended toward the query vector rather than used raw. A
    deterministic test provider emits near-orthogonal vectors whose cosine
    similarity lands around zero and often below it, and the vector lane drops
    anything under ``vector_min_score``, so raw test embeddings would leave the
    dense half of the arm silently untested while the suite stayed green.

    Rows carry no ``project_id``: the embedded engine returns zero rows for an
    OR predicate sitting beside an HNSW ``<|k, ef|>`` bracket even when the row
    satisfies each half on its own, which would take the dense lane out for a
    reason that has nothing to do with the arm.
    """

    texts = {
        "lexical_only": "The deployment pipeline broke on a stale kubeconfig token.",
        "shared": "The deployment pipeline retries on a stale token before failing.",
        "dense_only": "Nothing in this row spells the question out loud.",
    }
    document_embeddings = await provider.embed_texts(list(texts.values()), input_kind="document")
    query_embedding = (await provider.embed_texts([CORPUS_QUERY], input_kind="query"))[0]
    rows = []
    for index, ((uuid, content), embedding) in enumerate(
        zip(texts.items(), document_embeddings, strict=True)
    ):
        blend = 1.0 - 0.05 * index
        rows.append(
            {
                "uuid": uuid,
                "group_id": group_id,
                "name": uuid.replace("_", " "),
                "content": content,
                "summary": content,
                "entity_type": "session",
                "name_embedding": [
                    blend * query_value + (1.0 - blend) * document_value
                    for query_value, document_value in zip(query_embedding, embedding, strict=True)
                ],
                "created_at": datetime.now(UTC),
            }
        )
    await client.execute_query("INSERT INTO entity $rows;", rows=rows)


async def _embedded_runtime(group_id: str) -> tuple[SurrealGraphClient, Any]:
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await prepare_graph_schema(client)

    class Runtime:
        def __init__(self, graph_client: SurrealGraphClient) -> None:
            self.client = graph_client
            self.entity_manager = EntityManager(graph_client, group_id=group_id)
            self.relationship_manager = RelationshipManager(graph_client, group_id=group_id)

    return client, Runtime(client)


@pytest.mark.asyncio
async def test_naive_arm_returns_rows_fused_from_both_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime = await _embedded_runtime(ORG_ID)
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

        response = await naive_search(
            plan=_plan(query=CORPUS_QUERY),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    assert response.filters["retrieval_mode"] == "naive"
    assert response.results, "the arm returned nothing on a corpus that matches the query"
    lanes = {
        lane for result in response.results for lane in result.metadata.get("retrieval_signals", [])
    }
    # Both halves of the arm have to be live: a BM25-only or KNN-only run would
    # race a different arm than the one the plan names.
    assert "node_fulltext" in lanes
    assert "node_vector" in lanes
    assert response.filters["naive_lane_counts"]["node_fulltext"] > 0
    assert response.filters["naive_lane_counts"]["node_vector"] > 0


@pytest.mark.asyncio
async def test_naive_arm_honors_the_char_budget_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runtime = await _embedded_runtime(f"{ORG_ID}-budget")
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
        plan = _plan(query=CORPUS_QUERY, organization_id=client.group_id)

        unbounded = await naive_search(
            plan=plan,
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
        bounded = await naive_search(
            plan=plan,
            types=["session"],
            limit=10,
            embedding_provider=provider,
            char_budget=60,
        )
    finally:
        await client.close()

    assert len(unbounded.results) > 1
    assert len(bounded.results) < len(unbounded.results)
    assert bounded.filters["naive_pack_budget_exhausted"] is True
    # The pack never truncates an item, so a budget smaller than the second
    # item's content ends the pack rather than admitting a partial span. The
    # first item is admitted whatever it costs, which is why chars_used is
    # bounded by "budget plus one whole item", not by the budget.
    assert len(bounded.results) == 1


@pytest.mark.asyncio
async def test_naive_arm_never_reads_the_machines_deleted_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph expansion, exact key, edge lanes, and raw lexical are gone, not quiet."""

    client, runtime = await _embedded_runtime(f"{ORG_ID}-lanes")
    provider = _embedding_provider()

    async def forbidden(**_kwargs: object) -> list[RetrievalCandidate]:
        raise AssertionError("the naive arm reached a lane it is supposed to delete")

    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
        monkeypatch.setattr(search_module, "_graph_expansion_candidates", forbidden)
        monkeypatch.setattr(search_module, "_exact_key_candidates", forbidden)
        monkeypatch.setattr(search_module, "_edge_fulltext_candidates", forbidden)
        monkeypatch.setattr(search_module, "_edge_vector_candidates", forbidden)
        monkeypatch.setattr(search_module, "_recall_raw_candidates", forbidden)

        response = await naive_search(
            plan=_plan(
                query=f"{CORPUS_QUERY} ERR_STALE_TOKEN_0x9",
                organization_id=client.group_id,
                limit=10,
            ),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    assert response.filters["naive_lanes"] == [
        "node_fulltext",
        "episode_fulltext",
        "node_vector",
    ]
    assert "exact_key_probe_fired" not in response.filters
    assert "planner_status" not in response.filters


@pytest.mark.asyncio
async def test_naive_arm_applies_no_boost_to_the_fused_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned score is the raw RRF sum: no freshness, project, or type boost."""

    client, runtime = await _embedded_runtime(f"{ORG_ID}-scores")
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
        response = await naive_search(
            plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    # Every row is fresh, so the machine's freshness boost would fire on all of
    # them. The scores must still be exact sums of 1/(k + rank) terms.
    reachable = {
        round(sum(1.0 / (NAIVE_RRF_K + rank) for rank in ranks), 12)
        for ranks in ((1,), (2,), (3,), (1, 1), (1, 2), (2, 1), (2, 2), (1, 3), (3, 1))
    }
    for result in response.results:
        assert round(result.score, 12) in reachable


# ---------------------------------------------------------------------------
# Non-interference: the machine is byte-identical when the arm is not selected
# ---------------------------------------------------------------------------


def test_the_arm_does_not_change_the_machines_default_plan() -> None:
    """Importing and running the arm must not move a single machine default."""

    plan = RetrievalPlan(
        query="q",
        organization_id=ORG_ID,
        facets=(),
        facet_types={},
        scopes=(),
        denied_scopes=(),
    )

    assert plan.signals == (
        RetrievalSignal.RAW_LEXICAL,
        RetrievalSignal.NODE_FULLTEXT,
        RetrievalSignal.EPISODE_FULLTEXT,
        RetrievalSignal.EDGE_FULLTEXT,
        RetrievalSignal.NODE_VECTOR,
        RetrievalSignal.EDGE_VECTOR,
        RetrievalSignal.GRAPH_EXPANSION,
        RetrievalSignal.EXACT_KEY,
    )
    assert RetrievalWeights() == RetrievalWeights(
        rrf_k=60,
        active_task_state_boost=1.3,
        project_match_boost=1.2,
        direct_raw_source_boost=1.4,
        exact_key_boost=1.4,
        graph_expansion_only_boost=0.45,
        graph_native_signal_boost_cap=1.2,
        freshness_boost_cap=1.5,
    )


def test_narrowing_a_plan_for_the_arm_leaves_the_callers_plan_intact() -> None:
    plan = _plan(query="q")

    narrowed = naive_retrieval_plan(plan)

    assert len(plan.signals) == 8
    assert narrowed.signals == (
        RetrievalSignal.NODE_FULLTEXT,
        RetrievalSignal.EPISODE_FULLTEXT,
        RetrievalSignal.NODE_VECTOR,
    )
    # Scope and project authorization survive the narrowing untouched: the arm
    # deletes ranking surface, never access control.
    assert narrowed.scopes == plan.scopes
    assert narrowed.denied_scopes == plan.denied_scopes
    assert narrowed.accessible_projects == plan.accessible_projects
    assert narrowed.project == plan.project


@pytest.mark.asyncio
async def test_the_machine_returns_the_same_results_after_the_arm_has_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decisive non-interference check: same corpus, same query, same answer.

    Run the machine, run the arm, run the machine again. If the arm mutated any
    shared plan, weight table, or module default, the two machine runs diverge.
    """

    client, runtime = await _embedded_runtime(f"{ORG_ID}-invariant")
    provider = _embedding_provider()

    async def no_raw_recall(**_kwargs: object) -> list[Any]:
        return []

    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
        plan = _plan(query=CORPUS_QUERY, organization_id=client.group_id)

        async def machine_run() -> list[tuple[str, float, tuple[str, ...], str]]:
            response = await context_search(
                plan=plan,
                types=["session"],
                limit=10,
                embedding_provider=provider,
                raw_memory_recall_fn=no_raw_recall,
            )
            return [
                (
                    result.id,
                    # The machine multiplies in a freshness boost computed
                    # against the wall clock, so its scores drift in the low
                    # decimals between two runs seconds apart. Six places is far
                    # tighter than any reordering the arm could cause and looser
                    # than a clock tick.
                    round(result.score, 6),
                    tuple(result.metadata.get("retrieval_signals", [])),
                    result.content,
                )
                for result in response.results
            ]

        before = await machine_run()
        await naive_search(
            plan=plan,
            types=["session"],
            limit=10,
            embedding_provider=provider,
            char_budget=50,
        )
        after = await machine_run()
    finally:
        await client.close()

    assert before == after
    assert before, "the machine returned nothing, so the comparison proves nothing"
    # The machine's own lanes must still be live in the comparison, or the
    # invariant would hold vacuously over a corpus the machine could not reach.
    assert {lane for _id, _score, lanes, _content in before for lane in lanes} >= {
        "node_fulltext",
        "node_vector",
    }


def test_the_arm_module_exposes_no_tunable_weights() -> None:
    """The arm's value is what it deletes. A weight table here would undo that."""

    tunables = {
        name
        for name in dir(naive_module)
        if name.isupper() and ("WEIGHT" in name or "BOOST" in name)
    }

    assert tunables == set()
