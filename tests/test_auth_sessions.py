"""Tests for SessionManager and user session handling."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from sibyl.auth.sessions import SessionManager
from sibyl.db.models import UserSession


def make_session_record(
    *,
    user_id=None,
    token_hash: str = "testhash",  # noqa: S107
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    last_active_at: datetime | None = None,
    is_current: bool = False,
) -> UserSession:
    """Create a UserSession for testing."""
    if user_id is None:
        user_id = uuid4()
    if expires_at is None:
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24)
    if last_active_at is None:
        last_active_at = datetime.now(UTC).replace(tzinfo=None)

    return UserSession(
        id=uuid4(),
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
        revoked_at=revoked_at,
        last_active_at=last_active_at,
        is_current=is_current,
    )


class TestSessionManager:
    """Tests for SessionManager."""

    def test_hash_token_is_deterministic(self) -> None:
        """Token hashing should be deterministic."""
        token = "test-token-12345"  # noqa: S105
        hash1 = SessionManager.hash_token(token)
        hash2 = SessionManager.hash_token(token)
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex digest is 64 chars

    def test_hash_token_different_for_different_inputs(self) -> None:
        """Different tokens should produce different hashes."""
        hash1 = SessionManager.hash_token("token1")
        hash2 = SessionManager.hash_token("token2")
        assert hash1 != hash2

    @pytest.mark.asyncio
    async def test_create_session_stores_hashed_token(self) -> None:
        """Session creation should store a hashed token, not raw token."""
        db_session = AsyncMock()
        db_session.add = MagicMock()
        db_session.flush = AsyncMock()

        manager = SessionManager(db_session)
        user_id = uuid4()
        token = "raw-token-abc123"  # noqa: S105
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24)

        session = await manager.create_session(
            user_id=user_id,
            token=token,
            expires_at=expires_at,
        )

        # Token should be hashed
        assert session.token_hash == SessionManager.hash_token(token)
        assert session.user_id == user_id
        assert session.expires_at == expires_at
        db_session.add.assert_called_once()
        db_session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_session_by_token_returns_active_session(self) -> None:
        """Should return session when token matches and not revoked."""
        db_session = AsyncMock()
        user_id = uuid4()
        token = "test-token"  # noqa: S105
        expected_session = make_session_record(
            user_id=user_id,
            token_hash=SessionManager.hash_token(token),
        )

        # Mock the execute result
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = expected_session
        db_session.execute = AsyncMock(return_value=mock_result)

        manager = SessionManager(db_session)
        session = await manager.get_session_by_token(token)

        assert session is expected_session

    @pytest.mark.asyncio
    async def test_get_session_by_token_returns_none_for_revoked(self) -> None:
        """Should return None for revoked sessions."""
        db_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db_session.execute = AsyncMock(return_value=mock_result)

        manager = SessionManager(db_session)
        session = await manager.get_session_by_token("any-token")

        assert session is None

    @pytest.mark.asyncio
    async def test_list_user_sessions_returns_active_sessions(self) -> None:
        """Should return all active (non-revoked) sessions for user."""
        db_session = AsyncMock()
        user_id = uuid4()
        sessions = [
            make_session_record(user_id=user_id, token_hash="hash1"),  # noqa: S106
            make_session_record(user_id=user_id, token_hash="hash2"),  # noqa: S106
        ]

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = sessions
        mock_result.scalars.return_value = mock_scalars
        db_session.execute = AsyncMock(return_value=mock_result)

        manager = SessionManager(db_session)
        result = await manager.list_user_sessions(user_id)

        assert len(result) == 2
        assert result[0].user_id == user_id

    @pytest.mark.asyncio
    async def test_update_activity_updates_timestamp(self) -> None:
        """Should update last_active_at for valid session."""
        db_session = AsyncMock()
        token = "test-token"  # noqa: S105
        session_record = make_session_record(
            token_hash=SessionManager.hash_token(token),
            last_active_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session_record
        db_session.execute = AsyncMock(return_value=mock_result)

        manager = SessionManager(db_session)
        old_timestamp = session_record.last_active_at
        result = await manager.update_activity(token)

        assert result is True
        assert session_record.last_active_at is not None
        assert session_record.last_active_at > old_timestamp

    @pytest.mark.asyncio
    async def test_update_activity_returns_false_for_invalid_token(self) -> None:
        """Should return False when session not found."""
        db_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db_session.execute = AsyncMock(return_value=mock_result)

        manager = SessionManager(db_session)
        result = await manager.update_activity("invalid-token")

        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_session_sets_revoked_at(self) -> None:
        """Should set revoked_at timestamp when revoking."""
        db_session = AsyncMock()
        session_id = uuid4()
        user_id = uuid4()
        session_record = make_session_record(user_id=user_id)
        session_record.id = session_id

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = session_record
        db_session.execute = AsyncMock(return_value=mock_result)
        db_session.commit = AsyncMock()

        manager = SessionManager(db_session)
        result = await manager.revoke_session(session_id, user_id)

        assert result is True
        assert session_record.revoked_at is not None
        db_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_revoke_session_returns_false_for_nonexistent(self) -> None:
        """Should return False when session doesn't exist."""
        db_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db_session.execute = AsyncMock(return_value=mock_result)

        manager = SessionManager(db_session)
        result = await manager.revoke_session(uuid4(), uuid4())

        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_all_sessions_excludes_current(self) -> None:
        """Should revoke all sessions except the one with excluded token hash."""
        db_session = AsyncMock()
        user_id = uuid4()
        current_token_hash = "current-hash"  # noqa: S105
        sessions = [
            make_session_record(user_id=user_id, token_hash="other1"),  # noqa: S106
            make_session_record(user_id=user_id, token_hash="other2"),  # noqa: S106
        ]

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = sessions
        mock_result.scalars.return_value = mock_scalars
        db_session.execute = AsyncMock(return_value=mock_result)
        db_session.commit = AsyncMock()

        manager = SessionManager(db_session)
        count = await manager.revoke_all_sessions(user_id, exclude_token_hash=current_token_hash)

        assert count == 2
        for session in sessions:
            assert session.revoked_at is not None
        db_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_current_clears_previous_and_sets_new(self) -> None:
        """Should clear is_current on other sessions and set on target."""
        db_session = AsyncMock()
        user_id = uuid4()
        token = "new-current-token"  # noqa: S105
        target_session = make_session_record(
            user_id=user_id,
            token_hash=SessionManager.hash_token(token),
            is_current=False,
        )
        other_session = make_session_record(user_id=user_id, is_current=True)

        # First call returns target session, second returns list of current sessions
        mock_result1 = MagicMock()
        mock_result1.scalar_one_or_none.return_value = target_session

        mock_result2 = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.__iter__ = MagicMock(return_value=iter([other_session]))
        mock_result2.scalars.return_value = mock_scalars

        db_session.execute = AsyncMock(side_effect=[mock_result1, mock_result2])

        manager = SessionManager(db_session)
        result = await manager.mark_current(token)

        assert result is True
        assert target_session.is_current is True
        assert other_session.is_current is False
