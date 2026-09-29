"""Read-only lifecycle checks for graph rows backed by canonical captures."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
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
    if not isinstance(metadata, Mapping):
        raise ValueError("capture metadata must be a mapping")
    bindings = metadata.get(SOURCE_BINDINGS_KEY)
    if bindings is None:
        bindings = {}
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
    organization_id: str,
    rows: Mapping[str, T],
    *,
    graph_client=None,
    source_visible: Callable[[Any], bool] | None = None,
) -> dict[str, T]:
    """Apply current source verdicts virtually without updating rows or bindings.

    Canonical capture IDs are exact identities, never source grouping keys.
    Batch loads share roots across rows and walk retained capture ancestry.
    Lookup failures exclude dependent rows while unrelated rows remain readable.
    Unbound legacy projections also retain their ancestors' reader audience.
    Bound publications retain the authority verified by their derivation ledger.
    """
    graph_rows, parents = await _graph_ancestry(organization_id, rows, graph_client)
    references: dict[str, set[str]] = {}
    legacy_audiences: set[str] = set()
    for identifier in rows:
        try:
            metadata = getattr(rows[identifier], "metadata", None) or {}
            if not isinstance(metadata, Mapping):
                raise ValueError("graph metadata must be a mapping")
            legacy = not metadata.get(SOURCE_BINDINGS_KEY) and bool(
                _projection_row(rows[identifier]) or parents.get(identifier)
            )
            if legacy:
                legacy_audiences.add(identifier)
            ancestry = _ancestry(identifier, parents, graph_rows)
            roots: set[str] = set()
            for ancestor in ancestry:
                row = graph_rows[ancestor]
                if ancestor != identifier and (
                    getattr(row, "organization_id", None) != organization_id
                    or not graph_metadata_recallable(getattr(row, "metadata", None))
                    or (legacy and source_visible is not None and not source_visible(row))
                ):
                    raise ValueError("graph source is unavailable")
                roots.update(_capture_ids(getattr(row, "metadata", None) or {}))
            references[identifier] = roots
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
                    try:
                        records = await content_client.select_many(
                            client,
                            "SELECT * OMIT raw_content, embedding FROM raw_captures "
                            "WHERE organization_id = $organization_id "
                            "AND uuid IN $source_ids;",
                            organization_id=organization_id,
                            source_ids=batch,
                        )
                    except Exception as exc:
                        log.warning(
                            "graph_capture_lifecycle_lookup_failed",
                            dependent_capture_count=len(batch),
                            error_type=type(exc).__name__,
                        )
                        continue
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
        try:
            for root in _capture_ancestry(roots, dependencies, captures):
                memory = captures.get(root)
                if memory is None:
                    raise ValueError("capture ancestry is unavailable")
                if (
                    identifier in legacy_audiences
                    and source_visible is not None
                    and not source_visible(_capture_policy_row(memory))
                ):
                    raise ValueError("capture source is unreadable")
                event = correction_event(
                    memory,
                    blocking=not raw_memory_lifecycle_recallable(
                        memory, include_source_corrections=False
                    ),
                )
                if event.blocking:
                    raise ValueError("capture is retired")
                metadata = merge_source_correction(metadata, event)
            if graph_metadata_recallable(metadata):
                available[identifier] = row
        except (TypeError, ValueError):
            continue
    return available


def _projection_row(row: Any) -> bool:
    metadata = getattr(row, "metadata", None) or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("graph metadata must be a mapping")
    entity_type = getattr(row, "entity_type", None) or getattr(row, "type", None)
    return bool(metadata.get("projection_kind")) or getattr(entity_type, "value", entity_type) in {
        "passage",
        "memory_fact",
        "proper_noun",
        "action_object",
        "quoted_phrase",
    }


def _parent_ids(row: Any) -> set[str]:
    metadata = getattr(row, "metadata", None) or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("graph metadata must be a mapping")
    ids: set[str] = set()
    keys = ["parent_entity_id"]
    if _projection_row(row):
        keys.append("source_entity_id")
    for key in keys:
        if key not in metadata:
            continue
        identifier = metadata[key]
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("graph parent identity is invalid")
        ids.add(identifier)
    return ids


async def _graph_ancestry(organization_id, rows, graph_client):
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.services.graph_records import entity_from_surreal_row

    current = dict(rows)
    parents: dict[str, set[str] | None] = {}
    frontier = set(rows)
    while frontier:
        for identifier in frontier:
            row = current.get(identifier)
            try:
                parents[identifier] = _parent_ids(row) if row is not None else None
            except (TypeError, ValueError):
                parents[identifier] = None
        requested = set().union(*(parents[key] or set() for key in frontier)) - current.keys()
        if not requested:
            break
        current.update(dict.fromkeys(requested))
        try:
            if graph_client is None:
                from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

                graph_client = (await get_surreal_graph_runtime(organization_id)).client
            for batch in content_client.value_batches(sorted(requested)):
                try:
                    records = normalize_records(
                        await graph_client.execute_query(
                            "SELECT * OMIT content, embedding, name_embedding, attributes.content "
                            "FROM entity WHERE group_id=$organization_id AND uuid IN $parent_ids;",
                            organization_id=organization_id,
                            parent_ids=batch,
                        )
                    )
                    for record in records:
                        try:
                            parent = entity_from_surreal_row(record)
                            if (
                                parent.id in requested
                                and parent.organization_id == organization_id
                                and parent.observed_revision is not None
                            ):
                                current[parent.id] = parent
                        except (TypeError, ValueError, KeyError):
                            continue
                except Exception as exc:
                    log.warning(
                        "graph_parent_lookup_failed",
                        dependent_parent_count=len(batch),
                        error_type=type(exc).__name__,
                    )
        except Exception as exc:
            log.warning(
                "graph_parent_lookup_failed",
                dependent_parent_count=len(requested),
                error_type=type(exc).__name__,
            )
        frontier = requested
    return current, parents


def _ancestry(identifier, dependencies, rows):
    # Walk each path explicitly so shared roots are valid and cycles are not.
    visited: set[str] = set()
    active: set[str] = set()
    stack = [(identifier, False)]
    while stack:
        key, leaving = stack.pop()
        if leaving:
            active.remove(key)
            visited.add(key)
            continue
        if key in active:
            raise ValueError("source ancestry is cyclic")
        if key in visited:
            continue
        if rows.get(key) is None or dependencies.get(key) is None:
            raise ValueError("source ancestry is unavailable")
        active.add(key)
        yield key
        stack.append((key, True))
        stack.extend((parent, False) for parent in dependencies[key])


def _capture_ancestry(roots, dependencies, captures):
    seen: set[str] = set()
    for root in roots:
        for identifier in _ancestry(root, dependencies, captures):
            if identifier not in seen:
                seen.add(identifier)
                yield identifier


def _capture_policy_row(memory):
    # Capture columns are authoritative even when graph metadata disagrees.
    return replace(
        memory,
        metadata={
            **memory.metadata,
            "memory_scope": memory.memory_scope,
            "scope_key": memory.scope_key,
            "principal_id": memory.principal_id,
            "project_id": memory.project_id,
            "agent_id": memory.agent_id,
        },
    )
