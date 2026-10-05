"""Read-only lifecycle checks for graph rows backed by canonical captures."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

import structlog

from sibyl_core.memory_pipeline.lifecycle import (
    graph_metadata_recallable,
    raw_memory_lifecycle_recallable,
)
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_BINDINGS_KEY,
    correction_event,
    declared_source_ids,
    merge_source_correction,
)
from sibyl_core.models.entities import Entity
from sibyl_core.services import content_client, content_models
from sibyl_core.services.graph_derivations import native_reflection_id, reflection_candidate_current
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
    ids = {
        identifier
        for identifier in declared_source_ids(metadata)
        if not re.fullmatch(r"reflection:input:[0-9a-f]{16}", identifier)
    }
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
    read=None,
) -> dict[str, T]:
    """Apply current source verdicts virtually without updating rows or bindings.

    Canonical capture IDs are exact identities, never source grouping keys.
    Batch loads share roots across rows and walk retained capture ancestry.
    Lookup failures exclude dependent rows while unrelated rows remain readable.
    Source bindings describe observed content, never audience authority.
    Audience expansion requires a verified protected publication association.
    """
    if read is not None:
        read._check_org(organization_id)
        for row in rows.values():
            if isinstance(row, Entity):
                read.record_entity(row)
    candidates: dict[str, T] = {}
    dependent_ids: set[str] = set()
    for identifier, row in rows.items():
        try:
            metadata = getattr(row, "metadata", None)
            metadata = {} if metadata is None else metadata
            if not isinstance(metadata, Mapping):
                raise ValueError("graph metadata must be a mapping")
            if (
                _projection_row(row)
                or native_reflection_id(identifier)
                or _parent_ids(row)
                or _capture_ids(metadata)
                or metadata.get(SOURCE_BINDINGS_KEY)
                or getattr(row, "derivation_required", False)
            ):
                dependent_ids.add(identifier)
            candidates[identifier] = row
        except (TypeError, ValueError):
            continue
    verdicts = (
        await _publication_verdicts(
            organization_id,
            {identifier: candidates[identifier] for identifier in dependent_ids},
            graph_client=graph_client,
            read=read,
        )
        if dependent_ids
        else {}
    )
    available: dict[str, T] = {
        identifier: row
        for identifier, row in candidates.items()
        if verdicts.get(identifier) is True
        and graph_metadata_recallable(getattr(row, "metadata", None))
        and (source_visible is None or source_visible(row))
    }
    # Protected publications use their typed durable observations. Legacy
    # metadata supplies ancestry only when no protected association exists.
    legacy_rows = {
        identifier: row
        for identifier, row in candidates.items()
        if verdicts.get(identifier) is None
    }
    graph_rows, parents = await _graph_ancestry(
        organization_id,
        legacy_rows,
        graph_client,
        refresh_ids=dependent_ids & legacy_rows.keys(),
        read=read,
    )

    async def terminal_graph_id(identifier, row):
        try:
            return None if await reflection_candidate_current(row, read) else identifier
        except Exception:
            return identifier

    terminal_graph_ids = set(
        await asyncio.gather(
            *(
                terminal_graph_id(identifier, row)
                for identifier, row in graph_rows.items()
                if row is not None and native_reflection_id(identifier)
            )
        )
    )
    references: dict[str, set[str]] = {}
    for identifier in legacy_rows:
        try:
            ancestry = _ancestry(identifier, parents, graph_rows)
            roots: set[str] = set()
            for ancestor in ancestry:
                row = graph_rows[ancestor]
                if (
                    (
                        ancestor != identifier
                        and getattr(row, "organization_id", None) != organization_id
                    )
                    or ancestor in terminal_graph_ids
                    or not graph_metadata_recallable(getattr(row, "metadata", None))
                    or (source_visible is not None and not source_visible(row))
                ):
                    raise ValueError("graph source is unavailable")
                roots.update(_capture_ids(getattr(row, "metadata", None) or {}))
            if identifier in dependent_ids and not roots and not parents.get(identifier):
                raise ValueError("projection ancestry is unavailable")
            references[identifier] = roots
            if read is not None:
                read.depend_on(
                    SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, identifier),
                    [
                        SourceIdentity(organization_id, SourceKind.RAW_CAPTURE, root)
                        for root in roots
                    ],
                )
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
            async with AsyncExitStack() as stack:
                client = None
                execute_query = read.content_execute_query if read is not None else None
                if execute_query is None:
                    client = await stack.enter_async_context(
                        content_client.surreal_content_client()
                    )
                    execute_query = client.execute_query
                for batch in content_client.value_batches(sorted(requested)):
                    try:
                        query = (
                            "SELECT * OMIT raw_content, embedding FROM raw_captures "
                            "WHERE organization_id = $organization_id "
                            "AND uuid IN $source_ids;"
                        )
                        if client is not None:
                            records = await content_client.select_many(
                                client, query, organization_id=organization_id, source_ids=batch
                            )
                        else:
                            result = await execute_query(
                                query, organization_id=organization_id, source_ids=batch
                            )
                            error = content_client.query_error(result)
                            if error is not None:
                                raise RuntimeError(error)
                            records = content_client.normalize_records(result)
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
                            if read is not None:
                                read.record_capture(memory)
                                read.depend_on(
                                    SourceIdentity(
                                        organization_id, SourceKind.RAW_CAPTURE, memory.id
                                    ),
                                    [
                                        SourceIdentity(
                                            organization_id, SourceKind.RAW_CAPTURE, source_id
                                        )
                                        for source_id in dependencies[memory.id]
                                    ],
                                )
                        except (TypeError, ValueError):
                            continue
        except Exception as exc:
            log.warning(
                "graph_capture_lifecycle_lookup_failed",
                dependent_capture_count=len(requested),
                error_type=type(exc).__name__,
            )
        frontier = set().union(*(dependencies.get(key, set()) for key in requested))
    for identifier, roots in references.items():
        row = legacy_rows[identifier]
        metadata: dict[str, Any] = dict(getattr(row, "metadata", None) or {})
        try:
            for root in _capture_ancestry(roots, dependencies, captures):
                memory = captures.get(root)
                if memory is None or memory.deleted_at is not None:
                    raise ValueError("capture ancestry is unavailable")
                if source_visible is not None and not source_visible(_capture_policy_row(memory)):
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


async def _publication_verdicts(organization_id, rows, *, graph_client, read):
    from sibyl_core.services.graph_derivations import graph_publication_verdicts
    from sibyl_core.services.graph_read_validation import GraphReadValidation

    verdicts: dict[str, bool | None] = dict.fromkeys(rows, False)
    try:
        if read is None:
            read = GraphReadValidation(organization_id)
        read._check_org(organization_id)
        if graph_client is None and read.graph_execute_query is None:
            from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

            graph_client = (await get_surreal_graph_runtime(organization_id)).client
        for batch in content_client.value_batches(sorted(rows)):
            try:
                verdicts.update(
                    await graph_publication_verdicts(
                        organization_id,
                        {identifier: rows[identifier] for identifier in batch},
                        client=graph_client,
                        read=read,
                    )
                )
            except Exception as exc:
                log.warning(
                    "graph_publication_proof_failed",
                    dependent_publication_count=len(batch),
                    error_type=type(exc).__name__,
                )
    except Exception as exc:
        log.warning(
            "graph_publication_proof_failed",
            dependent_publication_count=len(rows),
            error_type=type(exc).__name__,
        )
    return verdicts


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


async def _graph_ancestry(organization_id, rows, graph_client, *, refresh_ids, read=None):
    from sibyl_core.backends.surreal.records import normalize_records
    from sibyl_core.services.graph_records import entity_from_surreal_row

    if read is not None:
        read._check_org(organization_id)
    execute_query = read.graph_execute_query if read is not None else None
    current = dict(rows)
    parents: dict[str, set[str] | None] = {}
    loaded: set[str] = set()
    frontier = set(rows)
    refresh = set(refresh_ids)
    while frontier:
        for identifier in frontier:
            row = current.get(identifier)
            try:
                parents[identifier] = _parent_ids(row) if row is not None else None
            except (TypeError, ValueError):
                parents[identifier] = None
        requested = (set().union(*(parents[key] or set() for key in frontier)) | refresh) - loaded
        refresh.clear()
        if not requested:
            break
        # Supplied candidates may omit ancestry or predate a policy change.
        # Source-backed rows and their parents use current storage, once per batch.
        loaded.update(requested)
        current.update(dict.fromkeys(requested))
        try:
            if execute_query is None:
                if graph_client is None:
                    from sibyl_core.services.graph_runtime import get_surreal_graph_runtime

                    graph_client = (await get_surreal_graph_runtime(organization_id)).client
                execute_query = graph_client.execute_query
            for batch in content_client.value_batches(sorted(requested)):
                try:
                    records = normalize_records(
                        await execute_query(
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
    if read is not None:
        for identifier, row in current.items():
            if isinstance(row, Entity):
                read.record_entity(row, ancestry=True)
                read.depend_on(
                    SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, identifier),
                    [
                        SourceIdentity(organization_id, SourceKind.GRAPH_ENTITY, parent)
                        for parent in parents.get(identifier) or ()
                    ],
                )
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
