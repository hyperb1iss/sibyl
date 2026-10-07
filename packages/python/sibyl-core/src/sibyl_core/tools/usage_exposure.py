"""Exposure accounting for read surfaces."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import structlog

from sibyl_core.models.context import ContextItem
from sibyl_core.services.graph_client import get_surreal_graph_client
from sibyl_core.services.surreal_content import get_shared_surreal_content_client
from sibyl_core.services.usage import (
    MemoryUsageEvent,
    MemoryUsageItemKind,
    MemoryUsageSignal,
    MemoryUsageTarget,
    record_memory_usage_events,
    stamp_memory_usage,
    usage_targets_for_events,
)
from sibyl_core.tools.responses import SearchResult

log = structlog.get_logger()

_USAGE_EXPOSURE_METADATA_KEY = "usage_exposure"
_USAGE_EXPOSURE_SUMMARY_KEY = "usage_exposure"
_RAW_MEMORY_PREFIX = "raw_memory:"

# Stamps scheduled after a response was built. The request path appends the
# exposure events (one statement) and returns; the row stamps run here so a
# recall never waits on, or fails because of, a read-modify-write on the
# most popular rows in the store. Shutdown drains the set before the shared
# clients close, and tests drain it to observe the stamp.
_pending_stamps: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class _ExposureTarget:
    response_id: str
    item_kind: MemoryUsageItemKind
    item_id: str
    project_id: str | None


@dataclass(frozen=True, slots=True)
class _ExposureExclusion:
    response_id: str
    reason: str
    detail: str | None = None


async def annotate_search_result_exposures(
    results: Sequence[SearchResult],
    *,
    organization_id: str | None,
    principal_id: str | None,
    project_id: str | None,
    source_surface: str = "search",
    request_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Record and annotate exposure for search results returned to a caller."""

    summary = await _annotate_exposures(
        items=results,
        organization_id=organization_id,
        principal_id=principal_id,
        project_id=project_id,
        source_surface=source_surface,
        request_metadata=request_metadata,
        target_factory=_target_from_search_result,
        metadata_factory=lambda item: item.metadata,
    )
    return summary


