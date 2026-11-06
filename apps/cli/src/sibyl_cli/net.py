"""sibylnet: channels coding agents use to talk to each other.

Channels are org-visible, like public chat rooms: every member of the
organization who can read reads every channel, and anyone with a write role
posts. ``@name`` is the convention for one agent's inbox and ``#name`` for a
shared room. A message posted with ``--decision`` is also remembered as a
decision memory, and idle threads are distilled into memory by the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
import webbrowser
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

import typer
from rich.markup import escape

from sibyl_cli.client import SibylClient, SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    ERROR_RED,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    err_console,
    error,
    handle_client_error,
    info,
    print_json,
    resolve_content_input,
    run_async,
    success,
    truncate,
)
from sibyl_cli.config_store import config_dir, resolve_project_from_cwd
from sibyl_cli.project_refs import resolve_project_reference
from sibyl_cli.web_links import net_url

app = typer.Typer(
    name="net",
    help="Agent channels: post, read, and watch messages between agents",
    no_args_is_help=True,
)

MAX_WAIT_SECONDS = 25
# The feed holds a long poll open for up to --wait seconds before answering,
# so the request timeout has to outlast it by enough to cover the query.
LONG_POLL_MARGIN_SECONDS = 15
WATCH_LINE_CHARS = 200

# Markers a coding harness exports to the commands it runs, checked in order,
# each paired with the variable that carries that harness's session id. Codex
# goes first because `codex exec` launched from a Claude Code session inherits
# CLAUDECODE, while CODEX_THREAD_ID is only ever set by Codex itself.
HARNESS_ENV = (
    ("codex", "CODEX_THREAD_ID", "CODEX_THREAD_ID"),
    ("claude-code", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"),
)

_MARK_PATTERN = re.compile(r"[a-z0-9_-]{1,32}")
CHANNEL_HELP = "Channel: '@ada' for one agent's inbox, '#reviews' for a shared room"
THREAD_HELP = "Thread key, such as gh:acme/app#42, a task id, or a slug"


def _env(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _harness() -> tuple[str, str | None] | None:
    for label, marker, session_var in HARNESS_ENV:
        if _env(marker):
            return label, _env(session_var)
    return None


def detect_agent() -> str | None:
    """Label the caller: $SIBYL_AGENT, else the harness running this command."""
    if agent := _env("SIBYL_AGENT"):
        return agent
    harness = _harness()
    return harness[0] if harness else None


def detect_session() -> str | None:
    """Session id: $SIBYL_SESSION, else the running harness's own session id."""
    if session := _env("SIBYL_SESSION"):
        return session
    harness = _harness()
    return harness[1] if harness else None


class ReaderLabels(TypedDict, total=False):
    agent: str
    session: str


def reader_labels() -> ReaderLabels:
    """The agent label and session a feed read carries into presence, when there are any."""
    labels = ReaderLabels()
    if agent := detect_agent():
        labels["agent"] = agent
    if session := detect_session():
        labels["session"] = session
    return labels


def normalize_channels(channels: Sequence[str] | None) -> list[str]:
    """Lowercase, strip, and dedupe channel names the way the server stores them."""
    seen: dict[str, None] = {}
    for channel in channels or ():
        if name := channel.strip().lower():
            seen.setdefault(name)
    return list(seen)


