"""Scale harness for sibylnet: seed channels at size, then measure the routes.

Run it only against a disposable stack: a real SurrealDB server on disk
(never ``memory://``) and an API started from this checkout, both reading the
same ``SIBYL_*`` environment.

``seed`` writes organizations, users, API keys, messages and thread rows
straight into the auth and content stores the API reads, deterministically
from ``--seed``. ``run`` drives the HTTP routes and reports latency
percentiles, throughput, the server's SurrealDB query counts (scraped from
``/api/telemetry/prometheus`` as the seeded global admin) and the API
process's CPU time. ``distill`` adds idle undistilled threads and runs the
distillation job in this process with the server's settings. ``cleanup``
removes everything the seed wrote.

    export SIBYL_SURREAL_URL=ws://127.0.0.1:18940/rpc
    export SIBYL_SURREAL_USERNAME=root SIBYL_SURREAL_PASSWORD=root
    uv run python -m tools.perf.sibylnet_scale seed
    uv run sibyld serve --port 18941 &
    uv run python -m tools.perf.sibylnet_scale run \\
        --api-base-url http://127.0.0.1:18941/api --server-pid $!
    uv run python -m tools.perf.sibylnet_scale distill
    uv run python -m tools.perf.sibylnet_scale cleanup

``seed --reuse`` returns at once when the manifest's data is already in the
store, so rerunning one scenario (``run --scenarios watchers``) against a
stack that is still up skips the reseed. ``--manifest`` keeps one manifest per
stack when two are up side by side.

Reports land in ``.moon/cache/perf-results/sibylnet_scale_<scenario>_<run-id>.json``.
The distillation job reflects with the heuristic extractor, which calls no
model; run it with no provider keys in the environment and it costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from surrealdb import RecordID

from sibyl.config import settings
from sibyl.core_runtime_ports import install_core_runtime_ports
from sibyl.jobs.net import distill_idle_net_threads
from sibyl.persistence.surreal.auth import build_surreal_auth_client
from sibyl.persistence.surreal.auth_runtime.api_keys import create_api_key_for_user
from sibyl_core.backends.surreal import bootstrap_auth_schema, bootstrap_content_schema
from sibyl_core.backends.surreal.records import normalize_records, query_error
from sibyl_core.services.content_client import build_surreal_content_client
from sibyl_core.services.net import (
    CHANNEL_SUMMARY_STATEMENT,
    CHANNEL_THREADS_STATEMENT,
    NetCursor,
    feed_statement,
    idle_net_threads,
    idle_threads_statement,
    post_net_message,
)

JsonObject = dict[str, Any]

RESULTS_DIR = Path(".moon/cache/perf-results")
MANIFEST_PATH = RESULTS_DIR / "sibylnet_scale_seed.json"
BIG_ORG_INDEX = 0
INSERT_BATCH = 2_000
KEY_BATCH = 500
FEED_SAMPLES = 200
LISTING_SAMPLES = 100
WATCH_WAIT_SECONDS = 25
# uvicorn closes a keep-alive connection after 5 idle seconds. A client that
# keeps one as long (httpx's default) can send on it just as the server closes
# it and get RemoteProtocolError, so the harness lets its own go first.
KEEPALIVE_EXPIRY_SECONDS = 2.0
LOAD_POLL_SECONDS = 10.0
WATCHERS_PER_WORKER = 50
WORKER_READY = "WATCH-READY"
WORKER_RESULT = "WATCH-RESULT"
LAG_TICK_SECONDS = 0.05
ERROR_SAMPLES = 20
HTTP_OK = 200
HTTP_CREATED = 201
WORDS = (
    "rebased",
    "fixture",
    "review",
    "migration",
    "keyset",
    "paging",
    "index",
    "cursor",
    "thread",
    "handoff",
    "green",
    "ship",
    "deploy",
    "rollback",
    "schema",
    "queue",
    "watcher",
    "settle",
    "decision",
)


@dataclass(slots=True)
class SeedOrg:
    id: str
    slug: str
    big: bool
    users: list[str]
    keys: dict[str, str]
    channels: list[str]
    threads: list[tuple[str, str]] = field(default_factory=list)


def _rng(seed: int) -> random.Random:
    """Seeded generator for synthetic data; nothing here is a secret."""
    return random.Random(seed)  # noqa: S311


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _ok(response: httpx.Response | None, error: str | None, status: int = HTTP_OK) -> bool:
    return error is None and response is not None and response.status_code == status


def percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(latencies_ms: Sequence[float]) -> JsonObject:
    return {
        "count": len(latencies_ms),
        "p50_ms": _round(percentile(latencies_ms, 0.50)),
        "p95_ms": _round(percentile(latencies_ms, 0.95)),
        "p99_ms": _round(percentile(latencies_ms, 0.99)),
        "max_ms": _round(max(latencies_ms) if latencies_ms else None),
    }


def load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


# --- seed ----------------------------------------------------------------------------------------


async def seed(args: argparse.Namespace) -> JsonObject:
    if args.reuse and await _seeded(_existing_manifest(args.manifest)):
        return {"reused": str(args.manifest)}
    # Written only once seeding finishes, so a manifest always names a complete seed.
    args.manifest.unlink(missing_ok=True)
    rng = _rng(args.seed)
    auth = build_surreal_auth_client()
    content = build_surreal_content_client()
    started = time.perf_counter()
    try:
        if not args.skip_bootstrap:
            await bootstrap_auth_schema(auth)
            await bootstrap_content_schema(content)
        orgs = [_plan_org(rng, index, args) for index in range(args.orgs)]
        admin_id = str(uuid.UUID(int=rng.getrandbits(128)))
        await _seed_auth(auth, orgs, admin_id=admin_id)
        admin_key = await _create_keys(orgs, admin_id=admin_id)
        totals = {"messages": 0, "threads": 0}
        for org in orgs:
            counts = await _seed_messages(content, rng, org, args)
            totals["messages"] += counts["messages"]
            totals["threads"] += counts["threads"]
            _say(f"seeded {org.slug}: {counts}")
    finally:
        await content.close()
        await auth.close()
    manifest = {
        "seed": args.seed,
        "admin_id": admin_id,
        "admin_key": admin_key,
        "orgs": [
            {
                "id": org.id,
                "slug": org.slug,
                "users": org.users,
                "keys": org.keys,
                "channels": org.channels,
                "threads": org.threads[: args.thread_sample],
            }
            for org in orgs
        ],
        "totals": totals,
        "seconds": round(time.perf_counter() - started, 1),
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"totals": totals, "seconds": manifest["seconds"], "manifest": str(args.manifest)}


async def _seeded(manifest: JsonObject | None) -> bool:
    """Whether the store holds the seed this manifest describes."""
    if manifest is None:
        return False
    content = build_surreal_content_client()
    try:
        rows = await _scalar(
            content,
            "SELECT uuid FROM net_messages WHERE organization_id = $org LIMIT 1;",
            org=manifest["orgs"][BIG_ORG_INDEX]["id"],
        )
    except RuntimeError:
        return False
    finally:
        await content.close()
    return bool(normalize_records(rows))


def _plan_org(rng: random.Random, index: int, args: argparse.Namespace) -> SeedOrg:
    big = index == BIG_ORG_INDEX
    org_id = str(uuid.UUID(int=rng.getrandbits(128)))
    users = [
        str(uuid.UUID(int=rng.getrandbits(128)))
        for _ in range(args.big_users if big else args.small_users)
    ]
    channels = [
        f"#scale-{index:02d}-{number:04d}"
        for number in range(args.big_channels if big else args.small_channels)
    ]
    return SeedOrg(
        id=org_id,
        slug=f"scale-{index:02d}-{org_id[:8]}",
        big=big,
        users=users,
        keys={},
        channels=channels,
    )


async def _seed_auth(auth: Any, orgs: Sequence[SeedOrg], *, admin_id: str) -> None:
    users: list[JsonObject] = [
        {
            "uuid": admin_id,
            "email": f"admin-{admin_id[:8]}@scale.test",
            "name": "Scale Admin",
            "is_admin": True,
        }
    ]
    organizations = []
    members = []
    for org in orgs:
        organizations.append({"uuid": org.id, "name": org.slug, "slug": org.slug})
        members.append(
            {
                "uuid": str(uuid.uuid4()),
                "organization_id": org.id,
                "user_id": admin_id,
                "role": "owner",
            }
        )
        for number, user in enumerate(org.users):
            users.append(
                {
                    "uuid": user,
                    "email": f"u{number}-{user[:8]}@scale.test",
                    "name": f"Agent {number}",
                }
            )
            members.append(
                {
                    "uuid": str(uuid.uuid4()),
                    "organization_id": org.id,
                    "user_id": user,
                    "role": "member",
                }
            )
    for table, rows in (
        ("users", users),
        ("organizations", organizations),
        ("organization_members", members),
    ):
        for start in range(0, len(rows), INSERT_BATCH):
            await _checked(
                auth,
                f"INSERT INTO {table} $rows RETURN NONE;",
                rows=rows[start : start + INSERT_BATCH],
            )


async def _create_keys(orgs: Sequence[SeedOrg], *, admin_id: str) -> str:
    """One API key per seeded user, minted the way the API mints them."""

    async def mint(org_id: str, user_id: str) -> str:
        _, raw = await create_api_key_for_user(
            organization_id=uuid.UUID(org_id),
            user_id=uuid.UUID(user_id),
            name="scale",
            live=False,
            scopes=["api:read", "api:write"],
            project_ids=None,
            expires_at=None,
            request=None,
        )
        return str(raw)

    for org in orgs:
        secrets = await asyncio.gather(*(mint(org.id, user) for user in org.users))
        org.keys = dict(zip(org.users, secrets, strict=True))
    return await mint(orgs[BIG_ORG_INDEX].id, admin_id)


async def _seed_messages(
    content: Any, rng: random.Random, org: SeedOrg, args: argparse.Namespace
) -> dict[str, int]:
    total = args.big_messages if org.big else args.small_messages
    thread_count = args.big_threads if org.big else args.small_threads
    per_channel = max(1, thread_count // len(org.channels))
    threads: list[tuple[str, str]] = []
    for channel in org.channels:
        threads.append((channel, ""))
        threads.extend(
            (channel, f"gh:acme/app#{rng.randrange(1, 99_999)}") for _ in range(per_channel - 1)
        )
    threads = list(dict.fromkeys(threads))
    org.threads = threads
    now = datetime.now(UTC)
    messages: list[JsonObject] = []
    thread_rows: list[JsonObject] = []
    base, extra = divmod(total, len(threads))
    for position, (channel, thread) in enumerate(threads):
        count = base + (1 if position < extra else 0)
        if count == 0:
            continue
        gaps = [timedelta(minutes=rng.randrange(1, 240)) for _ in range(count - 1)]
        last = now - timedelta(days=1 + rng.random() * args.history_days)
        first = last - sum(gaps, timedelta())
        stamps = [first]
        for gap in gaps:
            stamps.append(stamps[-1] + gap)
        messages.extend(
            {
                "uuid": str(uuid.UUID(int=rng.getrandbits(128))),
                "organization_id": org.id,
                "channel": channel,
                "thread": thread,
                "kind": "message",
                "body": " ".join(rng.choice(WORDS) for _ in range(rng.randrange(12, 48))),
                "author_user_id": rng.choice(org.users),
                "author_agent": "scale",
                "created_at": stamp,
            }
            for stamp in stamps
        )
        thread_rows.append(
            {
                "organization_id": org.id,
                "channel": channel,
                "thread": thread,
                "message_count": count,
                "first_message_at": first,
                "last_message_at": stamps[-1],
                "reflected_through": stamps[-1],
                # The watermark sits on the last message, in the feed's order key.
                "reflected_through_ns": _nanos(stamps[-1]),
                "reflected_through_uuid": messages[-1]["uuid"],
                "reflection_attempts": 0,
                "updated_at": stamps[-1],
            }
        )
    for start in range(0, len(messages), INSERT_BATCH):
        await _checked(
            content,
            "INSERT INTO net_messages $rows RETURN NONE;",
            rows=messages[start : start + INSERT_BATCH],
        )
    await insert_thread_rows(content, thread_rows)
    return {"messages": len(messages), "threads": len(thread_rows)}


async def insert_thread_rows(content: Any, rows: list[JsonObject]) -> None:
    """Insert thread rows under the record id the API derives for each thread key.

    Fields the database's schema does not define are dropped, so the same
    seed loads a database migrated by an older checkout (for a baseline run).
    """
    info = normalize_records(await _checked(content, "INFO FOR TABLE net_threads;"))
    fields = info[0].get("fields") if info else None
    defined = set(fields) if isinstance(fields, dict) else set()
    if defined:
        for row in rows:
            for name in [name for name in row if name not in defined]:
                del row[name]
    for start in range(0, len(rows), INSERT_BATCH):
        batch = rows[start : start + INSERT_BATCH]
        keys = [[row["organization_id"], row["channel"], row["thread"]] for row in batch]
        # Wrapped in an object: a bare list of strings reads as an error envelope.
        result = await _checked(
            content,
            "RETURN { digests: array::map($keys, |$key| crypto::sha256(type::string($key))) };",
            keys=keys,
        )
        digests = normalize_records(result)[0]["digests"]
        if not isinstance(digests, list):
            msg = "thread key digests did not come back as a list"
            raise TypeError(msg)
        for row, digest in zip(batch, digests, strict=True):
            row["id"] = RecordID("net_threads", digest)
        await _checked(content, "INSERT INTO net_threads $rows RETURN NONE;", rows=batch)


async def cleanup(args: argparse.Namespace) -> JsonObject:
    manifest = _manifest(args.manifest)
    org_ids = [org["id"] for org in manifest["orgs"]]
    users = [user for org in manifest["orgs"] for user in org["users"]] + [manifest["admin_id"]]
    auth = build_surreal_auth_client()
    content = build_surreal_content_client()
    try:
        for org_id in org_ids:
            await _checked(
                content,
                "DELETE net_messages WHERE organization_id = $org RETURN NONE; "
                "DELETE net_threads WHERE organization_id = $org RETURN NONE; "
                "DELETE raw_captures WHERE organization_id = $org RETURN NONE;",
                org=org_id,
            )
            await _checked(
                auth,
                "DELETE organization_members WHERE organization_id = $org RETURN NONE; "
                "DELETE api_keys WHERE organization_id = $org RETURN NONE; "
                "DELETE organizations WHERE uuid = $org RETURN NONE;",
                org=org_id,
            )
        for start in range(0, len(users), 32):
            await _checked(
                auth, "DELETE users WHERE uuid IN $ids RETURN NONE;", ids=users[start : start + 32]
            )
    finally:
        await content.close()
        await auth.close()
    args.manifest.unlink(missing_ok=True)
    return {"organizations": len(org_ids), "users": len(users)}


# --- HTTP scenarios ------------------------------------------------------------------------------


class Api:
    """HTTP clients for one or more API replicas; ``route`` picks the replica, round robin."""

    def __init__(self, base_urls: Sequence[str], *, connections: int) -> None:
        self.clients = [
            httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                timeout=httpx.Timeout(WATCH_WAIT_SECONDS + 30),
                limits=httpx.Limits(
                    max_connections=connections,
                    max_keepalive_connections=connections,
                    keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
                ),
            )
            for base_url in base_urls
        ]
        self.error_samples: list[str] = []

    @property
    def replicas(self) -> int:
        return len(self.clients)

    async def request(
        self, method: str, path: str, key: str, *, route: int = 0, **kwargs: Any
    ) -> tuple[float, httpx.Response | None, str | None]:
        started = time.perf_counter()
        client = self.clients[route % len(self.clients)]
        try:
            response = await client.request(
                method, path, headers={"Authorization": f"Bearer {key}"}, **kwargs
            )
        except httpx.HTTPError as exc:
            if len(self.error_samples) < ERROR_SAMPLES:
                self.error_samples.append(f"{method} {path}: {type(exc).__name__}: {exc}")
            return (time.perf_counter() - started) * 1000, None, type(exc).__name__
        return (time.perf_counter() - started) * 1000, response, None

    async def close(self) -> None:
        for client in self.clients:
            await client.aclose()


@dataclass(slots=True)
class ServerProbe:
    """The API processes' CPU time and SurrealDB query counters, summed over replicas."""

    api: Api
    admin_key: str
    pids: Sequence[int]

    async def sample(self) -> JsonObject:
        cpus = [_process_cpu_seconds(pid) for pid in self.pids]
        cpu = sum(c for c in cpus if c is not None) if cpus and None not in cpus else None
        counters: dict[str, dict[str, float]] = {
            name: {} for name in ("queries", "retries", "wait_count", "wait_ms")
        }
        series = {
            "sibyl_surreal_queries_total": "queries",
            "sibyl_surreal_query_retries_total": "retries",
            "sibyl_surreal_pool_wait_ms_count": "wait_count",
            "sibyl_surreal_pool_wait_ms_sum": "wait_ms",
        }
        errors: list[str] = []
        for replica in range(self.api.replicas):
            _, response, error = await self.api.request(
                "GET", "/telemetry/prometheus", self.admin_key, route=replica
            )
            if response is None or not _ok(response, error):
                errors.append(error or str(response.status_code if response else "none"))
                continue
            for line in response.text.splitlines():
                match = re.match(r"^(sibyl_surreal_\w+)\{([^}]*)\} ([0-9.e+-]+)$", line)
                if match is None or match.group(1) not in series:
                    continue
                name, labels, value = match.groups()
                client = re.search(r'client="([^"]+)"', labels)
                key = client.group(1) if client else "unknown"
                bucket = counters[series[name]]
                bucket[key] = bucket.get(key, 0.0) + float(value)
        return {"at": time.perf_counter(), "cpu_seconds": cpu, **counters, "errors": errors}

    @staticmethod
    def delta(before: JsonObject, after: JsonObject) -> JsonObject:
        seconds = after["at"] - before["at"]

        def change(name: str) -> dict[str, float]:
            clients = set(before[name]) | set(after[name])
            return {
                client: after[name].get(client, 0.0) - before[name].get(client, 0.0)
                for client in sorted(clients)
            }

        queries = change("queries")
        waits = change("wait_count")
        wait_ms = change("wait_ms")
        total = sum(queries.values())
        cpu = (
            after["cpu_seconds"] - before["cpu_seconds"]
            if after["cpu_seconds"] is not None and before["cpu_seconds"] is not None
            else None
        )
        return {
            "seconds": round(seconds, 2),
            "queries": {client: int(count) for client, count in sorted(queries.items())},
            "queries_per_second": round(total / seconds, 1) if seconds > 0 else None,
            "api_cpu_percent": round(100 * cpu / seconds, 1)
            if cpu is not None and seconds
            else None,
            "pool_waits": int(sum(waits.values())),
            "pool_wait_ms": round(sum(wait_ms.values()), 1),
            "pool_waits_by_client": {
                client: {"waits": int(count), "ms": round(wait_ms.get(client, 0.0), 1)}
                for client, count in waits.items()
                if count
            },
            "conflict_retries": {
                client: int(count) for client, count in change("retries").items() if count
            },
        }


