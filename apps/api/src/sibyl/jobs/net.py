"""Settle idle sibylnet threads into memory.

Every five minutes, threads that have been quiet for
``net_thread_idle_minutes`` and hold messages newer than their last
distillation are reflected with intent ``decide``, so every thread's decisions
are tracked, not only the ones an agent posted as a decision. Each pass takes
one window per thread (oldest messages first, capped), persists the transcript
as the reflection source and moves the thread's ``reflected_through`` to the
window's last message; a longer backlog continues on the next pass.

Authority. A window is reflected as the author of its first message, through
the calls ``POST /context/reflect`` makes for a caller. The principal's
``AuthContext`` is resolved from the auth store with the claims a session of
theirs would carry, as the MCP context does for every principal, and project
access, the readable and writable project sets and the default write scope
then come from the same request-path helpers given that context. There is no
second authorization path: a principal who left the organization, lost the
write role, or can no longer contribute to the thread's project fails the
attempt with that reason. The resolved context carries no API-key narrowing,
so the reflection runs with the user's own grants, the way the dream cycle
reflects a capture as its principal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import HTTPException

from sibyl.api.context_audit import log_reflection_audit
from sibyl.api.routes import entity_contracts as contracts, memory_auth
from sibyl.api.routes.net import member_directory
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.config import settings
from sibyl.persistence.auth_runtime import (
    InvalidAuthClaimsError,
    UserNotFoundError,
    list_accessible_project_graph_ids,
    resolve_auth_context,
)
from sibyl_core.auth import ProjectRole
from sibyl_core.models.context import ContextIntent
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.net import (
    NetThreadState,
    claim_net_thread,
    fail_net_thread,
    idle_net_threads,
    net_source_title,
    net_thread_window,
    render_net_transcript,
    settle_net_thread,
)
from sibyl_core.tools.reflect import reflect_memory

log = structlog.get_logger()

NET_DISTILL_SURFACE = "sibylnet_distill"
# Threads claimed per pass. A pass reflects them one at a time; the rest wait
# for the next tick five minutes later.
THREADS_PER_RUN = 50
_REFLECTION_LIMIT = 12


class NetDistillationRefusedError(Exception):
    """The window cannot be reflected as its principal; the message is the reason."""


async def distill_idle_net_threads(ctx: dict[str, Any]) -> dict[str, int]:
    """Reflect every idle thread's undistilled window into memory, one claim at a time."""
    del ctx
    candidates = await idle_net_threads(
        idle_minutes=settings.net_thread_idle_minutes, limit=THREADS_PER_RUN
    )
    summary = {
        "candidates": len(candidates),
        "distilled": 0,
        "settled_empty": 0,
        "contended": 0,
        "failed": 0,
    }
    for candidate in candidates:
        thread = await claim_net_thread(candidate)
        if thread is None:
            summary["contended"] += 1
            continue
        try:
            outcome = await distill_net_thread(thread)
        except Exception as exc:
            reason = _failure_reason(exc)
            log.warning(
                "net_thread_distillation_failed",
                organization_id=thread.organization_id,
                channel=thread.channel,
                thread=thread.thread or None,
                attempts=thread.reflection_attempts,
                reason=reason,
                exc_info=not isinstance(exc, NetDistillationRefusedError | HTTPException),
            )
            try:
                await fail_net_thread(thread, reason=reason)
            except Exception:
                log.warning("net_thread_failure_not_recorded", exc_info=True)
            summary["failed"] += 1
            continue
        summary[outcome] += 1
    if summary["distilled"] or summary["settled_empty"] or summary["failed"]:
        log.info("net_threads_distilled", **summary)
    return summary


