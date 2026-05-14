"""Audit helpers for context routes and MCP tools."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import structlog

from sibyl.persistence.auth_runtime import log_memory_audit_event
from sibyl_core.models.context import ContextPack
from sibyl_core.models.reflection import ReflectionPack

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


def _append_unique_many(values: list[str], items: object | None) -> None:
    if isinstance(items, list | tuple | set | frozenset):
        for item in items:
            _append_unique(values, item)
        return
    _append_unique(values, items)


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


def _reflection_source_ids(pack: ReflectionPack) -> list[str]:
    source_ids: list[str] = []
    _append_unique(source_ids, pack.source_id)
    for candidate in pack.candidates:
        _append_unique_many(source_ids, candidate.raw_source_ids)
        metadata = getattr(candidate, "metadata", {})
        if not isinstance(metadata, Mapping):
            continue
        for key in ("source_id", "raw_source_id", "reflection_source_id"):
            _append_unique_many(source_ids, metadata.get(key))
    return source_ids


def _context_pack_derived_ids(pack: ContextPack) -> list[str]:
    derived_ids: list[str] = []
    for item in pack.items:
        _append_unique(derived_ids, getattr(item, "id", None))
    return derived_ids


def _reflection_derived_ids(pack: ReflectionPack) -> list[str]:
    derived_ids: list[str] = []
    for candidate in pack.candidates:
        _append_unique(derived_ids, candidate.persisted_id)
    return derived_ids


def _audit_scope(
    *,
    project: str | None,
) -> tuple[str, str | None, str | None]:
    if project:
        return "project", project, project
    return "mixed", None, None


def _write_audit_scope(
    *,
    project: str | None,
) -> tuple[str, str | None, str | None]:
    if project:
        return "project", project, project
    return "private", None, None


def _reflection_policy_state(pack: ReflectionPack) -> tuple[bool | None, str]:
    allowed: list[bool] = []
    reasons: list[str] = []
    for candidate in pack.candidates:
        metadata = getattr(candidate, "metadata", {})
        if not isinstance(metadata, Mapping):
            continue
        policy_allowed = metadata.get("policy_allowed")
        if isinstance(policy_allowed, bool):
            allowed.append(policy_allowed)
        policy_reasons = metadata.get("policy_reasons")
        if isinstance(policy_reasons, list | tuple | set | frozenset):
            for reason in policy_reasons:
                _append_unique(reasons, reason)
            continue
        _append_unique(reasons, metadata.get("policy_reason"))

    if not allowed:
        return True, "reflection_rendered"
    policy_allowed = all(allowed)
    if reasons:
        return policy_allowed, ",".join(reasons)
    if policy_allowed:
        return True, "reflection_policy_allowed"
    return False, "reflection_policy_denied"


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


async def log_reflection_audit(
    *,
    user_id: str | None,
    organization_id: str,
    pack: ReflectionPack,
    project: str | None,
    accessible_projects: Iterable[str] | None,
    source_surface: str,
    persist: bool,
    persist_source: bool,
    persist_review: bool,
    active_task: bool,
    related_to: list[str] | None,
    task_ids: list[str] | None,
    limit: int,
) -> None:
    """Record a metadata-only receipt for a reflection render."""

    memory_scope, scope_key, project_id = _write_audit_scope(project=project)
    accessible_project_values = (
        {str(accessible_project_id) for accessible_project_id in accessible_projects}
        if accessible_projects is not None
        else None
    )
    policy_allowed, policy_reason = _reflection_policy_state(pack)
    try:
        await log_memory_audit_event(
            action="memory.reflect",
            user_id=user_id,
            organization_id=organization_id,
            request=None,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=source_surface,
            source_ids=_reflection_source_ids(pack),
            derived_ids=_reflection_derived_ids(pack),
            policy_allowed=policy_allowed,
            policy_reason=policy_reason,
            details={
                "active_task": active_task,
                "candidate_count": len(pack.candidates),
                "domain": pack.domain,
                "intent": _as_value(pack.intent),
                "limit": limit,
                "persist": persist,
                "persist_review": persist_review,
                "persist_source": persist_source,
                "persisted_count": pack.persisted_count,
                "related_to_count": len(related_to or []),
                "source_title_length": len(pack.source_title),
                "task_id_count": len(task_ids or []),
                "accessible_project_count": len(accessible_project_values)
                if accessible_project_values is not None
                else None,
            },
        )
    except Exception as exc:
        log.warning(
            "reflection_audit_event_failed",
            error=str(exc),
            source_surface=source_surface,
            exc_info=True,
        )


__all__ = ["log_context_pack_audit", "log_reflection_audit"]
