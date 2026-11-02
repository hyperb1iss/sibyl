"""sibylnet: organization channels agents talk in, and the threads that settle into memory.

Messages and per-thread state live in the shared content namespace, filtered
by ``organization_id`` in every statement the way ``reflection_supersessions``
is. Both tables carry ``PERMISSIONS NONE``; only the server reads them.

Visibility is deliberately flat in v0: every member of an organization with a
read role reads every channel, and posting needs a write role. Channels are
organization-visible like public chat channels. The routes enforce the roles;
nothing here filters by reader.

Order and cursors. The feed is ordered by ``(created_at, uuid)``. A cursor
carries the stored ISO text of ``created_at`` rather than a Python datetime,
because the SDK keeps only microseconds and a bound truncated that way would
hand back the row it was cut from forever. On a server the text reaches the
engine as a native datetime parameter, which the planner seeks the composite
index on; the embedded engine gets an inline cast (see
``graph_community_managers._cursor_binding`` for the same split). A page after
a cursor runs two statements, the cursor's own timestamp drained by an
equality seek and then the strict range after it, because the 3.x planner
drops rows sharing the bound's timestamp from a single combined predicate.

Settle window. Nothing younger than ``SETTLE_SECONDS`` by the database clock
is ever returned, so a write that is still committing cannot land behind a
reader whose cursor already moved past its timestamp. Delivery is
at-least-once; consumers dedupe by message id.

Thread state. Every post upserts one ``net_threads`` row per
``(organization, channel, thread)``, addressed by a record id derived from
that key, so concurrent first posts to a new thread conflict on one record and
the client replays the loser instead of tripping the unique index. Threadless
messages share the thread ``''`` so the key stays total. The distillation job
claims idle rows with a conditional update before it reflects them.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog

from sibyl_core.backends.surreal.records import coerce_datetime
from sibyl_core.services import content_client

log = structlog.get_logger(__name__)

type NetMessageKind = Literal["message", "decision"]

MAX_BODY_CHARS = 16_000
MAX_THREAD_CHARS = 200
MAX_AGENT_CHARS = 64
MAX_SESSION_CHARS = 128
DEFAULT_FEED_LIMIT = 100
MAX_FEED_LIMIT = 500
# SurrealDB 3.x index-unions an IN list only up to 32 values; past that the
# channel filter plans as a table scan.
MAX_FEED_CHANNELS = 32
MAX_FEED_WAIT_SECONDS = 25
SETTLE_SECONDS = 2
LISTING_LIMIT = 200

MAX_REFLECTION_ATTEMPTS = 3
DISTILL_WINDOW_MESSAGES = 200
DISTILL_WINDOW_CHARS = 60_000
# A claim older than this belongs to a run that died mid-reflection; the next
# run may take the thread over. Reflection itself takes seconds.
CLAIM_LEASE_MINUTES = 15

_CHANNEL_PATTERN = re.compile(r"[@#]?[a-z0-9][a-z0-9._-]{0,63}")
_CURSOR_TIME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})"
)
_CURSOR_SEPARATOR = "|"

_SETTLED = f"created_at <= time::now() - {SETTLE_SECONDS}s"
_MESSAGE_FIELDS = (
    "uuid, organization_id, channel, thread, kind, body, project_id, author_user_id, "
    "api_key_id, author_agent, author_session, decision_memory_id, created_at, "
    "<string>created_at AS created_at_text"
)
_THREAD_FIELDS = (
    "organization_id, channel, thread, project_id, message_count, first_message_at, "
    "last_message_at, reflected_through, reflection_source_id, reflection_attempts, "
    "reflection_error"
)
# One record per (organization, channel, thread). Concurrent upserts of the
# same thread then contend for a single key, which the server reports as a
# retryable transaction conflict, rather than racing two creates into the
# unique index.
_THREAD_RECORD = (
    "type::record(string::concat('net_threads:', "
    "crypto::sha256(type::string([$organization_id, $channel, $thread]))))"
)

_POST_MESSAGE = f"""
RETURN {{
    CREATE net_messages CONTENT $record RETURN NONE;
    LET $created = (SELECT VALUE created_at FROM net_messages WHERE uuid = $uuid)[0];
    UPSERT {_THREAD_RECORD} SET
        organization_id = $organization_id,
        channel = $channel,
        thread = $thread,
        project_id = $project_id ?? project_id,
        message_count = (message_count ?? 0) + 1,
        first_message_at = first_message_at ?? $created,
        last_message_at = time::max([last_message_at ?? $created, $created]),
        reflection_attempts = reflection_attempts ?? 0,
        updated_at = time::now()
    RETURN NONE;
    RETURN (SELECT {_MESSAGE_FIELDS} FROM net_messages WHERE uuid = $uuid);
}};
"""

_CHANNEL_SUMMARY = (
    "SELECT channel, math::sum(message_count) AS message_count, "
    "count(thread != '') AS thread_count, time::max(last_message_at) AS last_message_at "
    "FROM net_threads WHERE organization_id = $organization_id GROUP BY channel;"
)
_CHANNEL_THREADS = (
    f"SELECT {_THREAD_FIELDS} FROM net_threads "
    "WHERE organization_id = $organization_id AND channel = $channel "
    f"ORDER BY last_message_at DESC LIMIT {LISTING_LIMIT};"
)

_IDLE_THREAD_CONDITIONS = (
    "(reflected_through = NONE OR reflected_through < last_message_at) "
    f"AND reflection_attempts < {MAX_REFLECTION_ATTEMPTS} "
    "AND (reflection_claimed_at = NONE "
    f"OR reflection_claimed_at < time::now() - {CLAIM_LEASE_MINUTES}m)"
)
_CLAIM_THREAD = (
    f"UPDATE {_THREAD_RECORD} SET reflection_attempts = reflection_attempts + 1, "
    "reflection_claimed_at = time::now(), updated_at = time::now() "
    f"WHERE {_IDLE_THREAD_CONDITIONS} RETURN AFTER;"
)
# The window starts after the last distilled message, compared in the engine
# so the stored nanoseconds never round-trip through Python.
_THREAD_WINDOW = f"""
RETURN {{
    LET $key = {_THREAD_RECORD};
    LET $after = (SELECT VALUE reflected_through FROM $key)[0]
        ?? <datetime>'1970-01-01T00:00:00Z';
    RETURN (
        SELECT {_MESSAGE_FIELDS} FROM net_messages
        WHERE organization_id = $organization_id AND channel = $channel
            AND thread = $thread AND created_at > $after AND {_SETTLED}
        ORDER BY created_at ASC, uuid ASC LIMIT $limit
    );
}};
"""
_SETTLE_THROUGH_MESSAGE = f"""
RETURN {{
    LET $through = (
        SELECT VALUE created_at FROM net_messages
        WHERE uuid = $through_uuid AND organization_id = $organization_id
    )[0];
    IF $through = NONE {{ THROW 'the last distilled net message is gone'; }};
    UPDATE {_THREAD_RECORD} SET
        reflected_through = time::max([reflected_through ?? $through, $through]),
        reflection_source_id = $source_id ?? reflection_source_id,
        reflection_attempts = 0,
        reflection_error = NONE,
        reflection_claimed_at = NONE,
        updated_at = time::now()
    RETURN NONE;
}};
"""
_SETTLE_THROUGH_LAST = (
    f"UPDATE {_THREAD_RECORD} SET reflected_through = last_message_at, "
    "reflection_attempts = 0, reflection_error = NONE, reflection_claimed_at = NONE, "
    "updated_at = time::now() RETURN NONE;"
)
_FAIL_THREAD = (
    f"UPDATE {_THREAD_RECORD} SET reflection_error = $reason, "
    "reflection_claimed_at = NONE, updated_at = time::now() RETURN NONE;"
)
_SETTLED_END = f"RETURN {{ settled_end: <string>(time::now() - {SETTLE_SECONDS}s) }};"


def normalize_channel(value: str) -> str:
    """Lowercase and validate a channel name, or raise ``ValueError``.

    ``@name`` is the convention for one agent's inbox and ``#name`` for a
    shared room; bare names are allowed too.
    """
    channel = value.strip().lower()
    if not _CHANNEL_PATTERN.fullmatch(channel):
        msg = (
            "channel must be 1-64 characters of a-z, 0-9, '.', '_' or '-', starting "
            "with a letter or digit, optionally prefixed with '@' or '#'"
        )
        raise ValueError(msg)
    return channel


def normalize_thread(value: str | None) -> str:
    """Strip and validate a thread name; no thread is the empty string."""
    if value is None:
        return ""
    thread = value.strip()
    if len(thread) > MAX_THREAD_CHARS:
        msg = f"thread must be at most {MAX_THREAD_CHARS} characters"
        raise ValueError(msg)
    if any(unicodedata.category(char) == "Cc" for char in thread):
        msg = "thread must not contain control characters"
        raise ValueError(msg)
    return thread


def normalize_body(value: str) -> str:
    """Strip a message body and hold it to 1..``MAX_BODY_CHARS`` characters."""
    body = value.strip()
    if not body:
        msg = "body must not be empty"
        raise ValueError(msg)
    if len(body) > MAX_BODY_CHARS:
        msg = f"body must be at most {MAX_BODY_CHARS} characters"
        raise ValueError(msg)
    return body


@dataclass(frozen=True, slots=True)
class NetCursor:
    """A position in the feed: the stored ``created_at`` text and a message id.

    The empty id sorts before every real one, so a cursor minted at an
    instant rather than at a message resumes with everything after it.
    """

    created_at: str
    uuid: str

    def encode(self) -> str:
        raw = f"{self.created_at}{_CURSOR_SEPARATOR}{self.uuid}".encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> NetCursor:
        """Parse a cursor the server issued, or raise ``ValueError``."""
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (UnicodeError, ValueError, binascii.Error) as exc:
            msg = "since is not a cursor this server issued"
            raise ValueError(msg) from exc
        created_at, separator, message_id = raw.rpartition(_CURSOR_SEPARATOR)
        if not separator or not _CURSOR_TIME_PATTERN.fullmatch(created_at):
            msg = "since is not a cursor this server issued"
            raise ValueError(msg)
        if message_id:
            try:
                UUID(message_id)
            except ValueError as exc:
                msg = "since is not a cursor this server issued"
                raise ValueError(msg) from exc
        return cls(created_at=created_at, uuid=message_id)


@dataclass(frozen=True, slots=True)
class NetMessageRecord:
    """One stored message, as the feed and the distillation job read it."""

    id: str
    organization_id: str
    channel: str
    thread: str
    kind: str
    body: str
    project_id: str | None
    author_user_id: str
    api_key_id: str | None
    author_agent: str | None
    author_session: str | None
    decision_memory_id: str | None
    created_at: datetime
    cursor: NetCursor


@dataclass(frozen=True, slots=True)
class NetFeedPage:
    """Messages oldest first, the cursor to resume from, and whether more wait."""

    messages: list[NetMessageRecord]
    next_since: NetCursor
    more: bool


@dataclass(frozen=True, slots=True)
class NetChannelSummary:
    channel: str
    message_count: int
    thread_count: int
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class NetThreadState:
    """One ``net_threads`` row: counts plus where distillation stands."""

    organization_id: str
    channel: str
    thread: str
    project_id: str | None
    message_count: int
    first_message_at: datetime | None
    last_message_at: datetime | None
    reflected_through: datetime | None
    reflection_source_id: str | None
    reflection_attempts: int
    reflection_error: str | None


def new_message_id() -> str:
    """A fresh server-side message id, minted before any write that names it."""
    return str(uuid4())


async def post_net_message(
    *,
    organization_id: str,
    channel: str,
    thread: str,
    kind: NetMessageKind,
    body: str,
    author_user_id: str,
    message_id: str | None = None,
    project_id: str | None = None,
    api_key_id: str | None = None,
    author_agent: str | None = None,
    author_session: str | None = None,
    decision_memory_id: str | None = None,
) -> NetMessageRecord:
    """Store one message and count it on its thread, in one statement.

    The message and the thread upsert share a single ``RETURN`` block, so a
    conflicting post to the same thread replays both together and the count
    never drifts from the rows.
    """
    uuid = message_id or new_message_id()
    record = {
        key: value
        for key, value in {
            "uuid": uuid,
            "organization_id": organization_id,
            "channel": channel,
            "thread": thread,
            "kind": kind,
            "body": body,
            "project_id": project_id,
            "author_user_id": author_user_id,
            "api_key_id": api_key_id,
            "author_agent": author_agent,
            "author_session": author_session,
            "decision_memory_id": decision_memory_id,
        }.items()
        if value is not None
    }
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            _POST_MESSAGE,
            record=record,
            uuid=uuid,
            organization_id=organization_id,
            channel=channel,
            thread=thread,
            project_id=project_id,
        )
    if not rows:
        msg = f"net message {uuid} was not stored"
        raise RuntimeError(msg)
    return _message_from_row(rows[0])


async def read_net_feed(
    *,
    organization_id: str,
    since: NetCursor | None,
    channels: Sequence[str] = (),
    thread: str | None = None,
    limit: int = DEFAULT_FEED_LIMIT,
) -> NetFeedPage:
    """One page of the feed.

    After a cursor: the settled rows strictly after it, oldest first, and
    whether more remain. Without one: the newest ``limit`` settled rows, still
    returned oldest first, so a new reader starts at the tail instead of
    replaying history; ``limit=0`` returns only the cursor at the settled end.
    """
    if len(channels) > MAX_FEED_CHANNELS:
        msg = f"at most {MAX_FEED_CHANNELS} channels per feed read"
        raise ValueError(msg)
    filters, params = _feed_filters(channels=channels, thread=thread)
    params["organization_id"] = organization_id
    select = (
        f"SELECT {_MESSAGE_FIELDS} FROM net_messages "
        f"WHERE organization_id = $organization_id{filters} AND {_SETTLED}"
    )
    async with content_client.surreal_content_client() as client:
        if since is not None:
            return await _page_after(client, select, params, since=since, limit=limit)
        if limit <= 0:
            return NetFeedPage(messages=[], next_since=await _settled_end(client), more=False)
        rows = await content_client.select_many(
            client,
            f"{select} ORDER BY created_at DESC, uuid DESC LIMIT $limit;",
            limit=limit,
            **params,
        )
        messages = [_message_from_row(row) for row in reversed(rows)]
        if not messages:
            return NetFeedPage(messages=[], next_since=await _settled_end(client), more=False)
    return NetFeedPage(messages=messages, next_since=messages[-1].cursor, more=False)


async def list_net_channels(*, organization_id: str) -> list[NetChannelSummary]:
    """Channels with any message, most recently active first."""
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, _CHANNEL_SUMMARY, organization_id=organization_id
        )
    summaries = [
        NetChannelSummary(
            channel=str(row.get("channel") or ""),
            message_count=_as_int(row.get("message_count")),
            thread_count=_as_int(row.get("thread_count")),
            last_message_at=_as_utc(row.get("last_message_at")),
        )
        for row in rows
        if row.get("channel")
    ]
    summaries.sort(key=lambda summary: _sort_instant(summary.last_message_at), reverse=True)
    return summaries[:LISTING_LIMIT]


async def list_net_threads(*, organization_id: str, channel: str) -> list[NetThreadState]:
    """A channel's threads, most recently active first."""
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, _CHANNEL_THREADS, organization_id=organization_id, channel=channel
        )
    return [_thread_from_row(row) for row in rows]


