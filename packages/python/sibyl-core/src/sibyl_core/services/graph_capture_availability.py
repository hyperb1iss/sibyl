"""Read-only lifecycle checks for graph rows backed by canonical captures."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import structlog

from sibyl_core.memory_pipeline.lifecycle import (
    graph_metadata_recallable,
    raw_memory_lifecycle_recallable,
)
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_BINDINGS_KEY,
    correction_event,
    merge_source_correction,
)
from sibyl_core.services import content_client, content_models
from sibyl_core.services.memory_source_validation import _stored_source_ids

log = structlog.get_logger()


def _capture_ids(metadata: Mapping[str, object]) -> set[str]:
    bindings = metadata.get(SOURCE_BINDINGS_KEY) or {}
    if not isinstance(bindings, Mapping):
        raise ValueError("source bindings must be a mapping")
    ids: set[str] = set()
    for identifier, revision in bindings.items():
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("source binding id is invalid")
        if type(revision) is not int or revision < 0:
            raise ValueError("source binding revision is invalid")
        if not re.fullmatch(r"reflection:input:[0-9a-f]{16}", identifier):
            ids.add(identifier)
    canonical_id = metadata.get("raw_memory_id")
    if canonical_id is not None:
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise ValueError("canonical capture id is invalid")
        ids.add(canonical_id)
    return ids


async def available_capture_projection_rows[T](
    organization_id: str, rows: Mapping[str, T]
) -> dict[str, T]:
    """Apply current source verdicts virtually without updating rows or bindings.

    Canonical capture IDs are exact identities, never source grouping keys.
    Batch loads share roots across rows and walk retained capture ancestry.
    Lookup failures exclude dependent rows while unrelated rows remain readable.
    """
    references: dict[str, set[str]] = {}
    for identifier, row in rows.items():
        try:
            references[identifier] = _capture_ids(getattr(row, "metadata", None) or {})
        except (TypeError, ValueError):
            continue
    frontier = set().union(*references.values()) if references else set()
    captures: dict[str, content_models.RawMemory | None] = {}
    dependencies: dict[str, set[str]] = {}
    while frontier:
        requested = frontier - captures.keys()
        if not requested:
            break
        captures.update(dict.fromkeys(requested))
        try:
            async with content_client.surreal_content_client() as client:
                for batch in content_client.value_batches(sorted(requested)):
                    records = await content_client.select_many(
                        client,
                        "SELECT * FROM raw_captures WHERE organization_id = $organization_id "
                        "AND uuid IN $source_ids;",
                        organization_id=organization_id,
                        source_ids=batch,
                    )
                    for record in records:
                        try:
                            memory = content_models.raw_memory_from_record(record)
                            if (
                                memory.id not in requested
                                or memory.organization_id != organization_id
                                or memory.observed_revision is None
                            ):
                                continue
                            dependencies[memory.id] = _stored_source_ids(memory)
                            captures[memory.id] = memory
                        except (TypeError, ValueError):
                            continue
        except Exception as exc:
            log.warning(
                "graph_capture_lifecycle_lookup_failed",
                dependent_capture_count=len(requested),
                error_type=type(exc).__name__,
            )
        frontier = set().union(*(dependencies.get(key, set()) for key in requested))
    available: dict[str, T] = {}
    for identifier, roots in references.items():
        row = rows[identifier]
        metadata: dict[str, Any] = dict(getattr(row, "metadata", None) or {})
        pending = set(roots)
        seen: set[str] = set()
        try:
            while pending:
                root = pending.pop()
                if root in seen:
                    continue
                seen.add(root)
                memory = captures.get(root)
                if memory is None:
                    raise ValueError("capture ancestry is unavailable")
                event = correction_event(
                    memory,
                    blocking=not raw_memory_lifecycle_recallable(
                        memory, include_source_corrections=False
                    ),
                )
                if event.blocking:
                    raise ValueError("capture is retired")
                metadata = merge_source_correction(metadata, event)
                pending.update(dependencies.get(root, set()) - seen)
            if graph_metadata_recallable(metadata):
                available[identifier] = row
        except (TypeError, ValueError):
            continue
    return available