class WatchCursor:
    """Where a watch resumes, saved per context, server, channel set, and thread.

    The server is part of the key because a context can be repointed, and a
    cursor is only meaningful to the server that issued it.
    """

    def __init__(
        self,
        *,
        context: str | None,
        server: str,
        channels: Sequence[str],
        thread: str | None,
        kind: str = "cursors",
    ) -> None:
        self.key = {
            "context": context or "",
            "server": server,
            "channels": sorted(channels),
            "thread": thread or "",
        }
        digest = hashlib.sha256(json.dumps(self.key, sort_keys=True).encode()).hexdigest()
        # ``cursors`` are where watches resume; ``read-marks`` are where
        # ``net read --new`` last looked, so neither moves the other.
        self.path = config_dir() / "net" / kind / f"{digest[:24]}.json"

    def load(self) -> str | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        cursor = data.get("cursor") if isinstance(data, dict) else None
        return cursor if isinstance(cursor, str) and cursor else None

    def save(self, cursor: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({**self.key, "cursor": cursor}, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".cursor_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _short_time(value: object) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return str(value or "")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


def _strip_controls(value: object) -> str:
    """Drop terminal control characters, keeping newlines and tabs.

    Another agent wrote this text. Rich escapes only its own markup, so an
    ANSI or OSC sequence would otherwise reach the reader's terminal.
    """
    return "".join(
        char for char in str(value or "") if char in "\n\t" or unicodedata.category(char) != "Cc"
    )


def _one_line(value: object) -> str:
    """A label on one line: control characters dropped, line and paragraph breaks and tabs as spaces.

    Names are free text a member sets on their own profile. One with a line
    break would print a second line shaped like another author's message.
    """
    return "".join(
        " " if char in "\n\t" or unicodedata.category(char) in {"Zl", "Zp"} else char
        for char in _strip_controls(value)
    )


def _author(message: Mapping[str, Any]) -> tuple[str, str | None]:
    author = message.get("author")
    author = author if isinstance(author, Mapping) else {}
    name = author.get("name") or author.get("email") or author.get("user_id") or "unknown"
    agent = author.get("agent")
    return _one_line(name), _one_line(agent) if agent else None


def _who(message: Mapping[str, Any]) -> str:
    name, agent = _author(message)
    who = f"[{NEON_CYAN}]{escape(name)}[/{NEON_CYAN}]"
    return f"{who}[dim]({escape(agent)})[/dim]" if agent else who


def _where(message: Mapping[str, Any]) -> list[str]:
    parts = [f"[bold {ELECTRIC_PURPLE}]{escape(_one_line(message.get('channel')))}[/]"]
    if thread := message.get("thread"):
        parts.append(f"[{CORAL}]{escape(_one_line(thread))}[/{CORAL}]")
    return parts


def _first_line(body: object) -> str:
    text = _strip_controls(body)
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return truncate(line, WATCH_LINE_CHARS)


def watch_line(message: Mapping[str, Any]) -> str:
    """`<channel> <thread?> <author>(<agent>): <first line>` as Rich markup."""
    marker = (
        f"[{ELECTRIC_YELLOW}]◆[/{ELECTRIC_YELLOW}] " if message.get("kind") == "decision" else ""
    )
    head = " ".join([*_where(message), f"{_who(message)}:"])
    return f"{head} {marker}{escape(_first_line(message.get('body')))}"


def _print(markup: str) -> None:
    # Message text is untrusted: no emoji codes, no number/URL highlighting,
    # and no hard wrap, so one message stays one line when piped.
    console.print(markup, emoji=False, highlight=False, soft_wrap=True)


def message_url(server: str, message: Mapping[str, Any]) -> str:
    """The web Net page on a message's channel and thread."""
    thread = message.get("thread")
    return net_url(server, str(message.get("channel") or ""), str(thread) if thread else None)


def _link(url: str) -> str:
    return f"[dim]{escape(url)}[/dim]"


def _emit_watch(message: Mapping[str, Any], *, json_output: bool, server: str) -> None:
    url = message_url(server, message)
    if json_output:
        line = json.dumps({**message, "web_url": url}, ensure_ascii=False, default=str)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    _print(f"{watch_line(message)} [dim]·[/dim] {_link(url)}")


def _print_message(message: Mapping[str, Any]) -> None:
    header = [f"[dim]{escape(_short_time(message.get('created_at')))}[/dim]", *_where(message)]
    header.append(_who(message))
    if message.get("kind") == "decision":
        decision = f"[{ELECTRIC_YELLOW}]◆ decision[/{ELECTRIC_YELLOW}]"
        if memory_id := message.get("decision_memory_id"):
            decision += f" [dim]{escape(str(memory_id))}[/dim]"
        header.append(decision)
    _print("  ".join(header))
    for line in _strip_controls(message.get("body")).splitlines() or [""]:
        console.print(f"  {escape(line)}", emoji=False, highlight=False)


def _distilled(row: Mapping[str, Any]) -> str:
    if row.get("reflection_error"):
        return f"[{ERROR_RED}]failed[/{ERROR_RED}]"
    through = _parse_time(row.get("reflected_through"))
    if through is None:
        return "[dim]pending[/dim]"
    last = _parse_time(row.get("last_message_at"))
    try:
        caught_up = last is None or through >= last
    except TypeError:
        caught_up = False
    if caught_up:
        return f"[{SUCCESS_GREEN}]distilled[/{SUCCESS_GREEN}]"
    return f"[{ELECTRIC_YELLOW}]through {_short_time(row.get('reflected_through'))}[/]"


def _rows(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rows = data.get(key)
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("post")
def post_message(
    channel: Annotated[str, typer.Argument(help=CHANNEL_HELP)],
    body: Annotated[
        str | None,
        typer.Argument(help="Message body. Reads stdin when omitted or '-'."),
    ] = None,
    thread: Annotated[str | None, typer.Option("--thread", "-t", help=THREAD_HELP)] = None,
    decision: Annotated[
        bool,
        typer.Option("--decision", help="Also remember the message as a decision memory"),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help=(
                "Project ID or name. A plain message names one only when given; a decision "
                "defaults to the linked project"
            ),
        ),
    ] = None,
    all_projects: Annotated[
        bool,
        typer.Option("--all-projects", help="Do not scope a decision to the linked project"),
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent", help="Agent label. Default: $SIBYL_AGENT, else the detected harness"
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(
            "--session", help="Session id. Default: $SIBYL_SESSION, else the harness session"
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """Post a message to a channel.

    Quote channel names: an unquoted #name is a comment in most shells.

    Examples:
        sibyl net post '@ada' "PR is green and ready for review"
        sibyl net post '#reviews' -t gh:acme/app#42 "LGTM once CI passes"
        sibyl net post '#reviews' --decision "Retries stay at the client"
        git log -1 --format=%B | sibyl net post '#reviews'
    """
    text = (resolve_content_input(body) or "").strip()
    if not text:
        error("Provide a message body as an argument or on stdin.")
        raise typer.Exit(1)

    # Only a decision is remembered, so only a decision defaults to the linked
    # project, like remember. A plain message names a project when asked to:
    # naming one takes contributor access and pins the thread it starts.
    effective_project = project
    if decision and project is None and not all_projects:
        effective_project = resolve_project_from_cwd()
    agent_label = agent or detect_agent()
    session_id = session or detect_session()

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                project_id = (
                    await resolve_project_reference(client, effective_project)
                    if effective_project
                    else None
                )
                data = await client.post_net_message(
                    channel=channel,
                    body=text,
                    thread=thread,
                    decision=decision,
                    project_id=project_id,
                    agent=agent_label,
                    session=session_id,
                )
                server = client.base_url
            message = data.get("message")
            message = message if isinstance(message, Mapping) else {}
            url = message_url(server, {"channel": channel, **message})
            if json_output:
                print_json({**data, "web_url": url})
                return
            where = escape(str(message.get("channel") or channel))
            if posted_thread := message.get("thread"):
                where += f" {escape(str(posted_thread))}"
            line = f"Posted {escape(str(message.get('id') or 'unknown'))} to {where}"
            if memory_id := data.get("decision_memory_id") or message.get("decision_memory_id"):
                line += f" · decision memory {escape(str(memory_id))}"
            success(line)
            _print(f"  {_link(url)}")
        except SibylClientError as e:
            handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(1) from e

    _run()


@app.command("read")
def read_messages(
    channel: Annotated[
        str | None, typer.Argument(help="Channel to read. Default: every channel")
    ] = None,
    thread: Annotated[str | None, typer.Option("--thread", "-t", help=THREAD_HELP)] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-n",
            min=0,
            max=500,
            help="How many of the newest to show; 0 with --json returns just the end cursor",
        ),
    ] = 20,
    new: Annotated[
        bool,
        typer.Option(
            "--new",
            help=(
                "Only messages since the last --new read of the same channel and thread, "
                "then remember where this one stopped; the first one starts from now"
            ),
        ),
    ] = False,
    mark: Annotated[
        str,
        typer.Option(
            "--mark",
            help="Name of the place --new remembers; different names keep separate places",
        ),
    ] = "read",
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """Show a channel's newest messages, oldest first.

    '@me' is your own inbox. Start work by checking it: sibyl net read @me --new

    Examples:
        sibyl net read @me
        sibyl net read '#reviews'
        sibyl net read '#reviews' -t gh:acme/app#42 -n 50
    """
    channels = normalize_channels([channel] if channel else None)
    mark_name = mark.strip().lower()
    if new and not _MARK_PATTERN.fullmatch(mark_name):
        error("--mark is 1-32 characters of a-z, 0-9, '-' or '_'")
        raise typer.Exit(1)

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                server = client.base_url
                place = (
                    WatchCursor(
                        context=client.context_name,
                        server=server,
                        channels=channels,
                        thread=thread,
                        kind=f"read-marks/{mark_name}",
                    )
                    if new
                    else None
                )
                data = await _read_page(client, channels, thread, limit, place)
            messages = _rows(data, "messages")
            shown = str(messages[0].get("channel")) if messages and channel else channel
            url = net_url(server, shown, thread)
            if json_output:
                print_json({**data, "web_url": url})
            elif not messages:
                info("No new messages" if new else "No messages yet")
            else:
                for index, message in enumerate(messages):
                    if index:
                        console.print()
                    _print_message(message)
                console.print()
                _print(f"  {_link(url)}")
            next_since = data.get("next_since")
            if place is not None and isinstance(next_since, str) and next_since:
                # Saved only after printing, so a crash shows the batch again.
                place.save(next_since)
        except SibylClientError as e:
            handle_client_error(e)

    _run()


async def _read_page(
    client: SibylClient,
    channels: Sequence[str],
    thread: str | None,
    limit: int,
    mark: WatchCursor | None,
) -> dict[str, Any]:
    """The newest ``limit`` messages, or with a mark the ones after it (none on first use)."""
    if mark is None:
        return await client.net_feed(
            channels=channels, thread=thread, limit=limit, **reader_labels()
        )
    since = mark.load()
    if since is None:
        page = await client.net_feed(channels=channels, thread=thread, limit=0, **reader_labels())
        return {**page, "messages": []}
    try:
        return await client.net_feed(
            since=since, channels=channels, thread=thread, limit=limit, **reader_labels()
        )
    except SibylClientError as exc:
        # A mark the server no longer accepts starts over from now.
        if exc.status_code != 422:
            raise
        page = await client.net_feed(channels=channels, thread=thread, limit=0, **reader_labels())
        return {**page, "messages": []}


async def _end_cursor(
    client: SibylClient, channels: Sequence[str], thread: str | None
) -> str | None:
    """The cursor at the current end of the feed, so a new watch starts from now."""
    page = await client.net_feed(channels=channels, thread=thread, limit=0, **reader_labels())
    cursor = page.get("next_since")
    return cursor if isinstance(cursor, str) and cursor else None


async def watch_feed(
    client: SibylClient,
    *,
    channels: Sequence[str],
    thread: str | None,
    since: str | None,
    once: bool,
    wait: int,
    json_output: bool,
) -> None:
    """Long-poll the feed, print each batch, then save where it ended.

    An explicit ``since`` is the caller's cursor to manage, so it never touches
    the saved one. The saved cursor is written only after a batch is printed,
    which makes delivery at-least-once: a crash in between replays the batch.
    """
    store = (
        None
        if since
        else WatchCursor(
            context=client.context_name,
            server=client.base_url,
            channels=channels,
            thread=thread,
        )
    )
    cursor = since or (store.load() if store else None)
    resumed = store is not None and cursor is not None
    if store is not None and cursor is None:
        cursor = await _end_cursor(client, channels, thread)
        if cursor:
            store.save(cursor)

    while True:
        try:
            page = await client.net_feed(
                since=cursor, channels=channels, thread=thread, wait=wait, **reader_labels()
            )
        except SibylClientError as exc:
            # A saved cursor the server no longer accepts would wedge every
            # later watch on the same 422, so start over from now instead.
            if store is None or not resumed or exc.status_code != 422:
                raise
            err_console.print(
                f"[{ELECTRIC_YELLOW}]![/{ELECTRIC_YELLOW}] "
                "The saved watch cursor was rejected; watching from now."
            )
            resumed = False
            cursor = await _end_cursor(client, channels, thread)
            if cursor:
                store.save(cursor)
            continue
        resumed = False

        messages = _rows(page, "messages")
        for message in messages:
            _emit_watch(message, json_output=json_output, server=client.base_url)

        next_since = page.get("next_since")
        if isinstance(next_since, str) and next_since and next_since != cursor:
            cursor = next_since
            if store is not None:
                store.save(cursor)
        if once:
            return
        if not messages and not wait:
            await asyncio.sleep(1)


@app.command("watch")
def watch_messages(
    channels: Annotated[
        list[str] | None,
        typer.Option(
            "--channel", "-c", help="Channel to watch; repeat for several. Default: every channel"
        ),
    ] = None,
    thread: Annotated[str | None, typer.Option("--thread", "-t", help=THREAD_HELP)] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Start after this cursor (a message's `cursor`); the saved cursor is left alone",
        ),
    ] = None,
    once: Annotated[
        bool,
        typer.Option("--once", help="Exit 0 after the first batch, or when the wait elapses"),
    ] = False,
    wait: Annotated[
        int,
        typer.Option(
            "--wait",
            min=0,
            max=MAX_WAIT_SECONDS,
            help="Seconds each poll waits on the server for a message",
        ),
    ] = MAX_WAIT_SECONDS,
    json_output: Annotated[
        bool, typer.Option("--json", "-j", help="Print one NetMessage JSON object per line")
    ] = False,
) -> None:
    """Long-poll channels and print one line per message.

    Without --since, a watch resumes where the last watch of the same channels
    and thread left off in this context, and the first one starts from now.

    Examples:
        sibyl net watch -c '@ada'
        sibyl net watch -c '#reviews' -t gh:acme/app#42
        sibyl net watch -c '@ada' --once --json
    """
    selected = normalize_channels(channels)
    thread_key = (thread or "").strip() or None

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                client.timeout = max(float(client.timeout), wait + LONG_POLL_MARGIN_SECONDS)
                await watch_feed(
                    client,
                    channels=selected,
                    thread=thread_key,
                    since=since,
                    once=once,
                    wait=wait,
                    json_output=json_output,
                )
        except SibylClientError as e:
            handle_client_error(e)

    try:
        _run()
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


@app.command("channels")
def list_channels(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """List channels by most recent activity."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.list_net_channels()
            if json_output:
                print_json(data)
                return
            rows = _rows(data, "channels")
            if not rows:
                info("No channels yet. Start one: sibyl net post '#reviews' \"hello\"")
                return
            table = create_table(None, "Channel", "Messages", "Threads", "Last activity")
            for row in rows:
                table.add_row(
                    escape(str(row.get("channel") or "")),
                    str(row.get("message_count") or 0),
                    str(row.get("thread_count") or 0),
                    escape(_short_time(row.get("last_message_at"))),
                )
            console.print(table)
        except SibylClientError as e:
            handle_client_error(e)

    _run()


def _seen_ago(value: object) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return str(value or "")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = max(int((datetime.now(UTC) - parsed).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _member(row: Mapping[str, Any]) -> str:
    """``name(agent)`` for a presence row, as Rich markup."""
    name = _one_line(row.get("name") or row.get("user_id") or "unknown")
    who = f"[{NEON_CYAN}]{escape(name)}[/{NEON_CYAN}]"
    agent = row.get("agent")
    return f"{who}[dim]({escape(_one_line(agent))})[/dim]" if agent else who


@app.command("who")
def who(
    channel: Annotated[
        str | None,
        typer.Argument(help="Channel to ask about. Default: everyone in the organization"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """Find people and see who is listening.

    Without a channel: every member, the inbox that reaches their agents
    (@<handle>), how many of their agent sessions are listening, and when
    they last posted. With a channel: who is watching it and who posted there
    in the last hour. Listening is derived from use: any feed read lists its
    reader for two minutes after it ends.

    Examples:
        sibyl net who
        sibyl net who '@ada'
        sibyl net who --json
    """
    if channel is None:
        _directory(json_output=json_output)
        return

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.net_presence(channel.strip().lower())
            if json_output:
                print_json(data)
                return
            watching = _rows(data, "watching")
            recent = _rows(data, "recent")
            where = escape(channel)
            if not watching and not recent:
                info(f"Nobody has watched or posted in {where} lately")
                return
            if watching:
                table = create_table("Watching", "Channel", "Who", "Session", "Last read")
                for row in watching:
                    table.add_row(
                        escape(_one_line(row.get("channel") or "")) or "[dim](every channel)[/dim]",
                        _member(row),
                        escape(_one_line(row.get("session") or "")),
                        escape(_seen_ago(row.get("last_seen"))),
                    )
                console.print(table)
            else:
                info(f"Nobody is watching {where} right now")
            if recent:
                table = create_table("Posted in the last hour", "Channel", "Who", "Last post")
                for row in recent:
                    table.add_row(
                        escape(_one_line(row.get("channel") or "")),
                        _member(row),
                        escape(_seen_ago(row.get("last_post_at"))),
                    )
                console.print(table)
        except SibylClientError as e:
            handle_client_error(e)

    _run()


def _directory(*, json_output: bool) -> None:
    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.net_directory()
            if json_output:
                print_json(data)
                return
            members = _rows(data, "members")
            if not members:
                info("No members found")
                return
            table = create_table(None, "Inbox", "Name", "Listening", "Last post")
            for row in members:
                listening = int(row.get("listening") or 0)
                table.add_row(
                    escape(_one_line(row.get("inbox") or "")),
                    escape(_one_line(row.get("name") or row.get("user_id") or "")),
                    f"[{SUCCESS_GREEN}]{listening}[/{SUCCESS_GREEN}]"
                    if listening
                    else "[dim]0[/dim]",
                    escape(_seen_ago(row.get("last_post_at"))) if row.get("last_post_at") else "",
                )
            console.print(table)
            info('Reach someone\'s agents: sibyl net post @<handle> "..."')
        except SibylClientError as e:
            handle_client_error(e)

    _run()


@app.command("open")
def open_net(
    channel: Annotated[
        str | None, typer.Argument(help="Channel to open, '@me' for your inbox")
    ] = None,
    thread: Annotated[str | None, typer.Option("--thread", "-t", help=THREAD_HELP)] = None,
) -> None:
    """Open the web Net page on a channel and thread, or print its link.

    Examples:
        sibyl net open
        sibyl net open @me
        sibyl net open '#reviews' -t gh:acme/app#42
    """
    wanted = normalize_channels([channel])[0] if channel else None

    @run_async
    async def _run() -> str:
        async with get_client() as client:
            resolved = wanted
            if wanted == "@me":
                me = await client.get("/auth/me")
                resolved = str(me.get("inbox") or wanted)
            return net_url(client.base_url, resolved, (thread or "").strip() or None)

    try:
        url = _run()
    except SibylClientError as e:
        handle_client_error(e)
        return
    # The link always prints, so a machine without a browser still gets it.
    sys.stdout.write(url + "\n")
    with contextlib.suppress(webbrowser.Error):
        webbrowser.open(url, new=2)


@app.command("threads")
def list_threads(
    channel: Annotated[str, typer.Argument(help=CHANNEL_HELP)],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output as JSON")] = False,
) -> None:
    """List a channel's threads and whether each is distilled into memory."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.list_net_threads(channel.strip().lower())
            if json_output:
                print_json(data)
                return
            rows = _rows(data, "threads")
            if not rows:
                info(f"No threads in {escape(channel)} yet")
                return
            table = create_table(None, "Thread", "Messages", "Project ID", "Last message", "Memory")
            for row in rows:
                thread = row.get("thread")
                table.add_row(
                    escape(str(thread)) if thread else "[dim](no thread)[/dim]",
                    str(row.get("message_count") or 0),
                    escape(str(row.get("project_id") or "")),
                    escape(_short_time(row.get("last_message_at"))),
                    _distilled(row),
                )
            console.print(table)
        except SibylClientError as e:
            handle_client_error(e)

    _run()
