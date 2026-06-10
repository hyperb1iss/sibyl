"""Cross-encoder reranking for improved relevance scoring.

Cross-encoders score query-document pairs directly, providing more accurate
relevance scores than bi-encoder similarity. Applied after initial retrieval
(RRF fusion) to refine the top-k results.

Typical pipeline:
1. Initial retrieval: native vector/fulltext search + graph traversal
2. RRF fusion: Merge into unified ranking
3. Cross-encoder reranking: Refine top-k with pairwise scoring
4. Temporal boost: Favor recent content

This is an optional capability. Local cross-encoder reranking requires the
``reranking`` extra (sentence-transformers); when it is absent the path degrades
to the fused order rather than raising. API-based reranking (Cohere) requires the
``cohere`` package and an API key. Both default to off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    pass

log = structlog.get_logger()

__all__ = [
    "CrossEncoderConfig",
    "FeatureRerankCandidate",
    "FeatureRerankScoredCandidate",
    "FeatureWeightedRerankModel",
    "RerankResult",
    "cross_encoder_rerank",
    "fit_feature_weighted_reranker",
    "get_cross_encoder",
    "rerank_by_feature_weights",
    "rerank_results",
]


@dataclass
class CrossEncoderConfig:
    """Configuration for cross-encoder reranking.

    Attributes:
        enabled: Whether reranking is enabled.
        model_name: Cross-encoder model to use.
        top_k: Number of candidates to rerank (rest pass through unchanged).
        batch_size: Batch size for model inference.
        min_score: Minimum score threshold to include in results.
        use_gpu: Whether to use GPU if available.
        fallback_on_error: Return original results if reranking fails.
    """

    enabled: bool = False  # Disabled by default for performance
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    top_k: int = 20
    batch_size: int = 32
    min_score: float | None = None
    use_gpu: bool = False
    fallback_on_error: bool = True


@dataclass
class RerankResult:
    """Result from cross-encoder reranking.

    Attributes:
        results: List of (entity, score) tuples with cross-encoder scores.
        reranked_count: Number of results that were reranked.
        model_name: Model used for reranking.
        metadata: Additional reranking metadata.
    """

    results: list[tuple[Any, float]]
    reranked_count: int
    model_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeatureWeightedRerankModel:
    """Interpretable feature-weighted reranker artifact."""

    name: str
    weights: Mapping[str, float]
    intercept: float = 0.0

    def score(self, features: Mapping[str, float]) -> float:
        return self.intercept + sum(
            float(features.get(name, 0.0)) * weight for name, weight in self.weights.items()
        )

    def contributions(self, features: Mapping[str, float]) -> dict[str, float]:
        return {
            name: float(features.get(name, 0.0)) * weight
            for name, weight in self.weights.items()
            if features.get(name, 0.0)
        }


@dataclass(frozen=True, slots=True)
class FeatureRerankCandidate[T]:
    item: T
    features: Mapping[str, float]
    original_rank: int
    stable_id: str | None = None


@dataclass(frozen=True, slots=True)
class FeatureRerankScoredCandidate[T]:
    item: T
    score: float
    original_rank: int
    stable_id: str | None = None
    feature_contributions: dict[str, float] = field(default_factory=dict)


# Cache for cross-encoder model
_cross_encoder_cache: dict[str, Any] = {}


@lru_cache(maxsize=1)
def _check_sentence_transformers_available() -> bool:
    """Check if sentence-transformers is available."""
    try:
        import sentence_transformers  # noqa: F401

        return True
    except ImportError:
        return False


def get_cross_encoder(model_name: str, use_gpu: bool = False) -> Any | None:
    """Get or create a cached cross-encoder model.

    Reranking is an optional capability: install the ``reranking`` extra
    (``sentence-transformers``) to enable it. When the dependency is absent this
    returns ``None`` so callers fall back to the fused order instead of crashing.

    Args:
        model_name: HuggingFace model name.
        use_gpu: Whether to use GPU.

    Returns:
        CrossEncoder model instance, or ``None`` if sentence-transformers is not
        installed.
    """
    cache_key = f"{model_name}_{use_gpu}"

    if cache_key in _cross_encoder_cache:
        return _cross_encoder_cache[cache_key]

    if not _check_sentence_transformers_available():
        log.info("cross_encoder_unavailable", model=model_name)
        return None

    from sentence_transformers import CrossEncoder

    device = "cuda" if use_gpu else "cpu"
    log.info("loading_cross_encoder", model=model_name, device=device)

    model = CrossEncoder(model_name, device=device)
    _cross_encoder_cache[cache_key] = model

    log.info("cross_encoder_loaded", model=model_name)
    return model


def _extract_content(entity: Any) -> str:
    """Extract searchable content from an entity.

    Tries various attributes to get meaningful text for reranking.
    """
    # Try common content fields
    for attr in ["content", "description", "text", "summary"]:
        if hasattr(entity, attr):
            val = getattr(entity, attr)
            if val:
                return str(val)
        elif isinstance(entity, dict) and attr in entity:
            if entity[attr]:
                return str(entity[attr])

    # Fall back to name
    name = getattr(entity, "name", None) or (
        entity.get("name") if isinstance(entity, dict) else None
    )
    if name:
        return str(name)

    return str(entity)


def fit_feature_weighted_reranker(
    rows: Sequence[Mapping[str, Any]],
    *,
    name: str = "feature-linear-v1",
    features_key: str = "features",
    label_key: str = "label",
    min_abs_weight: float = 0.0,
) -> FeatureWeightedRerankModel:
    positive: list[Mapping[str, float]] = []
    negative: list[Mapping[str, float]] = []
    for row in rows:
        features = row.get(features_key)
        if not isinstance(features, Mapping):
            continue
        numeric = {
            str(key): float(value)
            for key, value in features.items()
            if isinstance(value, int | float)
        }
        if int(row.get(label_key) or 0) > 0:
            positive.append(numeric)
        else:
            negative.append(numeric)

    if not positive or not negative:
        return FeatureWeightedRerankModel(name=name, weights={})

    feature_names = sorted(
        {name for features in positive for name in features}
        | {name for features in negative for name in features}
    )
    weights: dict[str, float] = {}
    for feature_name in feature_names:
        positive_mean = sum(features.get(feature_name, 0.0) for features in positive) / len(
            positive
        )
        negative_mean = sum(features.get(feature_name, 0.0) for features in negative) / len(
            negative
        )
        weight = positive_mean - negative_mean
        if abs(weight) >= min_abs_weight:
            weights[feature_name] = weight
    return FeatureWeightedRerankModel(name=name, weights=weights)


def rerank_by_feature_weights[T](
    candidates: Sequence[FeatureRerankCandidate[T]],
    model: FeatureWeightedRerankModel,
) -> list[FeatureRerankScoredCandidate[T]]:
    scored = [
        FeatureRerankScoredCandidate(
            item=candidate.item,
            score=model.score(candidate.features),
            original_rank=candidate.original_rank,
            stable_id=candidate.stable_id,
            feature_contributions=model.contributions(candidate.features),
        )
        for candidate in candidates
    ]
    return sorted(scored, key=lambda candidate: (-candidate.score, candidate.original_rank))


def cross_encoder_rerank[T](
    query: str,
    results: list[tuple[T, float]],
    model: Any,
    top_k: int = 20,
    batch_size: int = 32,
    min_score: float | None = None,
) -> list[tuple[T, float]]:
    """Rerank results using a cross-encoder model.

    Applies cross-encoder scoring to top_k results, then combines
    with remaining results (keeping their relative order).

    Args:
        query: Original search query.
        results: Initial results as (entity, score) tuples.
        model: CrossEncoder model instance.
        top_k: Number of top results to rerank.
        batch_size: Batch size for inference.
        min_score: Minimum cross-encoder score to include.

    Returns:
        Reranked list of (entity, score) tuples.
    """
    if not results:
        return []

    # Split into candidates for reranking and the rest
    candidates = results[:top_k]
    remainder = results[top_k:]

    if not candidates:
        return results

    # Prepare query-document pairs
    pairs: list[tuple[str, str]] = []
    for entity, _ in candidates:
        content = _extract_content(entity)
        # Limit content length for efficiency
        truncated = content[:512] if len(content) > 512 else content
        pairs.append((query, truncated))

    # Score all pairs
    try:
        scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    except Exception as e:
        log.warning("cross_encoder_prediction_failed", error=str(e))
        return results

    # Combine entities with new scores
    reranked: list[tuple[T, float]] = []
    for i, (entity, _old_score) in enumerate(candidates):
        score = float(scores[i])
        if min_score is None or score >= min_score:
            reranked.append((entity, score))

    # Sort by cross-encoder score
    reranked.sort(key=lambda x: x[1], reverse=True)

    # Add remainder (not reranked, but adjust scores to be below reranked)
    if remainder and reranked:
        # Scale remainder scores to be below minimum reranked score
        min_reranked = min(s for _, s in reranked) if reranked else 0.0
        scale_factor = min_reranked * 0.9 if min_reranked > 0 else 0.1
        max_remainder = max(s for _, s in remainder) if remainder else 1.0

        for entity, old_score in remainder:
            # Normalize and scale to be below reranked results
            new_score = (old_score / max_remainder) * scale_factor if max_remainder > 0 else 0.0
            reranked.append((entity, new_score))

    log.debug(
        "cross_encoder_rerank_complete",
        candidates=len(candidates),
        reranked=len(reranked),
        min_score_filter=min_score,
    )

    return reranked


async def rerank_results[T](
    query: str,
    results: list[tuple[T, float]],
    config: CrossEncoderConfig | None = None,
) -> RerankResult:
    """Async wrapper for cross-encoder reranking.

    Runs reranking in a thread pool to avoid blocking the event loop.

    Args:
        query: Original search query.
        results: Initial results as (entity, score) tuples.
        config: Reranking configuration.

    Returns:
        RerankResult with reranked results and metadata.
    """
    if config is None:
        config = CrossEncoderConfig()

    # Return unchanged if reranking is disabled
    if not config.enabled:
        return RerankResult(
            results=results,
            reranked_count=0,
            model_name=None,
            metadata={"reranking_skipped": "disabled"},
        )

    # Return unchanged if no results
    if not results:
        return RerankResult(
            results=[],
            reranked_count=0,
            model_name=config.model_name,
            metadata={"reranking_skipped": "no_results"},
        )

    try:
        # Load model (cached). Returns None when the optional reranking extra
        # (sentence-transformers) is not installed; fall back to the fused order.
        model = get_cross_encoder(config.model_name, config.use_gpu)
        if model is None:
            return RerankResult(
                results=results,
                reranked_count=0,
                model_name=None,
                metadata={"reranking_skipped": "sentence_transformers_not_installed"},
            )

        # Run reranking in thread pool (CPU-bound)
        loop = asyncio.get_event_loop()
        reranked = await loop.run_in_executor(
            None,
            lambda: cross_encoder_rerank(
                query=query,
                results=results,
                model=model,
                top_k=config.top_k,
                batch_size=config.batch_size,
                min_score=config.min_score,
            ),
        )

        return RerankResult(
            results=reranked,
            reranked_count=min(len(results), config.top_k),
            model_name=config.model_name,
            metadata={
                "top_k": config.top_k,
                "original_count": len(results),
                "final_count": len(reranked),
            },
        )

    except ImportError as e:
        log.warning("sentence_transformers_not_available", error=str(e))
        if config.fallback_on_error:
            return RerankResult(
                results=results,
                reranked_count=0,
                model_name=None,
                metadata={"reranking_skipped": "sentence_transformers_not_installed"},
            )
        raise

    except Exception as e:
        log.exception("cross_encoder_rerank_failed", error=str(e))
        if config.fallback_on_error:
            return RerankResult(
                results=results,
                reranked_count=0,
                model_name=config.model_name,
                metadata={"reranking_failed": str(e)},
            )
        raise


async def cohere_rerank[T](
    query: str,
    results: list[tuple[T, float]],
    api_key: str | None = None,
    model: str = "rerank-english-v3.0",
    top_k: int = 20,
) -> RerankResult:
    """Rerank using Cohere's Rerank API.

    Alternative to local cross-encoder when:
    - Better accuracy needed (Cohere models are larger)
    - No GPU available for local inference
    - Willing to pay for API calls

    Args:
        query: Original search query.
        results: Initial results.
        api_key: Cohere API key (uses COHERE_API_KEY env var if not provided).
        model: Cohere rerank model name.
        top_k: Number of top results to return.

    Returns:
        RerankResult with reranked results.
    """
    import os

    api_key = api_key or os.environ.get("COHERE_API_KEY")
    if not api_key:
        log.warning("cohere_api_key_not_found")
        return RerankResult(
            results=results,
            reranked_count=0,
            model_name=None,
            metadata={"reranking_skipped": "no_api_key"},
        )

    try:
        import cohere

        client = cohere.Client(api_key)

        # Prepare documents
        documents = [_extract_content(entity)[:2000] for entity, _ in results[: top_k * 2]]

        # Call Cohere Rerank API
        response = client.rerank(
            query=query,
            documents=documents,
            top_n=top_k,
            model=model,
        )

        # Map results back to entities
        entity_map = {i: entity for i, (entity, _) in enumerate(results)}
        reranked: list[tuple[T, float]] = []

        for result in response.results:
            idx = result.index
            if idx in entity_map:
                reranked.append((entity_map[idx], result.relevance_score))

        return RerankResult(
            results=reranked,
            reranked_count=len(reranked),
            model_name=model,
            metadata={
                "api": "cohere",
                "original_count": len(results),
                "top_n": top_k,
            },
        )

    except ImportError:
        log.warning("cohere_not_installed")
        return RerankResult(
            results=results,
            reranked_count=0,
            model_name=None,
            metadata={"reranking_skipped": "cohere_not_installed"},
        )

    except Exception as e:
        log.exception("cohere_rerank_failed", error=str(e))
        return RerankResult(
            results=results,
            reranked_count=0,
            model_name=model,
            metadata={"reranking_failed": str(e)},
        )
