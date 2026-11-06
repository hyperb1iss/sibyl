"""No gaps under contention: concurrent writers and cursor-keeping readers on a live server.

The feed promises that a reader who keeps its cursor never skips a committed
message; duplicates are allowed, gaps are not. That rests on the settle
window: nothing younger than two seconds by the database clock is returned,
so a write still committing cannot land behind a cursor. This drives the real
routes the way a fleet would: K writers post M messages each across
overlapping channels and threads (every post upserts one of a handful of hot
thread rows), a second organization posts noise, and R readers page the feed
from a start-from-now cursor, some filtered by channel or thread, some with
small pages, some long-polling, until the writers are done and the settle
window has passed.

Every reader must see every message its filter matches, nothing from the other
organization, nothing its filter excludes, and every page (and the whole
stream) in the feed's ``(created_ns, id)`` order. The test reports duplicates and how long
after its ``created_at`` each post's response came back: an upper bound on how
long the write took to commit, against the two-second margin.

Live SurrealDB only: the embedded engine is clamped to one connection and
misses write-write conflicts, so it cannot contend. Size it with
``SIBYL_NET_CONTENTION_WRITERS`` (16), ``SIBYL_NET_CONTENTION_MESSAGES`` (50),
``SIBYL_NET_CONTENTION_READERS`` (6) and ``SIBYL_NET_CONTENTION_ROUNDS`` (1);
``SIBYL_NET_CONTENTION_REPORT`` names a JSON file to append each round's
numbers to.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import statistics
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from pydantic import SecretStr

from sibyl.api.routes import net as net_routes
from sibyl.auth.jwt import create_access_token, verify_access_token
from sibyl.persistence.surreal import content as surreal_content, organization_runtime
from sibyl.persistence.surreal.auth import SurrealAuthContextResolver
from sibyl.persistence.surreal.auth_runtime import _common as auth_common
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
)
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.services import content_client
from sibyl_core.services.net import SETTLE_SECONDS, NetCursor
from tests.test_routes_net import CREDENTIALS as SURREAL_LOGIN, engine_url

pytestmark = pytest.mark.asyncio(loop_scope="module")

WRITERS = int(os.environ.get("SIBYL_NET_CONTENTION_WRITERS", "16"))
MESSAGES = int(os.environ.get("SIBYL_NET_CONTENTION_MESSAGES", "50"))
READERS = int(os.environ.get("SIBYL_NET_CONTENTION_READERS", "6"))
ROUNDS = int(os.environ.get("SIBYL_NET_CONTENTION_ROUNDS", "1"))
REPORT = os.environ.get("SIBYL_NET_CONTENTION_REPORT", "")
NOISE_MESSAGES = max(WRITERS * MESSAGES // 10, 10)
# How long past the settle window a reader keeps polling before it may stop.
DRAIN_MARGIN_SECONDS = 1.0
CHANNELS = ("#reviews", "@ada", "#builds", "#ops")
THREADS = ("", "t1", "t2", "gh:acme/app#42", "")


@dataclass(frozen=True, slots=True)
class ReaderSpec:
    name: str
    channels: tuple[str, ...] = ()
    thread: str | None = None
    limit: int = 100
    wait: int = 0

    def matches(self, channel: str, thread: str) -> bool:
        if self.channels and channel not in self.channels:
            return False
        return self.thread is None or thread == self.thread


READER_SPECS = (
    ReaderSpec("everything-big-pages", limit=500),
    ReaderSpec("reviews-long-poll", channels=("#reviews",), limit=50, wait=3),
    ReaderSpec("two-channels-small-pages", channels=("#reviews", "@ada"), limit=7, wait=1),
    ReaderSpec("thread-t1-long-poll", thread="t1", limit=20, wait=2),
    ReaderSpec("everything-tiny-pages", limit=3),
    ReaderSpec("builds-ops-long-poll", channels=("#builds", "#ops"), limit=100, wait=5),
)


@dataclass(frozen=True, slots=True)
class Sent:
    id: str
    channel: str
    thread: str
    created_ns: int
    answered_ns: int
    created_text: str


@dataclass
class ReaderLog:
    spec: ReaderSpec
    seen: list[str] = field(default_factory=list)
    channels_threads: dict[str, tuple[str, str]] = field(default_factory=dict)
    pages: int = 0
    page_order_violations: list[str] = field(default_factory=list)
    stream_order_violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # (cursor key the page was read after, last key it returned, rows)
    page_log: list[tuple[tuple[int, str], tuple[int, str] | None, int]] = field(
        default_factory=list
    )


def order_key(message: dict[str, object]) -> tuple[int, str]:
    """The feed's order key: the message's instant in nanoseconds, then its id."""
    cursor = NetCursor.decode(str(message["cursor"]))
    return cursor.created_ns, str(message["id"])


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stores() -> AsyncIterator[SimpleNamespace]:
    """One live namespace, migrated once, behind production-sized pools."""
    url = engine_url("live")
    namespace = f"verify_net_contention_{uuid4().hex}"
    auth = SurrealAuthClient(
        url=url,
        namespace=namespace,
        pool_size=settings.surreal_client_pool_size("auth"),
        **SURREAL_LOGIN,
    )
    content = SurrealContentClient(
        url=url,
        namespace=namespace,
        pool_size=settings.surreal_client_pool_size("content"),
        **SURREAL_LOGIN,
    )
    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(settings, "surreal_url", url)
        patch.setattr(settings, "jwt_secret", SecretStr("isolated-sibylnet-contention-" * 3))
        await bootstrap_auth_schema(auth)
        await bootstrap_content_schema(content)

        @asynccontextmanager
        async def auth_scope():
            yield auth

        async def content_scope():
            return content

        patch.setattr(auth_common, "surreal_auth_client_scope", auth_scope)
        patch.setattr(organization_runtime, "surreal_auth_client_scope", auth_scope)
        patch.setattr(content_client, "get_shared_surreal_content_client", content_scope)
        patch.setattr(surreal_content, "get_shared_surreal_content_client", content_scope)
        resolver = SurrealAuthContextResolver.from_client(auth)
        app = FastAPI()

        @app.middleware("http")
        async def fixture_identity(request: Request, call_next):
            claims = verify_access_token(request.headers["Authorization"].removeprefix("Bearer "))
            request.state.auth_context = await resolver.resolve(claims)
            return await call_next(request)

        app.include_router(net_routes.router, prefix="/api")
        yield SimpleNamespace(auth=auth, content=content, app=app)
    finally:
        try:
            await auth.execute_query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
        finally:
            await content.close()
            await auth.close()
            patch.undo()


async def seed_round(stores: SimpleNamespace) -> SimpleNamespace:
    org, other = str(uuid4()), str(uuid4())
    people = {name: str(uuid4()) for name in ("ada", "grace", "owen", "vic", "ozzy")}
    roles = {"ada": "member", "grace": "member", "owen": "owner", "vic": "viewer"}
    rows: dict[str, list[dict[str, object]]] = {
        "organizations": [
            {"uuid": org, "name": "Acme", "slug": f"acme-{org[:8]}"},
            {"uuid": other, "name": "Elsewhere", "slug": f"else-{other[:8]}"},
        ],
        "users": [
            {"uuid": user, "email": f"{name}+{org[:8]}@example.test", "name": name.title()}
            for name, user in people.items()
        ],
        "organization_members": [
            {
                "uuid": str(uuid4()),
                "organization_id": org,
                "user_id": people[name],
                "role": role,
                "handle": name,
            }
            for name, role in roles.items()
        ]
        + [
            {
                "uuid": str(uuid4()),
                "organization_id": other,
                "user_id": people["ozzy"],
                "role": "member",
            }
        ],
    }
    for table, records in rows.items():
        for record in records:
            await stores.auth.execute_query(f"CREATE {table} CONTENT $record;", record=record)

    def token(name: str, organization: str = org) -> str:
        return create_access_token(user_id=UUID(people[name]), organization_id=UUID(organization))

    return SimpleNamespace(
        org=org,
        other=other,
        writers=[token("ada"), token("grace"), token("owen")],
        readers=[token("vic"), token("grace")],
        noise=token("ozzy", other),
    )


@asynccontextmanager
async def http_as(stores: SimpleNamespace, token: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stores.app),
        base_url="http://net.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    ) as client:
        yield client


async def write(
    stores: SimpleNamespace, token: str, writer: int, count: int, sent: list[Sent], *, noise: bool
) -> None:
    async with http_as(stores, token) as client:
        for index in range(count):
            channel = "#reviews" if noise else CHANNELS[(writer + index) % len(CHANNELS)]
            thread = "t1" if noise else THREADS[(writer * 7 + index) % len(THREADS)]
            body = {"channel": channel, "body": f"writer {writer} message {index}"}
            if thread:
                body["thread"] = thread
            response = await client.post("/api/net/messages", json=body)
            answered = time.time_ns()
            assert response.status_code == 201, response.text
            message = response.json()["message"]
            sent.append(
                Sent(
                    id=message["id"],
                    channel=channel,
                    thread=thread,
                    created_ns=order_key(message)[0],
                    answered_ns=answered,
                    created_text=str(message["created_at"]),
                )
            )


async def read(
    stores: SimpleNamespace,
    token: str,
    spec: ReaderSpec,
    start: str,
    writers_done: asyncio.Event,
    done_at: list[float],
    log: ReaderLog,
) -> None:
    cursor = start
    previous: tuple[int, str] | None = None
    deadline = time.monotonic() + 600
    async with http_as(stores, token) as client:
        while time.monotonic() < deadline:
            asked_at = time.time()
            params: dict[str, object] = {"since": cursor, "limit": spec.limit, "wait": spec.wait}
            if spec.channels:
                params["channel"] = list(spec.channels)
            if spec.thread is not None:
                params["thread"] = spec.thread
            response = await client.get("/api/net/feed", params=params)
            if response.status_code != 200:
                log.errors.append(f"{response.status_code}: {response.text[:200]}")
                return
            page = response.json()
            log.pages += 1
            keys = [order_key(message) for message in page["messages"]]
            texts = [str(key[0]) for key in keys]
            since_text = str(NetCursor.decode(str(cursor)).created_ns)
            for index, (earlier, later) in enumerate(itertools.pairwise(keys)):
                if not earlier < later:
                    log.page_order_violations.append(
                        f"after cursor {since_text!r}: {texts[index]!r} {earlier[1]} "
                        f"then {texts[index + 1]!r} {later[1]} (positions {index}, {index + 1} "
                        f"of {len(keys)})"
                    )
            if keys and previous is not None and not previous < keys[0]:
                log.stream_order_violations.append(
                    f"cursor {since_text!r} then {texts[0]!r} {keys[0][1]}"
                )
            since_cursor = NetCursor.decode(str(cursor))
            log.page_log.append(
                (
                    (since_cursor.created_ns, since_cursor.uuid),
                    keys[-1] if keys else None,
                    len(keys),
                )
            )
            if keys:
                previous = keys[-1]
            for message in page["messages"]:
                log.seen.append(message["id"])
                log.channels_threads[message["id"]] = (message["channel"], message["thread"] or "")
            cursor = page["next_since"]
            settled = writers_done.is_set() and asked_at > (
                done_at[0] + SETTLE_SECONDS + DRAIN_MARGIN_SECONDS
            )
            if settled and not page["messages"] and not page["more"]:
                return
            if not page["messages"] and spec.wait == 0:
                await asyncio.sleep(0.05)
    log.errors.append("reader did not drain within 600 s")


async def start_cursor(stores: SimpleNamespace, token: str, spec: ReaderSpec) -> str:
    params: dict[str, object] = {"limit": 0}
    if spec.channels:
        params["channel"] = list(spec.channels)
    if spec.thread is not None:
        params["thread"] = spec.thread
    async with http_as(stores, token) as client:
        response = await client.get("/api/net/feed", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []
    return response.json()["next_since"]


def publish(round_index: int, report: dict[str, object]) -> None:
    """Print the round's numbers (visible with -s) and append them to the report file."""
    print(f"\nsibylnet contention round {round_index}: {json.dumps(report, indent=2)}")  # noqa: T201
    if REPORT:
        with Path(REPORT).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report) + "\n")