async def annotate_context_item_exposures(
    items: Sequence[ContextItem],
    *,
    organization_id: str | None,
    principal_id: str | None,
    project_id: str | None,
    source_surface: str = "context_pack",
    request_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Record and annotate exposure for context-pack items returned to a caller."""

    summary = await _annotate_exposures(
        items=items,
        organization_id=organization_id,
        principal_id=principal_id,
        project_id=project_id,
        source_surface=source_surface,
        request_metadata=request_metadata,
        target_factory=_target_from_context_item,
        metadata_factory=lambda item: item.metadata,
    )
    return summary


async def drain_pending_exposure_stamps() -> int:
    """Await every deferred stamp and return how many were waited on."""
    drained = 0
    while _pending_stamps:
        pending = list(_pending_stamps)
        await asyncio.gather(*pending, return_exceptions=True)
        drained += len(pending)
    return drained


async def _annotate_exposures(
    *,
    items: Sequence[Any],
    organization_id: str | None,
    principal_id: str | None,
    project_id: str | None,
    source_surface: str,
    request_metadata: Mapping[str, object] | None,
    target_factory: Any,
    metadata_factory: Any,
) -> dict[str, object]:
    session_key, message_key = _usage_keys(
        source_surface=source_surface,
        organization_id=organization_id,
        principal_id=principal_id,
        project_id=project_id,
        request_metadata=request_metadata,
    )
    targets: list[_ExposureTarget] = []
    exclusions: list[_ExposureExclusion] = []
    for item in items:
        metadata = metadata_factory(item)
        metadata.setdefault("cite_id", str(getattr(item, "id", "")))
        target = target_factory(item)
        if isinstance(target, _ExposureExclusion):
            exclusions.append(target)
            _mark_excluded(
                metadata,
                target,
                source_surface=source_surface,
                session_key=session_key,
                message_key=message_key,
            )
        else:
            targets.append(target)

    if targets and not organization_id:
        for target in targets:
            exclusion = _ExposureExclusion(target.response_id, "missing_organization_id")
            exclusions.append(exclusion)
            _mark_excluded(
                _metadata_for_response_id(items, metadata_factory, target.response_id),
                exclusion,
                source_surface=source_surface,
                session_key=session_key,
                message_key=message_key,
            )
        targets = []

    recorded_targets: set[tuple[MemoryUsageItemKind, str]] = set()
    failed_response_ids: set[str] = set()
    if targets:
        recordable = list(targets)
        graph_client: Any | None = None
        graph_targets = [
            target for target in targets if target.item_kind is MemoryUsageItemKind.GRAPH_ENTITY
        ]
        if graph_targets:
            # The graph client is resolved before the single insert so a graph
            # outage excludes only the graph items; raw exposures still land.
            try:
                graph_client = await get_surreal_graph_client(str(organization_id))
            except Exception as exc:
                log.warning(
                    "usage_exposure_recording_failed",
                    source_surface=source_surface,
                    item_kind=MemoryUsageItemKind.GRAPH_ENTITY.value,
                    error_type=type(exc).__name__,
                )
                failed_response_ids.update(
                    _exclude_failed_targets(
                        graph_targets,
                        items=items,
                        metadata_factory=metadata_factory,
                        exclusions=exclusions,
                        source_surface=source_surface,
                        session_key=session_key,
                        message_key=message_key,
                        error_type=type(exc).__name__,
                    )
                )
                recordable = [
                    target
                    for target in targets
                    if target.item_kind is not MemoryUsageItemKind.GRAPH_ENTITY
                ]
        if recordable:
            try:
                content_client = await get_shared_surreal_content_client()
                recorded_targets.update(
                    await _record_target_exposures(
                        content_client,
                        recordable,
                        organization_id=str(organization_id),
                        principal_id=principal_id,
                        project_id=project_id,
                        source_surface=source_surface,
                        session_key=session_key,
                        message_key=message_key,
                        graph_client=graph_client,
                    )
                )
            except Exception as exc:
                log.warning(
                    "usage_exposure_recording_failed",
                    source_surface=source_surface,
                    error_type=type(exc).__name__,
                )
                failed_response_ids.update(
                    _exclude_failed_targets(
                        recordable,
                        items=items,
                        metadata_factory=metadata_factory,
                        exclusions=exclusions,
                        source_surface=source_surface,
                        session_key=session_key,
                        message_key=message_key,
                        error_type=type(exc).__name__,
                    )
                )

    for target in targets:
        if target.response_id in failed_response_ids:
            continue
        metadata = _metadata_for_response_id(items, metadata_factory, target.response_id)
        if (target.item_kind, target.item_id) in recorded_targets:
            _mark_stamped(
                metadata,
                target,
                source_surface=source_surface,
                session_key=session_key,
                message_key=message_key,
            )
            continue
        exclusion = _ExposureExclusion(target.response_id, "stamp_target_missing")
        exclusions.append(exclusion)
        _mark_excluded(
            metadata,
            exclusion,
            source_surface=source_surface,
            session_key=session_key,
            message_key=message_key,
        )

    stamped_count = sum(
        1
        for item in items
        if metadata_factory(item).get(_USAGE_EXPOSURE_METADATA_KEY, {}).get("status") == "stamped"
    )
    excluded = [
        {
            "response_id": exclusion.response_id,
            "reason": exclusion.reason,
            **({"detail": exclusion.detail} if exclusion.detail else {}),
        }
        for exclusion in exclusions
    ]
    returned_count = len(items)
    return {
        "source_surface": source_surface,
        "signal_type": MemoryUsageSignal.EXPOSURE.value,
        "session_key": session_key,
        "message_key": message_key,
        "returned_count": returned_count,
        "stamped_count": stamped_count,
        "excluded_count": len(excluded),
        "coverage_count": stamped_count + len(excluded),
        "coverage_complete": stamped_count + len(excluded) == returned_count,
        "exclusions": excluded,
    }


def _target_from_search_result(result: SearchResult) -> _ExposureTarget | _ExposureExclusion:
    return _target_from_parts(
        response_id=result.id,
        result_origin=result.result_origin,
        item_type=result.type,
        metadata=result.metadata,
    )


def _target_from_context_item(item: ContextItem) -> _ExposureTarget | _ExposureExclusion:
    quality = getattr(item, "quality", None)
    return _target_from_parts(
        response_id=item.id,
        result_origin=str(
            getattr(quality, "origin", "") or item.metadata.get("result_origin") or ""
        ),
        item_type=item.type,
        metadata=item.metadata,
    )


def _target_from_parts(
    *,
    response_id: str,
    result_origin: str,
    item_type: str,
    metadata: Mapping[str, object],
) -> _ExposureTarget | _ExposureExclusion:
    origin = result_origin.lower()
    candidate_kind = str(metadata.get("candidate_kind") or "").lower()
    if (
        origin == "raw_memory"
        or candidate_kind == "raw_memory"
        or response_id.startswith(_RAW_MEMORY_PREFIX)
    ):
        item_id = response_id.removeprefix(_RAW_MEMORY_PREFIX)
        if not item_id:
            return _ExposureExclusion(response_id, "missing_item_id")
        return _ExposureTarget(
            response_id=response_id,
            item_kind=MemoryUsageItemKind.RAW_CAPTURE,
            item_id=item_id,
            project_id=_project_id_from_metadata(metadata),
        )
    if origin == "graph" or candidate_kind in {"node", "episode"}:
        if not response_id:
            return _ExposureExclusion(response_id, "missing_item_id")
        return _ExposureTarget(
            response_id=response_id,
            item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
            item_id=response_id,
            project_id=_project_id_from_metadata(metadata),
        )
    if origin == "document" or item_type == "document" or metadata.get("document_id"):
        return _ExposureExclusion(response_id, "unsupported_item_kind")
    return _ExposureExclusion(response_id, "unsupported_item_kind")


async def _record_target_exposures(
    content_client: Any,
    targets: Sequence[_ExposureTarget],
    *,
    organization_id: str,
    principal_id: str | None,
    project_id: str | None,
    source_surface: str,
    session_key: str,
    message_key: str,
    graph_client: Any | None,
) -> set[tuple[MemoryUsageItemKind, str]]:
    """Append the exposure events now; stamp the rows after the response.

    The events are the durable record, written in one statement for every
    returned item. The per-row stamps (counts and timestamps derived from
    those events) are scheduled as one deferred batch, so the request pays
    one write and never blocks on the hot rows.
    """
    rows = await record_memory_usage_events(
        content_client,
        [
            MemoryUsageEvent(
                organization_id=organization_id,
                session_key=session_key,
                message_key=message_key,
                source_surface=source_surface,
                item_kind=target.item_kind,
                item_id=target.item_id,
                signal_type=MemoryUsageSignal.EXPOSURE,
                principal_id=principal_id,
                project_id=target.project_id or project_id,
                metadata={
                    "response_id": target.response_id,
                    "source_surface": source_surface,
                },
            )
            for target in targets
        ],
    )
    stamp_targets = usage_targets_for_events(rows)
    if stamp_targets and _stamps_run_inline(content_client, graph_client):
        await _run_exposure_stamp(
            content_client,
            stamp_targets,
            organization_id=organization_id,
            source_surface=source_surface,
            graph_client=graph_client,
        )
    elif stamp_targets:
        _schedule_exposure_stamp(
            content_client,
            stamp_targets,
            organization_id=organization_id,
            source_surface=source_surface,
            needs_graph_client=graph_client is not None,
        )
    return {(target.item_kind, target.item_id) for target in stamp_targets}


def _stamps_run_inline(content_client: Any, graph_client: Any | None) -> bool:
    """Whether the stamp must finish before the response instead of after it.

    Deferral only pays against a networked server, where the stamp's round
    trips would otherwise sit between the recall and its response. An
    embedded store is clamped to one connection, so a deferred stamp would
    queue behind the next query anyway, and it dies with the process: a
    query still in flight when the loop closes aborts the interpreter from
    the engine's worker thread. The stamp therefore runs inline whenever
    either store it touches is embedded.
    """
    # Only a literal True counts: a test double that manufactures attributes
    # on demand must not look like an embedded store.
    return any(
        getattr(client, "is_embedded", False) is True
        for client in (content_client, graph_client)
        if client is not None
    )


def _schedule_exposure_stamp(
    content_client: Any,
    targets: Sequence[MemoryUsageTarget],
    *,
    organization_id: str,
    source_surface: str,
    needs_graph_client: bool,
) -> None:
    # The task carries the organization id, not the graph client instance.
    # The per-organization graph client cache evicts and closes idle clients,
    # and a captured instance would be reconnected by this stamp outside the
    # cache; resolving by id when the stamp runs always lands on the live one.
    task = asyncio.create_task(
        _run_exposure_stamp(
            content_client,
            targets,
            organization_id=organization_id,
            source_surface=source_surface,
            needs_graph_client=needs_graph_client,
        ),
        name=f"usage_exposure_stamp:{source_surface}",
    )
    _pending_stamps.add(task)
    task.add_done_callback(_pending_stamps.discard)


async def _run_exposure_stamp(
    content_client: Any,
    targets: Sequence[MemoryUsageTarget],
    *,
    organization_id: str,
    source_surface: str,
    graph_client: Any | None = None,
    needs_graph_client: bool = False,
) -> None:
    try:
        if needs_graph_client and graph_client is None:
            graph_client = await get_surreal_graph_client(organization_id)
        await stamp_memory_usage(content_client, targets, graph_client=graph_client)
    except Exception as exc:
        # The events already landed, so the next stamp of these rows carries
        # this exposure; the recall that scheduled this stamp has returned.
        log.warning(
            "usage_exposure_stamp_failed",
            source_surface=source_surface,
            targets=len(targets),
            error_type=type(exc).__name__,
        )


def _exclude_failed_targets(
    targets: Sequence[_ExposureTarget],
    *,
    items: Sequence[Any],
    metadata_factory: Any,
    exclusions: list[_ExposureExclusion],
    source_surface: str,
    session_key: str,
    message_key: str,
    error_type: str,
) -> set[str]:
    failed_response_ids: set[str] = set()
    for target in targets:
        exclusion = _ExposureExclusion(
            target.response_id,
            "recording_failed",
            error_type,
        )
        failed_response_ids.add(target.response_id)
        exclusions.append(exclusion)
        _mark_excluded(
            _metadata_for_response_id(items, metadata_factory, target.response_id),
            exclusion,
            source_surface=source_surface,
            session_key=session_key,
            message_key=message_key,
        )
    return failed_response_ids


def _metadata_for_response_id(
    items: Sequence[Any],
    metadata_factory: Any,
    response_id: str,
) -> dict[str, Any]:
    for item in items:
        if str(getattr(item, "id", "")) == response_id:
            return metadata_factory(item)
    return {}


def _mark_stamped(
    metadata: dict[str, Any],
    target: _ExposureTarget,
    *,
    source_surface: str,
    session_key: str,
    message_key: str,
) -> None:
    metadata[_USAGE_EXPOSURE_METADATA_KEY] = {
        "status": "stamped",
        "signal_type": MemoryUsageSignal.EXPOSURE.value,
        "source_surface": source_surface,
        "session_key": session_key,
        "message_key": message_key,
        "item_kind": target.item_kind.value,
        "item_id": target.item_id,
    }


def _mark_excluded(
    metadata: dict[str, Any],
    exclusion: _ExposureExclusion,
    *,
    source_surface: str,
    session_key: str,
    message_key: str,
) -> None:
    metadata[_USAGE_EXPOSURE_METADATA_KEY] = {
        "status": "excluded",
        "signal_type": MemoryUsageSignal.EXPOSURE.value,
        "source_surface": source_surface,
        "session_key": session_key,
        "message_key": message_key,
        "reason": exclusion.reason,
        **({"detail": exclusion.detail} if exclusion.detail else {}),
    }


def _usage_keys(
    *,
    source_surface: str,
    organization_id: str | None,
    principal_id: str | None,
    project_id: str | None,
    request_metadata: Mapping[str, object] | None,
) -> tuple[str, str]:
    payload = {
        "organization_id": organization_id,
        "principal_id": principal_id,
        "project_id": project_id,
        "request": dict(request_metadata or {}),
        "source_surface": source_surface,
    }
    digest = sha256(json.dumps(payload, default=str, sort_keys=True).encode("utf-8")).hexdigest()[
        :24
    ]
    return (f"{source_surface}:{digest}", f"{source_surface}:exposure:{digest}")


def _project_id_from_metadata(metadata: Mapping[str, object]) -> str | None:
    for key in ("candidate_project_id", "project_id", "project"):
        value = metadata.get(key)
        if value:
            return str(value)
    return None


__all__ = [
    "_USAGE_EXPOSURE_METADATA_KEY",
    "_USAGE_EXPOSURE_SUMMARY_KEY",
    "annotate_context_item_exposures",
    "annotate_search_result_exposures",
    "drain_pending_exposure_stamps",
]