async def idle_net_threads(*, idle_minutes: int, limit: int) -> list[NetThreadState]:
    """Threads across every organization that went quiet with undistilled messages.

    Quiet means no message for ``idle_minutes``. A thread past its retry
    budget, or claimed by a run that is still inside its lease, is left out.
    """
    idle = max(int(idle_minutes), 0)
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            f"SELECT {_THREAD_FIELDS} FROM net_threads "
            f"WHERE last_message_at < time::now() - {idle}m AND {_IDLE_THREAD_CONDITIONS} "
            "ORDER BY last_message_at ASC LIMIT $limit;",
            limit=max(int(limit), 1),
        )
    return [_thread_from_row(row) for row in rows]


async def claim_net_thread(thread: NetThreadState) -> NetThreadState | None:
    """Claim a thread for one distillation attempt, or None when another run holds it.

    The claim is a conditional update of the row itself: it spends one retry
    and stamps the lease only while the thread still needs distilling, sits
    under its retry budget, and no live lease stands. Two runs racing for one
    thread conflict on the record and the replayed loser matches nothing. A
    run that dies after claiming has spent the attempt; the lease lets the next
    run in once it lapses.
    """
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(client, _CLAIM_THREAD, **_thread_key(thread))
    return _thread_from_row(rows[0]) if rows else None


