"""Reciprocal Rank Fusion (RRF) for merging ranked result lists.

RRF combines multiple ranked lists using the formula:
    score(d) = sum(1 / (k + rank(d, L))) for each list L

This produces a unified ranking that balances results from different sources.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger()

T = TypeVar("T")


@dataclass
class FusionConfig:
    """Configuration for RRF fusion.

    Attributes:
        k: RRF constant (default 60, higher = more uniform weighting).
        weights: Optional weights for each result list.
        dedup_key: Function to extract dedup key from entity.
    """

    k: float = 60.0
    weights: list[float] | None = None
    dedup_key: Callable[[Any], str] | None = None


def default_dedup_key(entity: Any) -> str:
    """Extract deduplication key from an entity.

    Tries 'id', 'uuid', then falls back to str representation.
    """
    if isinstance(entity, dict):
        return str(entity.get("id") or entity.get("uuid") or id(entity))

    entity_id = getattr(entity, "id", None) or getattr(entity, "uuid", None)
    return str(entity_id) if entity_id else str(id(entity))


def rrf_score(rank: int, k: float = 60.0) -> float:
    """Calculate RRF score for a given rank.

    Args:
        rank: 1-based rank in the list.
        k: RRF constant (typically 60).

    Returns:
        RRF score contribution.
    """
    return 1.0 / (k + rank)


def rrf_merge(
    result_lists: list[list[tuple[T, float]]],
    k: float = 60.0,
    weights: list[float] | None = None,
    dedup_key: Callable[[T], str] | None = None,
    limit: int | None = None,
) -> list[tuple[T, float]]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    RRF is robust to different score scales across lists and produces
    a balanced ranking that considers all input sources.

    Args:
        result_lists: List of result lists, each containing (entity, score) tuples.
        k: RRF constant (default 60). Higher values weight ranks more uniformly.
        weights: Optional weights for each list (default: equal weights).
        dedup_key: Function to extract unique key from entity (for deduplication).
        limit: Maximum results to return (default: no limit).

    Returns:
        Merged list of (entity, rrf_score) tuples, sorted by RRF score descending.

    Example:
        >>> vector_results = [(e1, 0.9), (e2, 0.8), (e3, 0.7)]
        >>> graph_results = [(e2, 0.95), (e4, 0.85), (e1, 0.75)]
        >>> merged = rrf_merge([vector_results, graph_results])
        >>> # e2 ranks high in both, so it tops the merged list
    """
    if not result_lists:
        return []

    # Filter out empty lists
    result_lists = [r for r in result_lists if r]
    if not result_lists:
        return []

    # Set up weights
    if weights is None:
        weights = [1.0] * len(result_lists)
    elif len(weights) != len(result_lists):
        log.warning(
            "rrf_weight_mismatch",
            expected=len(result_lists),
            got=len(weights),
        )
        weights = [1.0] * len(result_lists)

    # Set up dedup key function
    if dedup_key is None:
        dedup_key = default_dedup_key

    # Accumulate RRF scores
    scores: dict[str, float] = defaultdict(float)
    entities: dict[str, T] = {}
    sources: dict[str, list[int]] = defaultdict(list)

    for list_idx, results in enumerate(result_lists):
        weight = weights[list_idx]

        for rank, (entity, _original_score) in enumerate(results, start=1):
            key = dedup_key(entity)

            # Calculate weighted RRF score
            rrf = rrf_score(rank, k) * weight
            scores[key] += rrf

            # Keep the first occurrence of each entity
            if key not in entities:
                entities[key] = entity

            # Track which lists contained this entity
            sources[key].append(list_idx)

    # Sort by RRF score descending
    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    # Build result list
    results: list[tuple[T, float]] = []
    for key in sorted_keys:
        results.append((entities[key], scores[key]))
        if limit and len(results) >= limit:
            break

    log.debug(
        "rrf_merge_complete",
        input_lists=len(result_lists),
        total_entities=len(entities),
        output_count=len(results),
    )

    return results


