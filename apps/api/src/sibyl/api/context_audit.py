"""Audit helpers for context pack render surfaces."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import structlog

from sibyl.persistence.auth_runtime import log_memory_audit_event
from sibyl_core.models.context import ContextPack

log = structlog.get_logger()


def _as_value(value: object) -> str:
    enum_value = getattr(value, "value", value)
    return str(enum_value)


def _compact_string(value: object | None) -> str | None:
    if value is None:
        return None
    compact = " ".join(str(value).strip().split())
    return compact or None


def _append_unique(values: list[str], value: object | None) -> None:
    compact = _compact_string(value)
    if compact and compact not in values:
        values.append(compact)


def _metadata_source_id(metadata: Mapping[str, object]) -> str | None:
    for key in (
        "source_id",
        "raw_source_id",
        "source",
        "source_name",
        "source_title",
        "reflection_source_title",
    ):
        if value := _compact_string(metadata.get(key)):
            return value
    return None


def _context_pack_source_ids(pack: ContextPack) -> list[str]:
    source_ids: list[str] = []
    for item in pack.items:
        metadata = getattr(item, "metadata", {})
        if isinstance(metadata, Mapping):
            _append_unique(source_ids, _metadata_source_id(metadata))
        _append_unique(source_ids, getattr(item, "source", None))
        quality = getattr(item, "quality", None)
        _append_unique(source_ids, getattr(quality, "source", None))
    return source_ids


def _context_pack_derived_ids(pack: ContextPack) -> list[str]:
    derived_ids: list[str] = []
    for item in pack.items:
        _append_unique(derived_ids, getattr(item, "id", None))
    return derived_ids


def _audit_scope(
    *,
    project: str | None,
) -> tuple[str, str | None, str | None]:
    if project:
        return "project", project, project
    return "mixed", None, None


async def log_context_pack_audit(
    *,
    user_id: str | None,
    organization_id: str,
    pack: ContextPack,
    project: str | None,
    accessible_projects: Iterable[str] | None,
    source_surface: str,
    agent_id: str | None,
    limit: int,
    include_related: bool,
    related_limit: int,
) -> None:
    """Record a metadata-only receipt for a rendered context pack."""

    memory_scope, scope_key, project_id = _audit_scope(project=project)
    accessible_project_values = (
        {str(accessible_project_id) for accessible_project_id in accessible_projects}
        if accessible_projects is not None
        else None
    )
    try:
        await log_memory_audit_event(
            action="memory.context_pack",
            user_id=user_id,
            organization_id=organization_id,
            request=None,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=source_surface,
            source_ids=_context_pack_source_ids(pack),
            derived_ids=_context_pack_derived_ids(pack),
            policy_allowed=True,
            policy_reason="context_pack_rendered",
            details={
                "agent_id": agent_id,
                "domain": pack.domain,
                "goal_length": len(pack.goal),
                "include_related": include_related,
                "intent": _as_value(pack.intent),
                "layer": _as_value(pack.layer),
                "limit": limit,
                "related_limit": related_limit,
                "result_count": len(pack.items),
                "section_count": len(pack.sections),
                "accessible_project_count": len(accessible_project_values)
                if accessible_project_values is not None
                else None,
            },
        )
    except Exception as exc:
        log.warning(
            "context_pack_audit_event_failed",
            error=str(exc),
            source_surface=source_surface,
            exc_info=True,
        )


__all__ = ["log_context_pack_audit"]
