"""Settle idle sibylnet threads into memory.

Every five minutes, threads that have been quiet for
``net_thread_idle_minutes`` and hold messages newer than their last
distillation are reflected with intent ``decide``, so every thread's decisions
are tracked, not only the ones an agent posted as a decision. Each pass takes
one window per thread (oldest messages first, capped) and moves the thread's
``reflected_through`` to the window's last message; a longer backlog
continues on the next pass.

Per author. A window is split by author, and by the credential each message
was posted with, and every part is reflected on its own, as its author, from
a transcript that holds only that author's lines. Nobody's words enter
someone else's reflection, and a body cannot pass for another author's line:
every entry names the one verified author and continuation lines are
indented.

Authority. Each part goes through the calls ``POST /context/reflect`` makes
for a caller. The author's ``AuthContext`` is rebuilt from the auth store, and
when they posted with an API key it is rebuilt from that key's current grants
the way request auth builds one, so a key held to one project distills into
nothing wider. A revoked or expired key, or one that lost write scope, skips
that author's lines. The lines land in the thread's pinned project when the
author can contribute to it now, otherwise in their private scope, and an
author who can write neither is skipped. Skips are recorded on the thread and
are not failures: the thread settles and its retry budget is untouched.
Anything else, a store or reflection error, fails the attempt and the whole
window is retried; parts reflected before the error are reflected again then.

Key grants. A key's authority is rebuilt from the grants it holds now, not
the ones it held when the message was posted. That is sound only because a
key's project and memory-space grants cannot be widened after it is created:
today a key is created and revoked, never edited, so its current grants are
at most what it posted with. If key grants ever become editable, store the
grants a post was made under and intersect them with the current ones here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog
from fastapi import HTTPException

from sibyl.api.context_audit import log_reflection_audit
from sibyl.api.routes import entity_contracts as contracts, memory_auth
from sibyl.api.routes.net import member_directory
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import _api_key_allows_rest, _api_key_claims
from sibyl.config import settings
from sibyl.persistence.auth_runtime import (
    InvalidAuthClaimsError,
    UserNotFoundError,
    list_accessible_project_graph_ids,
    resolve_api_key_authority,
    resolve_auth_context,
)
from sibyl_core.auth import ProjectRole
from sibyl_core.models.context import ContextIntent
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.net import (
    NetAuthorWindow,
    NetThreadState,
    author_windows,
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


class NetAuthorSkippedError(Exception):
    """One author's lines cannot be reflected as them; the message is the reason."""


async def distill_idle_net_threads(ctx: dict[str, Any]) -> dict[str, int]:
    """Reflect every idle thread's undistilled window into memory, one claim at a time."""
    del ctx
    candidates = await idle_net_threads(
        idle_minutes=settings.net_thread_idle_minutes, limit=THREADS_PER_RUN
    )
    summary = {
        "candidates": len(candidates),
        "distilled": 0,
        "settled_skipped": 0,
        "settled_empty": 0,
        "contended": 0,
        "failed": 0,
        "authors_skipped": 0,
    }
    for candidate in candidates:
        thread = await claim_net_thread(candidate)
        if thread is None:
            summary["contended"] += 1
            continue
        try:
            outcome, skipped = await distill_net_thread(thread)
        except Exception as exc:
            reason = _failure_reason(exc)
            log.warning(
                "net_thread_distillation_failed",
                organization_id=thread.organization_id,
                channel=thread.channel,
                thread=thread.thread or None,
                attempts=thread.reflection_attempts,
                reason=reason,
                exc_info=not isinstance(exc, HTTPException),
            )
            try:
                await fail_net_thread(thread, reason=reason)
            except Exception:
                log.warning("net_thread_failure_not_recorded", exc_info=True)
            summary["failed"] += 1
            continue
        summary[outcome] += 1
        summary["authors_skipped"] += skipped
    if any(summary[key] for key in ("distilled", "settled_skipped", "settled_empty", "failed")):
        log.info("net_threads_distilled", **summary)
    return summary


async def distill_net_thread(thread: NetThreadState) -> tuple[str, int]:
    """Reflect one claimed thread's window author by author, then settle it.

    Returns the outcome key and how many author parts were skipped. Raises
    on anything that is not a skip; the caller records it on the thread.
    """
    window = await net_thread_window(thread)
    if not window:
        # Nothing settled after the last distillation (the messages it saw
        # are gone, or none ever arrived past it): stop qualifying.
        await settle_net_thread(thread, through_message_id=None)
        return "settled_empty", 0
    sources: list[str] = []
    skipped: list[str] = []
    for part in author_windows(window):
        try:
            sources.append(await reflect_author_window(thread, part))
        except NetAuthorSkippedError as skip:
            skipped.append(f"{part.author_user_id}: {skip}")
            log.info(
                "net_thread_author_skipped",
                organization_id=thread.organization_id,
                channel=thread.channel,
                thread=thread.thread or None,
                author_user_id=part.author_user_id,
                api_key_id=part.api_key_id,
                messages=len(part.messages),
                reason=str(skip),
            )
    await settle_net_thread(
        thread,
        through_message_id=window[-1].id,
        source_id=sources[-1] if sources else None,
        skipped=skipped,
    )
    return ("distilled" if sources else "settled_skipped"), len(skipped)


