"""sibylnet: organization channels for agent-to-agent messages.

Visibility in v0 is flat and organization-wide. Every member of the
organization with a read role reads every channel, and posting needs a write
role, enforced with the same ``READ_ROLES`` / ``WRITE_ROLES`` gates as the
entity routes. Channels are organization-visible like public chat channels;
there are no private channels or delivery receipts. Receivers own dedupe (by
message id), permissions on what they act on, and loop limits.

Presence is derived from use and never maintained: every feed read an agent
makes lists its reader as watching what it read, for two minutes after the
read's wait (see ``sibyl.coordination.presence``), and ``GET /presence``
adds who posted in the last hour. A read made by the web app's own session
is a person looking, not an agent listening, so it lists no one.

Naming a graph project on a post takes contributor access to it, within
the project grants of the API key the caller used, the same narrowing a
remembered write gets. On a named thread, the first post that names a
project pins the thread to it. A later plain message naming a different
project is refused with 409; a post naming none always goes through, and so
does a decision, whose project only decides where its own memory goes.
Threadless messages never pin. A ``task:<id>`` thread naming a task of this
organization is pinned to that task's project from its first message on,
whoever posts it, so it cannot be claimed for another project ahead of the
task's handoff.

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
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sibyl.api.idempotency import (
    replay_idempotent_response,
    save_idempotent_response,
    serialize_idempotent_request,
)
from sibyl.api.net_events import announce_net_message, net_wakeups
from sibyl.api.net_reads import FeedQuestion, SharedFeedReads
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
    NetMessageKindLabel,
    NetPresenceResponse,
    NetRecentPoster,
    NetThread,
    NetThreadsResponse,
    NetWatcher,
)
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.coordination.presence import WATCHING_TTL_SECONDS, get_presence
from sibyl.persistence.auth_runtime import get_users_by_ids
from sibyl_core.auth import AuthOrganization, ProjectRole
from sibyl_core.auth.memory_policy import MemoryPolicyAction
from sibyl_core.errors import EntityNotFoundError
from sibyl_core.models.entities import EntityType
from sibyl_core.services.net import (
    DEFAULT_FEED_LIMIT,
    MAX_AGENT_CHARS,
    MAX_FEED_CHANNELS,
    MAX_FEED_LIMIT,
    MAX_FEED_WAIT_SECONDS,
    MAX_SESSION_CHARS,
    NetCursor,
    NetFeedPage,
    NetMessageRecord,
    NetThreadProjectPinnedError,
    list_net_channels,
    list_net_threads,
    net_thread_project,
    new_message_id,
    normalize_channel,
    normalize_label,
    normalize_thread,
    post_net_message,
    read_net_feed,
    recent_net_authors,
)
from sibyl_core.services.surreal_content import remember_raw_memory
from sibyl_core.tools.helpers import _project_id_for_policy

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


def caller_name(ctx: AuthContext) -> dict[str, AuthorName]:
    """The caller's display name, from the user the request authenticated as."""
    email = str(ctx.user.email).strip() if ctx.user.email else None
    name = str(ctx.user.name).strip() if ctx.user.name else None
    return {str(ctx.user.id): AuthorName(name=name or email, email=email)}


async def _read_feed_page(question: FeedQuestion, since: NetCursor, limit: int) -> NetFeedPage:
    organization_id, channels, thread = question
    return await read_net_feed(
        organization_id=organization_id,
        since=since,
        channels=sorted(channels or ()),
        thread=thread,
        limit=limit,
    )


shared_feed_reads: SharedFeedReads[AuthorName] = SharedFeedReads(_read_feed_page, author_directory)


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


TASK_THREAD_PREFIX = "task:"


async def get_knowledge_read_adapter(group_id: str) -> Any:
    from sibyl.persistence.graph_runtime import get_knowledge_read_adapter as service

    return await service(group_id)


async def task_thread_pin(organization_id: str, thread: str) -> str | None:
    """The project a ``task:<id>`` thread belongs to: its task's, when that task exists here.

    Such a thread is the conversation about one task, so it is pinned to that
    task's project whoever posts first, and nobody can claim it for another
    project ahead of a handoff. Any other thread, or one naming a task this
    organization does not hold, follows the first-post rule.
    """
    if not thread.startswith(TASK_THREAD_PREFIX):
        return None
    task_id = thread.removeprefix(TASK_THREAD_PREFIX).strip()
    if not task_id:
        return None
    service = await get_knowledge_read_adapter(organization_id)
    try:
        entity = await service.get_entity(task_id)
    except (EntityNotFoundError, KeyError):
        return None
    if entity is None or getattr(entity, "entity_type", None) != EntityType.TASK:
        return None
    return _project_id_for_policy(entity)


