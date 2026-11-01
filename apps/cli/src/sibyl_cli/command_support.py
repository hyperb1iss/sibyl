"""Shared identifier parsing for CLI command modules."""

from __future__ import annotations


def parse_csv_ids(value: str | None) -> list[str]:
    """Parse a comma-separated identifier option."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def append_unique_ids(existing: list[str], additions: list[str]) -> list[str]:
    """Append identifiers while preserving their first-seen order."""
    seen = set(existing)
    combined = list(existing)
    for item in additions:
        if item not in seen:
            combined.append(item)
            seen.add(item)
    return combined


def parse_id_args(values: list[str]) -> list[str]:
    """Parse positional identifier arguments that may each contain CSV values."""
    ids: list[str] = []
    for value in values:
        ids = append_unique_ids(ids, parse_csv_ids(value))
    return ids
