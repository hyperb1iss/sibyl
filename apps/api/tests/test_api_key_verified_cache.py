"""API-key authentication pays the argon2id KDF once per key, off the loop.

Every ``authenticate_api_key`` call still reads the api_keys row, so the
revocation, expiry and scope semantics are observed here against a store
whose row the test mutates between calls; only the KDF verdict is memoized.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest

from sibyl.auth.api_key_cache import (
    VerifiedApiKeyCache,
    api_key_digest,
    api_key_hash_fingerprint,
    verified_api_key_cache,
)
from sibyl.auth.api_key_common import api_key_prefix, hash_api_key
from sibyl.persistence.surreal.auth_runtime import api_keys
from sibyl_core.backends.surreal.records import utcnow

RAW_KEY = "sk_live_cache-probe-key"


class _ApiKeyStore:
    """One api_keys row plus empty scope tables, served by query shape."""

    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        if "FROM api_keys WHERE key_prefix" in query:
            return [dict(self.row)] if self.row["key_prefix"] == params["key_prefix"] else []
        return []


def _row(raw_key: str = RAW_KEY) -> dict[str, object]:
    salt_hex, hash_hex = hash_api_key(raw_key)
    return {
        "uuid": str(uuid4()),
        "user_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "key_prefix": api_key_prefix(raw_key),
        "key_salt": salt_hex,
        "key_hash": hash_hex,
        "scopes": ["mcp"],
        "revoked_at": None,
        "expires_at": None,
    }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _ApiKeyStore:
    fake = _ApiKeyStore(_row())

    @asynccontextmanager
    async def scope():
        yield fake

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    return fake


@pytest.fixture
def kdf_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real_verify = api_keys.verify_api_key

    def counting_verify(raw_key: str, *, salt_hex: str, hash_hex: str) -> bool:
        calls.append(raw_key)
        return real_verify(raw_key, salt_hex=salt_hex, hash_hex=hash_hex)

    monkeypatch.setattr(api_keys, "verify_api_key", counting_verify)
    return calls


@pytest.mark.asyncio
async def test_repeat_authentication_skips_the_kdf(
    store: _ApiKeyStore, kdf_calls: list[str]
) -> None:
    results = [await api_keys.authenticate_api_key(RAW_KEY) for _ in range(3)]

    assert all(auth is not None for auth in results)
    assert {str(auth.api_key_id) for auth in results if auth} == {store.row["uuid"]}
    assert kdf_calls == [RAW_KEY]
    # The row is still read on every call: the cache memoizes the verdict,
    # never the record.
    assert sum("FROM api_keys WHERE key_prefix" in query for query, _ in store.calls) == 3


@pytest.mark.asyncio
async def test_rotated_stored_hash_is_verified_again(
    store: _ApiKeyStore, kdf_calls: list[str]
) -> None:
    assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    assert kdf_calls == [RAW_KEY]

    store.row["key_salt"], store.row["key_hash"] = hash_api_key(RAW_KEY)
    assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    assert kdf_calls == [RAW_KEY, RAW_KEY]

    store.row["key_salt"], store.row["key_hash"] = hash_api_key("sk_live_someone-else")
    assert await api_keys.authenticate_api_key(RAW_KEY) is None
    assert kdf_calls == [RAW_KEY, RAW_KEY, RAW_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["revoked_at", "expires_at"])
async def test_revoked_or_expired_key_is_rejected_even_when_cached(
    store: _ApiKeyStore, kdf_calls: list[str], field: str
) -> None:
    assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    assert len(verified_api_key_cache) == 1

    store.row[field] = utcnow() - timedelta(seconds=1)

    assert await api_keys.authenticate_api_key(RAW_KEY) is None
    assert kdf_calls == [RAW_KEY]


@pytest.mark.asyncio
async def test_failed_verification_is_not_cached(store: _ApiKeyStore, kdf_calls: list[str]) -> None:
    wrong = RAW_KEY[:16] + "-wrong-suffix"
    assert api_key_prefix(wrong) == store.row["key_prefix"]

    assert await api_keys.authenticate_api_key(wrong) is None
    assert await api_keys.authenticate_api_key(wrong) is None

    assert kdf_calls == [wrong, wrong]
    assert len(verified_api_key_cache) == 0


@pytest.mark.asyncio
async def test_kdf_runs_off_the_event_loop(
    store: _ApiKeyStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    verify_threads: list[int] = []

    def slow_verify(raw_key: str, *, salt_hex: str, hash_hex: str) -> bool:
        verify_threads.append(threading.get_ident())
        time.sleep(0.05)
        return True

    monkeypatch.setattr(api_keys, "verify_api_key", slow_verify)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    pulse = asyncio.create_task(ticker())
    try:
        assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    finally:
        pulse.cancel()

    assert verify_threads
    assert verify_threads[0] != loop_thread
    # A verify on the loop thread would freeze the ticker for its whole run.
    assert ticks > 2


def test_cache_honors_ttl_bound_and_key_invalidation() -> None:
    cache = VerifiedApiKeyCache(max_entries=2, ttl_seconds=10.0)
    fingerprint = api_key_hash_fingerprint(salt_hex="argon2id", hash_hex="$argon2id$x")

    cache.store(api_key_digest("a"), api_key_id="key-a", hash_fingerprint=fingerprint, now=0.0)
    assert cache.get(api_key_digest("a"), now=9.0) is not None
    assert cache.get(api_key_digest("a"), now=10.0) is None

    cache.store(api_key_digest("a"), api_key_id="key-a", hash_fingerprint=fingerprint, now=0.0)
    cache.store(api_key_digest("b"), api_key_id="key-b", hash_fingerprint=fingerprint, now=0.0)
    assert cache.get(api_key_digest("a"), now=1.0) is not None
    cache.store(api_key_digest("c"), api_key_id="key-c", hash_fingerprint=fingerprint, now=1.0)
    assert cache.get(api_key_digest("b"), now=1.0) is None, "least recently used entry is evicted"
    assert cache.get(api_key_digest("a"), now=1.0) is not None

    cache.invalidate_key("key-a")
    assert cache.get(api_key_digest("a"), now=1.0) is None
    assert cache.get(api_key_digest("c"), now=1.0) is not None