def rrf_merge_with_metadata(
    result_lists: list[list[tuple[T, float]]],
    list_names: list[str] | None = None,
    k: float = 60.0,
    weights: list[float] | None = None,
    dedup_key: Callable[[T], str] | None = None,
    limit: int | None = None,
) -> list[tuple[T, float, dict[str, Any]]]:
    """Merge with additional metadata about result sources.

    Same as rrf_merge but includes metadata about which lists
    contributed to each result.

    Args:
        result_lists: List of result lists.
        list_names: Names for each list (e.g., ['vector', 'graph', 'bm25']).
        k: RRF constant.
        weights: Optional weights.
        dedup_key: Deduplication key function.
        limit: Maximum results.

    Returns:
        List of (entity, rrf_score, metadata) tuples where metadata includes:
        - sources: List of source names that contained this entity
        - ranks: Dict mapping source name to rank in that list
        - original_scores: Dict mapping source name to original score
    """
    if not result_lists:
        return []

    result_lists = [r for r in result_lists if r]
    if not result_lists:
        return []

    if list_names is None:
        list_names = [f"list_{i}" for i in range(len(result_lists))]

    if weights is None:
        weights = [1.0] * len(result_lists)

    if dedup_key is None:
        dedup_key = default_dedup_key

    # Track detailed info per entity
    scores: dict[str, float] = defaultdict(float)
    entities: dict[str, T] = {}
    metadata: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"sources": [], "ranks": {}, "original_scores": {}}
    )

    for list_idx, result_list in enumerate(result_lists):
        weight = weights[list_idx]
        list_name = list_names[list_idx] if list_idx < len(list_names) else f"list_{list_idx}"

        for rank, (entity, original_score) in enumerate(result_list, start=1):
            key = dedup_key(entity)

            rrf = rrf_score(rank, k) * weight
            scores[key] += rrf

            if key not in entities:
                entities[key] = entity

            metadata[key]["sources"].append(list_name)
            metadata[key]["ranks"][list_name] = rank
            metadata[key]["original_scores"][list_name] = original_score

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    results: list[tuple[T, float, dict[str, Any]]] = []
    for key in sorted_keys:
        results.append((entities[key], scores[key], metadata[key]))
        if limit and len(results) >= limit:
            break

    return results


def weighted_score_merge(
    result_lists: list[list[tuple[T, float]]],
    weights: list[float] | None = None,
    dedup_key: Callable[[T], str] | None = None,
    normalize: bool = True,
    limit: int | None = None,
) -> list[tuple[T, float]]:
    """Simple weighted score merging (alternative to RRF).

    Directly combines normalized scores with weights, rather than using ranks.
    Better when score scales are comparable across lists.

    Args:
        result_lists: List of result lists.
        weights: Weights for each list (default: equal).
        dedup_key: Deduplication key function.
        normalize: Whether to normalize scores within each list.
        limit: Maximum results.

    Returns:
        Merged list sorted by combined score.
    """
    if not result_lists:
        return []

    result_lists = [r for r in result_lists if r]
    if not result_lists:
        return []

    if weights is None:
        weights = [1.0] * len(result_lists)

    if dedup_key is None:
        dedup_key = default_dedup_key

    scores: dict[str, float] = defaultdict(float)
    entities: dict[str, T] = {}
    counts: dict[str, int] = defaultdict(int)

    for list_idx, results in enumerate(result_lists):
        if not results:
            continue

        weight = weights[list_idx]

        # Normalize scores within list if requested
        if normalize:
            max_score = max(s for _, s in results) if results else 1.0
            min_score = min(s for _, s in results) if results else 0.0
            score_range = max_score - min_score if max_score > min_score else 1.0
        else:
            min_score = 0.0
            score_range = 1.0

        for entity, original_score in results:
            key = dedup_key(entity)

            norm_score = (original_score - min_score) / score_range if normalize else original_score

            scores[key] += norm_score * weight
            counts[key] += 1

            if key not in entities:
                entities[key] = entity

    # Average by count (optional - could also just sum)
    for key in scores:
        scores[key] /= counts[key]

    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

    results: list[tuple[T, float]] = []
    for key in sorted_keys:
        results.append((entities[key], scores[key]))
        if limit and len(results) >= limit:
            break

    return results
