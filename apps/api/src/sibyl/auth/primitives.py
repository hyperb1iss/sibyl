"""Pure auth helpers shared across runtimes."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

_SLUG_SAFE = re.compile(r"[^a-z0-9]+")
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = _SLUG_SAFE.sub("-", slug).strip("-")
    return slug or "org"


def generate_invite_token() -> str:
    return secrets.token_urlsafe(48)


def generate_device_code() -> str:
    return secrets.token_urlsafe(32)


def hash_device_code(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def generate_user_code() -> str:
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def normalize_user_code(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().upper().replace(" ", "").replace("-", "")
    if len(cleaned) != 8:
        return None
    if any(ch not in _USER_CODE_ALPHABET for ch in cleaned):
        return None
    return cleaned[:4] + "-" + cleaned[4:]


@dataclass(frozen=True)
class DeviceTokenError(Exception):
    error: str
    error_description: str | None = None
