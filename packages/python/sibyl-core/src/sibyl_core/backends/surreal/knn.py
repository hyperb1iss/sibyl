"""SurrealDB KNN query helpers."""

from __future__ import annotations

KNN_TYPE_OVERFETCH_CAP = 4_000
"""Ceiling on the untyped pool a typed overfetch read walks.

Probed warm at 0.82s on a 95K-row namespace; past this depth the walk cost
approaches the table-scan it exists to avoid.
"""


def knn_overfetch_pool(candidate_limit: int, overfetch: int) -> int:
    """Depth of the untyped KNN pool for a typed overfetch read.

    A selective predicate beside the HNSW bracket forces the walk 10-15x
    deeper regardless of syntax, so typed vector reads walk an untyped pool
    `overfetch` times the candidate budget and filter types on the
    materialized rows instead. When the filtered head fills the budget it is
    exactly the typed KNN head: every typed row nearer than the pool's k-th
    member is inside the pool by definition.
    """
    return min(max(int(candidate_limit), 1) * max(int(overfetch), 1), KNN_TYPE_OVERFETCH_CAP)


def knn_search_effort(k: int, configured_ef: int) -> int:
    """Return the search effort for a `<|k, ef|>` HNSW read.

    An HNSW search keeps at most `ef` nodes in flight, so an effort below the
    requested `k` truncates the read to `ef` rows and Surreal raises nothing.
    The configured effort is a floor on search quality, never a ceiling on the
    rows a caller asked for, so `k` raises it whenever the requested pool runs
    deeper than the configuration.
    """
    return max(1, int(configured_ef), int(k))
