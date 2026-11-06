"""sibylnet: organization channels agents talk in, and the threads that settle into memory.

Messages and per-thread state live in the shared content namespace, filtered
by ``organization_id`` in every statement the way ``reflection_supersessions``
is. Both tables carry ``PERMISSIONS NONE``; only the server reads them.

Visibility is deliberately flat in v0: every member of an organization with a
read role reads every channel, and posting needs a write role. Channels are
organization-visible like public chat channels. The routes enforce the roles;
nothing here filters by reader.

Order and cursors. The feed is ordered by ``(created_ns, uuid)``, where
``created_ns`` is ``created_at`` as an integer count of nanoseconds, computed
by the engine on every write. Ordering and paging never touch the datetime:
SurrealDB 3.2 orders a datetime index by the instant's printed form, which
drops trailing zeros, so ``...10.277579Z`` sorts after ``...10.277579001Z``
though it is earlier, and pages built on it skip, reorder and replay. An
integer has one width and one order on every engine. A cursor is that integer
and the message's canonical id, so it survives the SDK (which keeps only
microseconds of a datetime) unchanged.

Settle window. Nothing younger than ``SETTLE_SECONDS`` by the database clock,
compared in nanoseconds, is ever returned, so a write that is still
committing cannot land behind a reader whose cursor already moved past it.
Delivery is at-least-once; consumers dedupe by message id.

Thread state. Every post upserts one ``net_threads`` row per
``(organization, channel, thread)``, addressed by a record id derived from
that key, so concurrent first posts to a new thread conflict on one record and
the client replays the loser instead of tripping the unique index. Threadless
messages share the thread ``''`` so the key stays total.

Project pins. The first post to a named thread that names a project pins the
thread to it, so its project can never be moved by whoever writes last. A
later plain message naming a different project is refused in the same
statement; a decision never is, because its project only decides where its
own memory goes, and it leaves an existing pin alone. Threadless messages
never pin: ``''`` is everyone's inbox, and a project named on one of them
scopes only that message's decision memory. A new post also resets the
distillation retry budget, since new material deserves a fresh one.

Distillation. Every post marks its thread ``reflection_pending``, and only a
distillation that reaches the thread's last message clears it, in the same
statement that moves ``reflected_through``. The idle selection ranges over
the ``(reflection_pending, last_message_at)`` index, so its cost follows the
threads waiting to be distilled, not every thread ever written. The job
claims an idle row with a conditional update before it reflects it.
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
# A cursor's instant: nanoseconds since the epoch, written in canonical
# decimal (no sign, no leading zeros), within a signed 64-bit integer.
_CURSOR_NANOS_PATTERN = re.compile(r"0|[1-9][0-9]{0,18}", flags=re.ASCII)
_MAX_NANOS = 2**63 - 1
# Characters that end a line or steer a terminal. A label carrying one could
# start a forged attribution line in a transcript.
_LINE_BREAKING_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_CURSOR_SEPARATOR = "|"
TRANSCRIPT_CONTINUATION = "    "

_SETTLED = f"created_ns <= time::nano(time::now() - {SETTLE_SECONDS}s)"
_MESSAGE_FIELDS = (
    "uuid, organization_id, channel, thread, kind, body, project_id, author_user_id, "
    "api_key_id, author_agent, author_session, decision_memory_id, created_at, created_ns"
)
_THREAD_FIELDS = (
    "organization_id, channel, thread, project_id, message_count, first_message_at, "
    "last_message_at, reflected_through, reflected_through_ns, reflected_through_uuid, "
    "reflection_source_id, reflection_attempts, reflection_error, reflection_skipped"
)
# One record per (organization, channel, thread). Concurrent upserts of the
# same thread then contend for a single key, which the server reports as a
# retryable transaction conflict, rather than racing two creates into the
# unique index. The schema stores the same hash on the row as ``thread_key``,
# which is how an archive restore puts a thread back at this record.
_THREAD_RECORD = (
    "type::record(string::concat('net_threads:', "
    "crypto::sha256(type::string([$organization_id, $channel, $thread]))))"
)

PROJECT_PINNED_MARKER = "net_thread_project_pinned"
# The pin is checked before anything is written, inside the same statement as
# the write, so two first posts naming different projects conflict on the
# thread record and the replayed loser sees the winner's pin. Only a plain
# message on a named thread can be refused; the threadless row never holds a
# project, so it never pins.
_POST_MESSAGE = f"""
RETURN {{
    LET $key = {_THREAD_RECORD};
    LET $pinned = (SELECT VALUE project_id FROM $key)[0];
    IF $kind = 'message' AND $thread != '' AND $project_id != NONE
        AND $pinned != NONE AND $pinned != $project_id {{
        THROW '{PROJECT_PINNED_MARKER}';
    }};
    LET $pin = IF $thread = '' {{ NONE }} ELSE {{ $pinned ?? $project_id }};
    CREATE net_messages CONTENT $record RETURN NONE;
    LET $created = (SELECT VALUE created_at FROM net_messages WHERE uuid = $uuid)[0];
    UPSERT $key SET
        organization_id = $organization_id,
        channel = $channel,
        thread = $thread,
        project_id = $pin,
        message_count = (message_count ?? 0) + 1,
        first_message_at = first_message_at ?? $created,
        last_message_at = time::max([last_message_at ?? $created, $created]),
        reflection_attempts = 0,
        reflection_pending = true,
        updated_at = time::now()
    RETURN NONE;
    RETURN (SELECT {_MESSAGE_FIELDS} FROM net_messages WHERE uuid = $uuid);
}};
"""
_THREAD_PROJECT = f"""
RETURN {{
    LET $key = {_THREAD_RECORD};
    RETURN {{ project_id: (SELECT VALUE project_id FROM $key)[0] }};
}};
"""

CHANNEL_SUMMARY_STATEMENT = (
    "SELECT channel, math::sum(message_count) AS message_count, "
    "count(thread != '') AS thread_count, time::max(last_message_at) AS last_message_at "
    "FROM net_threads WHERE organization_id = $organization_id GROUP BY channel;"
)
CHANNEL_THREADS_STATEMENT = (
    f"SELECT {_THREAD_FIELDS} FROM net_threads "
    "WHERE organization_id = $organization_id AND channel = $channel "
    f"ORDER BY last_message_at DESC LIMIT {LISTING_LIMIT};"
)

# ``reflection_pending`` is set by every post and cleared only when a
# distillation reaches the thread's last message, so selection ranges over
# the threads with undistilled messages instead of all thread history.
_IDLE_THREAD_CONDITIONS = (
    "reflection_pending = true "
    f"AND reflection_attempts < {MAX_REFLECTION_ATTEMPTS} "
    "AND (reflection_claimed_at = NONE "
    f"OR reflection_claimed_at < time::now() - {CLAIM_LEASE_MINUTES}m)"
)
_CLAIM_THREAD = (
    f"UPDATE {_THREAD_RECORD} SET reflection_attempts = reflection_attempts + 1, "
    "reflection_claimed_at = time::now(), updated_at = time::now() "
    f"WHERE {_IDLE_THREAD_CONDITIONS} RETURN AFTER;"
)
# Seeks within one thread, on the thread index: the planner would otherwise
# range the organization index on created_ns and filter the thread out.
_THREAD_SCOPE = (
    "FROM net_messages WITH INDEX net_message_org_channel_thread_ns "
    "WHERE organization_id = $organization_id AND channel = $channel AND thread = $thread"
)
# The distillation watermark is the last distilled message's (created_ns,
# uuid), the feed's own order. Rows after it are the rows sharing its instant
# with a later id plus the rows after its instant, sought the way
# feed_statement seeks a page.
#
# The bounds are client parameters, never values computed inside the block:
# SurrealDB 4.0 plans an index range only from literals and client
# parameters, and a LET operand leaves it walking the whole thread from its
# first message.
_AT_MARK_AFTER_ID = "created_ns = $mark_ns AND uuid > $mark_uuid"
_AFTER_MARK_INSTANT = "created_ns > $mark_ns"
_THREAD_WINDOW = f"""
RETURN {{
    LET $take = $limit + count((SELECT VALUE uuid {_THREAD_SCOPE} AND created_ns = $mark_ns));
    LET $rows = array::flatten([
        (SELECT {_MESSAGE_FIELDS} {_THREAD_SCOPE} AND {_AT_MARK_AFTER_ID}
            ORDER BY created_ns ASC, uuid ASC LIMIT $limit),
        (SELECT {_MESSAGE_FIELDS} {_THREAD_SCOPE} AND {_AFTER_MARK_INSTANT}
            ORDER BY created_ns ASC, uuid ASC LIMIT $take)
    ]);
    RETURN (
        SELECT * FROM $rows WHERE {_SETTLED}
        ORDER BY created_ns ASC, uuid ASC LIMIT $limit
    );
}};
"""


# Whether any message, settled or not, sits after the new watermark, read
# with the thread's message count. Reads inside a block that writes plan
# differently on SurrealDB 4.0 (a compound index ranges on its first column
# only, so this count would walk the organization's whole history), so the
# count runs on its own and the write below trusts it only while the thread
# row still shows the count it was taken against and the watermark it was
# taken from. A post in between bumps the count, and a post racing the write
# conflicts with it on the thread record, so a message that lands while the
# thread is being reflected always leaves it pending.
_PENDING_AFTER_MARK = f"""
RETURN {{
    seen: (SELECT VALUE message_count FROM {_THREAD_RECORD})[0] ?? 0,
    later: count((SELECT VALUE uuid {_THREAD_SCOPE} AND {_AT_MARK_AFTER_ID}))
        + count((SELECT VALUE uuid {_THREAD_SCOPE} AND {_AFTER_MARK_INSTANT}))
}};
"""
_COUNTED_AGAINST_THIS_ROW = (
    "$state.message_count = $seen "
    "AND ($state.reflected_through_ns ?? -1) = $expected_ns "
    "AND ($state.reflected_through_uuid ?? '') = $expected_uuid"
)
_SETTLE_THROUGH_MESSAGE = f"""
RETURN {{
    LET $through = (
        SELECT created_at, created_ns, organization_id FROM net_messages WHERE uuid = $through_uuid
    )[0];
    IF $through = NONE OR $through.organization_id != $organization_id
        OR $through.created_ns != $through_ns {{
        THROW 'the last distilled net message is gone or has moved';
    }};
    LET $key = {_THREAD_RECORD};
    LET $state = (
        SELECT message_count, reflected_through, reflected_through_ns, reflected_through_uuid
        FROM $key
    )[0];
    LET $advance = $state.reflected_through_ns = NONE
        OR $through_ns > $state.reflected_through_ns
        OR ($through_ns = $state.reflected_through_ns
            AND $through_uuid > $state.reflected_through_uuid);
    LET $counted = {_COUNTED_AGAINST_THIS_ROW};
    UPDATE $key SET
        reflected_through = IF $advance {{ $through.created_at }} ELSE {{ $state.reflected_through }},
        reflected_through_ns = IF $advance {{ $through_ns }} ELSE {{ $state.reflected_through_ns }},
        reflected_through_uuid = IF $advance {{ $through_uuid }} ELSE {{ $state.reflected_through_uuid }},
        reflection_pending = IF $counted {{ $later > 0 }} ELSE {{ true }},
        reflection_source_id = $source_id ?? reflection_source_id,
        reflection_attempts = 0,
        reflection_error = NONE,
        reflection_skipped = $skipped,
        reflection_claimed_at = NONE,
        updated_at = time::now()
    RETURN NONE;
}};
"""
# An empty window moves nothing: the watermark stays, and the thread stays
# pending only while some message (still settling, say) sits after it.
_SETTLE_EMPTY_WINDOW = f"""
RETURN {{
    LET $key = {_THREAD_RECORD};
    LET $state = (
        SELECT message_count, reflected_through_ns, reflected_through_uuid FROM $key
    )[0];
    LET $counted = {_COUNTED_AGAINST_THIS_ROW};
    UPDATE $key SET
        reflection_pending = IF $counted {{ $later > 0 }} ELSE {{ true }},
        reflection_attempts = 0,
        reflection_error = NONE,
        reflection_skipped = [],
        reflection_claimed_at = NONE,
        updated_at = time::now()
    RETURN NONE;
}};
"""
_FAIL_THREAD = (
    f"UPDATE {_THREAD_RECORD} SET reflection_error = $reason, "
    "reflection_claimed_at = NONE, updated_at = time::now() RETURN NONE;"
)
_SETTLED_END = f"RETURN {{ settled_end: time::nano(time::now() - {SETTLE_SECONDS}s) }};"


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
    if _breaks_lines(thread):
        msg = "thread must not contain control characters or line breaks"
        raise ValueError(msg)
    return thread


def normalize_label(value: str | None, *, field: str, max_chars: int) -> str | None:
    """Strip a client-asserted label (agent, session); None when empty.

    Labels are printed inside transcript attribution lines, so a control
    character or line break is refused rather than escaped.
    """
    if value is None:
        return None
    label = value.strip()
    if len(label) > max_chars:
        msg = f"{field} must be at most {max_chars} characters"
        raise ValueError(msg)
    if _breaks_lines(label):
        msg = f"{field} must not contain control characters or line breaks"
        raise ValueError(msg)
    return label or None


def _breaks_lines(text: str) -> bool:
    return any(unicodedata.category(char) in _LINE_BREAKING_CATEGORIES for char in text)


def _single_line(text: str) -> str:
    return "".join(
        " " if unicodedata.category(char) in _LINE_BREAKING_CATEGORIES else char for char in text
    )


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
    """A position in the feed: an instant in nanoseconds and a message id.

    Opaque to clients. The empty id sorts before every real one, so a cursor
    minted at an instant rather than at a message resumes with everything
    after it.
    """

    created_ns: int
    uuid: str

    def encode(self) -> str:
        raw = f"{self.created_ns}{_CURSOR_SEPARATOR}{self.uuid}".encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> NetCursor:
        """Parse a cursor exactly as the server writes it, or raise ``ValueError``.

        Strict on purpose: the instant must be canonical decimal within a
        signed 64-bit integer and the id a canonical lowercase UUID (or empty),
        because the id is compared as text against stored ids, so any other
        spelling of the same UUID would sort elsewhere and replay or skip
        messages. Anything else, older cursor formats included, is refused,
        and the feed answers it with 422.
        """
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        except (UnicodeError, ValueError, binascii.Error) as exc:
            raise ValueError(_NOT_A_CURSOR) from exc
        nanos, separator, message_id = raw.partition(_CURSOR_SEPARATOR)
        if not separator or not _CURSOR_NANOS_PATTERN.fullmatch(nanos):
            raise ValueError(_NOT_A_CURSOR)
        created_ns = int(nanos)
        if created_ns > _MAX_NANOS:
            raise ValueError(_NOT_A_CURSOR)
        if message_id and not _canonical_uuid(message_id):
            raise ValueError(_NOT_A_CURSOR)
        cursor = cls(created_ns=created_ns, uuid=message_id)
        if cursor.encode() != token:
            raise ValueError(_NOT_A_CURSOR)
        return cursor


_NOT_A_CURSOR = "since is not a cursor this server issued"


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


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
    reflection_skipped: tuple[str, ...] = ()
    reflected_mark: NetCursor | None = None
    """The last distilled message's position, which the next window starts after."""