async def net_thread_window(thread: NetThreadState) -> list[NetMessageRecord]:
    """The settled messages after the thread's last distilled one, oldest first.

    Capped at ``DISTILL_WINDOW_MESSAGES`` messages and ``DISTILL_WINDOW_CHARS``
    characters of body, keeping the oldest so the next pass continues where
    this one stopped. The first message always fits: a body is far smaller
    than the character cap.
    """
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, _THREAD_WINDOW, limit=DISTILL_WINDOW_MESSAGES, **_thread_key(thread)
        )
    window: list[NetMessageRecord] = []
    characters = 0
    for row in rows:
        message = _message_from_row(row)
        characters += len(message.body)
        if window and characters > DISTILL_WINDOW_CHARS:
            break
        window.append(message)
    return window


async def settle_net_thread(
    thread: NetThreadState,
    *,
    through_message_id: str | None,
    source_id: str | None = None,
) -> None:
    """Record a finished distillation and release the claim.

    ``reflected_through`` moves to the named message's own stored timestamp,
    never backwards; with no message (an empty window) it moves to the
    thread's last message so the row stops qualifying. The retry budget
    resets and any earlier error clears.
    """
    async with content_client.surreal_content_client() as client:
        if through_message_id is None:
            await content_client.select_many(client, _SETTLE_THROUGH_LAST, **_thread_key(thread))
            return
        await content_client.select_many(
            client,
            _SETTLE_THROUGH_MESSAGE,
            through_uuid=through_message_id,
            source_id=source_id,
            **_thread_key(thread),
        )


