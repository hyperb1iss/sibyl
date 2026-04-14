"""Tests for log routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sibyl.api.routes.logs import _validate_owner_token, router
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


class _FakeLogEntry:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


class _FakeLogBuffer:
    def __init__(self, queue: asyncio.Queue[_FakeLogEntry]) -> None:
        self._queue = queue
        self.entries: list[dict[str, Any]] = []
        self.unsubscribed: list[asyncio.Queue[_FakeLogEntry]] = []

    def append(self, entry: Any) -> None:
        self.entries.append(entry.to_dict() if hasattr(entry, "to_dict") else dict(entry))

    def subscribe(self) -> asyncio.Queue[_FakeLogEntry]:
        return self._queue

    def unsubscribe(self, queue: asyncio.Queue[_FakeLogEntry]) -> None:
        self.unsubscribed.append(queue)


class TestLogStreamRoute:
    @staticmethod
    def _create_client() -> TestClient:
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_owner_websocket_stream_connects_and_receives_logs(self) -> None:
        queue: asyncio.Queue[_FakeLogEntry] = asyncio.Queue()
        entry = {"timestamp": "2026-04-13T12:00:00+00:00", "service": "api", "level": "info"}
        queue.put_nowait(_FakeLogEntry(entry))
        buffer = _FakeLogBuffer(queue)

        with (
            self._create_client() as client,
            patch("sibyl.api.routes.logs._validate_owner_token", AsyncMock(return_value=True)),
            patch("sibyl.api.routes.logs.LogBuffer.get", return_value=buffer),
            client.websocket_connect("/logs/stream?token=owner-token") as websocket,
        ):
            assert websocket.receive_json() == entry

        assert buffer.unsubscribed == [queue]

    def test_non_owner_websocket_stream_is_rejected(self) -> None:
        with (
            self._create_client() as client,
            patch("sibyl.api.routes.logs._validate_owner_token", AsyncMock(return_value=False)),
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/logs/stream?token=member-token"),
        ):
            pass

        assert exc_info.value.code == 1008