@dataclass(frozen=True, slots=True)
class NetAuthorWindow:
    """One author's messages in a distillation window, under one credential.

    Each is reflected on its own, as that author and with the authority of
    the credential the messages were posted with, so nobody's lines enter
    another person's reflection.
    """

    author_user_id: str
    api_key_id: str | None
    messages: tuple[NetMessageRecord, ...]


class NetThreadProjectPinnedError(Exception):
    """A post named a project other than the one its thread is pinned to."""

    def __init__(self, pinned_project_id: str | None) -> None:
        self.pinned_project_id = pinned_project_id
        super().__init__(PROJECT_PINNED_MARKER)


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
    never drifts from the rows. Raises ``NetThreadProjectPinnedError``, with
    nothing written, when a plain message on a named thread names a project
    other than the thread's pin. The message keeps the project it named
    either way.
    """
    # The API validates these on the way in; a direct caller is held to the
    # same rules, since the labels are printed inside transcript lines.
    author_agent = normalize_label(author_agent, field="agent", max_chars=MAX_AGENT_CHARS)
    author_session = normalize_label(author_session, field="session", max_chars=MAX_SESSION_CHARS)
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
        try:
            rows = await content_client.select_many(
                client,
                _POST_MESSAGE,
                record=record,
                uuid=uuid,
                organization_id=organization_id,
                channel=channel,
                thread=thread,
                kind=kind,
                project_id=project_id,
            )
        except Exception as failure:
            # The live engine raises its own error type and the client its
            # parsed one, so the thrown marker is the reliable signal.
            if PROJECT_PINNED_MARKER not in str(failure):
                raise
            raise NetThreadProjectPinnedError(None) from failure
    if not rows:
        msg = f"net message {uuid} was not stored"
        raise RuntimeError(msg)
    return _message_from_row(rows[0])


async def net_thread_project(*, organization_id: str, channel: str, thread: str) -> str | None:
    """The project a thread is pinned to, or None when no post has named one."""
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            _THREAD_PROJECT,
            organization_id=organization_id,
            channel=channel,
            thread=thread,
        )
    return _optional_str(rows[0].get("project_id")) if rows else None


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
    page_limit = max(int(limit), 0)
    async with content_client.surreal_content_client() as client:
        statement, params = feed_statement(
            organization_id=organization_id,
            since=since,
            channels=channels,
            thread=thread,
            limit=page_limit,
        )
        if statement is None:
            if since is not None:
                return NetFeedPage(messages=[], next_since=since, more=False)
            return NetFeedPage(messages=[], next_since=await _settled_end(client), more=False)
        rows = await content_client.select_many(client, statement, **params)
        if since is not None:
            messages = [_message_from_row(row) for row in rows]
            more = len(messages) > page_limit
            messages = messages[:page_limit]
            next_since = messages[-1].cursor if messages else since
            return NetFeedPage(messages=messages, next_since=next_since, more=more)
        messages = [_message_from_row(row) for row in reversed(rows)]
        if not messages:
            return NetFeedPage(messages=[], next_since=await _settled_end(client), more=False)
    return NetFeedPage(messages=messages, next_since=messages[-1].cursor, more=False)


def feed_statement(
    *,
    organization_id: str,
    since: NetCursor | None,
    channels: Sequence[str],
    thread: str | None,
    limit: int,
) -> tuple[str | None, dict[str, Any]]:
    """The one statement a feed read issues, with its parameters; None reads nothing.

    Every source is an index seek on ``created_ns`` bounded by its own
    ``LIMIT``, so a read costs about ``limit`` rows per channel however long
    the history is:

    - Each seek names its index with ``WITH INDEX``: the organization index,
      or with a channel or thread filter the index that leads with them, one
      seek per channel. Left to itself the planner ranges the organization
      index on ``created_ns`` and filters every message the organization ever
      posted. The seeks are merged and cut to ``limit`` in the engine.
    - A tail ranges below the settle line. A page after a cursor seeks the
      rows sharing the cursor's instant by equality (ranging only on the id)
      and the rows after it, and the settle line applies to the merged rows:
      a settle predicate inside a seek hands the planner a second range,
      which it prefers, walking history from the start.
    - The two engines disagree on bounds over a leading index column, so
      only the forms both get right are used. SurrealDB 3.2 counts the rows
      equal to an exclusive lower bound against ``LIMIT`` before skipping
      them (``created_ns > $t LIMIT n`` returns n minus the rows at ``$t``),
      so that seek's ``LIMIT`` is padded by the count of those rows; the
      embedded engine treats ``>=`` there as ``>``, so no inclusive bound is
      relied on.
    """
    if len(channels) > MAX_FEED_CHANNELS:
        msg = f"at most {MAX_FEED_CHANNELS} channels per feed read"
        raise ValueError(msg)
    if limit <= 0:
        return None, {}
    params: dict[str, Any] = {"organization_id": organization_id}
    thread_clause = ""
    if thread is not None:
        thread_clause = " AND thread = $thread"
        params["thread"] = thread
    unique_channels = list(dict.fromkeys(channels))
    sources: list[str] = []
    if unique_channels:
        index = (
            "net_message_org_channel_thread_ns"
            if thread is not None
            else "net_message_org_channel_ns"
        )
        for number, channel in enumerate(unique_channels):
            params[f"channel_{number}"] = channel
            sources.append(
                f"FROM net_messages WITH INDEX {index} WHERE organization_id = $organization_id "
                f"AND channel = $channel_{number}{thread_clause}"
            )
    else:
        index = "net_message_org_thread_ns" if thread is not None else "net_message_org_ns"
        sources.append(
            f"FROM net_messages WITH INDEX {index} "
            f"WHERE organization_id = $organization_id{thread_clause}"
        )
    if since is None:
        params["limit"] = limit
        newest = "ORDER BY created_ns DESC, uuid DESC LIMIT $limit"
        seeks = [f"(SELECT {_MESSAGE_FIELDS} {scope} AND {_SETTLED} {newest})" for scope in sources]
        if len(seeks) == 1:
            return f"RETURN {seeks[0]};", params
        return (
            "RETURN { "
            f"LET $rows = array::flatten([{', '.join(seeks)}]); "
            f"RETURN (SELECT * FROM $rows {newest}); "
            "};",
            params,
        )
    params.update(since_ns=since.created_ns, since_uuid=since.uuid, limit=limit + 1)
    lets: list[str] = []
    seeks = []
    order = "ORDER BY created_ns ASC, uuid ASC"
    for number, scope in enumerate(sources):
        lets.append(
            f"LET $take_{number} = $limit + "
            f"count((SELECT VALUE uuid {scope} AND created_ns = $since_ns));"
        )
        seeks.append(
            f"(SELECT {_MESSAGE_FIELDS} {scope} AND created_ns = $since_ns "
            f"AND uuid > $since_uuid {order} LIMIT $limit)"
        )
        seeks.append(
            f"(SELECT {_MESSAGE_FIELDS} {scope} AND created_ns > $since_ns "
            f"{order} LIMIT $take_{number})"
        )
    return (
        "RETURN { "
        f"{' '.join(lets)} "
        f"LET $rows = array::flatten([{', '.join(seeks)}]); "
        f"RETURN (SELECT * FROM $rows WHERE {_SETTLED} {order} LIMIT $limit); "
        "};",
        params,
    )


async def list_net_channels(*, organization_id: str) -> list[NetChannelSummary]:
    """Channels with any message, most recently active first."""
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, CHANNEL_SUMMARY_STATEMENT, organization_id=organization_id
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
            client, CHANNEL_THREADS_STATEMENT, organization_id=organization_id, channel=channel
        )
    return [_thread_from_row(row) for row in rows]


async def idle_net_threads(*, idle_minutes: int, limit: int) -> list[NetThreadState]:
    """Threads across every organization that went quiet with undistilled messages.

    Quiet means no message for ``idle_minutes``. A thread past its retry
    budget, or claimed by a run that is still inside its lease, is left out.
    """
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, idle_threads_statement(idle_minutes=idle_minutes), limit=max(int(limit), 1)
        )
    return [_thread_from_row(row) for row in rows]


def idle_threads_statement(*, idle_minutes: int) -> str:
    """The distillation selection, bound by ``$limit``; exposed so it can be explained."""
    idle = max(int(idle_minutes), 0)
    return (
        f"SELECT {_THREAD_FIELDS} FROM net_threads "
        f"WHERE last_message_at < time::now() - {idle}m AND {_IDLE_THREAD_CONDITIONS} "
        "ORDER BY last_message_at ASC LIMIT $limit;"
    )


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

    Starts after ``thread.reflected_mark``, the watermark as the thread was
    read when claimed; only the claim holder moves it. Capped at
    ``DISTILL_WINDOW_MESSAGES`` messages and ``DISTILL_WINDOW_CHARS``
    characters of body, keeping the oldest so the next pass continues where
    this one stopped. The first message always fits: a body is far smaller
    than the character cap.
    """
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            _THREAD_WINDOW,
            limit=DISTILL_WINDOW_MESSAGES,
            **_thread_key(thread),
            **_mark_params(thread.reflected_mark),
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
    through: NetCursor | None,
    source_id: str | None = None,
    skipped: Sequence[str] = (),
) -> None:
    """Record a finished distillation and release the claim.

    ``thread`` is the state the pass claimed. The watermark moves to
    ``through``, the last distilled message's cursor, never backwards, with
    ``reflected_through`` that message's display time; with no message (an
    empty window) it stays put. The pending mark clears only when no message
    sits after the watermark: a post that lands while the thread is being
    reflected keeps it pending, and so does anything that moved the thread
    since it was claimed, until a pass that sees the current state clears it.
    The retry budget resets, any earlier error clears, and ``skipped`` records
    the authors whose lines this pass could not reflect, and why.
    """
    expected = thread.reflected_mark
    mark = expected if through is None else _later_position(expected, through)
    key = _thread_key(thread)
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client, _PENDING_AFTER_MARK, **key, **_mark_params(mark)
        )
        pending = rows[0] if rows else {}
        counted = {
            "seen": _as_int(pending.get("seen")),
            "later": _as_int(pending.get("later")),
            "expected_ns": expected.created_ns if expected is not None else -1,
            "expected_uuid": expected.uuid if expected is not None else "",
        }
        if through is None:
            await content_client.select_many(client, _SETTLE_EMPTY_WINDOW, **key, **counted)
            return
        await content_client.select_many(
            client,
            _SETTLE_THROUGH_MESSAGE,
            through_ns=through.created_ns,
            through_uuid=through.uuid,
            source_id=source_id,
            skipped=list(skipped),
            **key,
            **counted,
        )


