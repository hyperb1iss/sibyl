"""Rate limiting configuration for API endpoints."""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from sibyl.config import settings


def _get_key(request: Request) -> str:
    """Get rate limit key from request.

    Uses JWT user ID if authenticated, otherwise falls back to IP address.
    This prevents authenticated users from being grouped with anonymous traffic.
    """
    # Try to get user ID from JWT claims
    claims = getattr(request.state, "jwt_claims", None)
    if claims and "sub" in claims:
        return f"user:{claims['sub']}"

    # Fall back to IP address
    return get_remote_address(request)


# Global limiter instance
limiter = Limiter(
    key_func=_get_key,
    default_limits=[settings.rate_limit_default] if settings.rate_limit_default else [],
    storage_uri=settings.rate_limit_storage or "memory://",
    strategy="fixed-window",
)

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
