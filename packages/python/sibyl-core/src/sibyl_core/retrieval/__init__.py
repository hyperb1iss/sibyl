"""Retrieval components for Graph-RAG pipeline.

This module provides advanced retrieval strategies:
- temporal: Time-decay boosting for recency
- fusion: Reciprocal Rank Fusion for merging results
- bm25: Keyword-based BM25 search
- hybrid: Combined vector + graph traversal
- dedup: Entity deduplication via embeddings
- reranking: Cross-encoder reranking for improved relevance
"""

from sibyl_core.retrieval.bm25 import BM25Config, BM25Index, bm25_search, get_bm25_index
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
from sibyl_core.retrieval.native import (
    DEFAULT_FILTER_SELECTIVITY_THRESHOLD,
    NativeCandidateLimits,
    NativeRetrievalMode,
    NativeRetrievalPlan,
    NativeRetrievalSignal,
    NativeRetrievalWeights,
    NativeScopeSpec,
    build_native_context_retrieval_plan,
    coerce_native_retrieval_mode,
    native_retrieval_mode_from_env,
)
from sibyl_core.retrieval.reranking import (
    CrossEncoderConfig,
    RerankResult,
    cross_encoder_rerank,
    rerank_results,
)
from sibyl_core.retrieval.temporal import (
    TemporalConfig,
    calculate_boost,
    temporal_boost,
    temporal_boost_single,
)

__all__ = [
    "DEFAULT_FILTER_SELECTIVITY_THRESHOLD",
    "BM25Config",
    "BM25Index",
    "CrossEncoderConfig",
    "DedupConfig",
    "DuplicatePair",
    "EntityDeduplicator",
    "FusionConfig",
    "HybridConfig",
    "HybridResult",
    "NativeCandidateLimits",
    "NativeRetrievalMode",
    "NativeRetrievalPlan",
    "NativeRetrievalSignal",
    "NativeRetrievalWeights",
    "NativeScopeSpec",
    "RerankResult",
    "TemporalConfig",
    "bm25_search",
    "build_native_context_retrieval_plan",
    "calculate_boost",
    "coerce_native_retrieval_mode",
    "cosine_similarity",
    "cross_encoder_rerank",
    "find_duplicates",
    "get_bm25_index",
    "get_deduplicator",
    "hybrid_search",
    "native_retrieval_mode_from_env",
    "rerank_results",
    "rrf_merge",
    "rrf_merge_with_metadata",
    "simple_hybrid_search",
    "temporal_boost",
    "temporal_boost_single",
    "weighted_score_merge",
]
