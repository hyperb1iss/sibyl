"""Bootstrap the remaining PostgreSQL-backed services during startup."""

from __future__ import annotations

import structlog
from sqlalchemy import text

from sibyl.config import settings

log = structlog.get_logger()


async def check_legacy_postgres_connection() -> None:
    from sibyl.db.connection import get_session

    async with get_session() as session:
        await session.execute(text("SELECT 1"))


async def run_legacy_postgres_migrations() -> None:
    from sibyl.db.migrations import run_migrations

    await run_migrations()


async def recover_legacy_postgres_sources() -> None:
    from sibyl.api.routes.admin import recover_stuck_sources

    await recover_stuck_sources()


async def load_legacy_postgres_api_keys() -> None:
    from sibyl.services.settings import load_api_keys_from_db

    await load_api_keys_from_db()


async def bootstrap_legacy_postgres_support() -> bool:
    try:
        await check_legacy_postgres_connection()
    except Exception as exc:
        log.warning("PostgreSQL unavailable at startup", error=str(exc), store=settings.store)
        return False

    log.info("PostgreSQL connected", host=settings.postgres_host, store=settings.store)

    try:
        await run_legacy_postgres_migrations()
    except Exception:
        log.exception("Database migration failed")
        raise

    try:
        await recover_legacy_postgres_sources()
    except Exception as exc:
        log.warning("Source recovery failed", error=str(exc), store=settings.store)

    try:
        await load_legacy_postgres_api_keys()
    except Exception as exc:
        log.warning("API key preload failed", error=str(exc), store=settings.store)

    return True


__all__ = ["bootstrap_legacy_postgres_support"]
