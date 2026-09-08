"""Keep independent projection authorities from clearing each other's work."""

from collections.abc import Mapping
from typing import Any

from sibyl_core.memory_pipeline.lifecycle import RECONCILE_PENDING_KEY
from sibyl_core.memory_pipeline.source_lifecycle import SOURCE_VALIDATION_PENDING_KEY

PENDING_KEYS = (RECONCILE_PENDING_KEY, SOURCE_VALIDATION_PENDING_KEY)
_LEGACY_OWNER = "unknown"


def inherited_pending(metadata: Mapping[str, Any], authority: str) -> dict[str, Any]:
    """An inherited exclusion belongs to the immediate parent that supplied it."""
    return {key: {authority: True} for key in PENDING_KEYS if metadata.get(key)}


def pending_patch(
    metadata: Mapping[str, Any],
    verdict: Mapping[str, Any],
    *,
    authority: str,
) -> dict[str, Any]:
    """Replace only one authority's pending verdict under the caller's row CAS.

    Boolean markers predate source ownership. Retain those as unknown rather
    than allowing an arbitrary source to certify them. New inherited markers
    acquire an owner before their derived row is inserted.
    """
    patch: dict[str, Any] = {}
    for key in PENDING_KEYS:
        previous = metadata.get(key)
        owners = dict(previous) if isinstance(previous, Mapping) else {}
        if previous and not isinstance(previous, Mapping):
            owners[_LEGACY_OWNER] = True
        if verdict.get(key):
            owners[authority] = True
        else:
            owners.pop(authority, None)
        value = owners or None
        if previous != value:
            patch[key] = value
    return patch
