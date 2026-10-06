"""api_keys.last_used_at is written once per key per window, off the request.

The marker is advisory (only the key list reads it), so a burst of
authentications from one agent owes the store a single coarse UPDATE, and a
request never waits for it or fails because of it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from sibyl.auth.api_key_common import api_key_prefix, hash_api_key
from sibyl.persistence.surreal.auth_runtime import api_keys

RAW_KEY = "sk_live_debounce-probe-key"


class _ApiKeyStore:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.update_gate: asyncio.Event | None = None
        self.update_error: Exception | None = None
        self.updates_finished = 0

    async def execute_query(self, query: str, **params: object) -> object:
        self.calls.append((query, params))
        if "FROM api_keys WHERE key_prefix" in query:
            return [dict(self.row)]
        if "UPDATE api_keys" in query:
            if self.update_gate is not None:
                await self.update_gate.wait()
            if self.update_error is not None:
                raise self.update_error
            self.updates_finished += 1
        return []

    @property
    def update_calls(self) -> list[dict[str, object]]:
        return [params for query, params in self.calls if "UPDATE api_keys" in query]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _ApiKeyStore:
    salt_hex, hash_hex = hash_api_key(RAW_KEY)
    fake = _ApiKeyStore(
        {
            "uuid": str(uuid4()),
            "user_id": str(uuid4()),
            "organization_id": str(uuid4()),
            "key_prefix": api_key_prefix(RAW_KEY),
            "key_salt": salt_hex,
            "key_hash": hash_hex,
            "scopes": ["mcp"],
            "revoked_at": None,
            "expires_at": None,
        }
    )

    @asynccontextmanager
    async def scope():
        yield fake

    monkeypatch.setattr(api_keys, "_auth_client_scope", scope)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1_000.0]
    monkeypatch.setattr(api_keys, "_monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_fifty_authentications_in_one_window_write_last_used_once(
    store: _ApiKeyStore,
) -> None:
    for _ in range(50):
        assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    await api_keys.drain_last_used_writes()

    assert len(store.update_calls) == 1
    (params,) = store.update_calls
    assert params["api_key_id"] == store.row["uuid"]
    assert params["last_used_at"] == params["updated_at"]


@pytest.mark.asyncio
async def test_next_window_writes_again(store: _ApiKeyStore, clock: list[float]) -> None:
    assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    clock[0] += api_keys.LAST_USED_WRITE_INTERVAL_SECONDS - 1
    assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    await api_keys.drain_last_used_writes()
    assert len(store.update_calls) == 1

    clock[0] += 1
    assert await api_keys.authenticate_api_key(RAW_KEY) is not None
    await api_keys.drain_last_used_writes()
    assert len(store.update_calls) == 2


@pytest.mark.asyncio
async def test_request_never_waits_for_the_write(store: _ApiKeyStore) -> None:
    store.update_gate = asyncio.Event()

    auth = await asyncio.wait_for(api_keys.authenticate_api_key(RAW_KEY), timeout=2.0)

    assert auth is not None
    assert store.updates_finished == 0
    store.update_gate.set()
    await api_keys.drain_last_used_writes()
    assert store.updates_finished == 1


@pytest.mark.asyncio
async def test_failed_write_is_logged_and_dropped(
    store: _ApiKeyStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.update_error = RuntimeError("transaction conflict")

    with caplog.at_level(logging.WARNING, logger=api_keys.__name__):
        assert await api_keys.authenticate_api_key(RAW_KEY) is not None
        await api_keys.drain_last_used_writes()
        assert await api_keys.authenticate_api_key(RAW_KEY) is not None
        await api_keys.drain_last_used_writes()

    assert len(store.update_calls) == 1, "a dropped write is not retried inside its window"
    assert any("Dropped api_keys.last_used_at write" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_write_windows_expire_and_stay_bounded(
    store: _ApiKeyStore, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_keys, "LAST_USED_WINDOW_MAX_KEYS", 3)
    for _ in range(5):
        api_keys._schedule_last_used_write(uuid4())
    assert len(api_keys._last_used_written_at) == 3

    clock[0] += api_keys.LAST_USED_WRITE_INTERVAL_SECONDS
    api_keys._schedule_last_used_write(uuid4())
    assert len(api_keys._last_used_written_at) == 1, "closed windows are dropped"
    await api_keys.drain_last_used_writes()
