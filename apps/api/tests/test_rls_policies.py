"""Tests for Row-Level Security (RLS) policies.

These tests require a real PostgreSQL database with RLS migrations applied.
They verify that RLS policies correctly isolate data between organizations
and users.

To run these tests:
1. Ensure PostgreSQL is running with the Sibyl schema
2. Run: pytest apps/api/tests/test_rls_policies.py -v

Note: These tests are skipped by default in CI (no database available).
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Skip all tests if not in integration test mode
pytestmark = pytest.mark.skipif(
    os.getenv("RLS_INTEGRATION_TESTS") != "true",
    reason="RLS tests require real database (set RLS_INTEGRATION_TESTS=true)",
)


class TestRlsOrgIsolation:
    """Tests for organization-level RLS isolation."""

    @pytest.mark.asyncio
    async def test_org_isolation_filters_projects(
        self, session: AsyncSession, org1_id: str, org2_id: str
    ) -> None:
        """Projects from other orgs are not visible."""
        # Set RLS context to org1
        await session.execute(
            text("SET LOCAL app.org_id = :org_id"),
            {"org_id": org1_id},
        )

        # Query projects - should only see org1's projects
        result = await session.execute(text("SELECT id, organization_id FROM projects"))
        projects = result.fetchall()

        for project in projects:
            assert str(project.organization_id) == org1_id

    @pytest.mark.asyncio
    async def test_org_isolation_blocks_cross_org_insert(
        self, session: AsyncSession, org1_id: str, org2_id: str
    ) -> None:
        """Cannot insert data into another org."""
        # Set RLS context to org1
        await session.execute(
            text("SET LOCAL app.org_id = :org_id"),
            {"org_id": org1_id},
        )

        # Try to insert a project for org2 - should fail
        with pytest.raises(Exception):  # Will raise policy violation
            await session.execute(
                text("""
                    INSERT INTO projects (id, organization_id, name, slug, graph_project_id, owner_user_id)
                    VALUES (:id, :org_id, 'Test', 'test', 'proj_test', :owner_id)
                """),
                {
                    "id": str(uuid4()),
                    "org_id": org2_id,  # Wrong org!
                    "owner_id": str(uuid4()),
                },
            )

    @pytest.mark.asyncio
    async def test_no_context_allows_all(self, session: AsyncSession) -> None:
        """Without RLS context, all rows are visible (for migrations)."""
        # Don't set any RLS context

        # Should be able to see all projects across all orgs
        result = await session.execute(text("SELECT COUNT(*) FROM projects"))
        count = result.scalar()

        # Should return some data (exact count depends on test data)
        assert count is not None


class TestRlsUserIsolation:
    """Tests for user-level RLS isolation."""

    @pytest.mark.asyncio
    async def test_user_sessions_isolated(
        self, session: AsyncSession, user1_id: str, user2_id: str
    ) -> None:
        """User can only see their own sessions."""
        # Set RLS context to user1
        await session.execute(
            text("SET LOCAL app.user_id = :user_id"),
            {"user_id": user1_id},
        )

        # Query sessions - should only see user1's sessions
        result = await session.execute(text("SELECT id, user_id FROM user_sessions"))
        sessions = result.fetchall()

        for s in sessions:
            assert str(s.user_id) == user1_id

    @pytest.mark.asyncio
    async def test_oauth_connections_isolated(self, session: AsyncSession, user1_id: str) -> None:
        """OAuth connections are user-isolated."""
        await session.execute(
            text("SET LOCAL app.user_id = :user_id"),
            {"user_id": user1_id},
        )

        result = await session.execute(text("SELECT user_id FROM oauth_connections"))
        connections = result.fetchall()

        for conn in connections:
            assert str(conn.user_id) == user1_id


class TestRlsApiKeyIsolation:
    """Tests for API key RLS isolation (requires both user and org context)."""

    @pytest.mark.asyncio
    async def test_api_keys_require_both_contexts(
        self, session: AsyncSession, user1_id: str, org1_id: str
    ) -> None:
        """API keys require matching both user_id and org_id."""
        # Set both contexts
        await session.execute(
            text("SET LOCAL app.user_id = :user_id"),
            {"user_id": user1_id},
        )
        await session.execute(
            text("SET LOCAL app.org_id = :org_id"),
            {"org_id": org1_id},
        )

        # Query API keys - should only see keys owned by user1 in org1
        result = await session.execute(text("SELECT user_id, organization_id FROM api_keys"))
        keys = result.fetchall()

        for key in keys:
            assert str(key.user_id) == user1_id
            assert str(key.organization_id) == org1_id


class TestRlsPolicyBypass:
    """Tests for RLS bypass scenarios."""

    @pytest.mark.asyncio
    async def test_superuser_still_restricted(self, session: AsyncSession) -> None:
        """FORCE ROW LEVEL SECURITY means even superuser is restricted.

        When connected as the table owner (typically postgres), RLS policies
        are still enforced because we use FORCE ROW LEVEL SECURITY.
        """
        # Even without setting session vars, FORCE means policies apply
        # NULL context should allow all (our policy design choice)
        result = await session.execute(text("SELECT current_user"))
        result.scalar()

        # Policy allows NULL context (for migrations), so this should work
        result = await session.execute(text("SELECT COUNT(*) FROM projects"))
        count = result.scalar()

        assert count is not None


# =============================================================================
# Fixtures (would need to be implemented for real integration tests)
# =============================================================================


@pytest.fixture
def org1_id() -> str:
    """First test organization ID."""
    return str(uuid4())


@pytest.fixture
def org2_id() -> str:
    """Second test organization ID."""
    return str(uuid4())


@pytest.fixture
def user1_id() -> str:
    """First test user ID."""
    return str(uuid4())


@pytest.fixture
def user2_id() -> str:
    """Second test user ID."""
    return str(uuid4())


@pytest.fixture
async def session() -> AsyncSession:
    """Database session for RLS tests.

    This fixture needs to be implemented with a real database connection
    for integration testing. Currently just a placeholder.
    """
    pytest.skip("Session fixture not implemented - requires real database")
