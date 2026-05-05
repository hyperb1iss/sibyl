"""Compatibility wrapper for legacy SQL API key management."""

from __future__ import annotations

from sibyl.persistence.legacy.auth_managers.api_keys import (
    ApiKeyAuth,
    ApiKeyError,
    ApiKeyManager,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)

__all__ = [
    "ApiKeyAuth",
    "ApiKeyError",
    "ApiKeyManager",
    "api_key_prefix",
    "generate_api_key",
    "hash_api_key",
    "verify_api_key",
]
