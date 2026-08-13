"""Retrieval components for Graph-RAG pipeline.

This module provides advanced retrieval strategies:
- temporal: Time-decay boosting for recency
- fusion: Reciprocal Rank Fusion for merging results
- hybrid: Combined vector + graph traversal
- dedup: Entity deduplication via embeddings
- reranking: Feature-weighted and cross-encoder reranking

Lexical search is served natively by SurrealDB FULLTEXT indexes, not by an
in-process keyword index.
"""

from importlib import import_module

from sibyl_core.retrieval.candidates import (
    CandidateKind,
    CandidateScope,
    CandidateSignal,
    RetrievalCandidate,
    VectorCandidateFetch,
    candidate_contract_metadata,
    merge_candidate_signals,
)
from sibyl_core.retrieval.dedup import (
    DedupConfig,
    DuplicatePair,
    EntityDeduplicator,
    cosine_similarity,
    find_duplicates,
    get_deduplicator,
)
from sibyl_core.retrieval.fusion import (
    FusionConfig,
    rrf_merge,
    rrf_merge_with_metadata,
    weighted_score_merge,
)
from sibyl_core.retrieval.hybrid import (
    HybridConfig,
    HybridResult,
    hybrid_search,
    simple_hybrid_search,
)
from sibyl_core.retrieval.reranking import (
    CrossEncoderConfig,
    FeatureRerankCandidate,
    FeatureRerankScoredCandidate,
    FeatureWeightedRerankModel,
    RerankResult,
    cross_encoder_rerank,
    fit_feature_weighted_reranker,
    rerank_by_feature_weights,
    rerank_results,
)
from sibyl_core.retrieval.temporal import (
    TemporalConfig,
    calculate_boost,
    get_entity_decay_timestamp,
    temporal_boost,
    temporal_boost_single,
    temporal_decay_multiplier,
)

_SEARCH_EXPORTS = {
    "DEFAULT_FILTER_SELECTIVITY_THRESHOLD",
    "CandidateLimits",
    "RetrievalPlan",
    "RetrievalSignal",
    "RetrievalWeights",
    "ScopeSpec",
    "SearchFilter",
    "build_context_retrieval_plan",
    "context_search",
}
# The naive arm imports the search module's lane readers, so it inherits the
# same deferred-import treatment: naming it here must not drag the retrieval
# planner into every importer of this package.
_NAIVE_EXPORTS = {
    "NAIVE_RETRIEVAL_MODE",
    "NAIVE_RRF_K",
    "NaiveSignal",
    "fuse_naive_candidates",
    "naive_retrieval_plan",
    "naive_search",
    "pack_naive_results",
}

__all__ = [
    "DEFAULT_FILTER_SELECTIVITY_THRESHOLD",
    "NAIVE_RETRIEVAL_MODE",
    "NAIVE_RRF_K",
    "CandidateKind",
    "CandidateLimits",
    "CandidateScope",
    "CandidateSignal",
    "CrossEncoderConfig",
    "DedupConfig",
    "DuplicatePair",
    "EntityDeduplicator",
    "FeatureRerankCandidate",
    "FeatureRerankScoredCandidate",
    "FeatureWeightedRerankModel",
    "FusionConfig",
    "HybridConfig",
    "HybridResult",
    "NaiveSignal",
    "RerankResult",
    "RetrievalCandidate",
    "RetrievalPlan",
    "RetrievalSignal",
    "RetrievalWeights",
    "ScopeSpec",
    "SearchFilter",
    "TemporalConfig",
    "VectorCandidateFetch",
    "build_context_retrieval_plan",
    "calculate_boost",
    "candidate_contract_metadata",
    "context_search",
    "cosine_similarity",
    "cross_encoder_rerank",
    "find_duplicates",
    "fit_feature_weighted_reranker",
    "fuse_naive_candidates",
    "get_deduplicator",
    "get_entity_decay_timestamp",
    "hybrid_search",
    "merge_candidate_signals",
    "naive_retrieval_plan",
    "naive_search",
    "pack_naive_results",
    "rerank_by_feature_weights",
    "rerank_results",
    "rrf_merge",
    "rrf_merge_with_metadata",
    "simple_hybrid_search",
    "temporal_boost",
    "temporal_boost_single",
    "temporal_decay_multiplier",
    "weighted_score_merge",
]


def __getattr__(name: str) -> object:
    if name in _SEARCH_EXPORTS:
        search = import_module("sibyl_core.retrieval.search")
        value = getattr(search, name)
        globals()[name] = value
        return value
    if name in _NAIVE_EXPORTS:
        naive = import_module("sibyl_core.retrieval.naive")
        value = getattr(naive, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
