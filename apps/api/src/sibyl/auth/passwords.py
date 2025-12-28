"""Local password hashing utilities (PBKDF2-HMAC-SHA256)."""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from hashlib import pbkdf2_hmac

from sibyl import config as config_module


class PasswordError(ValueError):
    """Password hashing/verification error."""


@dataclass(frozen=True)
class PasswordHash:
    salt_hex: str
    hash_hex: str
    iterations: int


def _peppered(password: str) -> bytes:
    pepper = config_module.settings.password_pepper.get_secret_value()
    return (password + pepper).encode("utf-8")


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int | None = None,
) -> PasswordHash:
    if not password:
        raise PasswordError("Password is empty")
    salt_bytes = salt or secrets.token_bytes(16)
    iters = iterations or int(config_module.settings.password_iterations)
    dk = pbkdf2_hmac("sha256", _peppered(password), salt_bytes, iters, dklen=32)
    return PasswordHash(salt_hex=salt_bytes.hex(), hash_hex=dk.hex(), iterations=iters)


def verify_password(password: str, *, salt_hex: str, hash_hex: str, iterations: int) -> bool:
    if not password:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = pbkdf2_hmac("sha256", _peppered(password), salt, int(iterations), dklen=len(expected))
    return hmac.compare_digest(dk, expected)