def _later_position(first: NetCursor | None, second: NetCursor) -> NetCursor:
    if first is None:
        return second
    return max(first, second, key=lambda cursor: (cursor.created_ns, cursor.uuid))


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


def author_windows(messages: Sequence[NetMessageRecord]) -> list[NetAuthorWindow]:
    """Split a window by author and credential, in order of each one's first message."""
    grouped: dict[tuple[str, str | None], list[NetMessageRecord]] = {}
    for message in messages:
        grouped.setdefault((message.author_user_id, message.api_key_id), []).append(message)
    return [
        NetAuthorWindow(author_user_id=author, api_key_id=api_key_id, messages=tuple(rows))
        for (author, api_key_id), rows in grouped.items()
    ]


def render_net_transcript(
    thread: NetThreadState,
    window: NetAuthorWindow,
    *,
    author_name: str | None = None,
) -> str:
    """One author's lines as reflection reads them: a header, then one entry per message.

    The header and every entry name the verified author; the agent label in
    parentheses is what their client claimed. Each entry starts
    ``[<iso>] <author> (<agent>): `` and every further line of its body is
    indented, so no body can open a line that reads as someone else's entry.
    """
    # A display name is free text; one with a line break could open a line
    # of its own, so it renders on one line like every other label here.
    author = _single_line(author_name or window.author_user_id)
    header = f"# sibylnet channel {_single_line(thread.channel)}"
    if thread.thread:
        header += f", thread {_single_line(thread.thread)}"
    lines = [header, f"# messages by {author} ({window.author_user_id})", ""]
    for message in window.messages:
        agent = _single_line(message.author_agent) if message.author_agent else ""
        label = author + (f" ({agent})" if agent else "")
        stamp = message.created_at.isoformat().replace("+00:00", "Z")
        first, *rest = message.body.splitlines() or [""]
        lines.append(f"[{stamp}] {label}: {first}")
        lines.extend(f"{TRANSCRIPT_CONTINUATION}{line}" for line in rest)
    return "\n".join(lines)


