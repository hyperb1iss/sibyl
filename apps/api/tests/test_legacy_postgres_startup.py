from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sibyl import legacy_postgres_startup


@pytest.mark.asyncio
async def test_bootstrap_legacy_postgres_support_runs_all_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_connection = AsyncMock()
    run_migrations = AsyncMock()
    recover_sources = AsyncMock()
    load_api_keys = AsyncMock()

    monkeypatch.setattr(
        legacy_postgres_startup,
        "check_legacy_postgres_connection",
        check_connection,
    )
    monkeypatch.setattr(
        legacy_postgres_startup,
        "run_legacy_postgres_migrations",
        run_migrations,
    )
    monkeypatch.setattr(
        legacy_postgres_startup,
        "recover_legacy_postgres_sources",
        recover_sources,
    )
    monkeypatch.setattr(
        legacy_postgres_startup,
        "load_legacy_postgres_api_keys",
        load_api_keys,
    )

    assert await legacy_postgres_startup.bootstrap_legacy_postgres_support() is True

    check_connection.assert_awaited_once()
    run_migrations.assert_awaited_once()
    recover_sources.assert_awaited_once()
    load_api_keys.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_legacy_postgres_support_stops_when_postgres_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check_connection = AsyncMock(side_effect=RuntimeError("boom"))
    run_migrations = AsyncMock()
    recover_sources = AsyncMock()
    load_api_keys = AsyncMock()

    monkeypatch.setattr(
        legacy_postgres_startup,
        "check_legacy_postgres_connection",
        check_connection,
    )
    monkeypatch.setattr(
        legacy_postgres_startup,
        "run_legacy_postgres_migrations",
        run_migrations,
    )
    monkeypatch.setattr(
        legacy_postgres_startup,
        "recover_legacy_postgres_sources",
        recover_sources,
    )
    monkeypatch.setattr(
        legacy_postgres_startup,
        "load_legacy_postgres_api_keys",
        load_api_keys,
    )

    assert await legacy_postgres_startup.bootstrap_legacy_postgres_support() is False

    check_connection.assert_awaited_once()
    run_migrations.assert_not_awaited()
    recover_sources.assert_not_awaited()
    load_api_keys.assert_not_awaited()
