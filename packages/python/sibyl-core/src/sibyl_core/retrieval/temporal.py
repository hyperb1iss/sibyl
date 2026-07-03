"""Temporal boosting for search results.

Applies exponential decay to older entities so recent knowledge ranks higher.
Uses the formula: boosted_score = original_score * exp(-age_days / decay_days)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

log = structlog.get_logger()


class HasTimestamp(Protocol):
    """Protocol for objects with timestamp attributes."""

    @property
    def created_at(self) -> datetime | None: ...

    @property
    def valid_from(self) -> datetime | None: ...

    @property
    def valid_at(self) -> datetime | None: ...


_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_TIME_UNIT_DAYS = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}

_TEMPORAL_DATETIME_FORMATS = (
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)

EXPOSURE_DECAY_TIMESTAMP_WEIGHT = 0.90
LEGACY_ACCESS_DECAY_TIMESTAMP_WEIGHT = 0.75


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
        field: Which field to use ('created_at', 'valid_at', 'valid_from', 'auto').
               'auto' tries valid_at, then valid_from, then created_at.

    Returns:
        Datetime or None if no timestamp found.
    """
    if field == "auto":
        for candidate_field in ("valid_at", "valid_from", "created_at"):
            timestamp = get_entity_timestamp(entity, candidate_field)
            if timestamp is not None:
                return timestamp
        return None

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

    return parse_temporal_datetime(value)


def get_entity_decay_timestamp(entity: Any, field: str = "auto") -> datetime | None:
    """Extract the timestamp ordinary decay should use for an entity."""
    if field != "auto":
        return get_entity_timestamp(entity, field)

    base_timestamp = get_entity_timestamp(entity, "auto")
    last_used_at = get_entity_timestamp(entity, "last_used_at")
    last_recalled_at = get_entity_timestamp(entity, "last_recalled_at")
    last_accessed_at = get_entity_timestamp(entity, "last_accessed_at")

    usage_timestamps: list[datetime] = []
    if last_used_at is not None:
        usage_timestamps.append(_aware_datetime(last_used_at))
    if last_recalled_at is not None:
        usage_timestamps.append(
            _weighted_usage_timestamp(
                base_timestamp,
                last_recalled_at,
                weight=EXPOSURE_DECAY_TIMESTAMP_WEIGHT,
            )
        )
    if last_accessed_at is not None:
        accessed_timestamp = _weighted_usage_timestamp(
            base_timestamp,
            last_accessed_at,
            weight=LEGACY_ACCESS_DECAY_TIMESTAMP_WEIGHT,
        )
        if last_used_at is not None:
            accessed_timestamp = min(accessed_timestamp, _aware_datetime(last_used_at))
        usage_timestamps.append(accessed_timestamp)
    if usage_timestamps:
        usage_timestamp = max(usage_timestamps)
        if base_timestamp is not None:
            return max(_aware_datetime(base_timestamp), usage_timestamp)
        return usage_timestamp
    return base_timestamp


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _weighted_usage_timestamp(
    base_timestamp: datetime | None,
    usage_timestamp: datetime,
    *,
    weight: float,
) -> datetime:
    usage_timestamp = _aware_datetime(usage_timestamp)
    if base_timestamp is None:
        return usage_timestamp
    base_timestamp = _aware_datetime(base_timestamp)
    if usage_timestamp <= base_timestamp:
        return base_timestamp
    return base_timestamp + (usage_timestamp - base_timestamp) * weight


def usage_retention_multiplier(entity: Any) -> float:
    """Return a bounded half-life multiplier from W6 usage counters."""
    retrieval_count = _entity_int(entity, "retrieval_count")
    citation_count = _entity_int(entity, "citation_count")
    retrieval_bonus = min(max(retrieval_count, 0), 50) * 0.02
    citation_bonus = min(max(citation_count, 0), 20) * 0.12
    return min(4.0, 1.0 + retrieval_bonus + citation_bonus)


def temporal_decay_multiplier(
    entity: Any,
    *,
    decay_days: float = 365.0,
    min_boost: float = 0.1,
    max_age_days: float = 1825.0,
    timestamp_field: str = "auto",
    reference_time: datetime | None = None,
) -> float:
    """Calculate the usage-aware temporal multiplier for ordinary recency decay."""
    if reference_time is None:
        reference_time = datetime.now(UTC)

    timestamp = get_entity_decay_timestamp(entity, timestamp_field)
    if timestamp is None:
        return 1.0

    age_days = calculate_age_days(timestamp, reference_time)
    adjusted_decay_days = max(decay_days * usage_retention_multiplier(entity), 1.0)
    return calculate_boost(age_days, adjusted_decay_days, min_boost, max_age_days)