def _thread_key(thread: NetThreadState) -> dict[str, str]:
    return {
        "organization_id": thread.organization_id,
        "channel": thread.channel,
        "thread": thread.thread,
    }


def _mark_params(mark: NetCursor | None) -> dict[str, object]:
    # Before any distillation the window starts at the thread's beginning:
    # every created_ns is at least 0.
    return {
        "mark_ns": mark.created_ns if mark is not None else -1,
        "mark_uuid": mark.uuid if mark is not None else "",
    }


async def _settled_end(client: Any) -> NetCursor:
    """A cursor at the settled end of the feed by the database clock, between messages."""
    rows = await content_client.select_many(client, _SETTLED_END)
    nanos = rows[0].get("settled_end") if rows else None
    if not isinstance(nanos, int) or isinstance(nanos, bool):
        msg = "net feed could not read the settled end"
        raise RuntimeError(msg)
    return NetCursor(created_ns=nanos, uuid="")


def _message_from_row(row: Mapping[str, object]) -> NetMessageRecord:
    message_id = str(row.get("uuid") or "")
    created_ns = row.get("created_ns")
    created_at = _as_utc(row.get("created_at"))
    if (
        not message_id
        or created_at is None
        or not isinstance(created_ns, int)
        or isinstance(created_ns, bool)
    ):
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
        cursor=NetCursor(created_ns=created_ns, uuid=message_id),
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
        reflection_skipped=_string_tuple(row.get("reflection_skipped")),
        reflected_mark=_mark_from_row(row),
    )


