"""Retrieval components for Graph-RAG pipeline.

This module provides advanced retrieval strategies:
- temporal: Time-decay boosting for recency
- fusion: Reciprocal Rank Fusion for merging results
- bm25: Keyword-based BM25 search
- hybrid: Combined vector + graph traversal
- dedup: Entity deduplication via embeddings
"""

from sibyl.retrieval.bm25 import BM25Config, BM25Index, bm25_search, get_bm25_index
from sibyl.retrieval.dedup import (
    DedupConfig,
    DuplicatePair,
    EntityDeduplicator,
    cosine_similarity,
    find_duplicates,
    get_deduplicator,
)
from sibyl.retrieval.fusion import (
    FusionConfig,
    rrf_merge,
    rrf_merge_with_metadata,
    weighted_score_merge,
)
from sibyl.retrieval.hybrid import (
    HybridConfig,
    HybridResult,
    hybrid_search,
    simple_hybrid_search,
)
from sibyl.retrieval.temporal import (
    TemporalConfig,
    calculate_boost,
    temporal_boost,
    temporal_boost_single,
)

__all__ = [
    # Temporal
    "TemporalConfig",
    "temporal_boost",
    "temporal_boost_single",
    "calculate_boost",
    # Fusion
    "FusionConfig",
    "rrf_merge",
    "rrf_merge_with_metadata",
    "weighted_score_merge",
    # BM25
    "BM25Config",
    "BM25Index",
    "bm25_search",
    "get_bm25_index",
    # Hybrid
    "HybridConfig",
    "HybridResult",
    "hybrid_search",
    "simple_hybrid_search",
    # Dedup
    "DedupConfig",
    "DuplicatePair",
    "EntityDeduplicator",
    "cosine_similarity",
    "find_duplicates",
    "get_deduplicator",
]
