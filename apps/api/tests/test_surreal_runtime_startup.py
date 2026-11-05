from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl import surreal_runtime_startup


@pytest.fixture(autouse=True)
def reset_schema_bootstrap_status() -> None:
    surreal_runtime_startup.reset_runtime_schema_bootstrap_status()
    yield
    surreal_runtime_startup.reset_runtime_schema_bootstrap_status()


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
    status = surreal_runtime_startup.get_runtime_schema_bootstrap_status()
    assert status.ready is True
    assert status.auth_ready is True
    assert status.content_ready is True


@pytest.mark.asyncio
async def test_bootstrap_surreal_runtime_schemas_records_auth_failure_in_development(
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

    monkeypatch.setattr(
        surreal_runtime_startup.config_module.settings, "environment", "development"
    )

    assert await surreal_runtime_startup.bootstrap_surreal_runtime_schemas() is False

    bootstrap_auth.assert_awaited_once()
    bootstrap_content.assert_awaited_once()
    status = surreal_runtime_startup.get_runtime_schema_bootstrap_status()
    assert status.ready is False
    assert status.auth_ready is False
    assert status.content_ready is True
    assert [failure.plane for failure in status.failures] == ["auth"]
    assert "auth offline" in status.failures[0].error


@pytest.mark.asyncio
async def test_bootstrap_surreal_runtime_schemas_raises_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_auth = AsyncMock(side_effect=RuntimeError("auth offline"))
    bootstrap_content = AsyncMock()

    monkeypatch.setattr(surreal_runtime_startup.config_module.settings, "environment", "production")
    monkeypatch.setattr(surreal_runtime_startup, "bootstrap_surreal_auth_schema", bootstrap_auth)
    monkeypatch.setattr(
        surreal_runtime_startup,
        "bootstrap_surreal_content_schema",
        bootstrap_content,
    )

    with pytest.raises(surreal_runtime_startup.RuntimeSchemaBootstrapError) as exc_info:
        await surreal_runtime_startup.bootstrap_surreal_runtime_schemas()

    bootstrap_auth.assert_awaited_once()
    bootstrap_content.assert_awaited_once()
    assert "auth v" in str(exc_info.value)
    status = surreal_runtime_startup.get_runtime_schema_bootstrap_status()
    assert status.ready is False
    assert status.content_ready is True


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


@pytest.mark.asyncio
async def test_a_failed_index_rebuild_stops_startup_even_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sibyl_core.backends.surreal.index_engine import IndexRebuildFailed

    monkeypatch.setattr(
        surreal_runtime_startup.config_module.settings, "environment", "development"
    )
    monkeypatch.setattr(surreal_runtime_startup, "bootstrap_surreal_auth_schema", AsyncMock())
    monkeypatch.setattr(
        surreal_runtime_startup,
        "bootstrap_surreal_content_schema",
        AsyncMock(side_effect=IndexRebuildFailed("REBUILD INDEX raw_uuid ON raw failed")),
    )

    with pytest.raises(surreal_runtime_startup.RuntimeSchemaBootstrapError) as exc_info:
        await surreal_runtime_startup.bootstrap_surreal_runtime_schemas()

    assert "REBUILD INDEX raw_uuid" in str(exc_info.value)
    assert surreal_runtime_startup.get_runtime_schema_bootstrap_status().ready is False
