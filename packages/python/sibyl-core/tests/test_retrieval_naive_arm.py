"""The naive-strong arm: RRF rank math, tight packing, lanes, and non-interference.

The arm only means something if it is genuinely simpler than the machine and
the machine is genuinely unchanged when the arm is not selected. Both halves are
asserted here: the fusion tests pin the rank math the arm is allowed to have,
and the non-interference tests pin the surface it is not allowed to touch.
"""

from __future__ import annotations

import inspect
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
    metadata: dict[str, Any] | None = None,
) -> RetrievalCandidate:
    return RetrievalCandidate(
        id=candidate_id,
        kind=CandidateKind.NODE,
        type="session",
        name=candidate_id,
        content=content,
        score=score,
        source=None,
        metadata=dict(metadata or {}),
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


def test_ties_break_on_candidate_id_not_on_lane_order() -> None:
    """An exact RRF tie must not be resolved by which lane was passed first.

    Two rows each returned at rank one by a different lane score identically.
    Sorting on score alone leaves a stable sort to pick the earlier lane, which
    is a real ranking preference applied only at a binding cutoff and never
    declared anywhere.
    """

    lexical = [_candidate("zzz_lexical")]
    dense = [_candidate("aaa_dense")]

    forward = fuse_naive_candidates(
        [(RetrievalSignal.NODE_FULLTEXT, lexical), (RetrievalSignal.NODE_VECTOR, dense)],
        limit=1,
    )
    reversed_lanes = fuse_naive_candidates(
        [(RetrievalSignal.NODE_VECTOR, dense), (RetrievalSignal.NODE_FULLTEXT, lexical)],
        limit=1,
    )

    assert forward[0][0].id == reversed_lanes[0][0].id == "aaa_dense"
    assert forward[0][1] == reversed_lanes[0][1]


def test_tie_order_is_stated_rather_than_incidental() -> None:
    """Ascending id, all the way down, not just at the head."""

    lanes = [
        (RetrievalSignal.NODE_FULLTEXT, [_candidate("m")]),
        (RetrievalSignal.EPISODE_FULLTEXT, [_candidate("a")]),
        (RetrievalSignal.NODE_VECTOR, [_candidate("z")]),
    ]

    fused = fuse_naive_candidates(lanes, limit=10)

    assert [candidate.id for candidate, _score, _metadata in fused] == ["a", "m", "z"]


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


def test_pack_caps_each_item_before_charging_it_to_the_budget() -> None:
    """An item must cost the arm what the same item costs the machine.

    The enhanced evidence path truncates every result to `content_max_chars`
    before it leaves the server. An arm that packed untruncated spans against
    the same budget would fit fewer, larger items and the race would be
    comparing payload sizes rather than retrieval.
    """

    capped, receipt = pack_naive_results(
        _fused_for_pack(5_000, 5_000, 5_000),
        include_content=True,
        char_budget=250,
        content_max_chars=100,
    )
    uncapped, _receipt = pack_naive_results(
        _fused_for_pack(5_000, 5_000, 5_000),
        include_content=True,
        char_budget=250,
    )

    assert [len(result.content) for result in capped] == [100, 100]
    assert receipt["naive_pack_chars_used"] == 200
    assert receipt["content_max_chars"] == 100
    # The cap is charged, not cosmetic: the same budget buys one oversized item
    # without it and two capped ones with it.
    assert len(uncapped) == 1


def test_pack_leaves_content_whole_when_no_cap_is_requested() -> None:
    results, _receipt = pack_naive_results(
        _fused_for_pack(5_000),
        include_content=True,
        char_budget=None,
        content_max_chars=None,
    )

    assert len(results[0].content) == 5_000


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


async def _seed_private_row(
    client: SurrealGraphClient,
    provider: Any,
    *,
    group_id: str,
    owner: str,
) -> None:
    """One row that answers the query perfectly and belongs to somebody else."""

    embedding = (await provider.embed_texts([CORPUS_QUERY], input_kind="query"))[0]
    await client.execute_query(
        "INSERT INTO entity $rows;",
        rows=[
            {
                "uuid": "private_to_someone_else",
                "group_id": group_id,
                "name": "deployment pipeline stale token",
                "content": "The deployment pipeline stale token rotation runbook.",
                "summary": "deployment pipeline stale token",
                "entity_type": "session",
                "name_embedding": list(embedding),
                "created_at": datetime.now(UTC),
                "attributes": {"memory_scope": "private", "principal_id": owner},
            }
        ],
    )


@pytest.mark.asyncio
async def test_the_arm_withholds_a_row_the_reader_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm deletes ranking surface, never access control.

    The withheld row is the strongest lexical and dense match in the corpus, so
    a scope check that the arm skipped would put it at rank one rather than
    leaving it out, and the failure would be loud rather than subtle.
    """

    client, runtime = await _embedded_runtime(f"{ORG_ID}-scope")
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)
        await _seed_private_row(
            client,
            provider,
            group_id=client.group_id,
            owner="user-somebody-else",
        )

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

        stranger = await naive_search(
            plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
        owner = await naive_search(
            plan=build_context_retrieval_plan(
                query=CORPUS_QUERY,
                organization_id=client.group_id,
                facets=[ContextFacet.RECENT_MEMORY],
                facet_types={ContextFacet.RECENT_MEMORY: ["session"]},
                principal_id="user-somebody-else",
                project=None,
                accessible_projects=None,
                limit=10,
            ),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    assert "private_to_someone_else" not in {result.id for result in stranger.results}
    # The owner reaching it is what proves the row was retrievable at all, so
    # the withholding above is a scope decision rather than an empty lane.
    assert "private_to_someone_else" in {result.id for result in owner.results}


@pytest.mark.asyncio
async def test_vector_diagnostics_count_only_authorized_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics must not answer "does a row I cannot read exist?".

    A count taken before the scope filter turns an empty result set into an
    existence oracle: zero results beside a non-zero candidate count tells an
    unauthorized caller that a private row matched their query.
    """

    client, runtime = await _embedded_runtime(f"{ORG_ID}-diagnostics")
    provider = _embedding_provider()
    try:
        await _seed_private_row(
            client,
            provider,
            group_id=client.group_id,
            owner="user-somebody-else",
        )

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

        denied = await naive_search(
            plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
        permitted = await naive_search(
            plan=build_context_retrieval_plan(
                query=CORPUS_QUERY,
                organization_id=client.group_id,
                facets=[ContextFacet.RECENT_MEMORY],
                facet_types={ContextFacet.RECENT_MEMORY: ["session"]},
                principal_id="user-somebody-else",
                project=None,
                accessible_projects=None,
                limit=10,
            ),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    assert denied.results == []
    assert denied.filters["vector_candidate_count"] == 0
    # A lane that found only unreadable rows must be indistinguishable from a
    # lane that found nothing at all.
    assert denied.filters["vector_status"] == "empty"
    assert denied.filters["naive_lane_counts"]["node_vector"] == 0
    # The owner proves the row was there to be counted, so the zero above is a
    # scope decision rather than an empty corpus.
    assert permitted.filters["vector_candidate_count"] == 1
    assert permitted.filters["vector_status"] == "ok"


@pytest.mark.asyncio
async def test_lanes_read_deeper_than_the_limit_so_fusion_can_promote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clipping lanes to the answer size makes fusion decorative at small limits.

    A row ranked second in both lanes is exactly what RRF exists to promote over
    a row ranked first in one. If each lane is cut to the caller's limit before
    fusion runs, that row is gone before it can win.
    """

    client, runtime = await _embedded_runtime(f"{ORG_ID}-overfetch")
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)

        narrow = await naive_search(
            plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id, limit=1),
            types=["session"],
            limit=1,
            embedding_provider=provider,
        )
        wide = await naive_search(
            plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id, limit=10),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    assert len(narrow.results) == 1
    assert any(count > 1 for count in narrow.filters["naive_lane_counts"].values())
    # The one row a limit=1 request returns is the row full-pool fusion ranks
    # first, which is the property clipping breaks.
    assert narrow.results[0].id == wide.results[0].id


@pytest.mark.asyncio
async def test_the_arm_authorizes_every_lane_before_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filtering after fusion would let a denied row displace an allowed one."""

    seen: list[str] = []
    real_allowed = search_module._candidate_allowed

    def recording_allowed(candidate: Any, **kwargs: Any) -> bool:
        seen.append(candidate.id)
        return real_allowed(candidate, **kwargs)

    client, runtime = await _embedded_runtime(f"{ORG_ID}-lane-scope")
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)
        await _seed_private_row(
            client,
            provider,
            group_id=client.group_id,
            owner="user-somebody-else",
        )

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
        monkeypatch.setattr(naive_module, "_candidate_allowed", recording_allowed)

        response = await naive_search(
            plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id),
            types=["session"],
            limit=10,
            embedding_provider=provider,
        )
    finally:
        await client.close()

    # Every candidate any lane proposed was checked, including the denied one.
    assert "private_to_someone_else" in seen
    assert {result.id for result in response.results} <= set(seen)


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
async def test_naive_arm_filters_retired_rows_before_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.retrieval.candidates import VectorCandidateFetch

    current = _candidate("current")
    retired = _candidate("retired", metadata={"lifecycle_state": "superseded"})

    class Client:
        async def execute_query(self, _query: str, **_kwargs: object) -> list[object]:
            return []

    class Runtime:
        client = Client()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Runtime:
        return Runtime()

    async def node_fulltext(**_kwargs: object) -> list[RetrievalCandidate]:
        return [retired, current]

    async def episode_fulltext(**_kwargs: object) -> list[RetrievalCandidate]:
        return []

    async def vector_candidates(**_kwargs: object) -> VectorCandidateFetch:
        return VectorCandidateFetch(
            node_candidates=[retired, current],
            edge_candidates=[],
            requested=True,
            attempted=True,
        )

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(naive_module, "_node_fulltext_candidates", node_fulltext)
    monkeypatch.setattr(naive_module, "_episode_fulltext_candidates", episode_fulltext)
    monkeypatch.setattr(
        naive_module,
        "_vector_candidate_sources_detailed",
        vector_candidates,
    )

    response = await naive_search(plan=_plan(query=CORPUS_QUERY), limit=10)

    assert [result.id for result in response.results] == ["current"]
    assert response.filters["supersession_gate"] == {
        "lifecycle_dropped": 2,
        "superseded_dropped": 0,
        "superseded_uuids": [],
        "checked_candidates": 1,
        "edge_rows_read": 0,
    }


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
    # The module constants are the whole configuration surface, so enumerate
    # them rather than pattern-matching names a future knob could dodge. Adding
    # a name here is a deliberate act: NAIVE_LANE_OVERFETCH earns its place
    # because it sets how deep a lane reads and cannot reorder anything, so it
    # changes what fusion is allowed to see rather than how fusion scores it.
    assert {
        name
        for name in dir(naive_module)
        if name.isupper() and not name.startswith("_") and name != "TYPE_CHECKING"
    } == {"NAIVE_RRF_K", "NAIVE_RETRIEVAL_MODE", "MAX_RETRIEVAL_LIMIT"}


def test_no_caller_can_reweight_the_arm_per_request() -> None:
    """A tuned control measures nothing, so the entry point takes no knobs.

    ``fuse_naive_candidates`` keeps a ``k`` keyword for direct unit testing, but
    nothing reachable from the API or bench surface can set it: ``naive_search``
    neither accepts a fusion parameter nor forwards one.
    """

    parameters = set(inspect.signature(naive_module.naive_search).parameters)

    assert parameters == {
        "plan",
        "types",
        "facet",
        "limit",
        "include_content",
        "embedding_provider",
        "char_budget",
        "content_max_chars",
    }
    source = inspect.getsource(naive_module.naive_search)
    assert "fuse_naive_candidates(filtered_lists, limit=limit)" in source


# ---------------------------------------------------------------------------
# The arm governs the whole pack, and never hands off to the machine
# ---------------------------------------------------------------------------


def _machine_search_response(query: str) -> Any:
    from sibyl_core.tools.responses import SearchResponse, SearchResult

    return SearchResponse(
        results=[
            SearchResult(
                id="machine_row",
                type="note",
                name="machine row",
                content="this came from the eight-lane machine",
                score=0.9,
                result_origin="graph",
            )
        ],
        total=1,
        query=query,
        filters={},
        graph_count=1,
        limit=10,
    )


@pytest.mark.asyncio
async def test_a_failed_arm_raises_instead_of_serving_machine_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mislabelled data point is worse than a missing one.

    Every other retrieval failure in compile_context falls back to the machine.
    Under the arm that fallback would return eight-lane contents wearing the
    arm's label, and a race whose labels can be wrong measures nothing.
    """

    from sibyl_core.tools import context as context_module

    machine_calls: list[str] = []

    async def exploding_arm(**_kwargs: object) -> Any:
        raise RuntimeError("naive arm is down")

    async def machine_search(**kwargs: object) -> Any:
        machine_calls.append(str(kwargs.get("query")))
        return _machine_search_response(str(kwargs.get("query")))

    monkeypatch.setattr(context_module, "_compile_native_sections", exploding_arm)

    with pytest.raises(RuntimeError, match="naive arm is down"):
        await context_module.compile_context(
            goal=CORPUS_QUERY,
            organization_id=f"{ORG_ID}-failure",
            principal_id=PRINCIPAL_ID,
            naive_retrieval=True,
            search_fn=machine_search,
            record_exposure=False,
            include_related=False,
        )

    assert machine_calls == []


@pytest.mark.asyncio
async def test_the_machine_still_falls_back_when_the_arm_is_not_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fail-loud rule is scoped to the arm and must not change the default."""

    from sibyl_core.tools import context as context_module

    machine_calls: list[str] = []

    async def exploding_native(**_kwargs: object) -> Any:
        raise RuntimeError("native retrieval is down")

    async def machine_search(**kwargs: object) -> Any:
        machine_calls.append(str(kwargs.get("query")))
        return _machine_search_response(str(kwargs.get("query")))

    monkeypatch.setattr(context_module, "_compile_native_sections", exploding_native)

    pack = await context_module.compile_context(
        goal=CORPUS_QUERY,
        organization_id=f"{ORG_ID}-fallback",
        principal_id=PRINCIPAL_ID,
        search_fn=machine_search,
        record_exposure=False,
        include_related=False,
    )

    assert len(machine_calls) == 1
    assert [item.id for section in pack.sections for item in section.items] == ["machine_row"]


@pytest.mark.asyncio
async def test_the_arm_suppresses_active_work_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active work is a separate retrieval, so the arm must not inherit it."""

    from sibyl_core.tools import context as context_module

    active_calls: list[str] = []

    async def empty_sections(**_kwargs: object) -> list[Any]:
        return []

    async def active_work(**kwargs: object) -> list[Any]:
        active_calls.append(str(kwargs.get("project")))
        return []

    monkeypatch.setattr(context_module, "_compile_native_sections", empty_sections)

    await context_module.compile_context(
        goal=CORPUS_QUERY,
        organization_id=f"{ORG_ID}-active",
        principal_id=PRINCIPAL_ID,
        project=PROJECT_ID,
        accessible_projects={PROJECT_ID},
        intent="build",
        naive_retrieval=True,
        active_work_fn=active_work,
        record_exposure=False,
        include_related=False,
    )
    assert active_calls == []

    await context_module.compile_context(
        goal=CORPUS_QUERY,
        organization_id=f"{ORG_ID}-active",
        principal_id=PRINCIPAL_ID,
        project=PROJECT_ID,
        accessible_projects={PROJECT_ID},
        intent="build",
        active_work_fn=active_work,
        record_exposure=False,
        include_related=False,
    )
    # The machine still enriches, so the suppression above is the arm's doing
    # rather than a request shape that never reached the lookup.
    assert active_calls == [PROJECT_ID]


# ---------------------------------------------------------------------------
# Round-two hardening: lane depth, lane failure, and what the response discloses
# ---------------------------------------------------------------------------


def test_a_deep_cross_lane_agreement_survives_the_default_limit() -> None:
    """The boundary a limit-scaled overfetch leaves alive.

    At the bench's default limit of 12, reading four times the limit stops each
    lane at 48. A row ranked 49 in both lanes scores 2/(60+49), which beats the
    1/(60+1) of a row ranked first in one lane, so fusion should promote it and
    a 48-row pool would discard it first. Reading to the ceiling removes the
    class rather than moving it.
    """

    deep_rank = 49
    lexical = [_candidate(f"filler_lex_{index}") for index in range(deep_rank - 1)]
    dense = [_candidate(f"filler_vec_{index}") for index in range(deep_rank - 1)]
    lexical.append(_candidate("agreed_deep"))
    dense.append(_candidate("agreed_deep"))

    fused = fuse_naive_candidates(
        [(RetrievalSignal.NODE_FULLTEXT, lexical), (RetrievalSignal.NODE_VECTOR, dense)],
        limit=1,
    )

    assert fused[0][0].id == "agreed_deep"
    assert fused[0][1] == pytest.approx(2 / (NAIVE_RRF_K + deep_rank))
    assert fused[0][1] > 1 / (NAIVE_RRF_K + 1)


@pytest.mark.asyncio
async def test_every_lane_reads_to_the_ceiling_whatever_the_caller_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depths: list[int] = []

    async def recording_fulltext(**kwargs: Any) -> list[RetrievalCandidate]:
        depths.append(int(kwargs["limit"]))
        return []

    async def recording_vector(**kwargs: Any) -> Any:
        depths.append(int(kwargs["plan"].candidate_limits.node_vector))
        from sibyl_core.retrieval.candidates import VectorCandidateFetch

        return VectorCandidateFetch(
            node_candidates=[], edge_candidates=[], requested=True, attempted=True
        )

    class Runtime:
        client = object()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
        return Runtime()

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(naive_module, "_node_fulltext_candidates", recording_fulltext)
    monkeypatch.setattr(naive_module, "_episode_fulltext_candidates", recording_fulltext)
    monkeypatch.setattr(naive_module, "_vector_candidate_sources_detailed", recording_vector)

    await naive_search(plan=_plan(query=CORPUS_QUERY, limit=1), limit=1)

    assert depths and set(depths) == {search_module.MAX_RETRIEVAL_LIMIT}


@pytest.mark.asyncio
async def test_a_dead_lane_fails_the_request_instead_of_thinning_the_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thin pack scores as a recall miss and blames the arm for an outage."""

    async def dead_lane(**_kwargs: object) -> list[RetrievalCandidate]:
        raise RuntimeError("fulltext lane is down")

    client, runtime = await _embedded_runtime(f"{ORG_ID}-deadlane")
    provider = _embedding_provider()
    try:
        await _seed_corpus(client, provider, group_id=client.group_id)

        async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
        monkeypatch.setattr(naive_module, "_node_fulltext_candidates", dead_lane)

        with pytest.raises(RuntimeError, match="fulltext lane is down"):
            await naive_search(
                plan=_plan(query=CORPUS_QUERY, organization_id=client.group_id),
                types=["session"],
                limit=10,
                embedding_provider=provider,
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_degraded_vector_lane_fails_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coming back degraded is still not having run."""

    from sibyl_core.retrieval.candidates import VectorCandidateFetch

    async def degraded_vector(**_kwargs: object) -> VectorCandidateFetch:
        return VectorCandidateFetch(
            node_candidates=[],
            edge_candidates=[],
            requested=True,
            attempted=True,
            failures=("node_vector:TimeoutError",),
        )

    class Runtime:
        client = object()

    async def fake_runtime(_organization_id: str, **_kwargs: object) -> Any:
        return Runtime()

    async def empty_lane(**_kwargs: object) -> list[RetrievalCandidate]:
        return []

    monkeypatch.setattr(search_module, "get_surreal_graph_runtime", fake_runtime)
    monkeypatch.setattr(naive_module, "_node_fulltext_candidates", empty_lane)
    monkeypatch.setattr(naive_module, "_episode_fulltext_candidates", empty_lane)
    monkeypatch.setattr(naive_module, "_vector_candidate_sources_detailed", degraded_vector)

    with pytest.raises(RuntimeError, match="vector lane degraded"):
        await naive_search(plan=_plan(query=CORPUS_QUERY), limit=10)


@pytest.mark.asyncio
async def test_the_response_publishes_no_stage_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durations measured before the scope filter are a timing oracle.

    The candidate fetch runs before authorization, so a caller who may read
    none of the matches could still separate "nothing matched" from "matches
    exist but are not yours" by timing repeated queries. The counts were
    rebuilt from the authorized set for that reason; the clock would put the
    signal straight back.
    """

    client, runtime = await _embedded_runtime(f"{ORG_ID}-timings")
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

    assert "stage_timings_ms" not in response.filters
    assert not [key for key in response.filters if "timing" in key or key.endswith("_ms")]
