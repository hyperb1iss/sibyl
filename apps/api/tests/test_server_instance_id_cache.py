"""The server replay identity is read once per process.

Every replayed CLI write validated X-Sibyl-Server-Instance with a fresh
SELECT of an immutable row. The value only changes when an archive restore
replaces the data instance, which resets the cache.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import auth_archive
from sibyl.persistence.surreal.auth_runtime import _common as auth_common


class _IdentityStore:
    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.reads = 0

    async def execute_query(self, query: str, **_params: object) -> object:
        if "FROM server_identity" in query:
            self.reads += 1
            return [{"instance_id": self.instance_id}]
        return []


class _StaticScope:
    def __init__(self, client: object) -> None:
        self._client = client

    async def __aenter__(self) -> object:
        return self._client

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _IdentityStore:
    fake = _IdentityStore(str(uuid4()))
    monkeypatch.setattr(auth_common, "_auth_client_scope", lambda: _StaticScope(fake))
    return fake


@pytest.mark.asyncio
async def test_identity_is_read_once_per_process(store: _IdentityStore) -> None:
    first = await auth_common.get_server_instance_id()
    second = await auth_common.get_server_instance_id()

    assert first == second == store.instance_id
    assert store.reads == 1


@pytest.mark.asyncio
async def test_reset_reads_the_identity_again(store: _IdentityStore) -> None:
    assert await auth_common.get_server_instance_id() == store.instance_id

    store.instance_id = str(uuid4())
    auth_common.reset_server_instance_id_cache()

    assert await auth_common.get_server_instance_id() == store.instance_id
    assert store.reads == 2


@pytest.mark.asyncio
async def test_archive_restore_replacing_the_identity_resets_the_cache(
    store: _IdentityStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_archive, "backfill_api_key_scope_restrictions", AsyncMock())
    stale = await auth_common.get_server_instance_id()
    restored = str(uuid4())
    store.instance_id = restored

    await auth_archive._finalize_auth_restore(store, source_instance_id=restored, api_key_ids=[])

    assert await auth_common.get_server_instance_id() == restored != stale
