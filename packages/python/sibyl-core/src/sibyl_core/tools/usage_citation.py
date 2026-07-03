"""Citation accounting for memory usage feedback."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import structlog

from sibyl_core.services.graph import get_surreal_graph_client
from sibyl_core.services.surreal_content import get_shared_surreal_content_client
from sibyl_core.services.usage import (
    MemoryUsageEvent,
    MemoryUsageItemKind,
    MemoryUsageSignal,
    MemoryUsageStamp,
    record_memory_usage,
)

log = structlog.get_logger()

_RAW_MEMORY_PREFIX = "raw_memory:"
_RAW_CAPTURE_PREFIX = "raw_capture:"
_UNSUPPORTED_PREFIXES = ("document:", "doc:")


@dataclass(frozen=True, slots=True)
class _CitationTarget:
    cited_id: str
    item_kind: MemoryUsageItemKind
    item_id: str


@dataclass(frozen=True, slots=True)
class _CitationExclusion:
    cited_id: str
    reason: str
    detail: str | None = None


async def record_cited_item_usages(
    cited_ids: Sequence[str] | str | None,
    *,
    organization_id: str | None,
    principal_id: str | None,
    project_id: str | None,
    source_surface: str,
    request_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Record citation usage for graph entities and raw captures."""

    normalized_ids = normalize_cited_ids(cited_ids)
    session_key, message_key = _usage_keys(
        source_surface=source_surface,
        organization_id=organization_id,
        principal_id=principal_id,
        project_id=project_id,
        cited_ids=normalized_ids,
        request_metadata=request_metadata,
    )
    targets: list[_CitationTarget] = []
    exclusions: list[_CitationExclusion] = []
    for cited_id in normalized_ids:
        target = _target_from_cited_id(cited_id)
        if isinstance(target, _CitationExclusion):
            exclusions.append(target)
        else:
            targets.append(target)

    if targets and not organization_id:
        exclusions.extend(
            _CitationExclusion(target.cited_id, "missing_organization_id") for target in targets
        )
        targets = []

    stamped_targets: set[tuple[MemoryUsageItemKind, str]] = set()
    failed_cited_ids: set[str] = set()
    if targets:
        try:
            content_client = await get_shared_surreal_content_client()
        except Exception as exc:
            log.warning(
                "usage_citation_recording_failed",
                source_surface=source_surface,
                error_type=type(exc).__name__,
            )
            failed_cited_ids.update(
                _exclude_failed_targets(
                    targets,
                    exclusions=exclusions,
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
                        await _record_target_citations(
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
                        "usage_citation_recording_failed",
                        source_surface=source_surface,
                        item_kind=MemoryUsageItemKind.RAW_CAPTURE.value,
                        error_type=type(exc).__name__,
                    )
                    failed_cited_ids.update(
                        _exclude_failed_targets(
                            raw_targets,
                            exclusions=exclusions,
                            error_type=type(exc).__name__,
                        )
                    )
            if graph_targets:
                try:
                    graph_client = await get_surreal_graph_client(str(organization_id))
                    stamped_targets.update(
                        await _record_target_citations(
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
                        "usage_citation_recording_failed",
                        source_surface=source_surface,
                        item_kind=MemoryUsageItemKind.GRAPH_ENTITY.value,
                        error_type=type(exc).__name__,
                    )
                    failed_cited_ids.update(
                        _exclude_failed_targets(
                            graph_targets,
                            exclusions=exclusions,
                            error_type=type(exc).__name__,
                        )
                    )

    for target in targets:
        if target.cited_id in failed_cited_ids:
            continue
        if (target.item_kind, target.item_id) in stamped_targets:
            continue
        exclusions.append(_CitationExclusion(target.cited_id, "stamp_target_missing"))

    excluded = [
        {
            "cited_id": exclusion.cited_id,
            "reason": exclusion.reason,
            **({"detail": exclusion.detail} if exclusion.detail else {}),
        }
        for exclusion in exclusions
    ]
    stamped_count = sum(
        1
        for target in targets
        if target.cited_id not in failed_cited_ids
        and (target.item_kind, target.item_id) in stamped_targets
    )
    cited_count = len(normalized_ids)
    return {
        "source_surface": source_surface,
        "signal_type": MemoryUsageSignal.CITATION.value,
        "session_key": session_key,
        "message_key": message_key,
        "cited_count": cited_count,
        "stamped_count": stamped_count,
        "excluded_count": len(excluded),
        "coverage_count": stamped_count + len(excluded),
        "coverage_complete": stamped_count + len(excluded) == cited_count,
        "exclusions": excluded,
    }


async def _record_target_citations(
    content_client: Any,
    targets: Sequence[_CitationTarget],
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
                signal_type=MemoryUsageSignal.CITATION,
                principal_id=principal_id,
                project_id=project_id,
                metadata={
                    "cited_id": target.cited_id,
                    "source_surface": source_surface,
                },
            )
            for target in targets
        ],
        graph_client=graph_client,
    )
    return {
        (stamp.item_kind, stamp.item_id)
        for stamp in result.stamps
        if _citation_stamp_applied(stamp)
    }


def _target_from_cited_id(cited_id: str) -> _CitationTarget | _CitationExclusion:
    if not cited_id:
        return _CitationExclusion(cited_id, "missing_item_id")
    normalized = cited_id.strip()
    lowered = normalized.lower()
    if any(lowered.startswith(prefix) for prefix in _UNSUPPORTED_PREFIXES):
        return _CitationExclusion(normalized, "unsupported_item_kind")
    if lowered.startswith(_RAW_MEMORY_PREFIX) or lowered.startswith(_RAW_CAPTURE_PREFIX):
        item_id = normalized.split(":", 1)[1]
        if not item_id:
            return _CitationExclusion(normalized, "missing_item_id")
        return _CitationTarget(
            cited_id=normalized,
            item_kind=MemoryUsageItemKind.RAW_CAPTURE,
            item_id=item_id,
        )
    return _CitationTarget(
        cited_id=normalized,
        item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
        item_id=normalized,
    )


def _exclude_failed_targets(
    targets: Sequence[_CitationTarget],
    *,
    exclusions: list[_CitationExclusion],
    error_type: str,
) -> set[str]:
    failed_cited_ids: set[str] = set()
    for target in targets:
        failed_cited_ids.add(target.cited_id)
        exclusions.append(_CitationExclusion(target.cited_id, "recording_failed", error_type))
    return failed_cited_ids


def normalize_cited_ids(cited_ids: Sequence[str] | str | None) -> list[str]:
    if cited_ids is None:
        return []
    if isinstance(cited_ids, str):
        return _dedupe_ids(cited_ids.split(","))
    return _dedupe_ids(cited_ids)


def _dedupe_ids(cited_ids: Iterable[object]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for cited_id in cited_ids:
        normalized = str(cited_id or "").strip()
        if not normalized or normalized in seen:
            continue
        deduped.append(normalized)
        seen.add(normalized)
    return deduped


def _usage_keys(
    *,
    source_surface: str,
    organization_id: str | None,
    principal_id: str | None,
    project_id: str | None,
    cited_ids: Sequence[str],
    request_metadata: Mapping[str, object] | None,
) -> tuple[str, str]:
    payload = {
        "cited_ids": list(cited_ids),
        "organization_id": organization_id,
        "principal_id": principal_id,
        "project_id": project_id,
        "request": dict(request_metadata or {}),
        "source_surface": source_surface,
    }
    digest = sha256(json.dumps(payload, default=str, sort_keys=True).encode("utf-8")).hexdigest()[
        :24
    ]
    return (f"{source_surface}:{digest}", f"{source_surface}:citation:{digest}")


def _citation_stamp_applied(stamp: MemoryUsageStamp) -> bool:
    return stamp.last_used_at is not None or stamp.citation_count > 0


__all__ = ["normalize_cited_ids", "record_cited_item_usages"]
