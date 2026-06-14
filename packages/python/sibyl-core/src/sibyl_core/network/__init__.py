"""Network primitives shared across Sibyl runtimes."""

from sibyl_core.network.safe_fetch import (
    SAFE_FETCH_MAX_BYTES,
    SAFE_FETCH_MAX_REDIRECTS,
    SAFE_FETCH_TIMEOUT_SECONDS,
    SafeFetchResponse,
    decode_safe_fetch_body,
    normalize_safe_url,
    safe_fetch,
)

__all__ = [
    "SAFE_FETCH_MAX_BYTES",
    "SAFE_FETCH_MAX_REDIRECTS",
    "SAFE_FETCH_TIMEOUT_SECONDS",
    "SafeFetchResponse",
    "decode_safe_fetch_body",
    "normalize_safe_url",
    "safe_fetch",
]
