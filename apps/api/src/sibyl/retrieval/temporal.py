"""Temporal boosting for search results.

Applies exponential decay to older entities so recent knowledge ranks higher.
Uses the formula: boosted_score = original_score * exp(-age_days / decay_days)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

log = structlog.get_logger()


class HasTimestamp(Protocol):
    """Protocol for objects with timestamp attributes."""

    @property
    def created_at(self) -> datetime | None: ...

    @property
    def valid_from(self) -> datetime | None: ...


@dataclass
class TemporalConfig:
    """Configuration for temporal boosting.

    Attributes:
        decay_days: Half-life in days for exponential decay (default 365).
        min_boost: Minimum boost multiplier (prevents total suppression).
        max_age_days: Maximum age to consider (older gets min_boost).
        timestamp_field: Which field to use ('created_at', 'valid_from', 'auto').
    """

    decay_days: float = 365.0
    min_boost: float = 0.1
    max_age_days: float = 1825.0  # 5 years
    timestamp_field: str = "auto"


def get_entity_timestamp(entity: Any, field: str = "auto") -> datetime | None:
    """Extract timestamp from an entity.

    Args:
        entity: Entity object or dict.
        field: Which field to use ('created_at', 'valid_from', 'auto').
               'auto' tries valid_from first, then created_at.

    Returns:
        Datetime or None if no timestamp found.
    """
    if field == "auto":
        # Try valid_from first (more semantically correct for knowledge)
        ts = get_entity_timestamp(entity, "valid_from")
        if ts is not None:
            return ts
        return get_entity_timestamp(entity, "created_at")

    # Handle dict-like objects
    if isinstance(entity, dict):
        value = entity.get(field)
        if value is None:
            # Check metadata
            metadata = entity.get("metadata", {})
            value = metadata.get(field) if isinstance(metadata, dict) else None
    else:
        # Handle object attributes
        value = getattr(entity, field, None)
        if value is None:
            # Check metadata attribute
            metadata = getattr(entity, "metadata", None)
            if isinstance(metadata, dict):
                value = metadata.get(field)

    # Parse string timestamps
    if isinstance(value, str):
        try:
            # Handle ISO format with timezone
            value = datetime.fromisoformat(value)
        except ValueError:
            return None

    return value if isinstance(value, datetime) else None


def calculate_age_days(timestamp: datetime, reference: datetime | None = None) -> float:
    """Calculate age in days from a timestamp.

    Args:
        timestamp: The timestamp to measure from.
        reference: Reference time (defaults to now).

    Returns:
        Age in days (float).
    """
    if reference is None:
        reference = datetime.now(UTC)

    # Ensure both are timezone-aware
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    delta = reference - timestamp
    return max(0.0, delta.total_seconds() / 86400.0)


def calculate_boost(
    age_days: float,
    decay_days: float = 365.0,
    min_boost: float = 0.1,
    max_age_days: float = 1825.0,
) -> float:
    """Calculate temporal boost factor using exponential decay.

    Formula: boost = max(min_boost, exp(-age_days / decay_days))

    Args:
        age_days: Age of the entity in days.
        decay_days: Half-life for decay (larger = slower decay).
        min_boost: Minimum boost (prevents total suppression).
        max_age_days: Age beyond which min_boost is used.

    Returns:
        Boost multiplier between min_boost and 1.0.
    """
    if age_days >= max_age_days:
        return min_boost

    # Exponential decay: e^(-age/decay)
    # At age=decay_days, boost ≈ 0.368 (1/e)
    boost = math.exp(-age_days / decay_days)

    return max(min_boost, boost)


def temporal_boost(
    results: list[tuple[Any, float]],
    decay_days: float = 365.0,
    min_boost: float = 0.1,
    max_age_days: float = 1825.0,
    timestamp_field: str = "auto",
    reference_time: datetime | None = None,
) -> list[tuple[Any, float]]:
    """Apply temporal boosting to search results.

    Multiplies each result's score by a decay factor based on entity age.
    Recent entities get higher effective scores.

    Args:
        results: List of (entity, score) tuples.
        decay_days: Half-life for decay in days (default 365 = 1 year).
        min_boost: Minimum boost factor (default 0.1).
        max_age_days: Maximum age to consider (default 5 years).
        timestamp_field: Which timestamp to use ('created_at', 'valid_from', 'auto').
        reference_time: Reference time for age calculation (default: now).

    Returns:
        New list of (entity, boosted_score) tuples, re-sorted by score.

    Example:
        >>> results = [(entity1, 0.9), (entity2, 0.8)]
        >>> boosted = temporal_boost(results, decay_days=30)
        >>> # Recent entity1 keeps high score, old entity2 gets reduced
    """
    if not results:
        return []

    if reference_time is None:
        reference_time = datetime.now(UTC)

    boosted_results: list[tuple[Any, float]] = []
    boost_stats = {"boosted": 0, "unchanged": 0, "no_timestamp": 0}

    for entity, score in results:
        timestamp = get_entity_timestamp(entity, timestamp_field)

        if timestamp is None:
            # No timestamp - keep original score
            boosted_results.append((entity, score))
            boost_stats["no_timestamp"] += 1
            continue

        age_days = calculate_age_days(timestamp, reference_time)
        boost = calculate_boost(age_days, decay_days, min_boost, max_age_days)
        boosted_score = score * boost

        boosted_results.append((entity, boosted_score))

        if boost < 1.0:
            boost_stats["boosted"] += 1
        else:
            boost_stats["unchanged"] += 1

    # Re-sort by boosted score (descending)
    boosted_results.sort(key=lambda x: x[1], reverse=True)

    log.debug(
        "temporal_boost_applied",
        total=len(results),
        **boost_stats,
        decay_days=decay_days,
    )

    return boosted_results


def temporal_boost_single(
    entity: Any,
    score: float,
    config: TemporalConfig | None = None,
    reference_time: datetime | None = None,
) -> float:
    """Apply temporal boosting to a single entity score.

    Convenience function for single entity boosting.

    Args:
        entity: The entity to boost.
        score: Original relevance score.
        config: Temporal configuration (uses defaults if None).
        reference_time: Reference time for age calculation.

    Returns:
        Boosted score.
    """
    if config is None:
        config = TemporalConfig()

    if reference_time is None:
        reference_time = datetime.now(UTC)

    timestamp = get_entity_timestamp(entity, config.timestamp_field)

    if timestamp is None:
        return score

    age_days = calculate_age_days(timestamp, reference_time)
    boost = calculate_boost(
        age_days,
        config.decay_days,
        config.min_boost,
        config.max_age_days,
    )

    return score * boost
