"""Replay LongMemEval live reports with alternate final ranking strategies."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sibyl_core.evals.longmemeval import (
    USER_AND_ASSISTANT_CORPUS_TEXT_POLICY,
    average_metric,
    build_longmemeval_corpus,
    score_longmemeval_ranking,
)
from sibyl_core.retrieval.query_ranking import (
    QueryCoverageCandidate,
    rank_by_query_coverage,
)
from sibyl_core.retrieval.reranking import (
    FeatureRerankCandidate,
    FeatureWeightedRerankModel,
    fit_feature_weighted_reranker,
    rerank_by_feature_weights,
)
from sibyl_core.retrieval.temporal import parse_temporal_datetime, resolve_temporal_reference

ReplayStrategy = Literal["identity", "heuristic", "coverage", "learned", "oracle"]

STOP_WORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "have",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "please",
    "should",
    "that",
    "the",
    "this",
    "to",
    "upcoming",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
    "your",
}

PREFERENCE_TERMS = {
    "activity",
    "activities",
    "advice",
    "advic",
    "choose",
    "dinner",
    "hotel",
    "inspiration",
    "movie",
    "recommend",
    "recommendation",
    "serve",
    "show",
    "suggest",
    "suggestion",
    "tip",
    "tips",
}
RECENCY_TERMS = {
    "changed",
    "current",
    "currently",
    "decrease",
    "decreased",
    "increase",
    "increased",
    "latest",
    "most",
    "new",
    "newer",
    "now",
    "recent",
    "recently",
    "updated",
}
TEMPORAL_TERMS = {
    "after",
    "ago",
    "before",
    "between",
    "earlier",
    "first",
    "last",
    "later",
    "order",
    "sequence",
    "then",
}
MULTI_EVIDENCE_TERMS = {
    "all",
    "between",
    "both",
    "count",
    "many",
    "number",
    "order",
    "sequence",
    "siblings",
    "total",
}
PREFERENCE_PATTERNS = (
    re.compile(
        r"\bi (?:really |usually |always |never |still )?"
        r"(?:prefer|like|love|enjoy|want|need|hate|dislike)\b"
    ),
    re.compile(r"\bmy (?:favorite|preferred|ideal)\b"),
    re.compile(r"\bi'm (?:fond of|a fan of|into)\b"),
    re.compile(r"\bi tend to\b"),
)
PERSONAL_PATTERN = re.compile(r"\b(i|i'm|i've|i'd|me|my|mine|we|our)\b", re.IGNORECASE)
GENERIC_ASSISTANT_PATTERNS = (
    re.compile(r"\bas an ai\b", re.IGNORECASE),
    re.compile(r"\bi (?:can|cannot|can't) (?:help|assist)\b", re.IGNORECASE),
    re.compile(r"\bhere are (?:some|a few)\b", re.IGNORECASE),
)
TOKEN_PATTERN = re.compile(r"[a-z0-9']+")


@dataclass(frozen=True)
class ReplaySummary:
    strategy: ReplayStrategy
    overall: dict[str, float]
    baseline_overall: dict[str, float]
    delta: dict[str, float]
    per_type: dict[str, dict[str, float]]
    baseline_per_type: dict[str, dict[str, float]]
    improved_cases: int
    regressed_cases: int
    changed_cases: int
    case_results: list[dict[str, Any]]


@dataclass(frozen=True)
class _Candidate:
    session_id: str
    original_rank: int
    score: float
    text: str
    timestamp: str
    tokens: tuple[str, ...]
    token_set: frozenset[str]


@dataclass(frozen=True)
class _Intents:
    preference: bool
    personal: bool
    temporal: bool
    recent: bool
    multi_evidence: bool
    target_date: datetime | None


def load_longmemeval_replay_inputs(
    report_path: str | Path,
    *,
    dataset_path: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report_file = Path(report_path)
    report = json.loads(report_file.read_text(encoding="utf-8"))

    dataset_file = Path(dataset_path) if dataset_path is not None else _dataset_path(report)
    if not dataset_file.is_absolute():
        dataset_file = Path.cwd() / dataset_file
    dataset = json.loads(dataset_file.read_text(encoding="utf-8"))
    if not isinstance(dataset, list):
        msg = f"Expected LongMemEval dataset list at {dataset_file}"
        raise ValueError(msg)
    return report, dataset


def replay_longmemeval_report(
    report: Mapping[str, Any],
    dataset: Sequence[Mapping[str, Any]],
    *,
    strategy: ReplayStrategy = "heuristic",
    k_values: Sequence[int] | None = None,
    corpus_text_policy: str | None = None,
    learned_model: FeatureWeightedRerankModel | None = None,
) -> ReplaySummary:
    k_list = [int(value) for value in (k_values or report.get("k_values") or [5, 10])]
    text_policy = _resolve_corpus_text_policy(report, corpus_text_policy)
    auto_fit_learned = strategy == "learned" and learned_model is None
    case_results: list[dict[str, Any]] = []
    for case in report.get("case_results", []):
        if not isinstance(case, Mapping):
            continue
        case_index = int(case["case_index"])
        entry = dataset[case_index]
        original_ranked = [str(session_id) for session_id in case.get("ranked_session_ids", [])]
        case_learned_model = learned_model
        if auto_fit_learned:
            case_learned_model = fit_longmemeval_rerank_model(
                report,
                dataset,
                corpus_text_policy=text_policy,
                exclude_case_index=case_index,
            )
        reranked = rerank_longmemeval_case(
            case,
            entry,
            strategy=strategy,
            corpus_text_policy=text_policy,
            learned_model=case_learned_model,
        )
        answers = sorted(str(value) for value in case.get("answer_session_ids", []))
        metrics = score_longmemeval_ranking(reranked, answers, k_list)
        original_metrics = score_longmemeval_ranking(original_ranked, answers, k_list)
        top_k = min(k_list)
        case_results.append(
            {
                "case_index": case_index,
                "question_id": case.get("question_id"),
                "question_type": case.get("question_type"),
                "question": case.get("question"),
                "answer_session_ids": answers,
                "baseline_ranked_session_ids": original_ranked,
                "reranked_session_ids": reranked,
                "baseline_answer_ranks": _answer_ranks(original_ranked, answers),
                "reranked_answer_ranks": _answer_ranks(reranked, answers),
                "changed": original_ranked != reranked,
                "improved": metrics[f"recall@{top_k}"] > original_metrics[f"recall@{top_k}"],
                "regressed": metrics[f"recall@{top_k}"] < original_metrics[f"recall@{top_k}"],
                **metrics,
                **{f"baseline_{key}": value for key, value in original_metrics.items()},
            }
        )

    overall = _aggregate_case_metrics(case_results, k_list)
    baseline_overall = _aggregate_case_metrics(case_results, k_list, prefix="baseline_")
    per_type = _aggregate_per_type(case_results, k_list)
    baseline_per_type = _aggregate_per_type(case_results, k_list, prefix="baseline_")
    return ReplaySummary(
        strategy=strategy,
        overall=overall,
        baseline_overall=baseline_overall,
        delta={
            metric: overall.get(metric, 0.0) - baseline_overall.get(metric, 0.0)
            for metric in sorted(set(overall) | set(baseline_overall))
        },
        per_type=per_type,
        baseline_per_type=baseline_per_type,
        improved_cases=sum(1 for case in case_results if case["improved"]),
        regressed_cases=sum(1 for case in case_results if case["regressed"]),
        changed_cases=sum(1 for case in case_results if case["changed"]),
        case_results=case_results,
    )


def replay_longmemeval_report_path(
    report_path: str | Path,
    *,
    dataset_path: str | Path | None = None,
    strategy: ReplayStrategy = "heuristic",
    k_values: Sequence[int] | None = None,
    learned_model: FeatureWeightedRerankModel | None = None,
) -> ReplaySummary:
    report, dataset = load_longmemeval_replay_inputs(report_path, dataset_path=dataset_path)
    return replay_longmemeval_report(
        report,
        dataset,
        strategy=strategy,
        k_values=k_values,
        learned_model=learned_model,
    )


def rerank_longmemeval_case(
    case_result: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    strategy: ReplayStrategy,
    corpus_text_policy: str,
    learned_model: FeatureWeightedRerankModel | None = None,
) -> list[str]:
    ranked_session_ids = [
        str(session_id) for session_id in case_result.get("ranked_session_ids", [])
    ]
    if strategy == "identity" or not ranked_session_ids:
        return ranked_session_ids

    if strategy == "oracle":
        answers = {str(session_id) for session_id in case_result.get("answer_session_ids", [])}
        return sorted(ranked_session_ids, key=lambda session_id: session_id not in answers)

    candidates = _case_candidates(
        case_result,
        entry,
        ranked_session_ids=ranked_session_ids,
        corpus_text_policy=corpus_text_policy,
    )
    query = str(case_result.get("question") or "")
    if strategy == "coverage":
        temporal_target = resolve_temporal_reference(
            query,
            parse_temporal_datetime(str(case_result.get("question_date") or "")),
        )
        ranking = rank_by_query_coverage(
            query,
            [
                QueryCoverageCandidate(
                    item=candidate,
                    stable_id=candidate.session_id,
                    text=candidate.text,
                    prior_score=candidate.score,
                    original_rank=candidate.original_rank,
                    timestamp=candidate.timestamp,
                )
                for candidate in candidates
            ],
            temporal_target=temporal_target,
        )
        return [ranked.item.session_id for ranked in ranking.ranked]

    intents = _detect_intents(
        query,
        question_type=str(case_result.get("question_type") or ""),
        reference_time=str(case_result.get("question_date") or ""),
    )
    if strategy == "learned":
        if learned_model is None:
            msg = "learned LongMemEval replay requires a FeatureWeightedRerankModel"
            raise ValueError(msg)
        return _learned_feature_rerank(query, candidates, intents, learned_model)

    scored = _score_candidates(query, candidates, intents)
    if intents.multi_evidence:
        return _diversify_ranking(scored)
    return [candidate.session_id for _, candidate in scored]


def longmemeval_rerank_feature_rows(
    case_result: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    corpus_text_policy: str,
) -> list[dict[str, Any]]:
    ranked_session_ids = [
        str(session_id) for session_id in case_result.get("ranked_session_ids", [])
    ]
    candidates = _case_candidates(
        case_result,
        entry,
        ranked_session_ids=ranked_session_ids,
        corpus_text_policy=corpus_text_policy,
    )
    query = str(case_result.get("question") or "")
    intents = _detect_intents(
        query,
        question_type=str(case_result.get("question_type") or ""),
        reference_time=str(case_result.get("question_date") or ""),
    )
    answers = {str(session_id) for session_id in case_result.get("answer_session_ids", [])}
    return _candidate_feature_rows(query, candidates, intents, answers)


def fit_longmemeval_rerank_model(
    report: Mapping[str, Any],
    dataset: Sequence[Mapping[str, Any]],
    *,
    corpus_text_policy: str | None = None,
    exclude_case_index: int | None = None,
) -> FeatureWeightedRerankModel:
    text_policy = _resolve_corpus_text_policy(report, corpus_text_policy)
    rows: list[dict[str, Any]] = []
    for case in report.get("case_results", []):
        if not isinstance(case, Mapping):
            continue
        case_index = int(case["case_index"])
        if exclude_case_index is not None and case_index == exclude_case_index:
            continue
        rows.extend(
            longmemeval_rerank_feature_rows(
                case,
                dataset[case_index],
                corpus_text_policy=text_policy,
            )
        )
    return fit_feature_weighted_reranker(rows, name="longmemeval-feature-linear-v1")


def summary_to_dict(summary: ReplaySummary, *, include_cases: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "strategy": summary.strategy,
        "baseline_overall": summary.baseline_overall,
        "overall": summary.overall,
        "delta": summary.delta,
        "baseline_per_type": summary.baseline_per_type,
        "per_type": summary.per_type,
        "improved_cases": summary.improved_cases,
        "regressed_cases": summary.regressed_cases,
        "changed_cases": summary.changed_cases,
    }
    if include_cases:
        payload["case_results"] = summary.case_results
    return payload


def _dataset_path(report: Mapping[str, Any]) -> Path:
    path = _nested_get(report, ("dataset", "path"))
    if not isinstance(path, str) or not path:
        msg = "LongMemEval report does not include dataset.path; pass --dataset explicitly"
        raise ValueError(msg)
    return Path(path)


def _resolve_corpus_text_policy(
    report: Mapping[str, Any],
    corpus_text_policy: str | None,
) -> str:
    return corpus_text_policy or str(
        _nested_get(report, ("dataset", "corpus_text_policy"))
        or USER_AND_ASSISTANT_CORPUS_TEXT_POLICY
    )


def _nested_get(data: Mapping[str, Any], keys: Sequence[str]) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _answer_ranks(
    ranked_session_ids: Sequence[str],
    answer_session_ids: Sequence[str],
) -> list[dict[str, int | str | None]]:
    ranks: list[dict[str, int | str | None]] = []
    for session_id in answer_session_ids:
        rank = (
            ranked_session_ids.index(session_id) + 1 if session_id in ranked_session_ids else None
        )
        ranks.append({"session_id": session_id, "rank": rank})
    return ranks


def _score_by_session_id(case_result: Mapping[str, Any]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for result in case_result.get("ranked_results", []):
        if not isinstance(result, Mapping):
            continue
        session_id = result.get("longmemeval_session_id")
        if not isinstance(session_id, str):
            continue
        raw_score = result.get("score")
        scores[session_id] = float(raw_score) if isinstance(raw_score, int | float) else 0.0
    return scores


def _case_candidates(
    case_result: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    ranked_session_ids: Sequence[str],
    corpus_text_policy: str,
) -> list[_Candidate]:
    text_by_session_id = {
        document.session_id: (document.text, document.timestamp)
        for document in build_longmemeval_corpus(entry, text_policy=corpus_text_policy)
    }
    score_by_session_id = _score_by_session_id(case_result)
    candidates: list[_Candidate] = []
    for index, session_id in enumerate(ranked_session_ids):
        text, timestamp = text_by_session_id.get(session_id, ("", ""))
        tokens = tuple(_tokenize(text))
        candidates.append(
            _Candidate(
                session_id=session_id,
                original_rank=index + 1,
                score=score_by_session_id.get(session_id, 0.0),
                text=text,
                timestamp=timestamp,
                tokens=tokens,
                token_set=frozenset(tokens),
            )
        )
    return candidates


def _detect_intents(
    query: str,
    *,
    question_type: str,
    reference_time: str,
) -> _Intents:
    words = set(_tokenize(query, keep_stopwords=True))
    preference = question_type == "single-session-preference" or bool(words & PREFERENCE_TERMS)
    personal = bool({"i", "me", "my", "mine"} & words)
    temporal = question_type in {"knowledge-update", "temporal-reasoning"} or bool(
        words & (RECENCY_TERMS | TEMPORAL_TERMS)
    )
    recent = (
        question_type == "knowledge-update"
        or "most recently" in query.lower()
        or bool(words & RECENCY_TERMS)
    )
    multi_evidence = question_type in {"multi-session", "temporal-reasoning"} or bool(
        words & MULTI_EVIDENCE_TERMS
    )
    target_date = _target_date_from_query(query, _parse_datetime(reference_time))
    return _Intents(
        preference=preference,
        personal=personal,
        temporal=temporal,
        recent=recent,
        multi_evidence=multi_evidence,
        target_date=target_date,
    )


def _score_candidates(
    query: str,
    candidates: Sequence[_Candidate],
    intents: _Intents,
) -> list[tuple[float, _Candidate]]:
    feature_rows = _candidate_feature_rows(query, candidates, intents, set())
    scored = [
        (float(row["heuristic_score"]), candidate)
        for row, candidate in zip(feature_rows, candidates, strict=True)
    ]
    return sorted(scored, key=lambda item: (-item[0], item[1].original_rank))


def _learned_feature_rerank(
    query: str,
    candidates: Sequence[_Candidate],
    intents: _Intents,
    model: FeatureWeightedRerankModel,
) -> list[str]:
    feature_rows = _candidate_feature_rows(query, candidates, intents, set())
    ranked = rerank_by_feature_weights(
        [
            FeatureRerankCandidate(
                item=str(row["session_id"]),
                stable_id=str(row["session_id"]),
                features=row["features"],
                original_rank=int(row["original_rank"]),
            )
            for row in feature_rows
        ],
        model,
    )
    return [ranked_candidate.item for ranked_candidate in ranked]


def _candidate_feature_rows(
    query: str,
    candidates: Sequence[_Candidate],
    intents: _Intents,
    answer_session_ids: set[str],
) -> list[dict[str, Any]]:
    query_tokens = set(_tokenize(query))
    max_original_score = max((candidate.score for candidate in candidates), default=0.0) or 1.0
    coverage_features_by_rank = _query_coverage_features_by_original_rank(
        query,
        candidates,
        intents,
    )
    total = max(1, len(candidates) - 1)
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        features = _candidate_features(
            query_tokens=query_tokens,
            candidate=candidate,
            intents=intents,
            max_original_score=max_original_score,
            rank_span=total,
        )
        features.update(
            coverage_features_by_rank.get(
                candidate.original_rank,
                _empty_query_coverage_features(),
            )
        )
        rows.append(
            {
                "session_id": candidate.session_id,
                "label": int(candidate.session_id in answer_session_ids),
                "original_rank": candidate.original_rank,
                "prior_score": candidate.score,
                "heuristic_score": _heuristic_score_from_features(features, intents),
                "features": features,
            }
        )
    return rows


def _query_coverage_features_by_original_rank(
    query: str,
    candidates: Sequence[_Candidate],
    intents: _Intents,
) -> dict[int, dict[str, float]]:
    ranking = rank_by_query_coverage(
        query,
        [
            QueryCoverageCandidate(
                item=candidate.original_rank,
                stable_id=f"{candidate.session_id}:{candidate.original_rank}",
                text=candidate.text,
                prior_score=candidate.score,
                original_rank=candidate.original_rank,
                timestamp=candidate.timestamp,
            )
            for candidate in candidates
        ],
        temporal_target=intents.target_date,
    )
    rank_span = max(1, len(ranking.ranked) - 1)
    return {
        int(ranked.item): {
            "query_coverage_score": ranked.score,
            "query_coverage_overlap": ranked.overlap,
            "query_coverage_rank": float(index + 1),
            "query_coverage_rank_score": 1.0 - (index / rank_span),
            "query_coverage_applied": _bool_score(ranking.applied),
            "query_coverage_changed": _bool_score(ranking.changed),
        }
        for index, ranked in enumerate(ranking.ranked)
    }


def _empty_query_coverage_features() -> dict[str, float]:
    return {
        "query_coverage_score": 0.0,
        "query_coverage_overlap": 0.0,
        "query_coverage_rank": 0.0,
        "query_coverage_rank_score": 0.0,
        "query_coverage_applied": 0.0,
        "query_coverage_changed": 0.0,
    }


def _candidate_features(
    *,
    query_tokens: set[str],
    candidate: _Candidate,
    intents: _Intents,
    max_original_score: float,
    rank_span: int,
) -> dict[str, float]:
    text = candidate.text
    text_tokens = candidate.tokens
    text_token_set = candidate.token_set
    query_token_count = max(1, len(query_tokens))
    overlap = len(query_tokens & text_token_set) / query_token_count
    return {
        "original_rank_score": 1.0 - ((candidate.original_rank - 1) / rank_span),
        "provider_score": candidate.score / max_original_score if candidate.score > 0 else 0.0,
        "query_overlap": overlap,
        "query_density": sum(1 for token in text_tokens if token in query_tokens)
        / max(1.0, math.sqrt(len(text_tokens))),
        "stem_overlap": overlap,
        "preference_marker_count": float(_preference_marker_count(text)),
        "generic_assistant_count": float(_generic_assistant_count(text)),
        "personal_marker": _bool_score(PERSONAL_PATTERN.search(text)),
        "temporal_score": _temporal_score(candidate.timestamp, intents),
        "intent_preference": _bool_score(intents.preference),
        "intent_personal": _bool_score(intents.personal),
        "intent_temporal": _bool_score(intents.temporal),
        "intent_recent": _bool_score(intents.recent),
        "intent_multi_evidence": _bool_score(intents.multi_evidence),
        "token_count": float(len(text_tokens)),
        "query_token_count": float(len(query_tokens)),
    }


def _heuristic_score_from_features(features: Mapping[str, float], intents: _Intents) -> float:
    score = (0.72 * features.get("original_rank_score", 0.0)) + (
        0.12 * features.get("provider_score", 0.0)
    )
    score += 0.24 * features.get("query_overlap", 0.0)
    score += 0.035 * features.get("query_density", 0.0)

    if intents.preference:
        preference_markers = min(3.0, features.get("preference_marker_count", 0.0))
        stem_overlap = features.get("stem_overlap", 0.0)
        preference_evidence = preference_markers * (0.18 + (0.82 * stem_overlap))
        score += preference_evidence
        score += 0.08 * stem_overlap * features.get("personal_marker", 0.0)
        if preference_markers == 0:
            score -= 0.18 * features.get("generic_assistant_count", 0.0)
    elif intents.personal:
        score += 0.06 * features.get("personal_marker", 0.0)
        score -= 0.08 * features.get("generic_assistant_count", 0.0)

    if intents.temporal:
        score += features.get("temporal_score", 0.0)

    return score


def _diversify_ranking(scored: Sequence[tuple[float, _Candidate]]) -> list[str]:
    if len(scored) <= 5:
        return [candidate.session_id for _, candidate in scored]

    remaining = list(scored)
    selected: list[tuple[float, _Candidate]] = []
    while remaining and len(selected) < min(10, len(scored)):
        best_index = 0
        best_value = float("-inf")
        for index, (score, candidate) in enumerate(remaining):
            novelty = _novelty(
                candidate, [selected_candidate for _, selected_candidate in selected]
            )
            value = (0.86 * score) + (0.14 * novelty)
            if value > best_value:
                best_value = value
                best_index = index
        selected.append(remaining.pop(best_index))

    return [candidate.session_id for _, candidate in selected] + [
        candidate.session_id for _, candidate in remaining
    ]


def _novelty(candidate: _Candidate, selected: Sequence[_Candidate]) -> float:
    if not selected:
        return 1.0
    candidate_tokens = candidate.token_set
    if not candidate_tokens:
        return 0.0
    similarities = []
    for selected_candidate in selected:
        selected_tokens = selected_candidate.token_set
        if not selected_tokens:
            continue
        similarities.append(
            len(candidate_tokens & selected_tokens) / len(candidate_tokens | selected_tokens)
        )
    return 1.0 - max(similarities, default=0.0)


def _temporal_score(timestamp: str, intents: _Intents) -> float:
    doc_time = _parse_datetime(timestamp)
    if doc_time is None:
        return 0.0
    if intents.target_date is not None:
        days = abs((doc_time.date() - intents.target_date.date()).days)
        return 0.22 / (1.0 + (days / 7.0))
    if intents.recent:
        return 0.1
    return 0.0


def _target_date_from_query(query: str, reference_time: datetime | None) -> datetime | None:
    if reference_time is None:
        return None
    match = re.search(
        r"\b(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
        r"(?P<unit>day|days|week|weeks|month|months|year|years)\s+ago\b",
        query.lower(),
    )
    if match is None:
        return None
    count = _number_word(match.group("count"))
    unit = match.group("unit")
    days = count
    if unit.startswith("week"):
        days *= 7
    elif unit.startswith("month"):
        days *= 30
    elif unit.startswith("year"):
        days *= 365
    return reference_time - timedelta(days=days)


def _number_word(value: str) -> int:
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
    }
    if value in words:
        return words[value]
    if value.isdigit():
        return int(value)
    return 0


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.replace(tzinfo=None)
        return parsed
    return None


def _preference_marker_count(text: str) -> int:
    return sum(1 for pattern in PREFERENCE_PATTERNS if pattern.search(text.lower()))


def _generic_assistant_count(text: str) -> int:
    return sum(1 for pattern in GENERIC_ASSISTANT_PATTERNS if pattern.search(text))


def _bool_score(value: object) -> float:
    return 1.0 if value else 0.0


def _tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    tokens = [_normalize_token(token.strip("'").lower()) for token in TOKEN_PATTERN.findall(text)]
    if keep_stopwords:
        return [token for token in tokens if token]
    return [token for token in tokens if token and token not in STOP_WORDS and len(token) > 1]


def _normalize_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _aggregate_case_metrics(
    case_results: Sequence[Mapping[str, Any]],
    k_values: Sequence[int],
    *,
    prefix: str = "",
) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for k in k_values:
        for metric in ("hit", "legacy_recall", "recall", "ndcg"):
            key = f"{metric}@{k}"
            source_key = f"{prefix}{key}"
            metrics[key] = average_metric(case_results, source_key)
    return metrics


def _aggregate_per_type(
    case_results: Sequence[Mapping[str, Any]],
    k_values: Sequence[int],
    *,
    prefix: str = "",
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in case_results:
        grouped[str(result.get("question_type") or "unknown")].append(result)

    per_type: dict[str, dict[str, float]] = {}
    for question_type, results in sorted(grouped.items()):
        summary = _aggregate_case_metrics(results, k_values, prefix=prefix)
        summary["count"] = float(len(results))
        per_type[question_type] = summary
    return per_type
