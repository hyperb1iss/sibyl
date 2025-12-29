"""Core authentication primitives.

This module contains JWT and password handling that are used across
both the server and CLI. HTTP-specific auth (middleware, dependencies)
remains in sibyl-server.
"""

from sibyl_core.auth.context import AuthContext
from sibyl_core.auth.jwt import (
    create_access_token,
    decode_token_unverified,
    verify_access_token,
)
from sibyl_core.auth.passwords import hash_password, verify_password

__all__ = [
    "AuthContext",
    "create_access_token",
    "decode_token_unverified",
    "hash_password",
    "verify_access_token",
    "verify_password",
]
