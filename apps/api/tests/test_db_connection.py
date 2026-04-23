from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy.ext.asyncio as sa_asyncio

from sibyl.db import connection


def _reload_connection_module():
    return importlib.reload(connection)


def test_db_connection_defers_engine_creation_until_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patcher:
        create_engine = MagicMock()
        patcher.setattr(sa_asyncio, "create_async_engine", create_engine)

        reloaded = _reload_connection_module()

        assert reloaded._engine is None
        assert reloaded._test_engine is None
        assert create_engine.call_count == 0

    _reload_connection_module()


@pytest.mark.asyncio
async def test_get_session_initializes_engine_and_factory_lazily_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patcher:
        engine = MagicMock()
        create_engine = MagicMock(return_value=engine)
        patcher.setattr(sa_asyncio, "create_async_engine", create_engine)

        reloaded = _reload_connection_module()

        session_one = SimpleNamespace(
            commit=AsyncMock(),
            rollback=AsyncMock(),
            close=AsyncMock(),
        )
        session_two = SimpleNamespace(
            commit=AsyncMock(),
            rollback=AsyncMock(),
            close=AsyncMock(),
        )
        session_factory = MagicMock(side_effect=[session_one, session_two])
        builder = MagicMock(return_value=session_factory)
        patcher.setattr(reloaded, "async_sessionmaker", builder)

        async with reloaded.get_session() as session:
            assert session is session_one

        async with reloaded.get_session() as session:
            assert session is session_two

        assert create_engine.call_count == 1
        builder.assert_called_once()
        session_factory.assert_called()
        session_one.commit.assert_awaited_once()
        session_one.close.assert_awaited_once()
        session_two.commit.assert_awaited_once()
        session_two.close.assert_awaited_once()

    _reload_connection_module()
