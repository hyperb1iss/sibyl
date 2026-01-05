"""Metadata access utilities for safe extraction from entities.

These utilities eliminate the repeated `(entity.metadata or {}).get()` pattern
found throughout the codebase, providing type-safe, consistent metadata access.
"""

from __future__ import annotations

from typing import Any, TypeVar, overload

T = TypeVar("T")


def get_metadata(entity: Any) -> dict[str, Any]:
    """Get metadata dict from entity, returning empty dict if None.

    Args:
        entity: Any object with a `metadata` attribute.

    Returns:
        The metadata dict, or empty dict if metadata is None/missing.

    Example:
        >>> meta = get_metadata(entity)
        >>> project_id = meta.get("project_id")
    """
    return getattr(entity, "metadata", None) or {}


@overload
def safe_meta(entity: Any, key: str) -> Any: ...


@overload
def safe_meta(entity: Any, key: str, default: T) -> T: ...  # noqa: UP047


def safe_meta(entity: Any, key: str, default: Any = None) -> Any:
    """Safely extract a value from entity metadata.

    Replaces the common pattern: `(entity.metadata or {}).get("key", default)`

    Args:
        entity: Any object with a `metadata` attribute.
        key: The metadata key to retrieve.
        default: Default value if key is missing or metadata is None.

    Returns:
        The metadata value, or default if not found.

    Example:
        >>> status = safe_meta(entity, "status", "pending")
        >>> project_id = safe_meta(entity, "project_id")  # Returns None if missing
    """
    metadata = getattr(entity, "metadata", None) or {}
    return metadata.get(key, default)


@overload
def safe_attr(entity: Any, attr: str) -> Any: ...


@overload
def safe_attr(entity: Any, attr: str, *, default: T) -> T: ...  # noqa: UP047


@overload
def safe_attr(entity: Any, attr: str, *, meta_key: str) -> Any: ...


@overload
def safe_attr(entity: Any, attr: str, *, meta_key: str, default: T) -> T: ...  # noqa: UP047


def safe_attr(
    entity: Any,
    attr: str,
    *,
    meta_key: str | None = None,
    default: Any = None,
) -> Any:
    """Get attribute value with optional fallback to metadata.

    Replaces the pattern:
        `getattr(entity, "attr", None) or entity.metadata.get("key", default)`

    Args:
        entity: Any object with attributes and optional `metadata` dict.
        attr: The attribute name to retrieve.
        meta_key: Optional metadata key to check if attr is None/missing.
                  If not provided, falls back to using `attr` as the key.
        default: Default value if both attribute and metadata are None/missing.

    Returns:
        The attribute value, metadata value, or default.

    Example:
        >>> # Get 'category' attr, fall back to metadata['category']
        >>> category = safe_attr(entity, "category")

        >>> # Get 'tags' attr, fall back to metadata['tags'], default to []
        >>> tags = safe_attr(entity, "tags", default=[])

        >>> # Get 'status' attr, fall back to metadata['agent_status']
        >>> status = safe_attr(entity, "status", meta_key="agent_status", default="pending")
    """
    value = getattr(entity, attr, None)
    if value is not None:
        return value

    # Fall back to metadata
    fallback_key = meta_key if meta_key is not None else attr
    return safe_meta(entity, fallback_key, default)


def has_meta(entity: Any, key: str) -> bool:
    """Check if entity has a specific metadata key with a truthy value.

    Args:
        entity: Any object with a `metadata` attribute.
        key: The metadata key to check.

    Returns:
        True if the key exists and has a truthy value.

    Example:
        >>> if has_meta(entity, "archived"):
        ...     skip_entity()
    """
    return bool(safe_meta(entity, key))


def match_meta(entity: Any, key: str, value: Any) -> bool:
    """Check if entity metadata key matches a specific value.

    Args:
        entity: Any object with a `metadata` attribute.
        key: The metadata key to check.
        value: The value to match against.

    Returns:
        True if the metadata value equals the given value.

    Example:
        >>> # Filter agents by status
        >>> active = [a for a in agents if match_meta(a, "status", "working")]
    """
    return safe_meta(entity, key) == value


def filter_by_meta(
    entities: list[Any],
    key: str,
    value: Any | None = None,
    *,
    exclude: bool = False,
) -> list[Any]:
    """Filter entities by metadata key/value.

    Args:
        entities: List of entities to filter.
        key: Metadata key to check.
        value: If provided, filter by exact match. If None, filter by truthy value.
        exclude: If True, return entities that DON'T match.

    Returns:
        Filtered list of entities.

    Example:
        >>> # Get non-archived agents
        >>> active = filter_by_meta(agents, "archived", exclude=True)

        >>> # Get agents with status "working"
        >>> working = filter_by_meta(agents, "status", "working")
    """
    result = []
    for entity in entities:
        matches = match_meta(entity, key, value) if value is not None else has_meta(entity, key)

        if exclude:
            if not matches:
                result.append(entity)
        elif matches:
            result.append(entity)

    return result


def extract_meta(
    entity: Any,
    *keys: str,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract multiple metadata values at once.

    Args:
        entity: Any object with a `metadata` attribute.
        *keys: Metadata keys to extract.
        defaults: Optional dict of default values per key.

    Returns:
        Dict of extracted key-value pairs.

    Example:
        >>> data = extract_meta(entity, "status", "project_id", "agent_type",
        ...                     defaults={"status": "pending"})
        >>> data["status"]  # "working" or "pending" if not set
    """
    defaults = defaults or {}
    return {key: safe_meta(entity, key, defaults.get(key)) for key in keys}
