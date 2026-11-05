from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import worker


@pytest.mark.asyncio
async def test_worker_settles_auth_and_content_indexes_before_it_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients = {
        "auth": SimpleNamespace(close=AsyncMock()),
        "content": SimpleNamespace(close=AsyncMock()),
    }
    ensure = AsyncMock(return_value=False)
    monkeypatch.setattr(worker.settings, "surreal_url", "ws://127.0.0.1:8000/rpc")
    monkeypatch.setattr(
        "sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: clients["auth"]
    )
    monkeypatch.setattr(
        "sibyl.persistence.surreal.content.build_surreal_content_client", lambda: clients["content"]
    )
    monkeypatch.setattr("sibyl_core.backends.surreal.index_engine.ensure_database_indexes", ensure)

    await worker.ensure_runtime_plane_indexes()

    assert [(call.args[0], call.kwargs["name"]) for call in ensure.await_args_list] == [
        (clients["auth"], "auth"),
        (clients["content"], "content"),
    ]
    clients["auth"].close.assert_awaited_once()
    clients["content"].close.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_rebuild_stops_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl_core.backends.surreal.index_engine import IndexRebuildFailed

    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(worker.settings, "surreal_url", "ws://127.0.0.1:8000/rpc")
    monkeypatch.setattr("sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: client)
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.index_engine.ensure_database_indexes",
        AsyncMock(side_effect=IndexRebuildFailed("REBUILD INDEX x ON y failed")),
    )

    with pytest.raises(IndexRebuildFailed):
        await worker.ensure_runtime_plane_indexes()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_embedded_engine_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> object:
        raise AssertionError("no client for the embedded engine")

    monkeypatch.setattr(worker.settings, "surreal_url", "surrealkv:///tmp/sibyl-worker-test.db")
    monkeypatch.setattr("sibyl.persistence.surreal.auth.build_surreal_auth_client", refuse)

    await worker.ensure_runtime_plane_indexes()
