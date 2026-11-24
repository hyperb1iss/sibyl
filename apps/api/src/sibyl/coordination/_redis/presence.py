"""Redis-backed presence registry, shared by every API replica."""

from __future__ import annotations

import json
from collections.abc import Sequence

import structlog
from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from sibyl.config import settings
from sibyl.coordination.presence import (
    MAX_ROWS_PER_READER,
    PRESENCE_PREFIX,
    Watcher,
    WatcherSeen,
)

log = structlog.get_logger()

# Every socket operation is bounded: presence rides on feed reads, and a Redis
# that stopped answering must not hold a read open past its own wait.
SOCKET_TIMEOUT_SECONDS = 2.0

# KEYS: the organization's readers, then this reader's window set and
# sighting set. ARGV: user id, window seconds, row cap, then one member per
# channel read. Times come from Redis's own clock. Drops the reader's expired
# rows, raises each read row's window and sighting (never lowers them), drops
# the reader's oldest sightings past the cap, and keeps every key alive for at
# least the window just written.
_NOTE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local window = tonumber(ARGV[2])
local until_at = now + window
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, member in ipairs(expired) do
    redis.call('ZREM', KEYS[3], member)
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
for index = 4, #ARGV do
    redis.call('ZADD', KEYS[2], 'GT', until_at, ARGV[index])
    redis.call('ZADD', KEYS[3], 'GT', now, ARGV[index])
end
local excess = redis.call('ZCARD', KEYS[3]) - tonumber(ARGV[3])
if excess > 0 then
    for _, member in ipairs(redis.call('ZRANGE', KEYS[3], 0, excess - 1)) do
        redis.call('ZREM', KEYS[2], member)
        redis.call('ZREM', KEYS[3], member)
    end
end
redis.call('ZADD', KEYS[1], 'GT', until_at, ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local ttl = math.ceil(window * 1000)
for _, key in ipairs(KEYS) do
    if redis.call('PTTL', key) < ttl then
        redis.call('PEXPIRE', key, ttl)
    end
end
return #expired
"""


def _readers_key(organization_id: str) -> str:
    return f"{PRESENCE_PREFIX}{organization_id}:readers"


def _reader_keys(organization_id: str, user_id: str) -> tuple[str, str]:
    base = f"{PRESENCE_PREFIX}{organization_id}:reader:{user_id}"
    return f"{base}:until", f"{base}:seen"


def presence_keys(organization_id: str, user_ids: Sequence[str]) -> list[str]:
    """Every key an organization's presence uses for these readers, for cleanup."""
    keys = [_readers_key(organization_id)]
    for user_id in user_ids:
        keys.extend(_reader_keys(organization_id, user_id))
    return keys


def _member(channel: str | None, agent: str | None, session: str | None) -> str:
    return json.dumps([channel, agent, session], separators=(",", ":"))


def _row(member: str) -> tuple[str | None, str | None, str | None] | None:
    try:
        channel, agent, session = json.loads(member)
    except (TypeError, ValueError):
        return None
    return channel, agent, session


def _seconds(clock: Sequence[object]) -> float:
    seconds, micros = clock
    return int(str(seconds)) + int(str(micros)) / 1_000_000


class RedisPresenceRegistry:
    """Per reader, two sorted sets: when each row's window closes, and when it was last read.

    A sorted set of readers, scored by their latest window, says whose sets to
    read. A row is its (channel, agent, session) encoded as JSON, so nothing
    else has to be kept in step with the sets. Every key expires with its
    longest window, so an organization nobody watches leaves nothing behind.
    """

    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._note: AsyncScript | None = None

    async def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis(
                host=settings.redis_host or "127.0.0.1",
                port=settings.redis_port or 6381,
                password=settings.redis_password_value or None,
                db=settings.redis_jobs_db,
                decode_responses=True,
                socket_timeout=SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
            )
            await self._redis.ping()
        return self._redis

    async def note(
        self,
        organization_id: str,
        *,
        user_id: str,
        agent: str | None,
        session: str | None,
        channels: Sequence[str | None],
        window: float,
    ) -> None:
        """Record one read in one atomic script on Redis's clock."""
        if not channels:
            return
        redis = await self._get_redis()
        if self._note is None:
            self._note = redis.register_script(_NOTE)
        await self._note(
            keys=[_readers_key(organization_id), *_reader_keys(organization_id, user_id)],
            args=[
                user_id,
                window,
                MAX_ROWS_PER_READER,
                *(_member(channel, agent, session) for channel in channels),
            ],
        )

    async def watching(self, organization_id: str) -> list[WatcherSeen]:
        """Watchers whose window is still open: the live readers, then their rows."""
        redis = await self._get_redis()
        async with redis.pipeline(transaction=False) as pipe:
            pipe.time()
            pipe.zrange(_readers_key(organization_id), 0, -1, withscores=True)
            clock, readers = await pipe.execute()
        now = _seconds(clock)
        live = [user_id for user_id, until in readers if float(until) > now]
        if not live:
            return []
        async with redis.pipeline(transaction=False) as pipe:
            for user_id in live:
                until_key, seen_key = _reader_keys(organization_id, user_id)
                pipe.zrangebyscore(until_key, f"({now!r}", "+inf")
                pipe.zrange(seen_key, 0, -1, withscores=True)
            replies = await pipe.execute()
        found: list[WatcherSeen] = []
        for index, user_id in enumerate(live):
            open_rows, seen = replies[2 * index], replies[2 * index + 1]
            sightings = {member: float(score) for member, score in seen}
            for member in open_rows:
                row = _row(member)
                if row is None or member not in sightings:
                    log.warning("net_presence_member_unreadable", member=member[:200])
                    continue
                channel, agent, session = row
                found.append(
                    WatcherSeen(
                        watcher=Watcher(
                            channel=channel, user_id=user_id, agent=agent, session=session
                        ),
                        last_seen=sightings[member],
                    )
                )
        return found
