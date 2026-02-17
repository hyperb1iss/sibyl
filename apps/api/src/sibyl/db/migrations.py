"""Database migration utilities.

Auto-runs Alembic migrations on server startup to ensure schema is current.
"""

import asyncio
import os
from pathlib import Path

import structlog

log = structlog.get_logger()


def _run_migrations_sync() -> None:
    """Run Alembic migrations (synchronous)."""
    from alembic import command
    from alembic.config import Config

    # In Docker: /app/alembic.ini
    # In dev: find relative to source
    alembic_ini = Path(os.environ.get("ALEMBIC_CONFIG", "/app/alembic.ini"))

    # Fallback for development (source tree)
    if not alembic_ini.exists():
        # Walk up from apps/api/src/sibyl/db/migrations.py until we find apps/api/alembic.ini.
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "alembic.ini"
            if candidate.exists():
                alembic_ini = candidate
                break

    if not alembic_ini.exists():
        log.warning("alembic.ini not found, skipping migrations", path=str(alembic_ini))
        return

    log.info("Running database migrations...", config=str(alembic_ini))
    alembic_cfg = Config(str(alembic_ini))
    command.upgrade(alembic_cfg, "head")
    log.info("Database migrations complete")


async def run_migrations() -> None:
    """Run database migrations on startup (async wrapper)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_migrations_sync)