async def refuse_pinned_elsewhere(
    *,
    organization_id: str,
    channel: str,
    thread: str,
    project_id: str | None,
    thread_pin: str | None = None,
) -> None:
    """Refuse a plain message naming a project other than its thread's pin, before any write.

    Answered here so a caller can refuse before writing anything else; the
    message write checks again atomically.
    """
    if not project_id or not thread:
        return
    pinned = thread_pin or await net_thread_project(
        organization_id=organization_id, channel=channel, thread=thread
    )
    if pinned is not None and pinned != project_id:
        raise _pinned_conflict(channel, thread, pinned)


async def deliver_net_message(
    *,
    organization_id: str,
    ctx: AuthContext,
    message_id: str,
    channel: str,
    thread: str,
    kind: NetMessageKindLabel,
    body: str,
    project_id: str | None,
    agent: str | None,
    session: str | None,
    decision_memory_id: str | None = None,
    thread_pin: str | None = None,
) -> NetMessageRecord:
    """Store a message as the caller, under the key they used, and wake its watchers.

    Every post goes through here, so a message always carries the verified
    author and key whichever route wrote it.
    """
    try:
        record = await post_net_message(
            organization_id=organization_id,
            message_id=message_id,
            channel=channel,
            thread=thread,
            kind=kind,
            body=body,
            author_user_id=_caller_id(ctx),
            project_id=project_id,
            api_key_id=ctx.api_key_id,
            author_agent=agent,
            author_session=session,
            decision_memory_id=decision_memory_id,
            thread_pin=thread_pin,
        )
    except NetThreadProjectPinnedError as exc:
        # Only a plain message is refused, so no decision memory is behind it.
        pinned = thread_pin or await net_thread_project(
            organization_id=organization_id, channel=channel, thread=thread
        )
        raise _pinned_conflict(channel, thread, pinned) from exc
    await announce_net_message(
        organization_id=organization_id,
        channel=record.channel,
        thread=record.thread,
        message_id=record.id,
    )
    log.info(
        "net_message_posted",
        organization_id=organization_id,
        channel=record.channel,
        thread=record.thread or None,
        kind=record.kind,
        message_id=record.id,
        decision_memory_id=decision_memory_id,
    )
    return record


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
    pin = await task_thread_pin(str(org.id), thread)
    if not decision:
        await refuse_pinned_elsewhere(
            organization_id=str(org.id),
            channel=request.channel,
            thread=thread,
            project_id=request.project_id,
            thread_pin=pin,
        )
    message_id = new_message_id()
    members = caller_name(ctx)
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
    record = await deliver_net_message(
        organization_id=str(org.id),
        ctx=ctx,
        message_id=message_id,
        channel=request.channel,
        thread=thread,
        kind=request.kind,
        body=request.body,
        project_id=request.project_id,
        agent=request.agent,
        session=request.session,
        decision_memory_id=decision_memory_id,
        thread_pin=pin,
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


def _reader_label(value: str | None, *, field: str, max_chars: int) -> str | None:
    try:
        return normalize_label(value, field=field, max_chars=max_chars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def read_by_web_session(request: Request) -> bool:
    """Whether the web app's session cookie, not an agent's credential, authenticated a read.

    The browser sends only the cookie; the CLI and every other agent client
    send an Authorization header.
    """
    return not request.headers.get("authorization") and bool(
        request.cookies.get("sibyl_access_token")
    )


async def note_watching(
    *,
    organization_id: str,
    user_id: str,
    channels: Sequence[str],
    agent: str | None,
    session: str | None,
    wait: int,
) -> None:
    """List the reader as watching what it read until its wait plus the watching TTL.

    A read naming no channel, including a read of one thread across every
    channel, is one watcher with no channel. Presence is a nicety on top of
    the read, so a registry that cannot answer is logged and the read goes on.
    """
    read: list[str | None] = [*channels] or [None]
    try:
        await get_presence().note(
            organization_id,
            user_id=user_id,
            agent=agent,
            session=session,
            channels=read,
            window=wait + WATCHING_TTL_SECONDS,
        )
    except Exception as exc:
        log.warning("net_presence_note_failed", error=str(exc), organization_id=organization_id)


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
    agent: str | None = Query(
        default=None,
        max_length=MAX_AGENT_CHARS,
        description="Client-asserted agent label of the reader, shown in presence",
    ),
    session: str | None = Query(
        default=None,
        max_length=MAX_SESSION_CHARS,
        description="Client-asserted session id of the reader, shown in presence",
    ),
    ctx: AuthContext = Depends(get_auth_context),
) -> NetFeedResponse:
    """Messages oldest first, after ``since`` or from the tail.

    Rows younger than the settle window are held back, so delivery is
    at-least-once and never skips a write that was still committing;
    consumers dedupe by message id. With ``wait``, an empty read is held
    open until a matching post settles (announced on the event bus, from any
    pod) or the wait runs out, re-reading on a slow safety interval in case
    an announcement was lost.

    Every read lists its reader as watching the channels it read (see
    ``GET /presence``), alongside the first read rather than ahead of it.
    """
    cursor = _feed_cursor(since)
    channels = _feed_channels(channel)
    thread_filter = _feed_thread(thread)
    reader_agent = _reader_label(agent, field="agent", max_chars=MAX_AGENT_CHARS)
    reader_session = _reader_label(session, field="session", max_chars=MAX_SESSION_CHARS)
    reader_id = _caller_id(ctx)
    organization_id = str(org.id)
    question: FeedQuestion = (
        organization_id,
        frozenset(channels) if channels else None,
        thread_filter,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait

    async def read() -> tuple[NetFeedPage, Mapping[str, AuthorName]]:
        if cursor is not None and limit > 0:
            # Every waiter a post wakes reads at once; same-question reads
            # in this process share one query.
            return await shared_feed_reads.read(question, cursor, limit)
        page = await read_net_feed(
            organization_id=organization_id,
            since=cursor,
            channels=channels,
            thread=thread_filter,
            limit=limit,
        )
        return page, await author_directory(record.author_user_id for record in page.messages)

    async def noted() -> None:
        if not read_by_web_session(http_request):
            await note_watching(
                organization_id=organization_id,
                user_id=reader_id,
                channels=channels,
                agent=reader_agent,
                session=reader_session,
                wait=wait,
            )

    # Registered before the first read, so a post landing between that read
    # and the wait still wakes this request.
    with net_wakeups.watch(*question) as waiter:
        (page, authors), _ = await asyncio.gather(read(), noted())
        while not page.messages and limit > 0:
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0 or await http_request.is_disconnected():
                break
            await waiter.wait(waiter.recheck_in(now), remaining)
            page, authors = await read()
    return NetFeedResponse(
        messages=[net_message(record, authors) for record in page.messages],
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


@router.get("/presence", response_model=NetPresenceResponse)
async def read_presence(
    channel: str | None = Query(
        default=None, description="Only this channel; omit for the whole organization"
    ),
    org: AuthOrganization = Depends(get_current_organization),
) -> NetPresenceResponse:
    """Who is listening and who has been talking, derived from use alone.

    ``watching`` lists every reader whose feed read began within the last two
    minutes, or is still waiting: one row per channel, user, agent label and
    session. A reader of every channel has a null channel and is listed under
    any channel asked about, since it hears that channel too. ``recent`` lists
    who posted in the last hour, one row per channel, user and agent label.
    """
    selected = None
    if channel is not None:
        try:
            selected = normalize_channel(channel)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    organization_id = str(org.id)
    seen, posters = await asyncio.gather(
        get_presence().watching(organization_id),
        recent_net_authors(organization_id=organization_id, channel=selected),
    )
    if selected is not None:
        seen = [entry for entry in seen if entry.watcher.channel in (selected, None)]
    seen.sort(key=lambda entry: (-entry.last_seen, entry.watcher.channel or ""))
    names = await author_directory(
        dict.fromkeys(
            [entry.watcher.user_id for entry in seen]
            + [poster.author_user_id for poster in posters]
        )
    )

    def name(user_id: str) -> str | None:
        member = names.get(user_id)
        return member.name if member is not None else None

    return NetPresenceResponse(
        watching=[
            NetWatcher(
                channel=entry.watcher.channel,
                user_id=entry.watcher.user_id,
                name=name(entry.watcher.user_id),
                agent=entry.watcher.agent,
                session=entry.watcher.session,
                last_seen=datetime.fromtimestamp(entry.last_seen, tz=UTC),
            )
            for entry in seen
        ],
        recent=[
            NetRecentPoster(
                channel=poster.channel,
                user_id=poster.author_user_id,
                name=name(poster.author_user_id),
                agent=poster.author_agent,
                last_post_at=poster.last_post_at,
            )
            for poster in posters
        ],
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
    "TASK_THREAD_PREFIX",
    "authorize_project_naming",
    "author_directory",
    "caller_name",
    "decision_content",
    "decision_title",
    "deliver_net_message",
    "net_message",
    "note_watching",
    "read_by_web_session",
    "refuse_pinned_elsewhere",
    "router",
    "shared_feed_reads",
    "task_thread_pin",
]
