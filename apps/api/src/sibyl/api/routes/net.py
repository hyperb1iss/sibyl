"""sibylnet: organization channels for agent-to-agent messages.

Visibility in v0 is flat and organization-wide. Every member of the
organization with a read role reads every channel, and posting needs a write
role, enforced with the same ``READ_ROLES`` / ``WRITE_ROLES`` gates as the
entity routes. Channels are organization-visible like public chat channels;
there are no private channels, presence or delivery receipts. Receivers own
dedupe (by message id), permissions on what they act on, and loop limits.

Naming a graph project on a post takes contributor access to it, within
the project grants of the API key the caller used, the same narrowing a
remembered write gets. On a named thread, the first post that names a
project pins the thread to it. A later plain message naming a different
project is refused with 409; a post naming none always goes through, and so
does a decision, whose project only decides where its own memory goes.
Threadless messages never pin.

A ``decision`` is remembered first, through the same scope resolution,
project check and memory policy as ``POST /memory/raw``, and is posted only
once that write succeeds; a refused write refuses the post.
``Idempotency-Key`` is honored as it is on ``POST /memory/raw``: a retry of a
request that completed gets the first response back and writes nothing. A
request interrupted after its decision was remembered but before it finished
runs again on retry, so that decision can be remembered twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.idempotency import (
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
)
from sibyl.api.net_events import SAFETY_RECHECK_SECONDS, announce_net_message, net_wakeups
from sibyl.api.raw_capture_events import publish_raw_capture_changed
from sibyl.api.routes import entity_contracts as contracts, memory_auth
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
from sibyl.persistence.auth_runtime import get_users_by_ids
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
    NetThreadProjectPinnedError,
    list_net_channels,
    list_net_threads,
    net_thread_project,
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


@dataclass(frozen=True, slots=True)
class AuthorName:
    name: str | None
    email: str | None


async def author_directory(user_ids: Iterable[str]) -> dict[str, AuthorName]:
    """Display names for the given authors, or nothing when the lookup fails.

    Reads only those users, through the uuid index, so the cost follows the
    authors on the page rather than the size of the organization. Names are
    a nicety on a message; a failed lookup leaves authors as bare user ids
    rather than failing the read.
    """
    try:
        users = await get_users_by_ids(user_ids)
    except Exception as exc:
        log.warning("net_author_lookup_failed", error=str(exc))
        return {}
    directory: dict[str, AuthorName] = {}
    for user_id, user in users.items():
        email = str(user.email).strip() if user.email else None
        name = str(user.name).strip() if user.name else None
        directory[user_id] = AuthorName(name=name or email, email=email)
    return directory


def net_message(record: NetMessageRecord, members: Mapping[str, AuthorName]) -> NetMessage:
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


async def _net_messages(records: Sequence[NetMessageRecord]) -> list[NetMessage]:
    members = await author_directory(record.author_user_id for record in records)
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
    members: Mapping[str, AuthorName],
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


async def authorize_project_naming(ctx: AuthContext, project_id: str) -> None:
    """Refuse a post naming a project the caller cannot contribute to.

    Contributor on the project, and inside the API key's project grants when
    the caller used a restricted key: naming a project decides where the
    thread is distilled, so it takes the same authority as writing there.
    """
    await verify_entity_project_access(
        None,
        ctx,
        project_id,
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )
    if ctx.api_key_project_ids is not None and project_id not in ctx.api_key_project_ids:
        raise HTTPException(status_code=403, detail="api_key_project_denied")


def _pinned_conflict(channel: str, thread: str, pinned: str | None) -> HTTPException:
    where = f"{channel} thread {thread}" if thread else f"{channel} (no thread)"
    message = (
        f"{where} is pinned to project {pinned}; post without a project or name that one."
        if pinned
        else f"{where} is pinned to another project; post without a project."
    )
    return HTTPException(
        status_code=409,
        detail={"error": "thread_project_pinned", "message": message, "project_id": pinned},
    )


@router.post(
    "/messages",
    status_code=201,
    response_model=NetMessageCreateResponse,
    dependencies=[Depends(require_org_role(*contracts.WRITE_ROLES))],
)
@serialize_idempotent_request
async def post_message(
    request: NetMessageCreateRequest,
    http_request: Request = memory_auth.REQUEST_AUTO_INJECT_SENTINEL,
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> NetMessageCreateResponse:
    """Post a message to a channel; a decision is remembered before it is posted."""
    principal_id = _caller_id(ctx)
    decision = request.kind == "decision"
    thread = request.thread or ""
    if request.project_id:
        await authorize_project_naming(ctx, request.project_id)
    idempotency_payload = {"body": request.model_dump(mode="json")}
    replayed = await replay_idempotent_response(
        http_request,
        organization_id=org.id,
        principal_id=principal_id,
        method="POST",
        path="/net/messages",
        payload=idempotency_payload,
        response_model=NetMessageCreateResponse,
        content_session=None,
    )
    if replayed is not None:
        return replayed
    if request.project_id and thread and not decision:
        # Answered here for a clear refusal; the write checks again atomically.
        pinned = await net_thread_project(
            organization_id=str(org.id), channel=request.channel, thread=thread
        )
        if pinned is not None and pinned != request.project_id:
            raise _pinned_conflict(request.channel, thread, pinned)
    message_id = new_message_id()
    members = await author_directory([principal_id])
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
    try:
        record = await post_net_message(
            organization_id=str(org.id),
            message_id=message_id,
            channel=request.channel,
            thread=thread,
            kind=request.kind,
            body=request.body,
            author_user_id=principal_id,
            project_id=request.project_id,
            api_key_id=ctx.api_key_id,
            author_agent=request.agent,
            author_session=request.session,
            decision_memory_id=decision_memory_id,
        )
    except NetThreadProjectPinnedError as exc:
        # Only a plain message is refused, so no decision memory is behind it.
        pinned = await net_thread_project(
            organization_id=str(org.id), channel=request.channel, thread=thread
        )
        raise _pinned_conflict(request.channel, thread, pinned) from exc
    await announce_net_message(
        organization_id=str(org.id),
        channel=record.channel,
        thread=record.thread,
        message_id=record.id,
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
    response = NetMessageCreateResponse(
        message=net_message(record, members), decision_memory_id=decision_memory_id
    )
    await save_idempotent_response(
        http_request,
        organization_id=org.id,
        principal_id=principal_id,
        method="POST",
        path="/net/messages",
        payload=idempotency_payload,
        response=response,
        status_code=201,
        content_session=None,
    )
    return response


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
    consumers dedupe by message id. With ``wait``, an empty read is held
    open until a matching post settles (announced on the event bus, from any
    pod) or the wait runs out, re-reading on a slow safety interval in case
    an announcement was lost.
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

    # Registered before the first read, so a post landing between that read
    # and the wait still wakes this request.
    with net_wakeups.watch(
        organization_id, frozenset(channels) if channels else None, thread_filter
    ) as waiter:
        page = await read()
        while not page.messages and limit > 0:
            remaining = deadline - loop.time()
            if remaining <= 0 or await http_request.is_disconnected():
                break
            await waiter.wait(min(SAFETY_RECHECK_SECONDS, remaining))
            page = await read()
    return NetFeedResponse(
        messages=await _net_messages(page.messages),
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
                reflection_skipped=list(state.reflection_skipped),
            )
            for state in states
        ]
    )


__all__ = [
    "NET_CAPTURE_SURFACE",
    "authorize_project_naming",
    "decision_content",
    "decision_title",
    "author_directory",
    "net_message",
    "router",
]
