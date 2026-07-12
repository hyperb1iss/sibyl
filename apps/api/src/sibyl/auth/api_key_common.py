"""Pure API key helpers shared across runtimes."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
from typing import Literal
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

from sibyl_core.auth.memory_policy import memory_scope_policy_key

API_KEY_ARGON2ID_MARKER = "argon2id"
API_KEY_PBKDF2_ALGORITHM = "pbkdf2-sha256"

_API_KEY_HASHER = PasswordHasher(type=Type.ID)


def api_key_memory_scope_key(memory_scope: object, scope_key: object | None) -> str:
    return memory_scope_policy_key(
        str(memory_scope),
        None if scope_key is None else str(scope_key),
    )


class ApiKeyError(ValueError):
    """API key error."""


def generate_api_key(*, live: bool = True) -> str:
    prefix = "sk_live_" if live else "sk_test_"
    return prefix + secrets.token_urlsafe(32)


def api_key_prefix(key: str, length: int = 16) -> str:
    return key[: max(1, length)]


def hash_api_key(
    key: str,
    *,
    salt: bytes | None = None,
    iterations: int = 210_000,
    algorithm: Literal["argon2id", "pbkdf2-sha256"] = API_KEY_ARGON2ID_MARKER,
) -> tuple[str, str]:
    if not key:
        raise ApiKeyError("Key is empty")
    if algorithm == API_KEY_ARGON2ID_MARKER:
        return API_KEY_ARGON2ID_MARKER, _API_KEY_HASHER.hash(key)
    if algorithm != API_KEY_PBKDF2_ALGORITHM:
        msg = f"Unsupported API key hash algorithm: {algorithm}"
        raise ApiKeyError(msg)
    salt_bytes = salt or secrets.token_bytes(16)
    dk = pbkdf2_hmac("sha256", key.encode("utf-8"), salt_bytes, iterations, dklen=32)
    return salt_bytes.hex(), dk.hex()


def verify_api_key(key: str, *, salt_hex: str, hash_hex: str, iterations: int = 210_000) -> bool:
    if not key:
        return False
    if salt_hex == API_KEY_ARGON2ID_MARKER or hash_hex.startswith("$argon2"):
        try:
            return _API_KEY_HASHER.verify(hash_hex, key)
        except (InvalidHashError, VerificationError, TypeError, ValueError):
            return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = pbkdf2_hmac("sha256", key.encode("utf-8"), salt, iterations, dklen=len(expected))
    return hmac.compare_digest(dk, expected)


@dataclass(frozen=True)
class ApiKeyMemorySpaceAuth:
    """Memory-space scope attached to an API key."""

    memory_space_id: UUID
    memory_scope: str
    scope_key: str | None = None

    @property
    def policy_key(self) -> str:
        return api_key_memory_scope_key(self.memory_scope, self.scope_key)


@dataclass(frozen=True)
class ApiKeyAuth:
    """Result of API key authentication."""

    api_key_id: UUID
    user_id: UUID
    organization_id: UUID
    scopes: list[str]
    project_ids: list[str] | None = None
    memory_space_ids: list[UUID] | None = None
    memory_spaces: list[ApiKeyMemorySpaceAuth] | None = None
    agent_id: str | None = None
    delegated_authority: str | None = None
    capability_profile: str | None = None
