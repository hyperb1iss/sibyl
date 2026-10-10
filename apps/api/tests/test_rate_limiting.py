"""Tests for rate limiting configuration.

The cross-replica and frozen-Redis proofs run against a real Redis/Valkey
when SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT are set.
"""

import asyncio
import os
import socket
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from limits import parse
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from sibyl.api.rate_limit import (
    RATE_LIMITS,
    REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS,
    build_limiter,
    get_rate_limit,
    limiter,
    storage_options,
)
from sibyl.config import Settings


class TestRateLimitConfiguration:
    """Tests for rate limit configuration."""

    def test_rate_limits_defined(self) -> None:
        """All rate limit types should be defined."""
        assert "auth" in RATE_LIMITS
        assert "device_poll" in RATE_LIMITS
        assert "api" in RATE_LIMITS
        assert "search" in RATE_LIMITS
        assert "admin" in RATE_LIMITS
        assert "crawl" in RATE_LIMITS

    def test_auth_limits_are_strict(self) -> None:
        """Auth endpoints should have strict limits."""
        auth_limit = RATE_LIMITS["auth"]
        # Auth should be limited to prevent brute force
        assert auth_limit in {"5/minute", "10/minute"}

    def test_api_limits_are_reasonable(self) -> None:
        """API endpoints should have reasonable limits."""
        api_limit = RATE_LIMITS["api"]
        # Should allow ~100 requests per minute for normal use
        assert api_limit == "100/minute"

    def test_get_rate_limit_returns_correct_limit(self) -> None:
        """get_rate_limit should return correct limit for known types."""
        assert get_rate_limit("auth") == RATE_LIMITS["auth"]
        assert get_rate_limit("api") == RATE_LIMITS["api"]

    def test_get_rate_limit_defaults_to_api(self) -> None:
        """Unknown types should default to API limit."""
        assert get_rate_limit("unknown") == RATE_LIMITS["api"]

    def test_limiter_configured(self) -> None:
        """Limiter should be properly configured."""
        assert limiter is not None
        # Key function should be set
        assert limiter._key_func is not None


class TestRateLimitKeyExtraction:
    """Tests for rate limit key extraction."""

    def test_key_uses_user_id_when_authenticated(self) -> None:
        """Authenticated requests should use user ID as key."""
        from unittest.mock import MagicMock

        from sibyl.api.rate_limit import _get_key

        request = MagicMock()
        request.state.jwt_claims = {"sub": "user-123"}

        key = _get_key(request)
        assert key == "user:user-123"

    def test_key_uses_ip_when_anonymous(self) -> None:
        """Anonymous requests should use IP as key."""
        from unittest.mock import MagicMock

        from sibyl.api.rate_limit import _get_key

        request = MagicMock()
        request.state.jwt_claims = None
        request.client.host = "192.168.1.1"
        # Ensure headers are accessible for slowapi
        request.headers = {}
        request.scope = {"type": "http"}

        key = _get_key(request)
        # Should fall back to IP
        assert key == "192.168.1.1"


class TestRateLimitValues:
    """Tests for rate limit value formats."""

    def test_all_limits_have_valid_format(self) -> None:
        """All rate limits should be in valid format (N/unit)."""
        import re

        pattern = re.compile(r"^\d+/(second|minute|hour|day)$")
        for limit_type, limit_value in RATE_LIMITS.items():
            assert pattern.match(limit_value), f"Invalid format for {limit_type}: {limit_value}"

    def test_auth_stricter_than_api(self) -> None:
        """Auth limits should be stricter than API limits."""
        # Parse the numbers from the limits
        auth_num = int(RATE_LIMITS["auth"].split("/")[0])
        api_num = int(RATE_LIMITS["api"].split("/")[0])

        # Auth should allow fewer requests
        assert auth_num < api_num


def _live_redis() -> tuple[str, int]:
    host = os.environ.get("SIBYL_LIVE_REDIS_HOST", "")
    port = os.environ.get("SIBYL_LIVE_REDIS_PORT", "")
    if not host or not port:
        pytest.skip("Redis-backed rate-limit tests need SIBYL_LIVE_REDIS_HOST/PORT")
    return host, int(port)