def _mark_from_row(row: Mapping[str, object]) -> NetCursor | None:
    created_ns = row.get("reflected_through_ns")
    if created_ns is None:
        return None
    return NetCursor(
        created_ns=_as_int(created_ns), uuid=str(row.get("reflected_through_uuid") or "")
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


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(entry) for entry in value if entry)


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


__all__ = [
    "CHANNEL_SUMMARY_STATEMENT",
    "CHANNEL_THREADS_STATEMENT",
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
    "PROJECT_PINNED_MARKER",
    "SETTLE_SECONDS",
    "TRANSCRIPT_CONTINUATION",
    "NetAuthorWindow",
    "NetChannelSummary",
    "NetCursor",
    "NetFeedPage",
    "NetMessageKind",
    "NetMessageRecord",
    "NetThreadProjectPinnedError",
    "NetThreadState",
    "author_windows",
    "claim_net_thread",
    "fail_net_thread",
    "feed_statement",
    "idle_net_threads",
    "idle_threads_statement",
    "list_net_channels",
    "list_net_threads",
    "net_source_title",
    "net_thread_project",
    "net_thread_window",
    "new_message_id",
    "normalize_body",
    "normalize_channel",
    "normalize_label",
    "normalize_thread",
    "post_net_message",
    "read_net_feed",
    "render_net_transcript",
    "settle_net_thread",
]