async def reflect_author_window(thread: NetThreadState, part: NetAuthorWindow) -> str:
    """Reflect one author's lines as that author; returns the persisted source id.

    Raises ``NetAuthorSkippedError`` when the author, under the credential
    they posted with, can write nowhere this could land.
    """
    authority = await author_authority(thread.organization_id, part.author_user_id, part.api_key_id)
    writable_projects = _project_set(
        await list_accessible_project_graph_ids(authority, required_role=ProjectRole.CONTRIBUTOR)
    )
    project = thread.project_id if thread.project_id in writable_projects else None
    accessible_projects = (
        {project} if project else _project_set(await list_accessible_project_graph_ids(authority))
    )
    scope = await memory_auth.resolve_capture_default_scope(ctx=authority, project_id=project)
    if not memory_auth.api_key_memory_scope_allowed(
        authority, memory_scope=scope.memory_scope.value, scope_key=scope.scope_key
    ):
        raise NetAuthorSkippedError("no_writable_scope")
    organization = authority.organization
    members = (
        await member_directory(organization.slug, part.author_user_id)
        if organization is not None
        else {}
    )
    member = members.get(part.author_user_id)
    pack = await reflect_memory(
        render_net_transcript(thread, part, author_name=member.name if member else None),
        source_title=net_source_title(thread.channel, thread.thread),
        intent=ContextIntent.DECIDE.value,
        project=project,
        organization_id=thread.organization_id,
        principal_id=part.author_user_id,
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
        raise NetAuthorSkippedError(refusal)
    await log_reflection_audit(
        user_id=part.author_user_id,
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
    log.info(
        "net_thread_author_distilled",
        organization_id=thread.organization_id,
        channel=thread.channel,
        thread=thread.thread or None,
        author_user_id=part.author_user_id,
        messages=len(part.messages),
        source_id=pack.source_id,
        persisted=pack.persisted_count,
        memory_scope=scope.memory_scope.value,
    )
    return str(pack.source_id)


async def author_authority(
    organization_id: str, user_id: str, api_key_id: str | None
) -> AuthContext:
    """The author's current authority under the credential they posted with.

    A post made with an API key is reflected under that key as it stands
    now, resolved the way request auth resolves a key: its current project
    and memory-space grants narrow the context, and a key that was revoked,
    expired or lost write scope skips the author. A post made with a session
    is reflected with the user's own grants. Either way the author must still
    hold a write role in the organization.
    """
    claims: dict[str, object] = {"sub": user_id, "org": organization_id, "scopes": []}
    if api_key_id is not None:
        try:
            key = await resolve_api_key_authority(
                api_key_id=UUID(api_key_id),
                organization_id=UUID(organization_id),
                user_id=UUID(user_id),
            )
        except ValueError as exc:
            raise NetAuthorSkippedError("credential_unknown") from exc
        if key is None:
            raise NetAuthorSkippedError("credential_revoked")
        if not _api_key_allows_rest(scopes=list(key.scopes), method="POST"):
            raise NetAuthorSkippedError("credential_cannot_write")
        claims = _api_key_claims(key, scopes=list(key.scopes))
    try:
        authority = await resolve_auth_context(claims=claims)
    except (InvalidAuthClaimsError, UserNotFoundError) as exc:
        raise NetAuthorSkippedError("principal_unavailable") from exc
    if authority.user_id != user_id or authority.organization_id != organization_id:
        raise NetAuthorSkippedError("principal_unavailable")
    if authority.org_role not in contracts.WRITE_ROLES:
        raise NetAuthorSkippedError("principal_cannot_write")
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
    if isinstance(exc, HTTPException):
        return f"http_{exc.status_code}: {exc.detail}"[:200]
    return f"{type(exc).__name__}: {exc}"[:200]


__all__ = [
    "NET_DISTILL_SURFACE",
    "THREADS_PER_RUN",
    "NetAuthorSkippedError",
    "author_authority",
    "distill_idle_net_threads",
    "distill_net_thread",
    "reflect_author_window",
    "reflection_refusal",
]
