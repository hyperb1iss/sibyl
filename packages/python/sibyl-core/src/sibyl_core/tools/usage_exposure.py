"""Exposure accounting for read surfaces."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import structlog

from sibyl_core.models.context import ContextItem
from sibyl_core.services.graph import get_surreal_graph_client
from sibyl_core.services.surreal_content import get_shared_surreal_content_client
from sibyl_core.services.usage import (
    MemoryUsageEvent,
    MemoryUsageItemKind,
    MemoryUsageSignal,
    MemoryUsageStamp,
    record_memory_usage,
)
from sibyl_core.tools.responses import SearchResult

log = structlog.get_logger()

_USAGE_EXPOSURE_METADATA_KEY = "usage_exposure"
_USAGE_EXPOSURE_SUMMARY_KEY = "usage_exposure"
_RAW_MEMORY_PREFIX = "raw_memory:"


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

    stamped_targets: set[tuple[MemoryUsageItemKind, str]] = set()
    failed_response_ids: set[str] = set()
    if targets:
        try:
            content_client = await get_shared_surreal_content_client()
        except Exception as exc:
            log.warning(
                "usage_exposure_recording_failed",
                source_surface=source_surface,
                error_type=type(exc).__name__,
            )
            failed_response_ids.update(
                _exclude_failed_targets(
                    targets,
                    items=items,
                    metadata_factory=metadata_factory,
                    exclusions=exclusions,
                    source_surface=source_surface,
                    session_key=session_key,
                    message_key=message_key,
                    error_type=type(exc).__name__,
                )
            )
        else:
            raw_targets = [
                target for target in targets if target.item_kind == MemoryUsageItemKind.RAW_CAPTURE
            ]
            graph_targets = [
                target for target in targets if target.item_kind == MemoryUsageItemKind.GRAPH_ENTITY
            ]
            if raw_targets:
                try:
                    stamped_targets.update(
                        await _record_target_exposures(
                            content_client,
                            raw_targets,
                            organization_id=str(organization_id),
                            principal_id=principal_id,
                            project_id=project_id,
                            source_surface=source_surface,
                            session_key=session_key,
                            message_key=message_key,
                        )
                    )
                except Exception as exc:
                    log.warning(
                        "usage_exposure_recording_failed",
                        source_surface=source_surface,
                        item_kind=MemoryUsageItemKind.RAW_CAPTURE.value,
                        error_type=type(exc).__name__,
                    )
                    failed_response_ids.update(
                        _exclude_failed_targets(
                            raw_targets,
                            items=items,
                            metadata_factory=metadata_factory,
                            exclusions=exclusions,
                            source_surface=source_surface,
                            session_key=session_key,
                            message_key=message_key,
                            error_type=type(exc).__name__,
                        )
                    )
            if graph_targets:
                try:
                    graph_client = await get_surreal_graph_client(str(organization_id))
                    stamped_targets.update(
                        await _record_target_exposures(
                            content_client,
                            graph_targets,
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

    for target in targets:
        if target.response_id in failed_response_ids:
            continue
        metadata = _metadata_for_response_id(items, metadata_factory, target.response_id)
        if (target.item_kind, target.item_id) in stamped_targets:
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
    graph_client: Any | None = None,
) -> set[tuple[MemoryUsageItemKind, str]]:
    result = await record_memory_usage(
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
        graph_client=graph_client,
    )
    return {(stamp.item_kind, stamp.item_id) for stamp in result.stamps if _stamp_applied(stamp)}


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


def _stamp_applied(stamp: MemoryUsageStamp) -> bool:
    return stamp.last_recalled_at is not None or stamp.retrieval_count > 0


__all__ = [
    "_USAGE_EXPOSURE_METADATA_KEY",
    "_USAGE_EXPOSURE_SUMMARY_KEY",
    "annotate_context_item_exposures",
    "annotate_search_result_exposures",
]