async def fail_net_thread(thread: NetThreadState, *, reason: str) -> None:
    """Record why a distillation attempt failed and release the claim.

    The attempt itself was spent when the thread was claimed.
    """
    async with content_client.surreal_content_client() as client:
        await content_client.select_many(
            client, _FAIL_THREAD, reason=reason[:500], **_thread_key(thread)
        )


def net_source_title(channel: str, thread: str) -> str:
    """The title a distilled transcript is persisted under."""
    return f"sibylnet {channel} {thread}".rstrip()


def render_net_transcript(
    thread: NetThreadState,
    messages: Sequence[NetMessageRecord],
    *,
    names: Mapping[str, str] | None = None,
) -> str:
    """The transcript reflection reads: a header, then one line per message.

    Lines are ``[<iso>] <name or user id> (<agent>): <body>``; the agent label
    is omitted when the poster sent none.
    """
    names = names or {}
    header = f"# sibylnet channel {thread.channel}"
    if thread.thread:
        header += f", thread {thread.thread}"
    lines = [header, ""]
    for message in messages:
        author = names.get(message.author_user_id) or message.author_user_id
        if message.author_agent:
            author += f" ({message.author_agent})"
        stamp = message.created_at.isoformat().replace("+00:00", "Z")
        lines.append(f"[{stamp}] {author}: {message.body}")
    return "\n".join(lines)


