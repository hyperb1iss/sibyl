"""Auth-specific distributed lock scopes."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sibyl.locks import entity_lock

_AUTH_LOCK_SCOPE = "auth"
_FIRST_USER_ADMIN_LOCK = "bootstrap:first-user-admin"


def _digest(value: object) -> str:
    normalized = str(value).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


@asynccontextmanager
async def first_user_admin_lock() -> AsyncIterator[str | None]:
    async with entity_lock(_AUTH_LOCK_SCOPE, _FIRST_USER_ADMIN_LOCK) as token:
        yield token


@asynccontextmanager
async def signup_email_lock(email: str) -> AsyncIterator[str | None]:
    async with entity_lock(_AUTH_LOCK_SCOPE, f"signup-email:{_digest(email)}") as token:
        yield token


@asynccontextmanager
async def oauth_identity_lock(provider: str, subject: object) -> AsyncIterator[str | None]:
    key = f"oauth:{provider.strip().lower()}:{_digest(subject)}"
    async with entity_lock(_AUTH_LOCK_SCOPE, key) as token:
        yield token
