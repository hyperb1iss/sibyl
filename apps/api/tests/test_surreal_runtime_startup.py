from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl import surreal_runtime_startup


@pytest.mark.asyncio
async def test_bootstrap_surreal_runtime_schemas_runs_auth_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_auth = AsyncMock()
    bootstrap_content = AsyncMock()

    monkeypatch.setattr(surreal_runtime_startup, "bootstrap_surreal_auth_schema", bootstrap_auth)
    monkeypatch.setattr(
        surreal_runtime_startup,
        "bootstrap_surreal_content_schema",
        bootstrap_content,
    )

    assert await surreal_runtime_startup.bootstrap_surreal_runtime_schemas() is True

    bootstrap_auth.assert_awaited_once()
    bootstrap_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_surreal_runtime_schemas_continues_after_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_auth = AsyncMock(side_effect=RuntimeError("auth offline"))
    bootstrap_content = AsyncMock()

    monkeypatch.setattr(surreal_runtime_startup, "bootstrap_surreal_auth_schema", bootstrap_auth)
    monkeypatch.setattr(
        surreal_runtime_startup,
        "bootstrap_surreal_content_schema",
        bootstrap_content,
    )

    assert await surreal_runtime_startup.bootstrap_surreal_runtime_schemas() is True

    bootstrap_auth.assert_awaited_once()
    bootstrap_content.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_surreal_auth_schema_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(close=AsyncMock())
    bootstrap_auth_schema = AsyncMock()

    monkeypatch.setattr(
        "sibyl.persistence.surreal.auth.build_surreal_auth_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.bootstrap_auth_schema",
        bootstrap_auth_schema,
    )

    await surreal_runtime_startup.bootstrap_surreal_auth_schema()

    bootstrap_auth_schema.assert_awaited_once_with(client)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_surreal_content_schema_closes_client_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(close=AsyncMock())
    bootstrap_content_schema = AsyncMock(side_effect=RuntimeError("schema failed"))

    monkeypatch.setattr(
        "sibyl.persistence.surreal.content.build_surreal_content_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.bootstrap_content_schema",
        bootstrap_content_schema,
    )

    with pytest.raises(RuntimeError, match="schema failed"):
        await surreal_runtime_startup.bootstrap_surreal_content_schema()

    bootstrap_content_schema.assert_awaited_once_with(client)
    client.close.assert_awaited_once()