def _thread_key(thread: NetThreadState) -> dict[str, str]:
    return {
        "organization_id": thread.organization_id,
        "channel": thread.channel,
        "thread": thread.thread,
    }


def _feed_filters(*, channels: Sequence[str], thread: str | None) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    unique_channels = list(dict.fromkeys(channels))
    if len(unique_channels) == 1:
        clauses.append("channel = $channel")
        params["channel"] = unique_channels[0]
    elif unique_channels:
        clauses.append("channel IN $channels")
        params["channels"] = unique_channels
    if thread is not None:
        clauses.append("thread = $thread")
        params["thread"] = thread
    return "".join(f" AND {clause}" for clause in clauses), params


def _cursor_binding(client: object) -> tuple[str, Any]:
    """How the cursor's timestamp reaches the engine: a bound template and a value.

    A server seeks the composite index for a native datetime parameter, which
    the SDK's ``Datetime`` wrapper sends with its nanoseconds intact. The
    embedded engine gets the text and an inline cast.
    """
    from surrealdb.data.types.datetime import Datetime

    if getattr(client, "is_embedded", False):
        return "<datetime>$since_at", str
    return "$since_at", Datetime


async def _page_after(
    client: Any,
    select: str,
    params: dict[str, Any],
    *,
    since: NetCursor,
    limit: int,
) -> NetFeedPage:
    page_limit = max(int(limit), 0)
    if page_limit == 0:
        return NetFeedPage(messages=[], next_since=since, more=False)
    bound, value = _cursor_binding(client)
    order = "ORDER BY created_at ASC, uuid ASC LIMIT $limit;"
    statements = (
        f"{select} AND created_at = {bound} AND uuid > $since_uuid {order}",
        f"{select} AND created_at > {bound} {order}",
    )
    results = await client.execute_query_batch(
        " ".join(statements),
        since_at=value(since.created_at),
        since_uuid=since.uuid,
        limit=page_limit + 1,
        **params,
    )
    error = content_client.query_error(results)
    if error is not None:
        raise RuntimeError(error)
    if not isinstance(results, list) or len(results) != len(statements):
        msg = "net feed page returned no statement results"
        raise RuntimeError(msg)
    rows = [row for result in results for row in content_client.normalize_records(result)]
    messages = [_message_from_row(row) for row in rows[: page_limit + 1]]
    more = len(messages) > page_limit
    messages = messages[:page_limit]
    next_since = messages[-1].cursor if messages else since
    return NetFeedPage(messages=messages, next_since=next_since, more=more)


