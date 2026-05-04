from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl import relational_sidecar_startup


@pytest.mark.asyncio
async def test_bootstrap_relational_sidecar_support_runs_all_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_connection = AsyncMock()
    run_migrations = AsyncMock()
    recover_sources = AsyncMock()
    load_api_keys = AsyncMock()
    warning = MagicMock()

    monkeypatch.setattr(
        relational_sidecar_startup,
        "check_relational_sidecar_connection",
        check_connection,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "run_relational_sidecar_migrations",
        run_migrations,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "recover_relational_sidecar_sources",
        recover_sources,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "load_relational_sidecar_api_keys",
        load_api_keys,
    )
    monkeypatch.setattr(relational_sidecar_startup.settings, "store", "legacy")
    monkeypatch.setattr(relational_sidecar_startup.settings, "auth_store", "postgres")
    monkeypatch.setattr(relational_sidecar_startup.log, "warning", warning)

    assert await relational_sidecar_startup.bootstrap_relational_sidecar_support() is True

    warning.assert_called_once_with(
        "Legacy relational runtime is deprecated; migrate this install to SurrealDB",
        store="legacy",
        auth_store="postgres",
        suggested_store="surreal",
        suggested_auth_store="surreal",
    )
    check_connection.assert_awaited_once()
    run_migrations.assert_awaited_once()
    recover_sources.assert_awaited_once()
    load_api_keys.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_relational_sidecar_support_stops_when_postgres_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_connection = AsyncMock(side_effect=RuntimeError("boom"))
    run_migrations = AsyncMock()
    recover_sources = AsyncMock()
    load_api_keys = AsyncMock()

    monkeypatch.setattr(
        relational_sidecar_startup,
        "check_relational_sidecar_connection",
        check_connection,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "run_relational_sidecar_migrations",
        run_migrations,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "recover_relational_sidecar_sources",
        recover_sources,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "load_relational_sidecar_api_keys",
        load_api_keys,
    )
    monkeypatch.setattr(relational_sidecar_startup.settings, "store", "legacy")
    monkeypatch.setattr(relational_sidecar_startup.settings, "auth_store", "postgres")

    assert await relational_sidecar_startup.bootstrap_relational_sidecar_support() is False

    check_connection.assert_awaited_once()
    run_migrations.assert_not_awaited()
    recover_sources.assert_not_awaited()
    load_api_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_relational_sidecar_support_is_disabled_in_fully_surreal_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_connection = AsyncMock()
    run_migrations = AsyncMock()
    recover_sources = AsyncMock()
    load_api_keys = AsyncMock()
    warning = MagicMock()

    monkeypatch.setattr(
        relational_sidecar_startup,
        "check_relational_sidecar_connection",
        check_connection,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "run_relational_sidecar_migrations",
        run_migrations,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "recover_relational_sidecar_sources",
        recover_sources,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "load_relational_sidecar_api_keys",
        load_api_keys,
    )
    monkeypatch.setattr(relational_sidecar_startup.settings, "store", "surreal")
    monkeypatch.setattr(relational_sidecar_startup.settings, "auth_store", "surreal")
    monkeypatch.setattr(relational_sidecar_startup.log, "warning", warning)

    assert await relational_sidecar_startup.bootstrap_relational_sidecar_support() is False

    check_connection.assert_not_awaited()
    run_migrations.assert_not_awaited()
    recover_sources.assert_not_awaited()
    load_api_keys.assert_not_awaited()
    warning.assert_not_called()


@pytest.mark.asyncio
async def test_bootstrap_relational_sidecar_support_skips_content_startup_in_surreal_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_connection = AsyncMock()
    run_migrations = AsyncMock()
    recover_sources = AsyncMock()
    load_api_keys = AsyncMock()
    warning = MagicMock()

    monkeypatch.setattr(
        relational_sidecar_startup,
        "check_relational_sidecar_connection",
        check_connection,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "run_relational_sidecar_migrations",
        run_migrations,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "recover_relational_sidecar_sources",
        recover_sources,
    )
    monkeypatch.setattr(
        relational_sidecar_startup,
        "load_relational_sidecar_api_keys",
        load_api_keys,
    )
    monkeypatch.setattr(relational_sidecar_startup.settings, "store", "surreal")
    monkeypatch.setattr(relational_sidecar_startup.settings, "auth_store", "postgres")
    monkeypatch.setattr(relational_sidecar_startup.log, "warning", warning)

    assert await relational_sidecar_startup.bootstrap_relational_sidecar_support() is True

    warning.assert_called_once_with(
        "Legacy relational runtime is deprecated; migrate this install to SurrealDB",
        store="surreal",
        auth_store="postgres",
        suggested_store="surreal",
        suggested_auth_store="surreal",
    )
    check_connection.assert_awaited_once()
    run_migrations.assert_awaited_once()
    recover_sources.assert_not_awaited()
    load_api_keys.assert_not_awaited()