def parse_temporal_datetime(value: Any) -> datetime | None:
    """Parse datetime values used by graph records and eval metadata."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None

    cleaned = re.sub(r"\s+\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\)", "", value.strip())
    if not cleaned:
        return None

    iso_value = cleaned.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_value)
    except ValueError:
        pass

    for date_format in _TEMPORAL_DATETIME_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).replace(tzinfo=UTC)
        except ValueError:
            continue

    return None


def _parse_relative_count(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return _NUMBER_WORDS.get(value.lower())


def _align_to_weekday(target: datetime, query: str) -> datetime:
    normalized = query.lower()
    matched_weekday = next(
        (weekday for name, weekday in _WEEKDAYS.items() if name in normalized),
        None,
    )
    if matched_weekday is None:
        return target

    candidates = [target + timedelta(days=offset) for offset in range(-3, 4)]
    return min(
        candidates,
        key=lambda candidate: min(
            abs(candidate.weekday() - matched_weekday),
            7 - abs(candidate.weekday() - matched_weekday),
        ),
    )


def resolve_temporal_reference(
    query: str,
    reference_time: datetime | None,
) -> datetime | None:
    """Resolve relative temporal language in a query against an as-of time."""
    if reference_time is None:
        return None
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)

    normalized = query.lower()
    target: datetime | None = None

    relative_match = re.search(
        r"\b(?P<count>\d+|[a-z]+)\s+(?P<unit>days?|weeks?|months?|years?)\s+ago\b",
        normalized,
    )
    if relative_match is not None:
        count = _parse_relative_count(relative_match.group("count"))
        unit_days = _TIME_UNIT_DAYS.get(relative_match.group("unit"))
        if count is not None and unit_days is not None:
            target = reference_time - timedelta(days=count * unit_days)
    elif "yesterday" in normalized:
        target = reference_time - timedelta(days=1)
    elif "last week" in normalized:
        target = reference_time - timedelta(days=7)
    elif "last month" in normalized:
        target = reference_time - timedelta(days=30)
    elif "last year" in normalized:
        target = reference_time - timedelta(days=365)
    elif "recently" in normalized:
        target = reference_time - timedelta(days=7)
    elif "today" in normalized:
        target = reference_time

    if target is None:
        return None
    return _align_to_weekday(target, normalized)


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
        timestamp = get_entity_decay_timestamp(entity, timestamp_field)
        if timestamp is None:
            # No timestamp - keep original score
            boosted_results.append((entity, score))
            boost_stats["no_timestamp"] += 1
            continue

        boost = temporal_decay_multiplier(
            entity,
            decay_days=decay_days,
            min_boost=min_boost,
            max_age_days=max_age_days,
            timestamp_field=timestamp_field,
            reference_time=reference_time,
        )
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


def temporal_proximity_boost(
    results: list[tuple[Any, float]],
    target_time: datetime | None,
    timestamp_field: str = "auto",
) -> list[tuple[Any, float]]:
    """Boost records whose timestamp is close to an explicit query time."""
    if not results or target_time is None:
        return list(results)
    if target_time.tzinfo is None:
        target_time = target_time.replace(tzinfo=UTC)

    boosted_results: list[tuple[Any, float]] = []
    for entity, score in results:
        timestamp = get_entity_timestamp(entity, timestamp_field)
        if timestamp is None:
            boosted_results.append((entity, score))
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        distance_days = abs((target_time - timestamp).total_seconds()) / 86400.0
        if distance_days <= 3:
            multiplier = 1.4
        elif distance_days <= 7:
            multiplier = 1.25
        elif distance_days <= 14:
            multiplier = 1.1
        else:
            multiplier = 1.0
        boosted_results.append((entity, score * multiplier))

    boosted_results.sort(key=lambda x: x[1], reverse=True)
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

    boost = temporal_decay_multiplier(
        entity,
        decay_days=config.decay_days,
        min_boost=config.min_boost,
        max_age_days=config.max_age_days,
        timestamp_field=config.timestamp_field,
        reference_time=reference_time,
    )

    return score * boost


def _entity_int(entity: Any, field: str) -> int:
    value: Any = None
    if isinstance(entity, dict):
        value = entity.get(field)
        if value is None:
            metadata = entity.get("metadata", {})
            value = metadata.get(field) if isinstance(metadata, dict) else None
    else:
        value = getattr(entity, field, None)
        if value is None:
            metadata = getattr(entity, "metadata", None)
            if isinstance(metadata, dict):
                value = metadata.get(field)

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
