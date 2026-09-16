"""Candidate fusion, ranking, and result materialization."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from sibyl_core.config import core_config
from sibyl_core.memory_pipeline.retrieval import CandidateSourceFailure
from sibyl_core.retrieval._search_candidates import _string_value
from sibyl_core.retrieval._search_database import _execute_query_records
from sibyl_core.retrieval._search_plan import (
    FusionBackend,
    RetrievalPlan,
    RetrievalSignal,
)
from sibyl_core.retrieval.candidates import RetrievalCandidate
from sibyl_core.retrieval.fusion import rrf_merge
from sibyl_core.retrieval.query_ranking import rank_items_by_query_coverage
from sibyl_core.retrieval.temporal import get_entity_timestamp, temporal_decay_multiplier

if TYPE_CHECKING:
    from sibyl_core.tools.responses import SearchResult

type FusedCandidate = tuple[RetrievalCandidate, float, dict[str, Any]]
type CoverageRanker = Callable[..., tuple[list[tuple[RetrievalCandidate, float]], bool, bool]]

DEFAULT_FUSION_BACKEND = FusionBackend.PYTHON_RRF
_ACTIVE_TASK_STATUSES = {"doing", "in_progress", "review"}
_GRAPH_EXPANSION_METADATA_KEYS = (
    "graph_expansion_depth",
    "graph_expansion_relationship",
    "graph_expansion_score",
    "graph_expansion_direction",
    "graph_expansion_community_id",
)
log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class FusionExecutionResult:
    candidates: list[FusedCandidate]
    actual_backend: FusionBackend


def _fuse_candidates(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    plan: RetrievalPlan,
    limit: int,
    temporal_target: datetime | None = None,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    return _rank_fused_candidates(
        source_lists,
        plan=plan,
        limit=limit,
        rrf_scores=_python_rrf_scores(source_lists, rrf_k=plan.weights.rrf_k),
        temporal_target=temporal_target,
    )


async def _fuse_candidates_for_plan(
    *,
    client: Any,
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    plan: RetrievalPlan,
    limit: int,
    temporal_target: datetime | None = None,
    fusion_backend: FusionBackend | None = None,
    fusion_failures: list[CandidateSourceFailure] | None = None,
) -> FusionExecutionResult:
    backend = fusion_backend or DEFAULT_FUSION_BACKEND
    if backend is FusionBackend.SURREAL_RRF:
        scores: dict[str, float] = {}
        try:
            scores = await _surreal_rrf_scores(client, source_lists, plan=plan, limit=limit)
        except Exception as exc:
            log.warning(
                "surreal_rrf_failed",
                organization_id=plan.organization_id,
                error_type=type(exc).__name__,
            )
            if fusion_failures is not None:
                fusion_failures.append(
                    CandidateSourceFailure(
                        source=backend.value,
                        error_type=type(exc).__name__,
                    )
                )
        if scores:
            return FusionExecutionResult(
                candidates=_rank_fused_candidates(
                    source_lists,
                    plan=plan,
                    limit=limit,
                    rrf_scores=scores,
                    backend=backend,
                    temporal_target=temporal_target,
                ),
                actual_backend=backend,
            )
        if any(candidates for _signal, candidates in source_lists):
            return FusionExecutionResult(
                candidates=_fuse_candidates(
                    source_lists,
                    plan=plan,
                    limit=limit,
                    temporal_target=temporal_target,
                ),
                actual_backend=FusionBackend.PYTHON_RRF,
            )
        return FusionExecutionResult(candidates=[], actual_backend=backend)
    return FusionExecutionResult(
        candidates=_fuse_candidates(
            source_lists,
            plan=plan,
            limit=limit,
            temporal_target=temporal_target,
        ),
        actual_backend=FusionBackend.PYTHON_RRF,
    )


def _python_rrf_scores(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    rrf_k: int,
) -> dict[str, float]:
    ranked_lists = [
        [(candidate, candidate.score) for candidate in candidates]
        for _signal, candidates in source_lists
    ]
    merged = rrf_merge(ranked_lists, k=float(rrf_k), dedup_key=lambda candidate: candidate.id)
    return {candidate.id: score for candidate, score in merged}


async def _surreal_rrf_scores(
    client: Any,
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    plan: RetrievalPlan,
    limit: int,
) -> dict[str, float]:
    rrf_inputs = [
        [
            {
                "id": candidate.id,
                "source_signal": signal.value,
                "score": candidate.score,
            }
            for candidate in candidates
        ]
        for signal, candidates in source_lists
    ]
    if not any(rrf_inputs):
        return {}
    unique_candidate_count = len(
        {candidate.id for _signal, candidates in source_lists for candidate in candidates}
    )
    rows = await _execute_query_records(
        client,
        "RETURN search::rrf($lists, $limit, $k);",
        lists=rrf_inputs,
        limit=max(int(limit), unique_candidate_count, 1),
        k=plan.weights.rrf_k,
    )

    scores: dict[str, float] = {}
    for row in rows:
        candidate_id = _string_value(row.get("id") or row.get("uuid") or row.get("record_id"))
        if not candidate_id:
            continue
        raw_score = row.get("rrf_score", row.get("rff_score", row.get("fuse_score")))
        if isinstance(raw_score, int | float):
            scores[candidate_id] = float(raw_score)
    return scores


def _rank_fused_candidates(
    source_lists: Sequence[tuple[RetrievalSignal, Sequence[RetrievalCandidate]]],
    *,
    plan: RetrievalPlan,
    limit: int,
    rrf_scores: Mapping[str, float],
    backend: FusionBackend = FusionBackend.PYTHON_RRF,
    temporal_target: datetime | None = None,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    score_by_id: dict[str, float] = defaultdict(float)
    candidates_by_id: dict[str, RetrievalCandidate] = {}
    metadata_by_id: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sources": [], "ranks": {}, "original_scores": {}, "fusion_backend": backend.value}
    )

    for signal, candidates in source_lists:
        for rank, candidate in enumerate(candidates, start=1):
            score_by_id[candidate.id] = float(rrf_scores.get(candidate.id, 0.0))
            candidates_by_id.setdefault(candidate.id, candidate)
            fusion_metadata = metadata_by_id[candidate.id]
            fusion_metadata["sources"].append(signal.value)
            fusion_metadata["ranks"][signal.value] = rank
            fusion_metadata["original_scores"][signal.value] = candidate.score
            if signal is RetrievalSignal.GRAPH_EXPANSION:
                _merge_graph_expansion_metadata(fusion_metadata, candidate)
            if signal is RetrievalSignal.EXACT_KEY:
                # Merged here, from the lane's own candidate instance, because
                # candidates_by_id keeps whichever lane saw the row first: a row
                # found by both full-text and the key would otherwise report an
                # empty match list off the full-text instance.
                _merge_exact_key_metadata(fusion_metadata, candidate)

    ranked: list[tuple[RetrievalCandidate, float, dict[str, Any]]] = []
    for candidate_id, score in score_by_id.items():
        candidate = candidates_by_id[candidate_id]
        fusion_metadata = metadata_by_id[candidate_id]
        demote_multiplier = _vector_only_demote_multiplier(
            plan,
            signals=fusion_metadata["sources"],
        )
        if demote_multiplier < 1.0:
            score *= demote_multiplier
            fusion_metadata["vector_only_demoted"] = True
            fusion_metadata["filter_selectivity"] = plan.filter_selectivity
            fusion_metadata["vector_only_demote_multiplier"] = demote_multiplier
        graph_multiplier = _graph_expansion_only_multiplier(
            plan,
            signals=fusion_metadata["sources"],
        )
        if graph_multiplier < 1.0:
            score *= graph_multiplier
            fusion_metadata["graph_expansion_only_demoted"] = True
            fusion_metadata["graph_expansion_only_multiplier"] = graph_multiplier
        graph_signal_multiplier = _graph_native_signal_multiplier(
            plan,
            signals=fusion_metadata["sources"],
            fusion_metadata=fusion_metadata,
        )
        if graph_signal_multiplier > 1.0:
            score *= graph_signal_multiplier
            fusion_metadata["graph_native_signal_boost"] = graph_signal_multiplier
        exact_key_multiplier = _exact_key_multiplier(plan, signals=fusion_metadata["sources"])
        if exact_key_multiplier > 1.0:
            score *= exact_key_multiplier
            fusion_metadata["exact_key_boost"] = exact_key_multiplier
        boosted, temporal_multiplier = _boost_score(
            candidate,
            score,
            plan=plan,
            temporal_target=temporal_target,
        )
        if temporal_multiplier != 1.0:
            fusion_metadata["temporal_decay_multiplier"] = temporal_multiplier
        ranked.append((candidate, boosted, fusion_metadata))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:limit]


def _merge_graph_expansion_metadata(
    fusion_metadata: dict[str, Any],
    candidate: RetrievalCandidate,
) -> None:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    for key in _GRAPH_EXPANSION_METADATA_KEYS:
        value = metadata.get(key)
        if value is not None:
            fusion_metadata[key] = value


def _merge_exact_key_metadata(
    fusion_metadata: dict[str, Any],
    candidate: RetrievalCandidate,
) -> None:
    metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
    matched = metadata.get("matched_retrieval_keys")
    if isinstance(matched, list | tuple):
        fusion_metadata["matched_retrieval_keys"] = [str(key) for key in matched]


def _candidate_query_text(
    candidate: RetrievalCandidate,
    *,
    matched_keys: Sequence[str] = (),
) -> str:
    # Only the keys this query actually matched, never the row's whole declared
    # list. The keys are here so the coverage re-rank cannot bury an exact-key
    # hit whose body never spells the token out, which needs the matched key and
    # nothing more. Folding in every declared key would move scores on queries
    # the arm never fired for, and would hand a writer an ungated ranking lever:
    # sixteen keys of prose keywords buying a permanent coverage lift.
    parts = [part for part in (candidate.name, candidate.content, *matched_keys) if part]
    return " ".join(parts).lower()


def _apply_query_coverage_to_fused(
    query: str,
    fused: list[tuple[RetrievalCandidate, float, dict[str, Any]]],
    *,
    temporal_target: datetime | None,
    ranker: CoverageRanker = rank_items_by_query_coverage,
) -> list[tuple[RetrievalCandidate, float, dict[str, Any]]]:
    """Re-rank fused context candidates through the shared query-coverage core.

    The native plan already produces a strong base order from RRF plus the
    in-DB-shaped demotions and boosts; that order is the prior the shared
    ranker refines, so context packs rank by the same scorer ``/api/search``
    uses while keeping the efficient DB-native candidate fetch upstream.
    """
    metadata_by_id = {
        id(candidate): fusion_metadata for candidate, _score, fusion_metadata in fused
    }
    # Fusion holds the matched keys, not the candidate, because a row reached by
    # two lanes keeps whichever instance arrived first. The coverage text is
    # therefore resolved through the fused metadata rather than off the row.
    matched_keys_by_id = {
        id(candidate): tuple(
            str(key) for key in (fusion_metadata.get("matched_retrieval_keys") or ())
        )
        for candidate, _score, fusion_metadata in fused
    }
    reranked, _applied, _refined = ranker(
        query,
        [(candidate, score) for candidate, score, _fusion_metadata in fused],
        text_fn=lambda candidate: _candidate_query_text(
            candidate,
            matched_keys=matched_keys_by_id.get(id(candidate), ()),
        ),
        id_fn=lambda candidate: candidate.id,
        timestamp_fn=lambda candidate: get_entity_timestamp(candidate) or candidate.created_at,
        temporal_target=temporal_target,
    )
    return [(candidate, score, metadata_by_id[id(candidate)]) for candidate, score in reranked]


def _vector_only_demote_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    if plan.filter_selectivity is None:
        return 1.0
    if plan.filter_selectivity >= plan.filter_selectivity_threshold:
        return 1.0
    if any(
        signal
        not in {
            RetrievalSignal.NODE_VECTOR.value,
            RetrievalSignal.EDGE_VECTOR.value,
        }
        for signal in signals
    ):
        return 1.0
    if plan.filter_selectivity_threshold <= 0:
        return 1.0
    return max(plan.filter_selectivity / plan.filter_selectivity_threshold, 0.1)


def _graph_expansion_only_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    if set(signals) != {RetrievalSignal.GRAPH_EXPANSION.value}:
        return 1.0
    return max(min(plan.weights.graph_expansion_only_boost, 1.0), 0.0)


def _exact_key_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
) -> float:
    """Lift a candidate whose writer declared one of the query's exact keys.

    Rank-only RRF would score this candidate like the top of any other lane,
    which is the failure that keeps a weak lexical arm out of fusion: an equal
    vote for an unequal signal. The boost is what makes the arm high-precision
    rather than merely present, and it applies to the candidate, not the lane,
    so a row found by both the key and the vector index is lifted once.
    """

    if RetrievalSignal.EXACT_KEY.value not in signals:
        return 1.0
    return max(plan.weights.exact_key_boost, 1.0)


def _graph_native_signal_multiplier(
    plan: RetrievalPlan,
    *,
    signals: Sequence[str],
    fusion_metadata: Mapping[str, Any],
) -> float:
    if RetrievalSignal.GRAPH_EXPANSION.value not in signals:
        return 1.0
    if set(signals) == {RetrievalSignal.GRAPH_EXPANSION.value}:
        return 1.0
    cap = max(plan.weights.graph_native_signal_boost_cap, 1.0)
    raw_path_score = fusion_metadata.get("graph_expansion_score")
    if not isinstance(raw_path_score, int | float):
        raw_scores = fusion_metadata.get("original_scores")
        if isinstance(raw_scores, Mapping):
            raw_path_score = raw_scores.get(RetrievalSignal.GRAPH_EXPANSION.value)
    if not isinstance(raw_path_score, int | float):
        return 1.0
    path_score = max(min(float(raw_path_score), 1.0), 0.0)
    if path_score <= 0.0:
        return 1.0
    return min(1.0 + path_score * (cap - 1.0), cap)


def _boost_score(
    candidate: RetrievalCandidate,
    score: float,
    *,
    plan: RetrievalPlan,
    temporal_target: datetime | None,
) -> tuple[float, float]:
    boosted = score
    status = _string_value(candidate.metadata.get("status"))
    if candidate.type == "task" and status in _ACTIVE_TASK_STATUSES:
        boosted *= plan.weights.active_task_state_boost
    if plan.project and candidate.project_id == plan.project:
        boosted *= plan.weights.project_match_boost
    if candidate.type == "raw_memory":
        boosted *= plan.weights.direct_raw_source_boost
    boosted *= _freshness_boost(candidate.created_at, cap=plan.weights.freshness_boost_cap)
    temporal_multiplier = (
        1.0
        if temporal_target is not None
        else temporal_decay_multiplier(
            candidate,
            decay_days=core_config.temporal_decay_days,
        )
    )
    boosted *= temporal_multiplier
    return boosted, temporal_multiplier


def _freshness_boost(created_at: datetime | None, *, cap: float) -> float:
    if created_at is None:
        return 1.0
    now = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age_days = max((now - created_at).total_seconds() / 86_400, 0.0)
    return min(cap, 1.0 + (0.5 / (1.0 + age_days)))


def _search_result_from_candidate(
    candidate: RetrievalCandidate,
    *,
    score: float,
    fusion_metadata: Mapping[str, Any],
    include_content: bool,
) -> SearchResult:
    from sibyl_core.tools.responses import SearchResult

    freshness = _freshness_boost(candidate.created_at, cap=1.5)
    metadata = {
        **candidate.contract_metadata(),
        "source_id": candidate.source or candidate.id,
        "visibility": candidate.visibility,
        "freshness": round(freshness, 4),
        "retrieval_signals": list(fusion_metadata.get("sources", [])),
        "retrieval_ranks": dict(fusion_metadata.get("ranks", {})),
        "retrieval_scores": dict(fusion_metadata.get("original_scores", {})),
        "policy_reason": candidate.policy_reason,
    }
    if fusion_metadata.get("vector_only_demoted"):
        metadata["vector_only_demoted"] = True
        metadata["filter_selectivity"] = fusion_metadata.get("filter_selectivity")
        metadata["vector_only_demote_multiplier"] = fusion_metadata.get(
            "vector_only_demote_multiplier"
        )
    if fusion_metadata.get("graph_expansion_only_demoted"):
        metadata["graph_expansion_only_demoted"] = True
        metadata["graph_expansion_only_multiplier"] = fusion_metadata.get(
            "graph_expansion_only_multiplier"
        )
    if fusion_metadata.get("graph_native_signal_boost"):
        metadata["graph_native_signal_boost"] = fusion_metadata.get("graph_native_signal_boost")
    if fusion_metadata.get("exact_key_boost"):
        metadata["exact_key_boost"] = fusion_metadata.get("exact_key_boost")
        metadata["matched_retrieval_keys"] = list(
            fusion_metadata.get("matched_retrieval_keys") or ()
        )
    if fusion_metadata.get("temporal_decay_multiplier") is not None:
        metadata["temporal_decay_multiplier"] = round(
            float(fusion_metadata["temporal_decay_multiplier"]),
            4,
        )
    for key in _GRAPH_EXPANSION_METADATA_KEYS:
        if key in fusion_metadata:
            metadata[key] = fusion_metadata[key]
    if candidate.project_id:
        metadata["project_id"] = candidate.project_id
    if candidate.created_at:
        metadata["created_at"] = candidate.created_at.isoformat()
    return SearchResult(
        id=candidate.id,
        source_revision=candidate.source_revision,
        type=candidate.type,
        name=candidate.name,
        content=candidate.content if include_content else "",
        score=score,
        source=candidate.source,
        result_origin="graph",
        metadata=metadata,
    )