async def scenario_posts(api: Api, manifest: JsonObject, args: argparse.Namespace) -> JsonObject:
    """``args.posters`` agents posting into the big organization for ``args.seconds``."""
    org = manifest["orgs"][BIG_ORG_INDEX]
    keys = list(org["keys"].values())[: args.posters]
    rng = _rng(args.seed)
    deadline = time.perf_counter() + args.seconds
    latencies: list[float] = []
    statuses: dict[str, int] = {}

    async def poster(number: int, key: str) -> None:
        while time.perf_counter() < deadline:
            channel, thread = rng.choice(org["threads"])
            body = {"channel": channel, "body": "scale post " + uuid.uuid4().hex, "agent": "scale"}
            if thread:
                body["thread"] = thread
            elapsed, response, error = await api.request(
                "POST", "/net/messages", key, route=number, json=body
            )
            latencies.append(elapsed)
            status = error or str(response.status_code if response is not None else "none")
            statuses[status] = statuses.get(status, 0) + 1

    started = time.perf_counter()
    await asyncio.gather(*(poster(number, key) for number, key in enumerate(keys)))
    seconds = time.perf_counter() - started
    errors = sum(count for status, count in statuses.items() if status != str(HTTP_CREATED))
    return {
        "posters": len(keys),
        "seconds": round(seconds, 1),
        "posts_per_second": round(len(latencies) / seconds, 1),
        "latency": latency_summary(latencies),
        "statuses": statuses,
        "errors": errors,
    }


