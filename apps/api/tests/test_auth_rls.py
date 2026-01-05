"""Tests for RLS session variable management."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl.auth.rls import (
    get_rls_session,
    require_rls_session,
    set_rls_context,
)


class TestSetRlsContext:
    """Tests for set_rls_context function."""

    @pytest.mark.asyncio
    async def test_sets_user_id(self) -> None:
        """Sets app.user_id when user_id is provided."""
        session = AsyncMock()
        user_id = uuid4()

        await set_rls_context(session, user_id=user_id)

        # Check that set_config was called with user_id
        calls = session.execute.call_args_list
        assert len(calls) >= 1

        # Find the set_config app.user_id call
        user_call = None
        for call in calls:
            sql = str(call[0][0])
            if "app.user_id" in sql and "set_config" in sql:
                user_call = call
                break

        assert user_call is not None
        assert user_call[0][1]["user_id"] == str(user_id)

    @pytest.mark.asyncio
    async def test_sets_org_id(self) -> None:
        """Sets app.org_id when org_id is provided."""
        session = AsyncMock()
        org_id = uuid4()

        await set_rls_context(session, org_id=org_id)

        calls = session.execute.call_args_list
        org_call = None
        for call in calls:
            sql = str(call[0][0])
            if "app.org_id" in sql and "set_config" in sql:
                org_call = call
                break

        assert org_call is not None
        assert org_call[0][1]["org_id"] == str(org_id)

    @pytest.mark.asyncio
    async def test_sets_both_ids(self) -> None:
        """Sets both user_id and org_id."""
        session = AsyncMock()
        user_id = uuid4()
        org_id = uuid4()

        await set_rls_context(session, user_id=user_id, org_id=org_id)

        # Should have at least 2 calls (one for each)
        assert session.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_resets_when_none(self) -> None:
        """Sets empty string when IDs are None (effectively resetting)."""
        session = AsyncMock()

        await set_rls_context(session, user_id=None, org_id=None)

        # Should call set_config with empty string for both
        calls = session.execute.call_args_list
        assert len(calls) == 2  # One for user_id, one for org_id

        # Check both params are empty strings
        user_call = calls[0]
        org_call = calls[1]
        assert user_call[0][1]["user_id"] == ""
        assert org_call[0][1]["org_id"] == ""

    @pytest.mark.asyncio
    async def test_accepts_string_ids(self) -> None:
        """Accepts string IDs as well as UUIDs."""
        session = AsyncMock()
        user_id = str(uuid4())
        org_id = str(uuid4())

        await set_rls_context(session, user_id=user_id, org_id=org_id)

        # Should work without error
        assert session.execute.call_count >= 2


class TestGetRlsSession:
    """Tests for get_rls_session dependency."""

    @pytest.mark.asyncio
    async def test_yields_session(self) -> None:
        """Yields a database session."""
        request = MagicMock()
        request.state.jwt_claims = {"sub": str(uuid4()), "org": str(uuid4())}
        request.headers.get.return_value = None
        request.cookies.get.return_value = None

        mock_session = AsyncMock()

        with (
            patch("sibyl.auth.rls.get_session") as mock_get_session,
            patch("sibyl.auth.rls.settings") as mock_settings,
        ):
            mock_settings.disable_auth = False

            async def session_context():
                yield mock_session

            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock()

            async for session in get_rls_session(request):
                assert session == mock_session

    @pytest.mark.asyncio
    async def test_skips_rls_when_auth_disabled(self) -> None:
        """Does not set RLS context when auth is disabled."""
        request = MagicMock()
        mock_session = AsyncMock()

        with (
            patch("sibyl.auth.rls.get_session") as mock_get_session,
            patch("sibyl.auth.rls.settings") as mock_settings,
        ):
            mock_settings.disable_auth = True

            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock()

            async for session in get_rls_session(request):
                # Should yield session without executing SET commands
                pass

            # No SET LOCAL calls should be made
            assert mock_session.execute.call_count == 0


class TestRequireRlsSession:
    """Tests for require_rls_session dependency."""

    @pytest.mark.asyncio
    async def test_raises_401_without_auth(self) -> None:
        """Raises 401 when no auth context is found."""
        from contextlib import asynccontextmanager

        request = MagicMock()
        request.state = MagicMock(spec=[])  # No jwt_claims attribute
        request.headers.get.return_value = None
        request.cookies.get.return_value = None

        mock_session = AsyncMock()

        @asynccontextmanager
        async def mock_get_session():
            yield mock_session

        with (
            patch("sibyl.auth.rls.get_session", mock_get_session),
            patch("sibyl.auth.rls.settings") as mock_settings,
            patch("sibyl.auth.rls._resolve_claims_minimal", new_callable=AsyncMock) as mock_resolve,
        ):
            mock_settings.disable_auth = False
            mock_resolve.return_value = None  # No claims found

            with pytest.raises(HTTPException) as exc_info:  # noqa: PT012
                gen = require_rls_session(request)
                await gen.__anext__()

            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_401_without_user(self) -> None:
        """Raises 401 when claims have no user ID."""
        from contextlib import asynccontextmanager

        request = MagicMock()
        mock_session = AsyncMock()

        @asynccontextmanager
        async def mock_get_session():
            yield mock_session

        with (
            patch("sibyl.auth.rls.get_session", mock_get_session),
            patch("sibyl.auth.rls.settings") as mock_settings,
            patch("sibyl.auth.rls._resolve_claims_minimal", new_callable=AsyncMock) as mock_resolve,
        ):
            mock_settings.disable_auth = False
            mock_resolve.return_value = {"org": str(uuid4())}  # No sub

            with pytest.raises(HTTPException) as exc_info:  # noqa: PT012
                gen = require_rls_session(request)
                await gen.__anext__()

            assert exc_info.value.status_code == 401
            assert "missing user" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_skips_auth_when_disabled(self) -> None:
        """Yields session without auth check when auth is disabled."""
        request = MagicMock()
        mock_session = AsyncMock()

        with (
            patch("sibyl.auth.rls.get_session") as mock_get_session,
            patch("sibyl.auth.rls.settings") as mock_settings,
        ):
            mock_settings.disable_auth = True

            mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_get_session.return_value.__aexit__ = AsyncMock()

            async for session in require_rls_session(request):
                assert session == mock_session
