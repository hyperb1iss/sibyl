"""SurrealDB KNN query helpers."""

from __future__ import annotations


def knn_search_effort(k: int, configured_ef: int) -> int:
    """Return the search effort for a `<|k, ef|>` HNSW read.

    An HNSW search keeps at most `ef` nodes in flight, so an effort below the
    requested `k` truncates the read to `ef` rows and Surreal raises nothing.
    The configured effort is a floor on search quality, never a ceiling on the
    rows a caller asked for, so `k` raises it whenever the requested pool runs
    deeper than the configuration.
    """
    return max(1, int(configured_ef), int(k))