async def scenario_feed(api: Api, manifest: JsonObject, args: argparse.Namespace) -> JsonObject:
    """Tail and cursor pages over the whole feed, one channel and 32 channels."""
    org = manifest["orgs"][BIG_ORG_INDEX]
    key = next(iter(org["keys"].values()))
    rng = _rng(args.seed)
    busiest = org["channels"][:32]
    results: JsonObject = {}
    for name, channels in (("all", []), ("one_channel", busiest[:1]), ("32_channels", busiest)):
        params: dict[str, Any] = {"limit": 100}
        if channels:
            params["channel"] = channels
        _, response, _ = await api.request("GET", "/net/feed", key, params={**params, "limit": 500})
        history = response.json()["messages"] if response is not None else []
        cursors = [message["cursor"] for message in history[: max(1, len(history) // 2)]]
        tails: list[float] = []
        pages: list[float] = []
        failures = 0
        for _ in range(args.samples):
            elapsed, response, error = await api.request("GET", "/net/feed", key, params=params)
            failures += int(not _ok(response, error))
            tails.append(elapsed)
            since = rng.choice(cursors) if cursors else None
            elapsed, response, error = await api.request(
                "GET", "/net/feed", key, params={**params, **({"since": since} if since else {})}
            )
            failures += int(not _ok(response, error))
            pages.append(elapsed)
        results[name] = {
            "tail": latency_summary(tails),
            "cursor_page": latency_summary(pages),
            "failures": failures,
        }
    return results


async def scenario_listings(api: Api, manifest: JsonObject, args: argparse.Namespace) -> JsonObject:
    org = manifest["orgs"][BIG_ORG_INDEX]
    key = next(iter(org["keys"].values()))
    channel = org["channels"][0]
    results: JsonObject = {}
    for name, path, params in (
        ("channels", "/net/channels", {}),
        ("threads", "/net/threads", {"channel": channel}),
    ):
        latencies = []
        failures = 0
        for _ in range(args.samples):
            elapsed, response, error = await api.request("GET", path, key, params=params)
            failures += int(not _ok(response, error))
            latencies.append(elapsed)
        results[name] = {**latency_summary(latencies), "failures": failures}
    return results


@dataclass(slots=True)
class WatchState:
    """What the watchers saw: each message id mapped to watcher index and arrival time.

    Arrival is ``time.time_ns()``: watchers run in several processes, and the
    wall clock is the one clock they share with the process that posts.
    """

    received: dict[str, dict[int, int]] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    ready: int = 0
    all_ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)


async def _watch(
    api: Api, key: str, channel: str, index: int, total: int, state: WatchState
) -> None:
    """Start from now on one channel, then long-poll until told to stop."""
    _, response, _ = await api.request(
        "GET", "/net/feed", key, route=index, params={"channel": channel, "limit": 0}
    )
    since = response.json()["next_since"] if response is not None else None
    state.ready += 1
    if state.ready == total:
        state.all_ready.set()
    while not state.stop.is_set():
        params: dict[str, Any] = {"channel": channel, "wait": WATCH_WAIT_SECONDS, "limit": 100}
        if since:
            params["since"] = since
        _, response, error = await api.request("GET", "/net/feed", key, route=index, params=params)
        if response is None or not _ok(response, error):
            label = error or str(response.status_code if response is not None else "none")
            state.failures[label] = state.failures.get(label, 0) + 1
            await asyncio.sleep(1)
            continue
        payload = response.json()
        arrived = time.time_ns()
        for message in payload["messages"]:
            state.received.setdefault(message["id"], {})[index] = arrived
        since = payload["next_since"]


@dataclass(frozen=True, slots=True)
class BurstPost:
    """One burst post: its channel, and wall-clock nanoseconds when sent and when stored."""

    channel: str
    sent: int
    stored: int


async def _bursts(
    api: Api,
    keys: Sequence[str],
    channels: Sequence[str],
    rng: random.Random,
    args: argparse.Namespace,
    failures: dict[str, int],
) -> dict[str, BurstPost]:
    """Post ``args.bursts`` bursts; returns each stored message's channel, send and stored time."""
    posted: dict[str, BurstPost] = {}

    async def one_post(number: int, channel: str, key: str) -> None:
        started = time.time_ns()
        _, response, error = await api.request(
            "POST",
            "/net/messages",
            key,
            route=number,
            json={"channel": channel, "body": "burst " + uuid.uuid4().hex},
        )
        if response is not None and _ok(response, error, HTTP_CREATED):
            posted[response.json()["message"]["id"]] = BurstPost(
                channel=channel, sent=started, stored=time.time_ns()
            )
        else:
            label = error or str(response.status_code if response is not None else "none")
            failures[label] = failures.get(label, 0) + 1

    for _ in range(args.bursts):
        await asyncio.gather(
            *(
                one_post(number, rng.choice(channels), rng.choice(keys))
                for number in range(args.burst_size)
            )
        )
        await asyncio.sleep(args.burst_gap)
    return posted


def _deliveries(
    posted: dict[str, BurstPost], assignments: Sequence[str], received: dict[str, dict[int, int]]
) -> tuple[list[float], list[float], int]:
    """Milliseconds from each send, and from each stored response, to each watcher's copy."""
    from_send: list[float] = []
    from_stored: list[float] = []
    missed = 0
    for message_id, sent in posted.items():
        seen = received.get(message_id, {})
        for index, assigned in enumerate(assignments):
            if assigned != sent.channel:
                continue
            if index in seen:
                from_send.append((seen[index] - sent.sent) / 1e6)
                from_stored.append((seen[index] - sent.stored) / 1e6)
            else:
                missed += 1
    return from_send, from_stored, missed


async def _loop_lag(stop: asyncio.Event, lags: list[float]) -> None:
    """How late this process's event loop runs a short sleep: the harness's own queueing."""
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(LAG_TICK_SECONDS)
        lags.append((time.perf_counter() - started - LAG_TICK_SECONDS) * 1000)


class CpuWindow:
    """This process's CPU use over a window, as a percent of one core."""

    def __init__(self) -> None:
        self.started = (time.process_time(), time.monotonic())

    def percent(self) -> float:
        cpu, wall = time.process_time() - self.started[0], time.monotonic() - self.started[1]
        return round(100 * cpu / wall, 1) if wall > 0 else 0.0


@dataclass(slots=True)
class WatchWorker:
    """One watcher process, driven over its stdin and read over its stdout."""

    process: asyncio.subprocess.Process
    watchers: int

    async def send(self, command: str) -> None:
        stdin = self.process.stdin
        if stdin is None:
            msg = "watch worker has no stdin"
            raise RuntimeError(msg)
        stdin.write(f"{command}\n".encode())
        await stdin.drain()

    async def expect(self, prefix: str) -> str:
        stdout = self.process.stdout
        if stdout is None:
            msg = "watch worker has no stdout"
            raise RuntimeError(msg)
        while line := (await stdout.readline()).decode():
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        msg = f"watch worker exited before {prefix.strip()}"
        raise RuntimeError(msg)


def watch_worker_count(args: argparse.Namespace) -> int:
    if args.watch_workers > 0:
        return args.watch_workers
    by_size = math.ceil(args.watchers / WATCHERS_PER_WORKER)
    return max(1, min(by_size, (os.cpu_count() or 2) // 2))


async def _start_watch_workers(
    args: argparse.Namespace, assignments: Sequence[str], keys: Sequence[str]
) -> list[WatchWorker]:
    count = watch_worker_count(args)
    shares: list[list[list[Any]]] = [[] for _ in range(count)]
    for index, channel in enumerate(assignments):
        shares[index % count].append([index, keys[index % len(keys)], channel])
    workers: list[WatchWorker] = []
    for share in shares:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tools.perf.sibylnet_scale",
            "watch-worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        worker = WatchWorker(process=process, watchers=len(share))
        await worker.send(json.dumps({"api_base_urls": args.api_base_url, "watchers": share}))
        workers.append(worker)
    await asyncio.gather(*(worker.expect(WORKER_READY) for worker in workers))
    return workers


async def watch_worker() -> None:
    """A watcher process: long-poll the watchers it is given and report what each saw.

    Reads one JSON line (base URLs and ``[index, key, channel]`` watchers), says
    ``WATCH-READY`` once every watcher holds a cursor, then takes commands:
    ``mark`` starts the measured window, ``stop`` ends it and prints the
    result as ``WATCH-RESULT <json>``.
    """
    # Only the protocol lines reach the parent; anything else this process
    # prints (a library's logging, say) goes to stderr, so it can never fill
    # the pipe the parent reads only between commands.
    protocol = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    loop = asyncio.get_running_loop()
    config = json.loads(await loop.run_in_executor(None, sys.stdin.readline))
    watchers = config["watchers"]
    api = Api(config["api_base_urls"], connections=len(watchers) + 10)
    state = WatchState()
    tasks = [
        asyncio.create_task(_watch(api, key, channel, index, len(watchers), state))
        for index, key, channel in watchers
    ]
    await state.all_ready.wait()
    idle = CpuWindow()
    protocol.write(f"{WORKER_READY}\n")
    await loop.run_in_executor(None, sys.stdin.readline)
    idle_cpu = idle.percent()
    measured = CpuWindow()
    lags: list[float] = []
    lag_stop = asyncio.Event()
    lag_task = asyncio.create_task(_loop_lag(lag_stop, lags))
    await loop.run_in_executor(None, sys.stdin.readline)
    burst_cpu = measured.percent()
    lag_stop.set()
    await lag_task
    state.stop.set()
    await asyncio.wait(tasks, timeout=WATCH_WAIT_SECONDS + 10)
    for task in tasks:
        task.cancel()
    await api.close()
    result = {
        "watchers": len(watchers),
        "idle_cpu_percent": idle_cpu,
        "burst_cpu_percent": burst_cpu,
        "burst_loop_lag": latency_summary(lags),
        "received": state.received,
        "failures": state.failures,
        "error_samples": api.error_samples,
    }
    protocol.write(f"{WORKER_RESULT} {json.dumps(result)}\n")
    protocol.close()


async def scenario_watchers(
    api: Api, probe: ServerProbe, manifest: JsonObject, args: argparse.Namespace
) -> JsonObject:
    """Idle long-poll watchers, then bursts of posts each matching watcher must see.

    The watchers run in worker processes of their own, so the client that
    measures delivery is never the bottleneck; this process only posts.
    """
    org = manifest["orgs"][BIG_ORG_INDEX]
    keys = list(org["keys"].values())
    rng = _rng(args.seed)
    watched = org["channels"][: args.watch_channels]
    assignments = [rng.choice(watched) for _ in range(args.watchers)]
    workers = await _start_watch_workers(args, assignments, keys)
    try:
        await asyncio.sleep(2)
        idle_before = await probe.sample()
        await asyncio.sleep(args.idle_seconds)
        idle = ServerProbe.delta(idle_before, await probe.sample())

        for worker in workers:
            await worker.send("mark")
        burst_before = await probe.sample()
        poster_cpu = CpuWindow()
        lags: list[float] = []
        lag_stop = asyncio.Event()
        lag_task = asyncio.create_task(_loop_lag(lag_stop, lags))
        post_failures: dict[str, int] = {}
        posted = await _bursts(api, keys, watched, rng, args, post_failures)
        await asyncio.sleep(WATCH_WAIT_SECONDS / 2)
        burst = ServerProbe.delta(burst_before, await probe.sample())
        lag_stop.set()
        await lag_task
        poster_cpu_percent = poster_cpu.percent()
        for worker in workers:
            await worker.send("stop")
        results = [json.loads(await worker.expect(WORKER_RESULT)) for worker in workers]
    finally:
        for worker in workers:
            if worker.process.returncode is None:
                worker.process.kill()
            await worker.process.wait()

    received: dict[str, dict[int, int]] = {}
    failures: dict[str, int] = {}
    for result in results:
        for message_id, seen in result["received"].items():
            received.setdefault(message_id, {}).update(
                {int(index): arrived for index, arrived in seen.items()}
            )
        for label, count in result["failures"].items():
            failures[label] = failures.get(label, 0) + count
        api.error_samples.extend(result["error_samples"][: ERROR_SAMPLES - len(api.error_samples)])
    from_send, from_stored, missed = _deliveries(posted, assignments, received)
    return {
        "watchers": len(assignments),
        "watched_channels": len(watched),
        "api_replicas": api.replicas,
        "idle": idle,
        "bursts": {
            "posts": len(posted),
            "post_latency": latency_summary(
                [(sent.stored - sent.sent) / 1e6 for sent in posted.values()]
            ),
            "post_failures": post_failures,
            "deliveries": len(from_send),
            "missed": missed,
            "post_to_delivery": latency_summary(from_send),
            "stored_to_delivery": latency_summary(from_stored),
            "server": burst,
        },
        "clients": {
            "watch_processes": [
                {
                    key: result[key]
                    for key in (
                        "watchers",
                        "idle_cpu_percent",
                        "burst_cpu_percent",
                        "burst_loop_lag",
                    )
                }
                for result in results
            ],
            "poster": {
                "burst_cpu_percent": poster_cpu_percent,
                "burst_loop_lag": latency_summary(lags),
            },
            "max_cpu_percent": max(
                [poster_cpu_percent, *(result["burst_cpu_percent"] for result in results)]
            ),
        },
        "watch_failures": failures,
    }


async def run(args: argparse.Namespace) -> JsonObject:
    manifest = _manifest(args.manifest)
    api = Api(args.api_base_url, connections=args.posters + args.burst_size + 50)
    probe = ServerProbe(api, manifest["admin_key"], args.server_pid)
    report: JsonObject = {"run_id": args.run_id, "scenarios": {}}
    try:
        for scenario in args.scenarios:
            waited_from = time.perf_counter()
            load = await _quiet_load(args)
            if args.max_load is not None and load is not None and load > args.max_load:
                report["scenarios"][scenario] = {
                    "skipped": f"load average {load} > {args.max_load} "
                    f"after waiting {args.load_wait:.0f}s"
                }
                continue
            load_waited = round(time.perf_counter() - waited_from, 1)
            before = await probe.sample()
            if scenario == "posts":
                result = await scenario_posts(api, manifest, args)
            elif scenario == "feed":
                result = await scenario_feed(api, manifest, args)
            elif scenario == "listings":
                result = await scenario_listings(api, manifest, args)
            elif scenario == "watchers":
                result = await scenario_watchers(api, probe, manifest, args)
            else:
                msg = f"unknown scenario {scenario}"
                raise ValueError(msg)
            result["server"] = result.get("server") or ServerProbe.delta(
                before, await probe.sample()
            )
            result["load_average_before"] = load
            result["load_average_after"] = load_average()
            result["load_wait_seconds"] = load_waited
            report["scenarios"][scenario] = result
            _say(json.dumps({scenario: result}, indent=2))
    finally:
        report["client_error_samples"] = api.error_samples
        await api.close()
    return report


async def _quiet_load(args: argparse.Namespace) -> float | None:
    """The 1-minute load average, once it is under ``--max-load`` or ``--load-wait`` ran out."""
    load = load_average()
    if args.max_load is None or load is None:
        return load
    deadline = time.perf_counter() + args.load_wait
    while load is not None and load > args.max_load and time.perf_counter() < deadline:
        _say(f"load average {load} > {args.max_load}; waiting")
        await asyncio.sleep(LOAD_POLL_SECONDS)
        load = load_average()
    return load


# --- distillation --------------------------------------------------------------------------------


async def distill(args: argparse.Namespace) -> JsonObject:
    """Add idle undistilled threads, then time selection and the job until none remain."""
    if any(os.environ.get(name) for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        msg = "provider keys are set; refusing to run a distillation that could spend"
        raise SystemExit(msg)
    install_core_runtime_ports()
    manifest = _manifest(args.manifest)
    rng = _rng(args.seed + 1)
    content = build_surreal_content_client()
    try:
        added = await _seed_pending(content, rng, manifest, args.pending)
        explain = await _explain_selection(content, settings.net_thread_idle_minutes)
    finally:
        await content.close()
    selection: list[float] = []
    for _ in range(args.samples):
        started = time.perf_counter()
        await idle_net_threads(idle_minutes=settings.net_thread_idle_minutes, limit=50)
        selection.append((time.perf_counter() - started) * 1000)
    runs: list[JsonObject] = []
    total_started = time.perf_counter()
    while True:
        started = time.perf_counter()
        summary = await distill_idle_net_threads({})
        runs.append({**summary, "seconds": round(time.perf_counter() - started, 2)})
        _say(json.dumps(runs[-1]))
        if summary["candidates"] == 0 or len(runs) > args.pending:
            break
    return {
        "pending_added": added,
        "load_average": load_average(),
        "selection": latency_summary(selection),
        "selection_plan": explain,
        "runs": runs,
        "total_seconds": round(time.perf_counter() - total_started, 1),
        "distilled": sum(run["distilled"] for run in runs),
        "failed": sum(run["failed"] for run in runs),
    }


async def _seed_pending(content: Any, rng: random.Random, manifest: JsonObject, count: int) -> int:
    """Post idle threads through the same write the API uses, then age them past idle."""
    threads: dict[str, list[str]] = {}
    for number in range(count):
        org = manifest["orgs"][number % len(manifest["orgs"])]
        channel = rng.choice(org["channels"])
        thread = f"pending-{number:05d}"
        threads.setdefault(org["id"], []).append(thread)
        for _ in range(4):
            await post_net_message(
                organization_id=org["id"],
                channel=channel,
                thread=thread,
                kind="message",
                body="We decided to " + " ".join(rng.choice(WORDS) for _ in range(20)),
                author_user_id=rng.choice(org["users"]),
                author_agent="scale",
            )
    for org_id in threads:
        await _checked(
            content,
            "UPDATE net_messages SET created_at = created_at - 3h "
            "WHERE organization_id = $org AND string::starts_with(thread, 'pending-') "
            "RETURN NONE; "
            "UPDATE net_threads SET last_message_at = last_message_at - 3h, "
            "first_message_at = first_message_at - 3h "
            "WHERE organization_id = $org AND string::starts_with(thread, 'pending-') "
            "RETURN NONE;",
            org=org_id,
        )
    return count


async def _explain_selection(content: Any, idle_minutes: int) -> object:
    """The engine's plan for the distillation selection, as the job issues it."""
    statement = idle_threads_statement(idle_minutes=idle_minutes).rstrip().rstrip(";")
    return await _scalar(content, f"{statement} EXPLAIN FULL;", limit=50)


# --- query plans ---------------------------------------------------------------------------------


async def plans(args: argparse.Namespace) -> JsonObject:
    """Explain and time each net read as the API issues it, straight against the store.

    No HTTP and no auth: this isolates the database's share of each route.
    Times are sequential, so they read as latency, not throughput.
    """
    manifest = _manifest(args.manifest)
    org = manifest["orgs"][BIG_ORG_INDEX]
    channels = org["channels"]
    content = build_surreal_content_client()
    report: JsonObject = {"load_average": load_average()}
    try:
        middle = await _middle_cursor(content, org["id"])
        cases: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []
        for name, chosen in (
            ("all", []),
            ("one_channel", channels[:1]),
            ("32_channels", channels[:32]),
        ):
            for mode, since in (("tail", None), ("cursor_page", middle)):
                statement, params = feed_statement(
                    organization_id=org["id"],
                    since=since,
                    channels=chosen,
                    thread=None,
                    limit=100,
                )
                if statement is not None:
                    cases.append((f"feed_{name}_{mode}", (statement,), params))
        cases.append(("channels", (CHANNEL_SUMMARY_STATEMENT,), {"organization_id": org["id"]}))
        cases.append(
            (
                "threads",
                (CHANNEL_THREADS_STATEMENT,),
                {"organization_id": org["id"], "channel": channels[0]},
            )
        )
        cases.append(
            (
                "distill_selection",
                (idle_threads_statement(idle_minutes=settings.net_thread_idle_minutes),),
                {"limit": 50},
            )
        )
        for name, statements, params in cases:
            report[name] = await _time_and_explain(content, statements, params, args.samples)
            _say(f"{name}: {report[name]['latency']}")
    finally:
        await content.close()
    return report


async def _middle_cursor(content: Any, organization_id: str) -> NetCursor:
    """A cursor halfway through the organization's history, for page reads."""
    result = await _checked(
        content,
        "RETURN { at: (SELECT VALUE created_ns FROM net_messages "
        "WHERE organization_id = $org ORDER BY created_ns ASC LIMIT 1 START 100000)[0] };",
        org=organization_id,
    )
    at = normalize_records(result)[0].get("at")
    if not isinstance(at, int):
        msg = "the big organization has no message to start a page from"
        raise SystemExit(msg)
    return NetCursor(created_ns=at, uuid="")


async def _time_and_explain(
    content: Any, statements: tuple[str, ...], params: dict[str, Any], samples: int
) -> JsonObject:
    latencies: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        await content.execute_query_batch(" ".join(statements), **params)
        latencies.append((time.perf_counter() - started) * 1000)
    selects: list[str] = []
    for statement in statements:
        if statement.startswith("SELECT"):
            selects.append(statement.rstrip().rstrip(";"))
        else:
            # A merged read: explain its first few index seeks.
            seeks = re.findall(r"\((SELECT [^;]*? LIMIT \$\w+)\)", statement)[:4]
            selects.extend(re.sub(r"\$take_\d+", "$limit", seek) for seek in seeks)
    explained = [await _checked(content, f"{select} EXPLAIN FULL;", **params) for select in selects]
    return {"latency": latency_summary(latencies), "plans": explained}


# --- plumbing ------------------------------------------------------------------------------------


def _existing_manifest(path: Path) -> JsonObject | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _manifest(path: Path) -> JsonObject:
    manifest = _existing_manifest(path)
    if manifest is None:
        msg = f"no seed manifest at {path}; run `seed` first"
        raise SystemExit(msg)
    return manifest


async def _checked(client: Any, query: str, **params: object) -> object:
    result = await client.execute_query(query, **params)
    error = query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return result


async def _scalar(client: Any, query: str, **params: object) -> Any:
    result = await _checked(client, query, **params)
    while isinstance(result, dict) and "result" in result:
        result = result["result"]
    return result


def _process_cpu_seconds(pid: int | None) -> float | None:
    if pid is None:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    except OSError:
        return None
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(fields[11]) + int(fields[12])) / ticks


def _nanos(moment: datetime) -> int:
    """Nanoseconds since the epoch, exactly, for a microsecond-precision datetime."""
    whole = moment.replace(microsecond=0)
    return int(whole.timestamp()) * 1_000_000_000 + moment.microsecond * 1_000


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="sibylnet scale harness")
    parser.add_argument("--seed", type=int, default=20261102)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d%H%M%S"))
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help="seed manifest to write or read; one per stack when several are up",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="write the organizations, keys, messages and threads")
    seed_parser.add_argument("--orgs", type=int, default=10)
    seed_parser.add_argument("--big-messages", type=int, default=200_000)
    seed_parser.add_argument("--big-channels", type=int, default=2_000)
    seed_parser.add_argument("--big-threads", type=int, default=20_000)
    seed_parser.add_argument("--big-users", type=int, default=60)
    seed_parser.add_argument("--small-messages", type=int, default=20_000)
    seed_parser.add_argument("--small-channels", type=int, default=200)
    seed_parser.add_argument("--small-threads", type=int, default=3_400)
    seed_parser.add_argument("--small-users", type=int, default=8)
    seed_parser.add_argument("--history-days", type=float, default=90.0)
    seed_parser.add_argument("--thread-sample", type=int, default=5_000)
    seed_parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help="leave the schema to the API (seeding a database for an older checkout)",
    )
    seed_parser.add_argument(
        "--reuse",
        action="store_true",
        help="do nothing when the store already holds this manifest's data",
    )

    run_parser = sub.add_parser("run", help="drive the HTTP scenarios")
    run_parser.add_argument(
        "--api-base-url",
        required=True,
        type=lambda value: [part for part in value.split(",") if part],
        help="API base URL; several, comma separated, spread the load across replicas",
    )
    run_parser.add_argument(
        "--server-pid",
        type=lambda value: [int(part) for part in value.split(",") if part],
        default=[],
        help="API process ids (comma separated) whose CPU time to report",
    )
    run_parser.add_argument(
        "--watch-workers",
        type=int,
        default=0,
        help=f"watcher processes; 0 runs one per {WATCHERS_PER_WORKER} watchers, "
        "at most half the cores",
    )
    run_parser.add_argument(
        "--scenarios",
        type=lambda value: [part for part in value.split(",") if part],
        default=["feed", "listings", "posts", "watchers"],
    )
    run_parser.add_argument(
        "--max-load",
        type=float,
        default=None,
        help="start a scenario only while the 1-minute load average is at most this",
    )
    run_parser.add_argument(
        "--load-wait",
        type=float,
        default=300.0,
        help="seconds to wait for the load to fall under --max-load before skipping a scenario",
    )
    run_parser.add_argument("--posters", type=int, default=50)
    run_parser.add_argument("--seconds", type=float, default=60.0)
    run_parser.add_argument("--samples", type=int, default=FEED_SAMPLES)
    run_parser.add_argument("--watchers", type=int, default=500)
    run_parser.add_argument("--watch-channels", type=int, default=50)
    run_parser.add_argument("--idle-seconds", type=float, default=60.0)
    run_parser.add_argument("--bursts", type=int, default=5)
    run_parser.add_argument("--burst-size", type=int, default=20)
    run_parser.add_argument("--burst-gap", type=float, default=5.0)

    distill_parser = sub.add_parser("distill", help="time distillation selection and runs")
    distill_parser.add_argument("--pending", type=int, default=500)
    distill_parser.add_argument("--samples", type=int, default=20)

    plans_parser = sub.add_parser("plans", help="explain and time the reads against the store")
    plans_parser.add_argument("--samples", type=int, default=50)

    sub.add_parser("cleanup", help="delete everything the seed wrote")
    sub.add_parser("watch-worker", help="internal: one process of watchers for `run`")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "watch-worker":
        asyncio.run(watch_worker())
        return 0
    handler = {
        "seed": seed,
        "run": run,
        "plans": plans,
        "distill": distill,
        "cleanup": cleanup,
    }[args.command]
    result = asyncio.run(handler(args))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output = RESULTS_DIR / f"sibylnet_scale_{args.command}_{args.run_id}.json"
    output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    _say(f"report: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
