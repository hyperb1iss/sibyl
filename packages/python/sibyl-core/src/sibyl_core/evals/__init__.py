"""Shared evaluation utilities for Sibyl retrieval surfaces."""

from sibyl_core.evals.metrics import (
    EvalMetrics,
    EvalQuery,
    RetrievalResult,
    aggregate_metrics,
    compute_metrics,
    dcg_at_k,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    success_at_k,
)
from sibyl_core.evals.runtime import (
    EvalConfig,
    EvalReport,
    EvalResult,
    EvalRunner,
    get_sample_queries,
    load_queries,
    run_evaluation_cli,
)

__all__ = [
    "EvalConfig",
    "EvalMetrics",
    "EvalQuery",
    "EvalReport",
    "EvalResult",
    "EvalRunner",
    "RetrievalResult",
    "aggregate_metrics",
    "compute_metrics",
    "dcg_at_k",
    "get_sample_queries",
    "load_queries",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "run_evaluation_cli",
    "success_at_k",
]
