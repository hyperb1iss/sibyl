"""Tests for `sibyl net`, the agent channel commands."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from sibyl_cli import net
from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.main import app

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
HARNESS_VARIABLES = (
    "SIBYL_AGENT",
    "SIBYL_SESSION",
    "CODEX_THREAD_ID",
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
)


@pytest.fixture(autouse=True)
def isolated_net_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    # These tests often run inside a coding harness, which exports the very
    # variables agent detection reads.
    for variable in HARNESS_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _message(
    message_id: str,
    body: str,
    *,
    channel: str = "#reviews",
    thread: str | None = "gh:acme/app#42",
    kind: str = "message",
    agent: str | None = "claude-code",
    decision_memory_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel": channel,
        "thread": thread,
        "kind": kind,
        "body": body,
        "project_id": None,
        "author": {"user_id": "user-1", "name": "ada", "agent": agent, "session": None},
        "api_key_id": None,
        "decision_memory_id": decision_memory_id,
        "created_at": "2026-11-02T10:15:00+00:00",
        "cursor": f"cursor-{message_id}",
    }


class FakeNetClient:
    def __init__(
        self,
        *,
        feed: Iterable[dict[str, Any] | Exception] = (),
        post: dict[str, Any] | Exception | None = None,
    ) -> None:
        self.context_name = "local"
        self.base_url = "http://example.test/api"
        self.timeout = 30.0
        self.net_feed = AsyncMock(side_effect=list(feed))
        self.post_net_message = AsyncMock(
            side_effect=[post] if isinstance(post, Exception) else None,
            return_value=post or {"message": _message("msg-1", "hello")},
        )
        self.list_net_channels = AsyncMock()
        self.list_net_threads = AsyncMock()
        self.net_presence = AsyncMock()
        self.net_directory = AsyncMock()
        self.get = AsyncMock()
        self.explore = AsyncMock(return_value={"entities": []})

    async def __aenter__(self) -> FakeNetClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


WEB = "http://example.test/net"


def _invoke(client: FakeNetClient, args: list[str], *, stdin: str | None = None) -> Any:
    with patch("sibyl_cli.net.get_client", return_value=client):
        return CliRunner().invoke(app, ["net", *args], input=stdin)


def _plain(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _cursor_file(channels: list[str], thread: str | None = None) -> Path:
    return net.WatchCursor(
        context="local",
        server="http://example.test/api",
        channels=net.normalize_channels(channels),
        thread=thread,
    ).path


# ---------------------------------------------------------------------------
# Client wire contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_net_message_sends_the_contract_body_unbuffered() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    request = AsyncMock(return_value={"message": {"id": "msg-1"}})
    client._request = request  # type: ignore[method-assign]

    await client.post_net_message(
        channel="#reviews",
        body="Retries stay at the client",
        thread="gh:acme/app#42",
        decision=True,
        project_id="project_abc",
        agent="codex",
        session="thread-9",
    )
    first_key = request.await_args.kwargs["_idempotency_key"]
    request.assert_awaited_once_with(
        "POST",
        "/net/messages",
        json={
            "channel": "#reviews",
            "body": "Retries stay at the client",
            "kind": "decision",
            "thread": "gh:acme/app#42",
            "project_id": "project_abc",
            "agent": "codex",
            "session": "thread-9",
        },
        _buffer_pending=False,
        _idempotency_key=first_key,
    )
    request.reset_mock()

    await client.post_net_message(channel="@ada", body="ping")
    second_key = request.await_args.kwargs["_idempotency_key"]
    request.assert_awaited_once_with(
        "POST",
        "/net/messages",
        json={"channel": "@ada", "body": "ping", "kind": "message"},
        _buffer_pending=False,
        _idempotency_key=second_key,
    )
    # Every post is its own operation: one key per call, never reused.
    assert first_key and second_key and first_key != second_key


@pytest.mark.asyncio
async def test_net_read_endpoints_send_contract_params() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    request = AsyncMock(return_value={})
    client._request = request  # type: ignore[method-assign]

    await client.net_feed(
        since="cursor-1", channels=["@ada", "#reviews"], thread="t", limit=5, wait=25
    )
    request.assert_awaited_once_with(
        "GET",
        "/net/feed",
        params={
            "since": "cursor-1",
            "channel": ["@ada", "#reviews"],
            "thread": "t",
            "limit": 5,
            "wait": 25,
        },
    )
    request.reset_mock()

    await client.net_feed(limit=0)
    request.assert_awaited_once_with("GET", "/net/feed", params={"limit": 0})
    request.reset_mock()

    await client.list_net_channels()
    request.assert_awaited_once_with("GET", "/net/channels")
    request.reset_mock()

    await client.list_net_threads("#reviews")
    request.assert_awaited_once_with("GET", "/net/threads", params={"channel": "#reviews"})
    request.reset_mock()

    await client.net_feed(since="cursor-1", wait=25, agent="dispatcher", session="s-1")
    request.assert_awaited_once_with(
        "GET",
        "/net/feed",
        params={"since": "cursor-1", "wait": 25, "agent": "dispatcher", "session": "s-1"},
    )
    request.reset_mock()

    await client.net_presence("@ada")
    request.assert_awaited_once_with("GET", "/net/presence", params={"channel": "@ada"})
    request.reset_mock()

    await client.net_presence()
    request.assert_awaited_once_with("GET", "/net/presence", params=None)


@pytest.mark.asyncio
async def test_net_feed_repeats_the_channel_query_parameter_on_the_wire() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [], "next_since": "c1", "more": False})

    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=httpx.MockTransport(handler)
    )
    async with client:
        page = await client.net_feed(since="c0", channels=["@ada", "#reviews"], wait=3)

    assert page["next_since"] == "c1"
    (request,) = seen
    assert request.url.path == "/api/net/feed"
    assert request.url.params.get_list("channel") == ["@ada", "#reviews"]
    assert request.url.params["since"] == "c0"
    assert request.url.params["wait"] == "3"


# ---------------------------------------------------------------------------
# Agent and session detection
# ---------------------------------------------------------------------------


def test_detection_is_empty_outside_any_harness() -> None:
    assert net.detect_agent() is None
    assert net.detect_session() is None


def test_sibyl_variables_outrank_the_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session")
    monkeypatch.setenv("SIBYL_AGENT", "dispatcher")
    monkeypatch.setenv("SIBYL_SESSION", "run-7")

    assert net.detect_agent() == "dispatcher"
    assert net.detect_session() == "run-7"


def test_claude_code_is_detected_with_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session")

    assert net.detect_agent() == "claude-code"
    assert net.detect_session() == "cc-session"


def test_codex_inside_claude_code_is_labelled_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread")

    assert net.detect_agent() == "codex"
    assert net.detect_session() == "codex-thread"


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value="project_abc")
def test_a_plain_post_names_no_project_and_carries_the_detected_agent(
    mock_resolve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session")
    client = FakeNetClient(post={"message": _message("msg-1", "LGTM once CI passes")})

    result = _invoke(client, ["post", "#reviews", "LGTM once CI passes", "-t", "gh:acme/app#42"])

    assert result.exit_code == 0, result.stdout
    client.post_net_message.assert_awaited_once_with(
        channel="#reviews",
        body="LGTM once CI passes",
        thread="gh:acme/app#42",
        decision=False,
        project_id=None,
        agent="claude-code",
        session="cc-session",
    )
    output = _plain(result.stdout)
    assert "Posted msg-1 to #reviews gh:acme/app#42" in output
    _posted, link = output.strip().splitlines()
    assert link.strip() == f"{WEB}?channel=%23reviews&thread=gh%3Aacme%2Fapp%2342"
    # A plain message never reads the linked project.
    mock_resolve.assert_not_called()


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value="project_abc")
def test_a_plain_post_names_a_project_only_when_asked(mock_resolve: Any) -> None:
    client = FakeNetClient()

    result = _invoke(client, ["post", "#reviews", "ping", "--project", "project_xyz"])

    assert result.exit_code == 0, result.stdout
    assert client.post_net_message.await_args.kwargs["project_id"] == "project_xyz"
    mock_resolve.assert_not_called()


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value="project_abc")
def test_a_decision_scopes_to_the_linked_project_like_remember(mock_resolve: Any) -> None:
    client = FakeNetClient()

    linked = _invoke(client, ["post", "@ada", "--decision", "Ship it"])
    unscoped = _invoke(client, ["post", "@ada", "--decision", "--all-projects", "Ship it"])
    chosen = _invoke(client, ["post", "@ada", "--decision", "-p", "project_xyz", "Ship it"])

    assert (linked.exit_code, unscoped.exit_code, chosen.exit_code) == (0, 0, 0)
    projects = [call.kwargs["project_id"] for call in client.post_net_message.await_args_list]
    assert projects == ["project_abc", None, "project_xyz"]
    mock_resolve.assert_called_once_with()


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value="project_abc")
def test_post_all_projects_skips_the_link_and_flags_override_env(
    mock_resolve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIBYL_AGENT", "from-env")
    client = FakeNetClient()

    result = _invoke(
        client,
        ["post", "@ada", "ping", "--all-projects", "--agent", "codex", "--session", "s-1"],
    )

    assert result.exit_code == 0, result.stdout
    kwargs = client.post_net_message.await_args.kwargs
    assert kwargs["project_id"] is None
    assert kwargs["agent"] == "codex"
    assert kwargs["session"] == "s-1"
    mock_resolve.assert_not_called()


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value=None)
def test_post_reads_the_body_from_stdin(_mock_resolve: Any) -> None:
    client = FakeNetClient()

    result = _invoke(client, ["post", "#reviews"], stdin="  from a pipe\nsecond line\n")

    assert result.exit_code == 0, result.stdout
    assert client.post_net_message.await_args.kwargs["body"] == "from a pipe\nsecond line"


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value=None)
def test_post_refuses_an_empty_body(_mock_resolve: Any) -> None:
    client = FakeNetClient()

    result = _invoke(client, ["post", "#reviews"], stdin="   \n")

    assert result.exit_code == 1
    assert "✗" in result.stdout
    client.post_net_message.assert_not_awaited()


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value=None)
def test_post_decision_prints_the_decision_memory_id(_mock_resolve: Any) -> None:
    posted = _message("msg-2", "Retries stay at the client", kind="decision")
    client = FakeNetClient(post={"message": posted, "decision_memory_id": "raw_mem_9"})

    result = _invoke(client, ["post", "#reviews", "--decision", "Retries stay at the client"])

    assert result.exit_code == 0, result.stdout
    assert client.post_net_message.await_args.kwargs["decision"] is True
    assert "decision memory raw_mem_9" in _plain(result.stdout)


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value=None)
def test_post_json_prints_the_server_response(_mock_resolve: Any) -> None:
    response = {"message": _message("msg-1", "hello"), "decision_memory_id": None}
    client = FakeNetClient(post=response)

    result = _invoke(client, ["post", "#reviews", "hello", "--json"])

    assert result.exit_code == 0, result.stdout
    # The server's response, plus the web page the message is on.
    assert json.loads(result.stdout) == {
        **response,
        "web_url": f"{WEB}?channel=%23reviews&thread=gh%3Aacme%2Fapp%2342",
    }


@patch("sibyl_cli.net.resolve_project_from_cwd", return_value=None)
def test_post_refusal_is_a_cross_line_and_nonzero_exit(_mock_resolve: Any) -> None:
    refusal = SibylClientError("API error", status_code=403, detail="Contributor role required")
    client = FakeNetClient(post=refusal)

    result = _invoke(client, ["post", "#reviews", "--decision", "Ship it"])

    assert result.exit_code == 1
    assert "✗ Access denied: Contributor role required" in _plain(result.stdout)


# ---------------------------------------------------------------------------
# read, channels, threads
# ---------------------------------------------------------------------------


def test_read_tails_one_channel() -> None:
    page = {
        "messages": [
            _message("m1", "first\nwith detail"),
            _message("m2", "Ship [it]", kind="decision", decision_memory_id="raw_mem_9"),
        ],
        "next_since": "cursor-m2",
        "more": False,
    }
    client = FakeNetClient(feed=[page])

    result = _invoke(client, ["read", "#Reviews", "-n", "5"])

    assert result.exit_code == 0, result.stdout
    client.net_feed.assert_awaited_once_with(channels=["#reviews"], thread=None, limit=5)
    output = _plain(result.stdout)
    assert "  with detail" in output
    assert "Ship [it]" in output
    assert "◆ decision raw_mem_9" in output
    assert output.index("first") < output.index("Ship [it]")


def test_read_json_prints_the_page() -> None:
    page = {"messages": [_message("m1", "hi")], "next_since": "cursor-m1", "more": False}
    client = FakeNetClient(feed=[page])

    result = _invoke(client, ["read", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {**page, "web_url": WEB}
    client.net_feed.assert_awaited_once_with(channels=[], thread=None, limit=20)


def test_read_limit_zero_returns_the_end_cursor() -> None:
    # A dispatcher starting from now asks for no rows and keeps the cursor.
    page = {"messages": [], "next_since": "cursor-end", "more": False}
    client = FakeNetClient(feed=[page])

    result = _invoke(client, ["read", "@ada", "--limit", "0", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {**page, "web_url": f"{WEB}?channel=%40ada"}
    client.net_feed.assert_awaited_once_with(channels=["@ada"], thread=None, limit=0)


def test_channels_and_threads_render_tables() -> None:
    client = FakeNetClient()
    client.list_net_channels.return_value = {
        "channels": [
            {
                "channel": "#reviews",
                "message_count": 12,
                "thread_count": 3,
                "last_message_at": "2026-11-02T10:15:00+00:00",
            }
        ]
    }
    client.list_net_threads.return_value = {
        "threads": [
            {
                "channel": "#reviews",
                "thread": "gh:acme/app#42",
                "project_id": "project_abc",
                "message_count": 4,
                "first_message_at": "2026-11-02T09:00:00+00:00",
                "last_message_at": "2026-11-02T10:15:00+00:00",
                "reflected_through": "2026-11-02T10:15:00+00:00",
                "reflection_source_id": "raw_src_1",
                "reflection_error": None,
            },
            {
                "channel": "#reviews",
                "thread": None,
                "project_id": None,
                "message_count": 1,
                "first_message_at": "2026-11-02T08:00:00+00:00",
                "last_message_at": "2026-11-02T08:00:00+00:00",
                "reflected_through": None,
                "reflection_source_id": None,
                "reflection_error": None,
            },
        ]
    }

    channels = _invoke(client, ["channels"])
    threads = _invoke(client, ["threads", "#Reviews"])
    threads_json = _invoke(client, ["threads", "#reviews", "--json"])

    assert channels.exit_code == 0, channels.stdout
    assert "#reviews" in channels.stdout
    assert threads.exit_code == 0, threads.stdout
    assert "gh:acme/app#42" in threads.stdout
    assert "distilled" in threads.stdout
    assert "pending" in threads.stdout
    assert "(no thread)" in threads.stdout
    client.list_net_threads.assert_awaited_with("#reviews")
    assert json.loads(threads_json.stdout) == client.list_net_threads.return_value


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def test_first_watch_starts_from_now_prints_lines_and_saves_the_cursor() -> None:
    batch = {
        "messages": [
            _message("m1", "PR is green\nmore detail"),
            _message("m2", "Retries stay", kind="decision", agent=None, thread=None),
        ],
        "next_since": "cursor-m2",
        "more": False,
    }
    client = FakeNetClient(feed=[{"messages": [], "next_since": "c0", "more": False}, batch])

    result = _invoke(client, ["watch", "-c", "#reviews", "--once"])

    assert result.exit_code == 0, result.stdout
    bootstrap, poll = client.net_feed.await_args_list
    assert bootstrap.kwargs == {"channels": ["#reviews"], "thread": None, "limit": 0}
    assert poll.kwargs == {"since": "c0", "channels": ["#reviews"], "thread": None, "wait": 25}
    assert _plain(result.stdout).splitlines() == [
        "#reviews gh:acme/app#42 ada(claude-code): PR is green · "
        f"{WEB}?channel=%23reviews&thread=gh%3Aacme%2Fapp%2342",
        f"#reviews ada: ◆ Retries stay · {WEB}?channel=%23reviews",
    ]
    saved = json.loads(_cursor_file(["#reviews"]).read_text())
    assert saved["cursor"] == "cursor-m2"
    assert saved["channels"] == ["#reviews"]
    assert client.timeout >= 25 + net.LONG_POLL_MARGIN_SECONDS


def test_watch_resumes_from_the_saved_cursor_for_the_same_channel_set() -> None:
    first = FakeNetClient(
        feed=[
            {"messages": [], "next_since": "c0", "more": False},
            {"messages": [_message("m1", "hi")], "next_since": "cursor-m1", "more": False},
        ]
    )
    assert _invoke(first, ["watch", "-c", "#reviews", "-c", "@ada", "--once"]).exit_code == 0

    second = FakeNetClient(feed=[{"messages": [], "next_since": "cursor-m1", "more": False}])
    result = _invoke(second, ["watch", "-c", "@ADA", "-c", "#reviews", "--once"])

    assert result.exit_code == 0, result.stdout
    (poll,) = second.net_feed.await_args_list
    assert poll.kwargs["since"] == "cursor-m1"
    assert poll.kwargs["channels"] == ["@ada", "#reviews"]


def test_watch_cursor_is_keyed_by_context_server_channels_and_thread() -> None:
    base = _cursor_file(["#reviews", "@ada"])

    assert _cursor_file(["@ADA", " #reviews", "@ada"]) == base
    assert _cursor_file(["#reviews"]) != base
    assert _cursor_file(["#reviews", "@ada"], thread="gh:acme/app#42") != base
    other_server = net.WatchCursor(
        context="local", server="http://other.test/api", channels=["#reviews", "@ada"], thread=None
    )
    assert other_server.path != base
    assert base.parent.parent.parent == Path.home() / ".sibyl"


def test_watch_once_exits_zero_when_the_wait_elapses_with_nothing() -> None:
    client = FakeNetClient(
        feed=[
            {"messages": [], "next_since": "c0", "more": False},
            {"messages": [], "next_since": "c0", "more": False},
        ]
    )

    result = _invoke(client, ["watch", "--once", "--wait", "2"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == ""
    assert client.net_feed.await_args_list[1].kwargs["wait"] == 2
    # The start-from-now cursor is kept, so the next run does not skip ahead.
    assert json.loads(_cursor_file([]).read_text())["cursor"] == "c0"


def test_watch_json_prints_one_message_per_line() -> None:
    messages = [_message("m1", "one"), _message("m2", "two\nlines")]
    client = FakeNetClient(
        feed=[
            {"messages": [], "next_since": "c0", "more": False},
            {"messages": messages, "next_since": "cursor-m2", "more": False},
        ]
    )

    result = _invoke(client, ["watch", "-c", "@ada", "--once", "--json"])

    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert [json.loads(line) for line in lines] == [
        {**message, "web_url": f"{WEB}?channel=%23reviews&thread=gh%3Aacme%2Fapp%2342"}
        for message in messages
    ]


def test_watch_saves_the_cursor_only_after_the_batch_is_printed() -> None:
    client = FakeNetClient(
        feed=[
            {"messages": [], "next_since": "c0", "more": False},
            {
                "messages": [_message("m1", "one"), _message("m2", "two")],
                "next_since": "cursor-m2",
                "more": False,
            },
        ]
    )
    printed: list[str] = []

    def emit(message: dict[str, Any], *, json_output: bool, server: str) -> None:
        if printed:
            raise BrokenPipeError
        printed.append(message["id"])

    with patch("sibyl_cli.net._emit_watch", side_effect=emit):
        result = _invoke(client, ["watch", "-c", "@ada", "--once"])

    assert result.exit_code != 0
    assert printed == ["m1"]
    assert json.loads(_cursor_file(["@ada"]).read_text())["cursor"] == "c0"


def test_watch_with_since_leaves_the_saved_cursor_alone() -> None:
    client = FakeNetClient(
        feed=[{"messages": [_message("m1", "hi")], "next_since": "cursor-m1", "more": False}]
    )

    result = _invoke(client, ["watch", "-c", "@ada", "--since", "explicit", "--once"])

    assert result.exit_code == 0, result.stdout
    (poll,) = client.net_feed.await_args_list
    assert poll.kwargs["since"] == "explicit"
    assert not _cursor_file(["@ada"]).exists()


def test_watch_restarts_from_now_when_the_saved_cursor_is_rejected() -> None:
    net.WatchCursor(
        context="local", server="http://example.test/api", channels=["@ada"], thread=None
    ).save("stale")
    client = FakeNetClient(
        feed=[
            SibylClientError("API error", status_code=422, detail="Invalid cursor"),
            {"messages": [], "next_since": "c9", "more": False},
            {"messages": [_message("m1", "hi")], "next_since": "cursor-m1", "more": False},
        ]
    )

    result = _invoke(client, ["watch", "-c", "@ada", "--once"])

    assert result.exit_code == 0, result.stdout
    calls = client.net_feed.await_args_list
    assert calls[0].kwargs["since"] == "stale"
    assert calls[1].kwargs["limit"] == 0
    assert calls[2].kwargs["since"] == "c9"
    assert "rejected" in result.stderr
    assert json.loads(_cursor_file(["@ada"]).read_text())["cursor"] == "cursor-m1"


def test_watch_explicit_since_rejection_is_an_error() -> None:
    client = FakeNetClient(
        feed=[SibylClientError("API error", status_code=422, detail="Invalid cursor")]
    )

    result = _invoke(client, ["watch", "--since", "garbage", "--once"])

    assert result.exit_code == 1
    assert "✗" in result.stdout
    assert client.net_feed.await_count == 1


def test_watch_rejects_a_wait_beyond_the_server_cap() -> None:
    result = _invoke(FakeNetClient(), ["watch", "--wait", "26"])

    assert result.exit_code == 2


def test_terminal_control_sequences_never_reach_the_terminal() -> None:
    message = _message("m1", "\x1b]0;pwned\x07hi \x1b[2Kthere\nsecond\tline")
    message["author"]["name"] = "Ada\x1b[31m"

    line = net.watch_line(message)

    assert "\x1b" not in line and "\x07" not in line
    assert "hi [2Kthere" in line
    assert "Ada[31m" in line


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_reads_and_watches_carry_the_agent_label_into_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIBYL_AGENT", "dispatcher")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-7")
    page = {"messages": [], "next_since": "c0", "more": False}
    client = FakeNetClient(feed=[page, page, page])

    assert _invoke(client, ["read", "@ada"]).exit_code == 0
    assert _invoke(client, ["watch", "-c", "@ada", "--once", "--wait", "1"]).exit_code == 0

    labels = {"agent": "dispatcher", "session": "session-7"}
    read, bootstrap, poll = client.net_feed.await_args_list
    for call in (read, bootstrap, poll):
        assert {key: call.kwargs.get(key) for key in labels} == labels


def _presence() -> dict[str, Any]:
    return {
        "watching": [
            {
                "channel": "@ada",
                "user_id": "user-ada",
                "name": "Ada",
                "agent": "claude-code",
                "session": "session-1",
                "last_seen": "2026-11-02T10:15:00+00:00",
            },
            {
                "channel": None,
                "user_id": "user-grace",
                "name": "Grace",
                "agent": None,
                "session": None,
                "last_seen": "2026-11-02T10:14:00+00:00",
            },
        ],
        "recent": [
            {
                "channel": "@ada",
                "user_id": "user-vic",
                "name": None,
                "agent": "codex",
                "last_post_at": "2026-11-02T10:00:00+00:00",
            }
        ],
    }


def test_who_shows_watchers_and_recent_posters() -> None:
    client = FakeNetClient()
    client.net_presence.return_value = _presence()

    result = _invoke(client, ["who", "@Ada"])

    assert result.exit_code == 0, result.stdout
    client.net_presence.assert_awaited_once_with("@ada")
    output = _plain(result.stdout)
    assert "Watching" in output
    assert "Ada(claude-code)" in output
    assert "session-1" in output
    assert "(every channel)" in output
    assert "Posted in the last hour" in output
    assert "user-vic(codex)" in output


def test_who_json_prints_the_server_response() -> None:
    client = FakeNetClient()
    client.net_presence.return_value = _presence()

    result = _invoke(client, ["who", "@ada", "--json"])

    assert result.exit_code == 0, result.stdout
    client.net_presence.assert_awaited_once_with("@ada")
    assert json.loads(result.stdout) == _presence()


def test_who_says_so_when_nobody_is_around() -> None:
    client = FakeNetClient()
    client.net_presence.return_value = {"watching": [], "recent": []}

    result = _invoke(client, ["who", "#reviews"])

    assert result.exit_code == 0, result.stdout
    assert "Nobody has watched or posted in #reviews lately" in _plain(result.stdout)

    client.net_presence.return_value = {"watching": [], "recent": _presence()["recent"]}
    result = _invoke(client, ["who", "@ada"])
    assert "Nobody is watching @ada right now" in _plain(result.stdout)


# ---------------------------------------------------------------------------
# Directory, web links, and what's new
# ---------------------------------------------------------------------------


def _directory() -> dict[str, Any]:
    return {
        "members": [
            {
                "user_id": "user-ada",
                "name": "Ada",
                "handle": "ada",
                "inbox": "@ada",
                "listening": 2,
                "last_post_at": "2026-11-02T10:15:00+00:00",
            },
            {
                "user_id": "user-grace",
                "name": "Grace",
                "handle": "grace",
                "inbox": "@grace",
                "listening": 0,
                "last_post_at": None,
            },
        ]
    }


def test_who_without_a_channel_is_the_directory() -> None:
    client = FakeNetClient()
    client.net_directory.return_value = _directory()

    shown = _invoke(client, ["who"])
    as_json = _invoke(client, ["who", "--json"])

    assert shown.exit_code == 0, shown.stdout
    output = _plain(shown.stdout)
    assert "@ada" in output and "@grace" in output and "Grace" in output
    assert "sibyl net post @<handle>" in output
    client.net_presence.assert_not_awaited()
    assert json.loads(as_json.stdout) == _directory()


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("https://sibyl.acme.test", "https://sibyl.acme.test/net"),
        ("https://sibyl.acme.test/api", "https://sibyl.acme.test/net"),
        ("http://localhost:3334/api", "http://localhost:3337/net"),
        ("http://127.0.0.1:3334", "http://127.0.0.1:3337/net"),
        ("http://localhost:8080/api", "http://localhost:8080/net"),
    ],
)
def test_web_links_follow_the_server_url(server: str, expected: str) -> None:
    from sibyl_cli.web_links import net_url

    assert net_url(server) == expected
    assert net_url(server, "#reviews", "gh:acme/app#42") == (
        f"{expected}?channel=%23reviews&thread=gh%3Aacme%2Fapp%2342"
    )


def test_open_resolves_me_and_prints_the_link(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(net.webbrowser, "open", lambda url, new=0: opened.append(url) or False)
    client = FakeNetClient()
    client.get.return_value = {"inbox": "@ada"}

    result = _invoke(client, ["open", "@me", "-t", "task:t-1"])

    assert result.exit_code == 0, result.stdout
    url = f"{WEB}?channel=%40ada&thread=task%3At-1"
    assert result.stdout.strip() == url
    assert opened == [url]
    client.get.assert_awaited_once_with("/auth/me")


def test_read_new_starts_from_now_then_shows_only_what_arrived() -> None:
    first = FakeNetClient(feed=[{"messages": [], "next_since": "c0", "more": False}])
    started = _invoke(first, ["read", "@me", "--new", "--json"])
    assert json.loads(started.stdout)["messages"] == []
    assert first.net_feed.await_args.kwargs == {"channels": ["@me"], "thread": None, "limit": 0}

    batch = {"messages": [_message("m1", "hi", channel="@ada")], "next_since": "c1", "more": False}
    second = FakeNetClient(feed=[batch, {"messages": [], "next_since": "c1", "more": False}])
    shown = _invoke(second, ["read", "@me", "--new", "--json"])
    assert [m["id"] for m in json.loads(shown.stdout)["messages"]] == ["m1"]
    assert json.loads(shown.stdout)["web_url"] == f"{WEB}?channel=%40ada"
    assert second.net_feed.await_args.kwargs["since"] == "c0"
    again = _invoke(second, ["read", "@me", "--new"])
    assert "No new messages" in _plain(again.stdout)
    assert second.net_feed.await_args.kwargs["since"] == "c1"

    # A differently named mark, like the session-start hook's, has its own place.
    hook = FakeNetClient(feed=[{"messages": [], "next_since": "c9", "more": False}])
    _invoke(hook, ["read", "@me", "--new", "--mark", "session-start", "--json"])
    assert hook.net_feed.await_args.kwargs == {"channels": ["@me"], "thread": None, "limit": 0}
    assert _invoke(hook, ["read", "@me", "--new", "--mark", "Bad Name"]).exit_code == 1
