"""sibylnet: organization channels for agent-to-agent messages.

Visibility in v0 is flat and organization-wide. Every member of the
organization with a read role reads every channel, and posting needs a write
role, enforced with the same ``READ_ROLES`` / ``WRITE_ROLES`` gates as the
entity routes. Channels are organization-visible like public chat channels;
there are no private channels, presence or delivery receipts. Receivers own
dedupe (by message id), permissions on what they act on, and loop limits.

A message may name a graph project the poster can read. A ``decision`` is
remembered first, through the same scope resolution, project check and
memory policy as ``POST /memory/raw``, and is posted only once that write
succeeds; a refused write refuses the post.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.raw_capture_events import publish_raw_capture_changed
from sibyl.api.routes import entity_contracts as contracts, memory_auth
from sibyl.api.routes.activity_rollup import Member, members_from_rows
from sibyl.api.schemas.net import (
    NetAuthor,
    NetChannel,
    NetChannelsResponse,
    NetFeedResponse,
    NetMessage,
    NetMessageCreateRequest,
    NetMessageCreateResponse,
    NetThread,
    NetThreadsResponse,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence import organization_runtime
from sibyl_core.auth import AuthOrganization, ProjectRole
from sibyl_core.auth.memory_policy import MemoryPolicyAction
from sibyl_core.services.net import (
    DEFAULT_FEED_LIMIT,
    MAX_FEED_CHANNELS,
    MAX_FEED_LIMIT,
    MAX_FEED_WAIT_SECONDS,
    NetCursor,
    NetFeedPage,
    NetMessageRecord,
    list_net_channels,
    list_net_threads,
    new_message_id,
    normalize_channel,
    normalize_thread,
    post_net_message,
    read_net_feed,
)
from sibyl_core.services.surreal_content import remember_raw_memory

log = structlog.get_logger()

NET_CAPTURE_SURFACE = "sibylnet"
NET_TAG = "sibylnet"
DECISION_TITLE_CHARS = 120
_POLL_SECONDS = 1.0

router = APIRouter(
    prefix="/net",
    tags=["net"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)


def _caller_id(ctx: AuthContext) -> str:
    principal_id = ctx.user_id
    if not principal_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return principal_id


async def member_directory(org_slug: str, actor_id: str) -> dict[str, Member]:
    """Organization members by user id, or nothing when the lookup fails.

    Names are a nicety on a message; a failed lookup leaves authors as bare
    user ids rather than failing the read.
    """
    try:
        rows = await organization_runtime.list_org_members(slug=org_slug, actor_id=UUID(actor_id))
    except Exception as exc:
        log.warning("net_member_lookup_failed", error=str(exc))
        return {}
    return members_from_rows(rows)


def net_message(record: NetMessageRecord, members: Mapping[str, Member]) -> NetMessage:
    member = members.get(record.author_user_id)
    return NetMessage(
        id=record.id,
        channel=record.channel,
        thread=record.thread or None,
        kind="decision" if record.kind == "decision" else "message",
        body=record.body,
        project_id=record.project_id,
        author=NetAuthor(
            user_id=record.author_user_id,
            name=member.name if member is not None else None,
            email=member.email if member is not None else None,
            agent=record.author_agent,
            session=record.author_session,
        ),
        api_key_id=record.api_key_id,
        decision_memory_id=record.decision_memory_id,
        created_at=record.created_at,
        cursor=record.cursor.encode(),
    )


async def _net_messages(
    records: Sequence[NetMessageRecord], *, org: AuthOrganization, ctx: AuthContext
) -> list[NetMessage]:
    members = await member_directory(org.slug, _caller_id(ctx)) if records else {}
    return [net_message(record, members) for record in records]


def decision_title(body: str) -> str:
    """The first non-empty line of the body, at most ``DECISION_TITLE_CHARS`` long."""
    first = next((line.strip() for line in body.splitlines() if line.strip()), body.strip())
    return first[:DECISION_TITLE_CHARS]


def decision_content(
    body: str, *, channel: str, thread: str, author: str, agent: str | None
) -> str:
    """The decision body plus one line saying where and by whom it was recorded."""
    where = f"sibylnet channel {channel}" + (f", thread {thread}" if thread else "")
    who = author + (f" ({agent})" if agent else "")
    return f"{body}\n\nRecorded in {where}, by {who}."


async def _remember_decision(
    request: NetMessageCreateRequest,
    *,
    message_id: str,
    org: AuthOrganization,
    ctx: AuthContext,
    members: Mapping[str, Member],
    http_request: Request | None,
) -> str:
    """Write the decision as a raw memory the way ``POST /memory/raw`` does.

    Same default-scope resolution for the named project, same project, scope
    and memory-policy checks in the same order, same audit row and capture
    event. Any refusal raises before anything is written.
    """
    principal_id = _caller_id(ctx)
    project_id = request.project_id
    thread = request.thread or ""
    resolved = await memory_auth.resolve_capture_default_scope(ctx=ctx, project_id=project_id)
    memory_scope = resolved.memory_scope.value
    scope_key = resolved.scope_key
    await memory_auth.authorize_project_filter(
        ctx=ctx,
        project_id=project_id,
        required_project_role=ProjectRole.CONTRIBUTOR,
        surface=NET_CAPTURE_SURFACE,
        memory_scope=memory_scope,
        scope_key=scope_key,
        policy_action="write",
        request=http_request,
    )
    await memory_auth.authorize_project_scope_write(
        ctx=ctx, memory_scope=memory_scope, scope_key=scope_key
    )
    write_decision = await memory_auth.authorize_memory_policy(
        ctx=ctx,
        action=MemoryPolicyAction.WRITE,
        memory_scope=memory_scope,
        scope_key=scope_key,
        surface=NET_CAPTURE_SURFACE,
        request=http_request,
        project_id=project_id,
    )
    author = members[principal_id].name if principal_id in members else principal_id
    provenance = {"channel": request.channel, "thread": thread or None, "message_id": message_id}
    metadata: dict[str, object] = {
        "capture_mode": "remember",
        "capture_surface": NET_CAPTURE_SURFACE,
        "remember_kind": "decision",
        NET_TAG: provenance,
    }
    if project_id:
        metadata["project_id"] = project_id
    try:
        memory = await remember_raw_memory(
            organization_id=str(org.id),
            principal_id=principal_id,
            source_id=f"{NET_CAPTURE_SURFACE}:manual",
            raw_content=decision_content(
                request.body,
                channel=request.channel,
                thread=thread,
                author=author,
                agent=request.agent,
            ),
            title=decision_title(request.body),
            memory_scope=memory_scope,
            scope_key=scope_key,
            tags=[NET_TAG, "decision"],
            metadata=metadata,
            provenance={"remember_kind": "decision", NET_TAG: provenance},
            capture_surface=NET_CAPTURE_SURFACE,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await memory_auth.log_memory_audit(
        action="memory.remember",
        ctx=ctx,
        request=http_request,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=project_id,
        source_surface=NET_CAPTURE_SURFACE,
        source_ids=[memory.source_id],
        derived_ids=[memory.id],
        policy_allowed=write_decision.allowed,
        policy_reason=write_decision.reason,
        details={
            "channel": request.channel,
            "thread": thread or None,
            "message_id": message_id,
            "scope_resolution": resolved.reason,
        },
    )
    await publish_raw_capture_changed(
        organization_id=memory.organization_id, raw_memory_ids=[memory.id]
    )
    return memory.id


@router.post(
    "/messages",
    status_code=201,
    response_model=NetMessageCreateResponse,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
async def post_message(
    request: NetMessageCreateRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> NetMessageCreateResponse:
    """Post a message to a channel; a decision is remembered before it is posted."""
    principal_id = _caller_id(ctx)
    decision = request.kind == "decision"
    if request.project_id and not decision:
        await verify_entity_project_access(
            None,
            ctx,
            request.project_id,
            required_role=ProjectRole.VIEWER,
            require_existing_project=True,
        )
    message_id = new_message_id()
    members = await member_directory(org.slug, principal_id)
    decision_memory_id = (
        await _remember_decision(
            request,
            message_id=message_id,
            org=org,
            ctx=ctx,
            members=members,
            http_request=http_request,
        )
        if decision
        else None
    )
    record = await post_net_message(
        organization_id=str(org.id),
        message_id=message_id,
        channel=request.channel,
        thread=request.thread or "",
        kind=request.kind,
        body=request.body,
        author_user_id=principal_id,
        project_id=request.project_id,
        api_key_id=ctx.api_key_id,
        author_agent=request.agent,
        author_session=request.session,
        decision_memory_id=decision_memory_id,
    )
    log.info(
        "net_message_posted",
        organization_id=str(org.id),
        channel=record.channel,
        thread=record.thread or None,
        kind=record.kind,
        message_id=record.id,
        decision_memory_id=decision_memory_id,
    )
    return NetMessageCreateResponse(
        message=net_message(record, members), decision_memory_id=decision_memory_id
    )


def _feed_channels(values: Iterable[str] | None) -> list[str]:
    channels: list[str] = []
    for value in values or ():
        try:
            channels.append(normalize_channel(value))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    channels = list(dict.fromkeys(channels))
    if len(channels) > MAX_FEED_CHANNELS:
        raise HTTPException(
            status_code=422, detail=f"at most {MAX_FEED_CHANNELS} channels per feed read"
        )
    return channels


def _feed_cursor(since: str | None) -> NetCursor | None:
    if since is None:
        return None
    try:
        return NetCursor.decode(since)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _feed_thread(thread: str | None) -> str | None:
    if thread is None:
        return None
    try:
        return normalize_thread(thread) or None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/feed", response_model=NetFeedResponse)
async def read_feed(
    http_request: Request,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    since: str | None = Query(
        default=None, description="Cursor from an earlier read; omit to start at the tail"
    ),
    channel: Annotated[
        list[str] | None,
        Query(description=f"Only these channels; repeat for several, at most {MAX_FEED_CHANNELS}"),
    ] = None,
    thread: str | None = Query(default=None, description="Only this thread"),
    limit: int = Query(
        default=DEFAULT_FEED_LIMIT,
        ge=0,
        le=MAX_FEED_LIMIT,
        description="Page size; 0 without `since` returns only a cursor at the current end",
    ),
    wait: int = Query(
        default=0,
        ge=0,
        le=MAX_FEED_WAIT_SECONDS,
        description="Seconds to hold the request open while nothing qualifies",
    ),
) -> NetFeedResponse:
    """Messages oldest first, after ``since`` or from the tail.

    Rows younger than the settle window are held back, so delivery is
    at-least-once and never skips a write that was still committing;
    consumers dedupe by message id. With ``wait``, an empty read re-checks
    about once a second until something qualifies or the wait runs out.
    """
    cursor = _feed_cursor(since)
    channels = _feed_channels(channel)
    thread_filter = _feed_thread(thread)
    organization_id = str(org.id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait

    async def read() -> NetFeedPage:
        return await read_net_feed(
            organization_id=organization_id,
            since=cursor,
            channels=channels,
            thread=thread_filter,
            limit=limit,
        )

    page = await read()
    while not page.messages and limit > 0:
        remaining = deadline - loop.time()
        if remaining <= 0 or await http_request.is_disconnected():
            break
        await asyncio.sleep(min(_POLL_SECONDS, remaining))
        page = await read()
    return NetFeedResponse(
        messages=await _net_messages(page.messages, org=org, ctx=ctx),
        next_since=page.next_since.encode(),
        more=page.more,
    )


@router.get("/channels", response_model=NetChannelsResponse)
async def list_channels(
    org: AuthOrganization = Depends(get_current_organization),
) -> NetChannelsResponse:
    """Channels with any message, most recently active first."""
    summaries = await list_net_channels(organization_id=str(org.id))
    return NetChannelsResponse(
        channels=[
            NetChannel(
                channel=summary.channel,
                message_count=summary.message_count,
                thread_count=summary.thread_count,
                last_message_at=summary.last_message_at,
            )
            for summary in summaries
        ]
    )


@router.get("/threads", response_model=NetThreadsResponse)
async def list_threads(
    channel: str = Query(description="Channel whose threads to list"),
    org: AuthOrganization = Depends(get_current_organization),
) -> NetThreadsResponse:
    """A channel's threads, most recently active first, with distillation state.

    Messages posted without a thread are counted on a row whose ``thread`` is
    null.
    """
    try:
        normalized = normalize_channel(channel)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    states = await list_net_threads(organization_id=str(org.id), channel=normalized)
    return NetThreadsResponse(
        threads=[
            NetThread(
                channel=state.channel,
                thread=state.thread or None,
                project_id=state.project_id,
                message_count=state.message_count,
                first_message_at=state.first_message_at,
                last_message_at=state.last_message_at,
                reflected_through=state.reflected_through,
                reflection_source_id=state.reflection_source_id,
                reflection_error=state.reflection_error,
            )
            for state in states
        ]
    )


__all__ = [
    "NET_CAPTURE_SURFACE",
    "decision_content",
    "decision_title",
    "member_directory",
    "net_message",
    "router",
]
