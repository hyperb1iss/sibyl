"""Redis-backed presence registry, shared by every API replica."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence

import structlog
from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from sibyl.config import settings
from sibyl.coordination.presence import PRESENCE_PREFIX, Watcher, WatcherSeen

log = structlog.get_logger()

# Every socket operation is bounded: presence rides on feed reads, and a Redis
# that stopped answering must not hold a read open past its own wait.
SOCKET_TIMEOUT_SECONDS = 2.0

# KEYS: the window set, the sighting set. ARGV: now, until, key ttl in
# milliseconds, then one member per watcher. Drops every expired watcher from
# both sets, raises each watcher's window and sighting (never lowers them),
# and keeps both keys alive for at least the longest window just written.
_NOTE = """
local now = tonumber(ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
for _, member in ipairs(expired) do
    redis.call('ZREM', KEYS[2], member)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
for index = 4, #ARGV do
    redis.call('ZADD', KEYS[1], 'GT', ARGV[2], ARGV[index])
    redis.call('ZADD', KEYS[2], 'GT', ARGV[1], ARGV[index])
end
local ttl = tonumber(ARGV[3])
for _, key in ipairs(KEYS) do
    if redis.call('PTTL', key) < ttl then
        redis.call('PEXPIRE', key, ttl)
    end
end
return #expired
"""


def _keys(organization_id: str) -> tuple[str, str]:
    base = f"{PRESENCE_PREFIX}{organization_id}"
    return f"{base}:until", f"{base}:seen"


def _member(watcher: Watcher) -> str:
    return json.dumps(
        [watcher.channel, watcher.user_id, watcher.agent, watcher.session],
        separators=(",", ":"),
    )


def _watcher(member: str) -> Watcher | None:
    try:
        channel, user_id, agent, session = json.loads(member)
    except (TypeError, ValueError):
        return None
    return Watcher(channel=channel, user_id=user_id, agent=agent, session=session)


class RedisPresenceRegistry:
    """Two sorted sets per organization: when each watcher's window closes, and when it last read.

    A member is the watcher itself, encoded as JSON, so nothing else has to
    be kept in step with the sets. Both keys expire with their longest window,
    so an organization nobody watches leaves nothing behind.
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
        watchers: Sequence[Watcher],
        *,
        now: float,
        until: float,
    ) -> None:
        """Record watchers in one atomic script, pruning what has expired."""
        if not watchers:
            return
        redis = await self._get_redis()
        if self._note is None:
            self._note = redis.register_script(_NOTE)
        ttl_ms = max(math.ceil((until - now) * 1000), 1)
        await self._note(
            keys=list(_keys(organization_id)),
            args=[now, until, ttl_ms, *(_member(watcher) for watcher in watchers)],
        )

    async def watching(self, organization_id: str, *, now: float) -> list[WatcherSeen]:
        """Watchers whose window is still open at ``now``, read in one round trip."""
        redis = await self._get_redis()
        until_key, seen_key = _keys(organization_id)
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zrangebyscore(until_key, f"({now!r}", "+inf")
            pipe.zrange(seen_key, 0, -1, withscores=True)
            live, seen = await pipe.execute()
        sightings = {member: float(score) for member, score in seen}
        found: list[WatcherSeen] = []
        for member in live:
            watcher = _watcher(member)
            if watcher is None:
                log.warning("net_presence_member_unreadable", member=member[:200])
                continue
            found.append(WatcherSeen(watcher=watcher, last_seen=sightings.get(member, now)))
        return found
