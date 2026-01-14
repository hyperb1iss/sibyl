"""Sibyl database module - PostgreSQL + pgvector for document storage.

This module provides:
- SQLModel schemas for documents, chunks, and embeddings
- Async connection management with SQLAlchemy 2.0
- Hybrid search (dense vectors + BM25 full-text)

Usage:
    from sibyl.db import get_session, CrawlSource, CrawledDocument, DocumentChunk

    async with get_session() as session:
        source = CrawlSource(name="FastAPI Docs", url="https://fastapi.tiangolo.com")
        session.add(source)
        await session.commit()
"""

from sibyl.db.connection import (
    async_session_factory,
    check_postgres_health,
    close_db,
    get_session,
    get_session_dependency,
    init_db,
)
from sibyl.db.models import (
    AgentMessage,
    AgentMessageRole,
    AgentMessageType,
    AgentOperationalStatus,
    AgentState,
    ApiKey,
    AuditLog,
    ChunkType,
    CrawledDocument,
    CrawlSource,
    CrawlStatus,
    DocumentChunk,
    InterAgentMessage,
    InterAgentMessageType,
    OrchestratorOperationalStatus,
    OrchestratorPhase,
    OrchestratorState,
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    OrganizationRole,
    Runner,
    RunnerProject,
    RunnerStatus,
    SourceType,
    User,
)

__all__ = [
    # Connection
    "init_db",
    "close_db",
    "get_session",
    "get_session_dependency",
    "async_session_factory",
    "check_postgres_health",
    # Models
    "AgentMessage",
    "AgentState",
    "ApiKey",
    "AuditLog",
    "CrawlSource",
    "CrawledDocument",
    "DocumentChunk",
    "InterAgentMessage",
    "OrchestratorState",
    "Organization",
    "OrganizationMember",
    "OrganizationRole",
    "OrganizationInvitation",
    "Runner",
    "RunnerProject",
    "User",
    # Enums
    "AgentMessageRole",
    "AgentMessageType",
    "AgentOperationalStatus",
    "ChunkType",
    "CrawlStatus",
    "InterAgentMessageType",
    "OrchestratorOperationalStatus",
    "OrchestratorPhase",
    "RunnerStatus",
    "SourceType",
]