async def _settled_end(client: Any) -> NetCursor:
    """A cursor at the settled end of the feed by the database clock, between messages."""
    rows = await content_client.select_many(client, _SETTLED_END)
    text = rows[0].get("settled_end") if rows else None
    if not isinstance(text, str) or not text:
        msg = "net feed could not read the settled end"
        raise RuntimeError(msg)
    return NetCursor(created_at=text, uuid="")


def _message_from_row(row: Mapping[str, object]) -> NetMessageRecord:
    message_id = str(row.get("uuid") or "")
    created_text = str(row.get("created_at_text") or "")
    created_at = _as_utc(row.get("created_at")) or _as_utc(created_text)
    if not message_id or created_at is None or not created_text:
        msg = "net message row is missing its id or timestamp"
        raise RuntimeError(msg)
    return NetMessageRecord(
        id=message_id,
        organization_id=str(row.get("organization_id") or ""),
        channel=str(row.get("channel") or ""),
        thread=str(row.get("thread") or ""),
        kind=str(row.get("kind") or "message"),
        body=str(row.get("body") or ""),
        project_id=_optional_str(row.get("project_id")),
        author_user_id=str(row.get("author_user_id") or ""),
        api_key_id=_optional_str(row.get("api_key_id")),
        author_agent=_optional_str(row.get("author_agent")),
        author_session=_optional_str(row.get("author_session")),
        decision_memory_id=_optional_str(row.get("decision_memory_id")),
        created_at=created_at,
        cursor=NetCursor(created_at=created_text, uuid=message_id),
    )


def _thread_from_row(row: Mapping[str, object]) -> NetThreadState:
    return NetThreadState(
        organization_id=str(row.get("organization_id") or ""),
        channel=str(row.get("channel") or ""),
        thread=str(row.get("thread") or ""),
        project_id=_optional_str(row.get("project_id")),
        message_count=_as_int(row.get("message_count")),
        first_message_at=_as_utc(row.get("first_message_at")),
        last_message_at=_as_utc(row.get("last_message_at")),
        reflected_through=_as_utc(row.get("reflected_through")),
        reflection_source_id=_optional_str(row.get("reflection_source_id")),
        reflection_attempts=_as_int(row.get("reflection_attempts")),
        reflection_error=_optional_str(row.get("reflection_error")),
    )


def _as_utc(value: object) -> datetime | None:
    try:
        naive = coerce_datetime(value)
    except ValueError:
        return None
    return naive.replace(tzinfo=UTC) if naive is not None else None


def _sort_instant(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    return 0


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


__all__ = [
    "DEFAULT_FEED_LIMIT",
    "DISTILL_WINDOW_CHARS",
    "DISTILL_WINDOW_MESSAGES",
    "LISTING_LIMIT",
    "MAX_AGENT_CHARS",
    "MAX_BODY_CHARS",
    "MAX_FEED_CHANNELS",
    "MAX_FEED_LIMIT",
    "MAX_FEED_WAIT_SECONDS",
    "MAX_REFLECTION_ATTEMPTS",
    "MAX_SESSION_CHARS",
    "MAX_THREAD_CHARS",
    "SETTLE_SECONDS",
    "NetChannelSummary",
    "NetCursor",
    "NetFeedPage",
    "NetMessageKind",
    "NetMessageRecord",
    "NetThreadState",
    "claim_net_thread",
    "fail_net_thread",
    "idle_net_threads",
    "list_net_channels",
    "list_net_threads",
    "net_source_title",
    "net_thread_window",
    "new_message_id",
    "normalize_body",
    "normalize_channel",
    "normalize_thread",
    "post_net_message",
    "read_net_feed",
    "render_net_transcript",
    "settle_net_thread",
]
