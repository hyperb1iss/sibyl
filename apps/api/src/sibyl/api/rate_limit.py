"""Rate limiting configuration for API endpoints."""

from typing import cast
from urllib.parse import urlsplit

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from sibyl.config import settings

# slowapi asks its storage synchronously, on the event loop. Against Redis that
# is a sub-millisecond call, but an unreachable or silent Redis would stall
# every request for as long as the socket waits (a dropped connect waits on
# the kernel's SYN retries, tens of seconds). Bounding each call keeps an
# outage to one short wait, after which the in-memory fallback takes over.
REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS = 0.5
_REDIS_STORAGE_SCHEMES = frozenset({"redis", "rediss", "valkey", "valkeys"})


def _get_key(request: Request) -> str:
    """Get rate limit key from request.

    Uses JWT user ID if authenticated, otherwise falls back to the client
    address. That address is the one uvicorn resolved from X-Forwarded-For
    when the direct peer is a trusted proxy (SIBYL_FORWARDED_ALLOW_IPS), so
    users behind one ingress get their own buckets instead of sharing the
    proxy's. This prevents authenticated users from being grouped with
    anonymous traffic.
    """
    # Try to get user ID from JWT claims
    claims = getattr(request.state, "jwt_claims", None)
    if claims and "sub" in claims:
        return f"user:{claims['sub']}"

    # Fall back to IP address
    return get_remote_address(request)


def storage_options(storage_uri: str) -> dict[str, float]:
    """Socket bounds for a Redis-family storage; other storages take none."""
    scheme = urlsplit(storage_uri).scheme.split("+", 1)[0]
    if scheme not in _REDIS_STORAGE_SCHEMES:
        return {}
    return {
        "socket_connect_timeout": REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS,
        "socket_timeout": REDIS_STORAGE_SOCKET_TIMEOUT_SECONDS,
    }


def build_limiter(storage_uri: str) -> Limiter:
    """A limiter on ``storage_uri`` that keeps limiting when that storage fails.

    When the storage raises (Redis refused, timed out or gone), slowapi
    switches to an in-memory copy of the same limits, so each replica keeps
    enforcing them on its own instead of failing the request, and it probes
    the storage again with backoff until it answers.
    """
    return Limiter(
        key_func=_get_key,
        default_limits=[settings.rate_limit_default] if settings.rate_limit_default else [],
        storage_uri=storage_uri,
        # slowapi annotates these as strings; limits passes them to redis-py as-is.
        storage_options=cast("dict[str, str]", storage_options(storage_uri)),
        in_memory_fallback_enabled=True,
        strategy="fixed-window",
    )


# Global limiter instance
limiter = build_limiter(settings.rate_limit_storage or "memory://")

# Rate limit decorators for different endpoint types
# Usage: @limits.auth on auth endpoints, @limits.api on regular API endpoints

RATE_LIMITS = {
    # Auth endpoints - stricter limits to prevent brute force
    "auth": "5/minute",
    # Device auth polling - allow frequent polling
    "device_poll": "60/minute",
    # Standard API endpoints
    "api": "100/minute",
    # Search/heavy endpoints
    "search": "30/minute",
    # Admin endpoints
    "admin": "60/minute",
    # Crawl/ingestion endpoints
    "crawl": "10/minute",
}


def get_rate_limit(endpoint_type: str) -> str:
    """Get rate limit string for endpoint type."""
    return RATE_LIMITS.get(endpoint_type, RATE_LIMITS["api"])
