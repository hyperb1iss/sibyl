"""Async PostgreSQL connection management.

Provides async engine, session factory, and connection lifecycle.
Uses SQLAlchemy 2.0 async patterns with SQLModel.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from sibyl.config import settings

log = structlog.get_logger()

_engine: AsyncEngine | None = None
_test_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_engine(*, use_null_pool: bool = False) -> AsyncEngine:
    engine_kwargs: dict[str, Any] = {
        "echo": False if use_null_pool else settings.log_level == "DEBUG",
    }
    if use_null_pool:
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs.update(
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return create_async_engine(settings.postgres_url, **engine_kwargs)


def _get_engine() -> AsyncEngine:
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = _build_engine()
    return _engine


def _get_test_engine() -> AsyncEngine:
    global _test_engine  # noqa: PLW0603
    if _test_engine is None:
        _test_engine = _build_engine(use_null_pool=True)
    return _test_engine


def _get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory  # noqa: PLW0603
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


class _AsyncSessionFactoryProxy:
    def __call__(self, *args: Any, **kwargs: Any) -> AsyncSession:
        return _get_async_session_factory()(*args, **kwargs)


async_session_factory = _AsyncSessionFactoryProxy()


async def init_db() -> None:
    """Initialize database tables and extensions.

    Creates all SQLModel tables if they don't exist.
    Should be called once at application startup.
    """
    async with _get_engine().begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        log.info("Enabled pgvector extension")
        await conn.run_sync(SQLModel.metadata.create_all)
        log.info("Database tables initialized")


async def close_db() -> None:
    """Close database connections.

    Should be called at application shutdown.
    """
    global _engine, _test_engine, _session_factory  # noqa: PLW0603

    if _engine is not None:
        await _engine.dispose()
        _engine = None
    if _test_engine is not None:
        await _test_engine.dispose()
        _test_engine = None
    _session_factory = None
    log.info("Database connections closed")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession]:
    """Get an async database session.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(Model))

    Yields:
        AsyncSession: Database session that auto-commits on success,
            rolls back on exception.
    """
    session = _get_async_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session_dependency() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency for database sessions.

    Usage in FastAPI routes:
        @app.get("/items")
        async def get_items(session: AsyncSession = Depends(get_session_dependency)):
            ...
    """
    async with get_session() as session:
        yield session


async def check_postgres_health() -> dict[str, str | None]:
    """Check PostgreSQL connection health.

    Returns:
        dict with status and version info
    """
    try:
        async with get_session() as session:
            result = await session.execute(text("SELECT version()"))
            version = result.scalar()

            result = await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            vector_version = result.scalar()

            return {
                "status": "healthy",
                "postgres_version": str(version) if version else None,
                "pgvector_version": str(vector_version) if vector_version else None,
            }
    except Exception as e:
        log.error("PostgreSQL health check failed", error=str(e))  # noqa: TRY400
        return {
            "status": "unhealthy",
            "error": str(e),
        }
