"""Canonical importance and confidence metadata for memory records."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

LEGACY_IMPORTANCE_KEYS = ("retention_importance", "memory_importance")
LEGACY_CONFIDENCE_KEYS = (
    "promotion_confidence",
    "reflection_confidence",
    "projection_confidence",
    "share_confidence",
)


def memory_metadata_score(metadata: Mapping[str, object], key: str) -> float | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        score = float(value)
    except ValueError:
        return None
    if not isfinite(score):
        return None
    return max(0.0, min(score, 1.0))


def normalize_memory_quality_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(metadata)
    importance = _first_score(
        normalized,
        ("retention_importance", "importance", "memory_importance"),
    )
    confidence = _first_score(
        normalized,
        (
            "promotion_confidence",
            "reflection_confidence",
            "projection_confidence",
            "confidence",
            "share_confidence",
        ),
    )
    for key in (*LEGACY_IMPORTANCE_KEYS, *LEGACY_CONFIDENCE_KEYS):
        normalized.pop(key, None)
    if importance is not None:
        normalized["importance"] = importance
    if confidence is not None:
        normalized["confidence"] = confidence
    return normalized


def expand_memory_quality_storage_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    expanded = normalize_memory_quality_metadata(metadata)
    importance = memory_metadata_score(expanded, "importance")
    confidence = memory_metadata_score(expanded, "confidence")
    if importance is not None:
        for key in LEGACY_IMPORTANCE_KEYS:
            expanded[key] = importance
    if confidence is not None:
        for key in LEGACY_CONFIDENCE_KEYS:
            expanded[key] = confidence
    return expanded


def _first_score(metadata: Mapping[str, object], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if (score := memory_metadata_score(metadata, key)) is not None:
            return score
    return None


__all__ = [
    "LEGACY_CONFIDENCE_KEYS",
    "LEGACY_IMPORTANCE_KEYS",
    "expand_memory_quality_storage_metadata",
    "memory_metadata_score",
    "normalize_memory_quality_metadata",
]
