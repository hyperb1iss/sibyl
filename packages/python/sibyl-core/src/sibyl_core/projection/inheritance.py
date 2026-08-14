"""What a projected row inherits from the memory it was derived from.

Every projection mints rows that carry some of a parent memory's text: spans
carry its words verbatim, projected entities carry its candidate context,
projected facts carry their span and content. Each of those rows is
independently indexed and independently servable, so a reader can reach the
parent's content through a row the parent's own lifecycle never touched.

Scope inheritance has always been here. Lifecycle inheritance is the same idea
applied to the same problem one level down: a derived row of a retired memory
that reads as active serves retired text under an id the retirement never
named.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

log = structlog.get_logger()

# What a correction stamps onto a memory. A derived row that does not carry
# these keeps serving the retired body under its own id.
LIFECYCLE_METADATA_KEYS = (
    "lifecycle_state",
    "lifecycle_flags",
    "lifecycle_action",
    "excluded_from_recall",
    "superseded_by_source_id",
    "duplicate_of_source_id",
)


def inherited_lifecycle_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """The lifecycle half of what a derived row inherits from its parent."""

    fields = metadata if isinstance(metadata, Mapping) else {}
    return {
        key: fields[key]
        for key in LIFECYCLE_METADATA_KEYS
        if key in fields and fields[key] is not None
    }


async def parent_lifecycle_as_stored(
    entity_manager: Any,
    source: Any,
    *,
    source_id: str,
) -> Any:
    """Re-read the parent's lifecycle, keeping the caller's body and scope.

    Projection is never one write. A correction landing between the parent
    write and this one stamps the parent, finds no derived rows because none
    exist yet, and returns; the projection then derives rows from the caller's
    copy of the parent, which predates the verdict. The stored row is the
    authority for lifecycle, so it is read rather than assumed.

    Only lifecycle is taken from storage. The body stays the caller's, because
    a re-projection derives from the text it was handed and a caller mid-update
    holds the newer copy.

    A read that fails raises, and the caller decides. This is the pre-write
    half of a bracket: the post-write pass in `projection/reconcile.py` reads
    the same verdict again after the rows commit and refuses to leave an
    unverified row servable, so a caller that cannot read the parent here does
    not have to choose between poisoning its job and minting a recallable copy
    of possibly-retired text.

    A parent that is simply absent is not a failure. Nothing has been stamped
    on a row that does not exist, so there is no verdict to inherit.
    """

    get = getattr(entity_manager, "get", None)
    if not callable(get):
        return source
    try:
        stored = await get(source_id)
    except KeyError:
        # The real manager raises this for a row that is not there
        # (`services/graph.py`). An absent parent is not an unreadable one:
        # nothing was stamped on a row that does not exist, so there is no
        # verdict to inherit and nothing to fail over.
        return source
    stored_metadata = getattr(stored, "metadata", None) if stored is not None else None
    if not isinstance(stored_metadata, Mapping):
        if stored is not None:
            log.warning("projection_parent_metadata_unreadable", source_id=source_id)
        return source
    carried = inherited_lifecycle_metadata(stored_metadata)
    if not carried:
        return source
    return source.model_copy(update={"metadata": {**(source.metadata or {}), **carried}})


__all__ = [
    "LIFECYCLE_METADATA_KEYS",
    "inherited_lifecycle_metadata",
    "parent_lifecycle_as_stored",
]
