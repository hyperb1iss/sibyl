"""SurrealDB KNN query helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _knn_row_sort_key(row: Mapping[str, Any]) -> tuple[float, str, str]:
    score = row.get("score")
    return (
        float(score) if isinstance(score, (int, float)) else 0.0,
        str(row.get("created_at") or ""),
        str(row.get("uuid") or ""),
    )


def merge_knn_row_batches(
    row_batches: Iterable[Sequence[Mapping[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Merge per-predicate KNN result batches into one ranked head.

    An `IN $list` predicate beside an HNSW `<|k, ef|>` bracket drops the
    planner off the vector index into a table scan, so callers fan the list
    out into per-value equality queries and merge here. Ordering mirrors the
    SQL `ORDER BY score DESC, created_at DESC, uuid DESC`; each batch is the
    full per-value head for the same `k`, so the merged head is a superset
    of what the single IN-form query could return.
    """
    merged: list[dict[str, Any]] = []
    for batch in row_batches:
        merged.extend(dict(row) for row in batch)
    merged.sort(key=_knn_row_sort_key, reverse=True)
    return merged[: max(int(limit), 0)]


def knn_search_effort(k: int, configured_ef: int) -> int:
    """Return the search effort for a `<|k, ef|>` HNSW read.

    An HNSW search keeps at most `ef` nodes in flight, so an effort below the
    requested `k` truncates the read to `ef` rows and Surreal raises nothing.
    The configured effort is a floor on search quality, never a ceiling on the
    rows a caller asked for, so `k` raises it whenever the requested pool runs
    deeper than the configuration.
    """
    return max(1, int(configured_ef), int(k))