async def distill_net_thread(thread: NetThreadState) -> str:
    """Reflect one claimed thread's window and settle it; returns the outcome key.

    Raises when the window cannot be reflected; the caller records the
    reason on the thread.
    """
    window = await net_thread_window(thread)
    if not window:
        # Nothing settled after the last distillation (the messages it saw
        # are gone, or none ever arrived past it): stop qualifying.
        await settle_net_thread(thread, through_message_id=None)
        return "settled_empty"
    principal_id = window[0].author_user_id
    authority = await principal_authority(thread.organization_id, principal_id)
    project = thread.project_id
    if project:
        await verify_entity_project_access(
            None, authority, project, required_role=ProjectRole.CONTRIBUTOR
        )
        accessible_projects = {project}
    else:
        accessible_projects = _project_set(await list_accessible_project_graph_ids(authority))
    writable_projects = _project_set(
        await list_accessible_project_graph_ids(authority, required_role=ProjectRole.CONTRIBUTOR)
    )
    scope = await memory_auth.resolve_capture_default_scope(ctx=authority, project_id=project)
    organization = authority.organization
    members = (
        await member_directory(organization.slug, principal_id) if organization is not None else {}
    )
    transcript = render_net_transcript(
        thread, window, names={user_id: member.name for user_id, member in members.items()}
    )
    pack = await reflect_memory(
        transcript,
        source_title=net_source_title(thread.channel, thread.thread),
        intent=ContextIntent.DECIDE.value,
        project=project,
        organization_id=thread.organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        writable_projects=writable_projects,
        allowed_memory_scope_keys=authority.api_key_memory_scope_keys,
        memory_scope=scope.memory_scope.value,
        scope_key=scope.scope_key,
        persist=True,
        persist_source=True,
        limit=_REFLECTION_LIMIT,
    )
    refusal = reflection_refusal(pack)
    if refusal is not None:
        raise NetDistillationRefusedError(refusal)
    await log_reflection_audit(
        user_id=principal_id,
        organization_id=thread.organization_id,
        request=None,
        pack=pack,
        project=project,
        accessible_projects=accessible_projects,
        source_surface=NET_DISTILL_SURFACE,
        persist=True,
        persist_source=True,
        persist_review=False,
        active_task=False,
        related_to=None,
        task_ids=None,
        limit=_REFLECTION_LIMIT,
    )
    await settle_net_thread(thread, through_message_id=window[-1].id, source_id=pack.source_id)
    log.info(
        "net_thread_distilled",
        organization_id=thread.organization_id,
        channel=thread.channel,
        thread=thread.thread or None,
        messages=len(window),
        source_id=pack.source_id,
        persisted=pack.persisted_count,
        memory_scope=scope.memory_scope.value,
    )
    return "distilled"


async def principal_authority(organization_id: str, user_id: str) -> AuthContext:
    """The principal's own authorization context, or a refusal when they cannot write.

    Resolved from the auth store with the claims a session of theirs would
    carry, so membership, role and team grants are current as of this pass.
    """
    try:
        authority = await resolve_auth_context(
            claims={"sub": user_id, "org": organization_id, "scopes": []}
        )
    except (InvalidAuthClaimsError, UserNotFoundError) as exc:
        raise NetDistillationRefusedError("principal_unavailable") from exc
    if authority.user_id != user_id or authority.organization_id != organization_id:
        raise NetDistillationRefusedError("principal_unavailable")
    if authority.org_role not in contracts.WRITE_ROLES:
        raise NetDistillationRefusedError("principal_cannot_write")
    return authority


def reflection_refusal(pack: ReflectionPack) -> str | None:
    """Why a persisted reflection stored nothing, or None when its source landed."""
    for candidate in pack.candidates:
        metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
        if metadata.get("promotion_state") == "denied":
            errors = metadata.get("promotion_errors")
            if isinstance(errors, list) and errors:
                return str(errors[0])
            return "reflection_source_denied"
    if pack.source_id:
        return None
    reasons: list[str] = []
    for candidate in pack.candidates:
        metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
        values = metadata.get("policy_reasons")
        for reason in values if isinstance(values, list) else ():
            if isinstance(reason, str) and reason not in reasons:
                reasons.append(reason)
    return ",".join(reasons) or "reflection_source_not_persisted"


def _project_set(values: object) -> set[str]:
    if not isinstance(values, set | frozenset | list | tuple):
        return set()
    return {str(value) for value in values}


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, NetDistillationRefusedError):
        return str(exc)
    if isinstance(exc, HTTPException):
        return f"http_{exc.status_code}: {exc.detail}"[:200]
    return f"{type(exc).__name__}: {exc}"[:200]


__all__ = [
    "NET_DISTILL_SURFACE",
    "THREADS_PER_RUN",
    "NetDistillationRefusedError",
    "distill_idle_net_threads",
    "distill_net_thread",
    "principal_authority",
    "reflection_refusal",
]
