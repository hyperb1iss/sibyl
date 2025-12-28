"""Async PostgreSQL connection management.

Provides async engine, session factory, and connection lifecycle.
Uses SQLAlchemy 2.0 async patterns with SQLModel.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from sibyl.config import settings

log = structlog.get_logger()

# =============================================================================
# Engine Configuration
# =============================================================================

# Create async engine with connection pooling
# Using asyncpg driver for PostgreSQL
_engine = create_async_engine(
    settings.postgres_url,
    echo=settings.log_level == "DEBUG",
    pool_size=settings.postgres_pool_size,
    max_overflow=settings.postgres_max_overflow,
    pool_pre_ping=True,  # Verify connections before use
    pool_recycle=3600,  # Recycle connections after 1 hour
)

# For testing - use NullPool to avoid connection issues
_test_engine = create_async_engine(
    settings.postgres_url,
    echo=False,
    poolclass=NullPool,
)

# Session factory - creates new sessions for each request
async_session_factory = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# =============================================================================
# Connection Lifecycle
# =============================================================================


async def init_db() -> None:
    """Initialize database tables and extensions.

    Creates all SQLModel tables if they don't exist.
    Should be called once at application startup.
    """
    async with _engine.begin() as conn:
        # Enable pgvector extension
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        log.info("Enabled pgvector extension")

        # Create all tables
        await conn.run_sync(SQLModel.metadata.create_all)
        log.info("Database tables initialized")


async def close_db() -> None:
    """Close database connections.

    Should be called at application shutdown.
    """
    await _engine.dispose()
    log.info("Database connections closed")


# =============================================================================
# Session Management
# =============================================================================


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
    session = async_session_factory()
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


# =============================================================================
# Health Check
# =============================================================================


async def check_postgres_health() -> dict[str, str | None]:
    """Check PostgreSQL connection health.

    Returns:
        dict with status and version info
    """
    try:
        async with get_session() as session:
            result = await session.execute(text("SELECT version()"))
            version = result.scalar()

            # Check pgvector extension
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
