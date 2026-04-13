"""Tests for log routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes.logs import _validate_owner_token
from sibyl.auth.jwt import create_access_token
from sibyl.config import Settings
from sibyl.db.models import OrganizationRole


class TestValidateOwnerToken:
    """Tests for OWNER validation on log streaming."""

    @pytest.mark.asyncio
    async def test_returns_true_for_owner_membership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        user_id = uuid4()
        org_id = uuid4()
        token = create_access_token(user_id=user_id, organization_id=org_id)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = OrganizationRole.OWNER
        session = AsyncMock()
        session.execute.return_value = mock_result

        @asynccontextmanager
        async def fake_get_session():
            yield session

        with patch("sibyl.api.routes.logs.get_session", fake_get_session):
            assert await _validate_owner_token(token) is True

    @pytest.mark.asyncio
    async def test_rejects_non_owner_membership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        user_id = uuid4()
        org_id = uuid4()
        token = create_access_token(user_id=user_id, organization_id=org_id)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = OrganizationRole.MEMBER
        session = AsyncMock()
        session.execute.return_value = mock_result

        @asynccontextmanager
        async def fake_get_session():
            yield session

        with patch("sibyl.api.routes.logs.get_session", fake_get_session):
            assert await _validate_owner_token(token) is False

    @pytest.mark.asyncio
    async def test_rejects_missing_org_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        token = create_access_token(user_id=uuid4(), organization_id=None)

        assert await _validate_owner_token(token) is False
