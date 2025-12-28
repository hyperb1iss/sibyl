"""Retrieval quality metrics for RAG evaluation.

Implements standard IR metrics:
- NDCG@K: Measures ranking quality with graded relevance
- Success@K: Binary success if answer in top K
- MRR: Mean Reciprocal Rank for first relevant result
- Precision@K: Proportion of relevant results in top K
- Recall@K: Proportion of relevant results retrieved
"""

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
    relevance: int = 0  # 0=irrelevant, 1=partially, 2=relevant, 3=highly relevant
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalQuery:
    """Evaluation query with expected results."""

    query: str
    expected_ids: list[str]  # IDs of relevant documents
    relevance_grades: dict[str, int] = field(default_factory=dict)  # ID -> grade (1-3)
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
        """Convert metrics to dict for serialization."""
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
    """Compute Discounted Cumulative Gain at rank K.

    DCG = sum(rel_i / log2(i + 1)) for i in 1..k

    Args:
        relevances: List of relevance scores in result order
        k: Cutoff rank

    Returns:
        DCG score
    """
    relevances = relevances[:k]
    if not relevances:
        return 0.0

    dcg = relevances[0]  # First position has no discount
    for i, rel in enumerate(relevances[1:], start=2):
        dcg += rel / math.log2(i + 1)

    return dcg


def ndcg_at_k(relevances: list[int], k: int) -> float:
    """Compute Normalized DCG at rank K.

    NDCG = DCG / IDCG where IDCG is the ideal DCG (best possible ranking)

    Args:
        relevances: List of relevance scores in result order
        k: Cutoff rank

    Returns:
        NDCG score (0-1)
    """
    dcg = dcg_at_k(relevances, k)

    # Ideal DCG: best possible ordering (sorted by relevance descending)
    ideal_relevances = sorted(relevances, reverse=True)
    idcg = dcg_at_k(ideal_relevances, k)

    if idcg == 0:
        return 0.0

    return dcg / idcg


def success_at_k(relevances: list[int], k: int, threshold: int = 1) -> float:
    """Compute Success@K (binary: was a relevant result in top K?).

    Args:
        relevances: List of relevance scores in result order
        k: Cutoff rank
        threshold: Minimum relevance to count as success

    Returns:
        1.0 if any result in top K meets threshold, else 0.0
    """
    for rel in relevances[:k]:
        if rel >= threshold:
            return 1.0
    return 0.0


def precision_at_k(relevances: list[int], k: int, threshold: int = 1) -> float:
    """Compute Precision@K (proportion of relevant results in top K).

    Args:
        relevances: List of relevance scores in result order
        k: Cutoff rank
        threshold: Minimum relevance to count as relevant

    Returns:
        Precision score (0-1)
    """
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
    """Compute Recall@K (proportion of all relevant results retrieved).

    Args:
        relevances: List of relevance scores in result order
        k: Cutoff rank
        total_relevant: Total number of relevant documents in corpus
        threshold: Minimum relevance to count as relevant

    Returns:
        Recall score (0-1)
    """
    if total_relevant == 0:
        return 0.0

    top_k = relevances[:k]
    retrieved_relevant = sum(1 for rel in top_k if rel >= threshold)

    return retrieved_relevant / total_relevant


def mean_reciprocal_rank(relevances: list[int], threshold: int = 1) -> float:
    """Compute Mean Reciprocal Rank.

    MRR = 1 / rank_of_first_relevant_result

    Args:
        relevances: List of relevance scores in result order
        threshold: Minimum relevance to count as relevant

    Returns:
        MRR score (0-1)
    """
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
    """Compute all metrics for a single query.

    Args:
        results: Retrieved results in order
        query: Evaluation query with ground truth
        latency_ms: Query latency in milliseconds
        k_values: K values to compute metrics at (default: [1, 3, 5, 10])

    Returns:
        EvalMetrics with all computed scores
    """
    if k_values is None:
        k_values = [1, 3, 5, 10]

    # Build relevance list from results
    relevances = []
    for result in results:
        if result.id in query.relevance_grades:
            relevances.append(query.relevance_grades[result.id])
        elif result.id in query.expected_ids:
            relevances.append(1)  # Binary relevance
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
    """Aggregate metrics across multiple queries.

    Args:
        all_metrics: List of EvalMetrics from individual queries

    Returns:
        EvalMetrics with averaged scores
    """
    if not all_metrics:
        return EvalMetrics()

    # Collect all K values
    k_values = set()
    for m in all_metrics:
        k_values.update(m.ndcg_at_k.keys())

    aggregated = EvalMetrics()

    for k in k_values:
        ndcg_scores = [m.ndcg_at_k.get(k, 0.0) for m in all_metrics]
        success_scores = [m.success_at_k.get(k, 0.0) for m in all_metrics]
        precision_scores = [m.precision_at_k.get(k, 0.0) for m in all_metrics]
        recall_scores = [m.recall_at_k.get(k, 0.0) for m in all_metrics]

        aggregated.ndcg_at_k[k] = sum(ndcg_scores) / len(ndcg_scores)
        aggregated.success_at_k[k] = sum(success_scores) / len(success_scores)
        aggregated.precision_at_k[k] = sum(precision_scores) / len(precision_scores)
        aggregated.recall_at_k[k] = sum(recall_scores) / len(recall_scores)

    mrr_scores = [m.mrr for m in all_metrics]
    latency_scores = [m.latency_ms for m in all_metrics]

    aggregated.mrr = sum(mrr_scores) / len(mrr_scores)
    aggregated.latency_ms = sum(latency_scores) / len(latency_scores)

    return aggregated
