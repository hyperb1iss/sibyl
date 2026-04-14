"""Retrieval quality metrics for Sibyl evaluation harnesses."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalResult:
    """Single retrieval result for evaluation."""

    id: str
    content: str
    score: float
    relevance: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalQuery:
    """Evaluation query with expected results."""

    query: str
    expected_ids: list[str]
    relevance_grades: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalMetrics:
    """Computed evaluation metrics."""

    ndcg_at_k: dict[int, float] = field(default_factory=dict)
    success_at_k: dict[int, float] = field(default_factory=dict)
    precision_at_k: dict[int, float] = field(default_factory=dict)
    recall_at_k: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to a JSON-safe dictionary."""
        return {
            "ndcg@5": self.ndcg_at_k.get(5, 0.0),
            "ndcg@10": self.ndcg_at_k.get(10, 0.0),
            "success@5": self.success_at_k.get(5, 0.0),
            "success@10": self.success_at_k.get(10, 0.0),
            "precision@5": self.precision_at_k.get(5, 0.0),
            "precision@10": self.precision_at_k.get(10, 0.0),
            "recall@5": self.recall_at_k.get(5, 0.0),
            "recall@10": self.recall_at_k.get(10, 0.0),
            "mrr": self.mrr,
            "latency_ms": self.latency_ms,
        }


def dcg_at_k(relevances: list[int], k: int) -> float:
    """Compute discounted cumulative gain at rank K."""
    relevances = relevances[:k]
    if not relevances:
        return 0.0

    dcg = relevances[0]
    for i, rel in enumerate(relevances[1:], start=2):
        dcg += rel / math.log2(i + 1)

    return dcg


def ndcg_at_k(relevances: list[int], k: int) -> float:
    """Compute normalized discounted cumulative gain at rank K."""
    dcg = dcg_at_k(relevances, k)
    ideal_relevances = sorted(relevances, reverse=True)
    idcg = dcg_at_k(ideal_relevances, k)
    if idcg == 0:
        return 0.0
    return dcg / idcg


def success_at_k(relevances: list[int], k: int, threshold: int = 1) -> float:
    """Return 1 if any result in top K is relevant enough."""
    for rel in relevances[:k]:
        if rel >= threshold:
            return 1.0
    return 0.0


def precision_at_k(relevances: list[int], k: int, threshold: int = 1) -> float:
    """Compute precision at rank K."""
    top_k = relevances[:k]
    if not top_k:
        return 0.0

    relevant = sum(1 for rel in top_k if rel >= threshold)
    return relevant / len(top_k)


def recall_at_k(
    relevances: list[int],
    k: int,
    total_relevant: int,
    threshold: int = 1,
) -> float:
    """Compute recall at rank K."""
    if total_relevant == 0:
        return 0.0

    top_k = relevances[:k]
    retrieved_relevant = sum(1 for rel in top_k if rel >= threshold)
    return retrieved_relevant / total_relevant


def mean_reciprocal_rank(relevances: list[int], threshold: int = 1) -> float:
    """Compute mean reciprocal rank for the first relevant result."""
    for i, rel in enumerate(relevances, start=1):
        if rel >= threshold:
            return 1.0 / i
    return 0.0


def compute_metrics(
    results: list[RetrievalResult],
    query: EvalQuery,
    latency_ms: float = 0.0,
    k_values: list[int] | None = None,
) -> EvalMetrics:
    """Compute all metrics for a single evaluation query."""
    if k_values is None:
        k_values = [1, 3, 5, 10]

    relevances = []
    for result in results:
        if result.id in query.relevance_grades:
            relevances.append(query.relevance_grades[result.id])
        elif result.id in query.expected_ids:
            relevances.append(1)
        else:
            relevances.append(0)

    total_relevant = len(query.expected_ids)
    metrics = EvalMetrics(latency_ms=latency_ms)

    for k in k_values:
        metrics.ndcg_at_k[k] = ndcg_at_k(relevances, k)
        metrics.success_at_k[k] = success_at_k(relevances, k)
        metrics.precision_at_k[k] = precision_at_k(relevances, k)
        metrics.recall_at_k[k] = recall_at_k(relevances, k, total_relevant)

    metrics.mrr = mean_reciprocal_rank(relevances)
    return metrics


def aggregate_metrics(all_metrics: list[EvalMetrics]) -> EvalMetrics:
    """Aggregate metrics across multiple evaluation queries."""
    if not all_metrics:
        return EvalMetrics()

    k_values = set()
    for metrics in all_metrics:
        k_values.update(metrics.ndcg_at_k.keys())

    aggregated = EvalMetrics()
    for k in k_values:
        aggregated.ndcg_at_k[k] = sum(m.ndcg_at_k.get(k, 0.0) for m in all_metrics) / len(
            all_metrics
        )
        aggregated.success_at_k[k] = sum(m.success_at_k.get(k, 0.0) for m in all_metrics) / len(
            all_metrics
        )
        aggregated.precision_at_k[k] = sum(m.precision_at_k.get(k, 0.0) for m in all_metrics) / len(
            all_metrics
        )
        aggregated.recall_at_k[k] = sum(m.recall_at_k.get(k, 0.0) for m in all_metrics) / len(
            all_metrics
        )

    aggregated.mrr = sum(m.mrr for m in all_metrics) / len(all_metrics)
    aggregated.latency_ms = sum(m.latency_ms for m in all_metrics) / len(all_metrics)
    return aggregated