@pytest.mark.parametrize("round_index", range(ROUNDS))
async def test_no_reader_skips_a_committed_message_under_contention(
    stores: SimpleNamespace, round_index: int
) -> None:
    world = await seed_round(stores)
    specs = [READER_SPECS[index % len(READER_SPECS)] for index in range(READERS)]
    tokens = [world.readers[index % len(world.readers)] for index in range(READERS)]
    starts = [
        await start_cursor(stores, token, spec) for token, spec in zip(tokens, specs, strict=True)
    ]
    logs = [ReaderLog(spec) for spec in specs]
    sent: list[Sent] = []
    noise: list[Sent] = []
    writers_done = asyncio.Event()
    done_at: list[float] = []

    async def writers() -> None:
        await asyncio.gather(
            *(
                write(stores, world.writers[w % len(world.writers)], w, MESSAGES, sent, noise=False)
                for w in range(WRITERS)
            ),
            write(stores, world.noise, -1, NOISE_MESSAGES, noise, noise=True),
        )
        done_at.append(time.time())
        writers_done.set()

    began = time.monotonic()
    await asyncio.gather(
        writers(),
        *(
            read(stores, token, spec, start, writers_done, done_at, log)
            for token, spec, start, log in zip(tokens, specs, starts, logs, strict=True)
        ),
    )
    elapsed = time.monotonic() - began

    lags_ms = sorted((item.answered_ns - item.created_ns) / 1e6 for item in sent)
    noise_ids = {item.id for item in noise}
    report: dict[str, object] = {
        "round": round_index,
        "writers": WRITERS,
        "messages_per_writer": MESSAGES,
        "messages": len(sent),
        "noise": len(noise),
        "readers": READERS,
        "seconds": round(elapsed, 2),
        "commit_lag_ms": {
            "max": round(lags_ms[-1], 1),
            "p99": round(lags_ms[int(len(lags_ms) * 0.99) - 1], 1),
            "p50": round(statistics.median(lags_ms), 1),
            "over_settle_window": sum(1 for lag in lags_ms if lag >= SETTLE_SECONDS * 1000),
        },
        "readers_detail": [],
    }
    problems: list[str] = []
    for log in logs:
        expected = {item.id for item in sent if log.spec.matches(item.channel, item.thread)}
        seen = set(log.seen)
        duplicates = sum(count - 1 for count in Counter(log.seen).values() if count > 1)
        missing = expected - seen
        foreign = seen & noise_ids
        unwanted = {
            message_id
            for message_id in seen - noise_ids
            if not log.spec.matches(*log.channels_threads[message_id])
        }
        report["readers_detail"].append(  # type: ignore[union-attr]
            {
                "reader": log.spec.name,
                "expected": len(expected),
                "seen": len(seen),
                "pages": log.pages,
                "duplicates": duplicates,
                "missing": len(missing),
            }
        )
        if log.errors:
            problems.append(f"{log.spec.name}: {log.errors[:2]}")
        if missing:
            by_id = {item.id: item for item in sent}
            described = []
            for message_id in sorted(missing)[:3]:
                item = by_id[message_id]
                key = (item.created_ns, item.id)
                straddling = [
                    entry
                    for entry in log.page_log
                    if entry[0] < key and entry[1] is not None and key <= entry[1]
                ]
                described.append(
                    f"{message_id} created {item.created_text} "
                    f"(answered {(item.answered_ns - item.created_ns) / 1e6:.1f} ms later), "
                    f"pages that stepped over it: {straddling[:2]}"
                )
            problems.append(
                f"{log.spec.name} missed {len(missing)} of {len(expected)}: {described}"
            )
        if foreign:
            problems.append(f"{log.spec.name} saw {len(foreign)} messages of another organization")
        if unwanted:
            problems.append(f"{log.spec.name} saw {len(unwanted)} messages its filter excludes")
        if log.page_order_violations:
            problems.append(f"{log.spec.name} page out of order: {log.page_order_violations[:2]}")
        if log.stream_order_violations:
            problems.append(
                f"{log.spec.name} stream went backwards: {log.stream_order_violations[:2]}"
            )

    publish(round_index, report)
    assert len(sent) == WRITERS * MESSAGES
    assert problems == [], problems
