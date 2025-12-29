"""Retrieval components for Graph-RAG pipeline.

This module provides advanced retrieval strategies:
- temporal: Time-decay boosting for recency
- fusion: Reciprocal Rank Fusion for merging results
- bm25: Keyword-based BM25 search
- hybrid: Combined vector + graph traversal
- dedup: Entity deduplication via embeddings
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
from sibyl_core.retrieval.temporal import (
    TemporalConfig,
    calculate_boost,
    temporal_boost,
    temporal_boost_single,
)

__all__ = [
    # BM25
    "BM25Config",
    "BM25Index",
    # Dedup
    "DedupConfig",
    "DuplicatePair",
    "EntityDeduplicator",
    # Fusion
    "FusionConfig",
    # Hybrid
    "HybridConfig",
    "HybridResult",
    # Temporal
    "TemporalConfig",
    "bm25_search",
    "calculate_boost",
    "cosine_similarity",
    "find_duplicates",
    "get_bm25_index",
    "get_deduplicator",
    "hybrid_search",
    "rrf_merge",
    "rrf_merge_with_metadata",
    "simple_hybrid_search",
    "temporal_boost",
    "temporal_boost_single",
    "weighted_score_merge",
]