def _admitted(storage_uri: str, *, replicas: int, requests_per_replica: int, key: str) -> int:
    """Requests the replicas admit together, each with its own limiter storage."""
    limit = parse(RATE_LIMITS["auth"])
    limiters = [FixedWindowRateLimiter(storage_from_string(storage_uri)) for _ in range(replicas)]
    return sum(limiter.hit(limit, key) for limiter in limiters for _ in range(requests_per_replica))


class TestRateLimitsAcrossReplicas:
    """Every replica counts against one limit once coordination is Redis."""

    def test_in_memory_counters_admit_the_limit_once_per_replica(self) -> None:
        storage = Settings(_env_file=None, coordination_backend="local").rate_limit_storage

        admitted = _admitted(storage, replicas=3, requests_per_replica=5, key=uuid4().hex)

        assert admitted == 15
        assert storage == "memory://"

    def test_derived_redis_storage_admits_the_limit_once_across_replicas(self) -> None:
        host, port = _live_redis()
        storage = Settings(
            _env_file=None, coordination_backend="redis", redis_host=host, redis_port=port
        ).rate_limit_storage

        admitted = _admitted(storage, replicas=3, requests_per_replica=5, key=uuid4().hex)

        assert admitted == 5
        assert storage == f"redis://{host}:{port}/4"


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _limited_logins(storage_uri: str, *, attempts: int) -> tuple[list[int], float]:
    """POST a 5/minute login ``attempts`` times; return the statuses and the
    longest the event loop went without running another task meanwhile."""
    limiter = build_limiter(storage_uri)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    @app.post("/login")
    @limiter.limit(RATE_LIMITS["auth"])
    async def login(request: Request) -> dict[str, bool]:
        return {"ok": True}

    last_beat = time.monotonic()
    longest_stall = 0.0

    async def heartbeat() -> None:
        nonlocal last_beat, longest_stall
        while True:
            now = time.monotonic()
            longest_stall = max(longest_stall, now - last_beat)
            last_beat = now
            await asyncio.sleep(0.02)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    statuses: list[int] = []
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://replica") as client:
            for _ in range(attempts):
                statuses.append((await client.post("/login")).status_code)
                await asyncio.sleep(0.05)
    finally:
        beat.cancel()
    return statuses, longest_stall


class TestRateLimitsWhenRedisFails:
    """An outage degrades to per-replica limits, never to errors or a stalled loop."""

    def test_redis_storages_get_bounded_sockets_and_others_none(self) -> None:
        bounded = {
            "socket_connect_timeout": REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS,
            "socket_timeout": REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS,
        }
        for uri in (
            "redis://valkey:6379/4",
            "rediss://:pw@valkey:6380/4",
            "redis+sentinel://sentinel:26379/main",
            "valkey://valkey:6379/4",
        ):
            assert storage_options(uri) == bounded, uri
        assert storage_options("memory://") == {}
        assert limiter._in_memory_fallback_enabled is True

    async def test_refused_redis_still_limits_and_never_errors(self) -> None:
        statuses, stall = await _limited_logins(f"redis://127.0.0.1:{_closed_port()}/4", attempts=7)

        assert statuses == [200] * 5 + [429] * 2
        assert stall < 1.0

    async def test_silent_redis_host_cannot_stall_the_event_loop(self) -> None:
        # 10.255.255.1 is unroutable: a connect there gets no answer at all,
        # which without a connect timeout blocks on the kernel's SYN retries.
        statuses, stall = await _limited_logins("redis://10.255.255.1:6379/4", attempts=7)

        assert statuses == [200] * 5 + [429] * 2
        assert stall < 2 * REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS + 0.5

    async def test_frozen_redis_cannot_stall_the_event_loop(self) -> None:
        """A Redis that accepts connections and then answers nothing."""
        import redis

        host, port = _live_redis()
        admin = redis.Redis(host=host, port=port, socket_timeout=5)
        # Every client's commands wait out the pause, the limiter's included.
        admin.execute_command("CLIENT", "PAUSE", 4000, "ALL")
        try:
            statuses, stall = await _limited_logins(f"redis://{host}:{port}/4", attempts=7)
        finally:
            admin.execute_command("CLIENT", "UNPAUSE")
            admin.close()

        assert statuses == [200] * 5 + [429] * 2
        assert stall < 2 * REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS + 0.5
