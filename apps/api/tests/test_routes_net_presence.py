"""Derived presence over HTTP: feed reads list their readers, posts list their authors.

Nobody announces anything. A feed read, long poll or peek, lists its reader as
watching what it read for two minutes after its wait; ``GET /presence`` adds
who posted in the last hour. Runs on the embedded engine and, with
SIBYL_LIVE_SURREAL_TESTS=1, on a live server; the cross-replica case runs
against a live Redis when SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT
name one. The presence clock is the route module's, held still by a fixture,
so no case waits out a TTL.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from sibyl.api.routes import net as net_routes
from sibyl.coordination import presence as presence_module
from sibyl.coordination._local.presence import LocalPresenceRegistry
from sibyl.coordination._redis.presence import RedisPresenceRegistry, _keys
from sibyl.coordination.presence import WATCHING_TTL_SECONDS
from tests.test_coordination_presence import (
    live_redis_settings as live_redis_settings,  # noqa: PLC0414
)
from tests.test_routes_net import (
    client_for,
    feed,
    live_namespace as live_namespace,  # noqa: PLC0414
    org as org,  # noqa: PLC0414
    post,
)


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> LocalPresenceRegistry:
    """A registry of this test's own, so nothing another test noted is listed."""
    registry = LocalPresenceRegistry()
    monkeypatch.setattr(net_routes, "get_presence", lambda: registry)
    return registry


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The presence clock, held still and moved by hand."""
    now = [1_800_000_000.0]
    monkeypatch.setattr(net_routes, "time", SimpleNamespace(time=lambda: now[0]))
    return now


async def presence(org: SimpleNamespace, user: str, **params: object) -> dict[str, Any]:
    async with client_for(org, user) as client:
        response = await client.get("/api/net/presence", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def watchers(body: dict[str, Any]) -> set[tuple[object, ...]]:
    return {
        (row["channel"], row["user_id"], row["agent"], row["session"]) for row in body["watching"]
    }


async def test_a_long_poll_is_listed_while_it_waits_and_two_minutes_after(
    org: SimpleNamespace, clock: list[float]
) -> None:
    started = clock[0]
    poll = asyncio.create_task(
        feed(org, org.ada, channel="@ada", wait=2, agent="codex", session="thread-9")
    )
    try:
        for _ in range(100):
            body = await presence(org, org.grace, channel="@ada")
            if body["watching"]:
                break
            await asyncio.sleep(0.02)
        assert not poll.done(), "the long poll ended before presence was read"
        assert body["watching"] == [
            {
                "channel": "@ada",
                "user_id": org.ada,
                "name": "Ada",
                "agent": "codex",
                "session": "thread-9",
                "last_seen": body["watching"][0]["last_seen"],
            }
        ]
    finally:
        assert (await poll).status_code == 200

    # Listed for the wait plus two minutes from when the read began, then gone.
    clock[0] = started + 2 + WATCHING_TTL_SECONDS - 0.5
    assert watchers(await presence(org, org.grace)) == {("@ada", org.ada, "codex", "thread-9")}
    clock[0] = started + 2 + WATCHING_TTL_SECONDS
    assert (await presence(org, org.grace))["watching"] == []


async def test_a_dispatcher_that_peeks_once_a_minute_stays_listed(
    org: SimpleNamespace, clock: list[float]
) -> None:
    for _ in range(3):
        peek = await feed(org, org.ada, channel="@ada", limit=0, agent="dispatcher")
        assert peek.status_code == 200, peek.text
        clock[0] += 60
        assert watchers(await presence(org, org.grace, channel="@ada")) == {
            ("@ada", org.ada, "dispatcher", None)
        }

    clock[0] += WATCHING_TTL_SECONDS - 60
    assert (await presence(org, org.grace, channel="@ada"))["watching"] == []


async def test_a_read_of_every_channel_is_listed_under_any_channel(
    org: SimpleNamespace, clock: list[float]
) -> None:
    assert (await feed(org, org.ada, limit=0, agent="claude-code")).status_code == 200
    assert (await feed(org, org.grace, channel=["#reviews", "@grace"], limit=0)).status_code == 200

    everyone = (None, org.ada, "claude-code", None)
    assert watchers(await presence(org, org.vic)) == {
        everyone,
        ("#reviews", org.grace, None, None),
        ("@grace", org.grace, None, None),
    }
    assert watchers(await presence(org, org.vic, channel="#Reviews")) == {
        everyone,
        ("#reviews", org.grace, None, None),
    }
    assert watchers(await presence(org, org.vic, channel="@ada")) == {everyone}


async def test_recent_lists_who_posted_in_the_last_hour(
    org: SimpleNamespace, clock: list[float]
) -> None:
    for user, channel, agent, body in (
        (org.ada, "#reviews", "claude-code", "Rebased; another look?"),
        (org.ada, "#reviews", "claude-code", "CI is green now"),
        (org.grace, "@ada", "codex", "Looking now"),
        (org.grace, "#reviews", None, "Long ago"),
    ):
        posted = await post(org, user, channel=channel, body=body, agent=agent)
        assert posted.status_code == 201, posted.text
        last = posted.json()["message"]
    # The last post moves out of the hour; the others stay in it.
    await org.content.execute_query(
        "UPDATE net_messages SET created_at = created_at - 61m WHERE uuid = $id;", id=last["id"]
    )

    body = await presence(org, org.vic)
    recent = {(row["channel"], row["user_id"], row["name"], row["agent"]) for row in body["recent"]}
    assert recent == {
        ("#reviews", org.ada, "Ada", "claude-code"),
        ("@ada", org.grace, "Grace", "codex"),
    }
    assert len(body["recent"]) == 2
    assert body["watching"] == []

    only = await presence(org, org.vic, channel="@ada")
    assert [(row["channel"], row["user_id"]) for row in only["recent"]] == [("@ada", org.grace)]


async def test_presence_stays_inside_its_organization(
    org: SimpleNamespace, clock: list[float]
) -> None:
    assert (await feed(org, org.ozzy, organization=org.other_org, limit=0)).status_code == 200
    async with client_for(org, org.ozzy, org.other_org) as client:
        posted = await client.post("/api/net/messages", json={"channel": "@ada", "body": "hi"})
        assert posted.status_code == 201, posted.text
        theirs = (await client.get("/api/net/presence")).json()
    assert (await feed(org, org.ada, limit=0)).status_code == 200

    ours = await presence(org, org.grace)
    assert watchers(ours) == {(None, org.ada, None, None)}
    assert ours["recent"] == []
    assert watchers(theirs) == {(None, org.ozzy, None, None)}
    assert [row["user_id"] for row in theirs["recent"]] == [org.ozzy]


async def test_viewers_read_presence_and_bad_input_is_refused(
    org: SimpleNamespace, clock: list[float]
) -> None:
    assert (await presence(org, org.vic)) == {"watching": [], "recent": []}
    async with client_for(org, org.vic) as client:
        assert (
            await client.get("/api/net/presence", params={"channel": "no spaces"})
        ).status_code == 422
        for label in ({"agent": "two\nlines"}, {"session": "x" * 129}, {"agent": "a" * 65}):
            refused = await client.get("/api/net/feed", params={"limit": 0, **label})
            assert refused.status_code == 422, label


async def test_a_registry_that_cannot_answer_never_fails_the_read(
    org: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unreachable:
        async def note(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("presence store is down")

    monkeypatch.setattr(net_routes, "get_presence", Unreachable)

    read = await feed(org, org.ada, channel="@ada", limit=0)

    assert read.status_code == 200, read.text
    assert read.json()["next_since"]


def _request(headers: dict[str, str]) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/api/net/feed", "headers": raw})


def test_only_a_read_by_the_web_session_alone_counts_as_the_web_app() -> None:
    assert net_routes.read_by_web_session(_request({"Cookie": "sibyl_access_token=jwt"}))
    assert not net_routes.read_by_web_session(_request({"Authorization": "Bearer jwt"}))
    assert not net_routes.read_by_web_session(
        _request({"Authorization": "Bearer sk_key", "Cookie": "sibyl_access_token=jwt"})
    )
    assert not net_routes.read_by_web_session(_request({}))


async def test_presence_noted_through_one_replica_is_read_through_another(
    org: SimpleNamespace,
    clock: list[float],
    live_redis_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = RedisPresenceRegistry(), RedisPresenceRegistry()
    try:
        monkeypatch.setattr(net_routes, "get_presence", lambda: first)
        read = await feed(org, org.ada, channel="@ada", limit=0, agent="codex", session="s-1")
        assert read.status_code == 200, read.text

        monkeypatch.setattr(net_routes, "get_presence", lambda: second)
        assert watchers(await presence(org, org.grace, channel="@ada")) == {
            ("@ada", org.ada, "codex", "s-1")
        }
        clock[0] += WATCHING_TTL_SECONDS
        assert (await presence(org, org.grace, channel="@ada"))["watching"] == []
    finally:
        client = await first._get_redis()
        await client.delete(*_keys(org.id))
        for registry in (first, second):
            if registry._redis is not None:
                await registry._redis.aclose()


def test_the_default_registry_follows_the_coordination_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presence_module, "_presence", None)
    monkeypatch.setattr(presence_module, "_presence_backend", None)
    monkeypatch.setattr(presence_module, "get_coordination_backend", lambda: "local")
    local = presence_module.get_presence()
    assert isinstance(local, LocalPresenceRegistry)
    assert presence_module.get_presence() is local

    monkeypatch.setattr(presence_module, "get_coordination_backend", lambda: "redis")
    assert isinstance(presence_module.get_presence(), RedisPresenceRegistry)
