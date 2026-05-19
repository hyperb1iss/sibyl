"""LongMemEval corpus and scoring helpers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CORPUS_TEXT_POLICY = "user-turns-only-v1"


@dataclass(frozen=True)
class LongMemEvalCorpusDocument:
    session_id: str
    text: str
    timestamp: str = ""


def build_longmemeval_corpus(
    entry: Mapping[str, Any],
) -> list[LongMemEvalCorpusDocument]:
    documents: list[LongMemEvalCorpusDocument] = []
    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    timestamps = entry.get("haystack_dates", [])

    for index, (session, session_id) in enumerate(zip(sessions, session_ids, strict=False)):
        user_turns = [turn["content"] for turn in session if turn.get("role") == "user"]
        if not user_turns:
            continue
        timestamp = timestamps[index] if index < len(timestamps) else ""
        documents.append(
            LongMemEvalCorpusDocument(
                session_id=str(session_id),
                text="\n".join(user_turns),
                timestamp=str(timestamp),
            )
        )

    return documents


def dcg_at_k(relevances: list[float], k: int) -> float:
    return sum(rel / math.log2(index + 2) for index, rel in enumerate(relevances[:k]))


def hit_at_k(ranked_session_ids: list[str], answer_session_ids: set[str], k: int) -> float:
    top_k = set(ranked_session_ids[:k])
    return float(any(session_id in top_k for session_id in answer_session_ids))


def recall_at_k(ranked_session_ids: list[str], answer_session_ids: set[str], k: int) -> float:
    if not answer_session_ids:
        return 0.0
    top_k = set(ranked_session_ids[:k])
    return len(top_k & answer_session_ids) / len(answer_session_ids)


def ndcg_at_k(ranked_session_ids: list[str], answer_session_ids: set[str], k: int) -> float:
    if not answer_session_ids:
        return 0.0

    relevances = [
        1.0 if session_id in answer_session_ids else 0.0 for session_id in ranked_session_ids
    ]
    ideal_relevances = [1.0] * min(k, len(answer_session_ids))
    ideal_dcg = dcg_at_k(ideal_relevances, k)
    if ideal_dcg == 0.0:
        return 0.0
    return dcg_at_k(relevances, k) / ideal_dcg


def score_longmemeval_ranking(
    ranked_session_ids: list[str],
    answer_session_ids: list[str] | set[str],
    k_values: list[int],
) -> dict[str, float]:
    answers = set(answer_session_ids)
    metrics: dict[str, float] = {}

    for k in k_values:
        hit = hit_at_k(ranked_session_ids, answers, k)
        metrics[f"hit@{k}"] = hit
        metrics[f"legacy_recall@{k}"] = hit
        metrics[f"recall@{k}"] = recall_at_k(ranked_session_ids, answers, k)
        metrics[f"ndcg@{k}"] = ndcg_at_k(ranked_session_ids, answers, k)

    return metrics


def average_metric(results: list[Mapping[str, Any]], metric: str) -> float:
    if not results:
        return 0.0
    return sum(float(result[metric]) for result in results) / len(results)
