"""Tests for log routes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sibyl.api.routes.logs import _validate_owner_websocket, router


class TestValidateOwnerWebsocket:
    """Tests for global-admin validation on log streaming."""

    @pytest.mark.asyncio
    async def test_returns_true_for_global_admin(self) -> None:
        user_id = uuid4()
        websocket = MagicMock()

        with (
            patch(
                "sibyl.api.routes.logs.resolve_active_websocket_claims",
                AsyncMock(return_value={"sub": str(user_id)}),
            ) as resolve_claims,
            patch(
                "sibyl.api.routes.logs.get_user_by_id",
                AsyncMock(return_value=SimpleNamespace(is_admin=True)),
            ) as get_user,
        ):
            assert await _validate_owner_websocket(websocket) is True

        resolve_claims.assert_awaited_once_with(websocket)
        get_user.assert_awaited_once_with(user_id)

    @pytest.mark.asyncio
    async def test_rejects_non_global_admin(self) -> None:
        user_id = uuid4()
        websocket = MagicMock()

        with (
            patch(
                "sibyl.api.routes.logs.resolve_active_websocket_claims",
                AsyncMock(return_value={"sub": str(user_id)}),
            ) as resolve_claims,
            patch(
                "sibyl.api.routes.logs.get_user_by_id",
                AsyncMock(return_value=SimpleNamespace(is_admin=False)),
            ) as get_user,
        ):
            assert await _validate_owner_websocket(websocket) is False

        resolve_claims.assert_awaited_once_with(websocket)
        get_user.assert_awaited_once_with(user_id)

    @pytest.mark.asyncio
    async def test_rejects_revoked_access_session(self) -> None:
        websocket = MagicMock()

        with (
            patch(
                "sibyl.api.routes.logs.resolve_active_websocket_claims",
                AsyncMock(return_value=None),
            ) as resolve_claims,
            patch(
                "sibyl.api.routes.logs.get_user_by_id",
                AsyncMock(),
            ) as get_user,
        ):
            assert await _validate_owner_websocket(websocket) is False

        resolve_claims.assert_awaited_once_with(websocket)
        get_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_auth_store_timeout(self) -> None:
        websocket = MagicMock()

        with (
            patch(
                "sibyl.api.routes.logs.resolve_active_websocket_claims",
                AsyncMock(return_value=None),
            ) as resolve_claims,
            patch(
                "sibyl.api.routes.logs.get_user_by_id",
                AsyncMock(),
            ) as get_user,
        ):
            assert await _validate_owner_websocket(websocket) is False

        resolve_claims.assert_awaited_once_with(websocket)
        get_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accepts_global_admin_without_org_context(self) -> None:
        user_id = uuid4()
        websocket = MagicMock()

        with (
            patch(
                "sibyl.api.routes.logs.resolve_active_websocket_claims",
                AsyncMock(return_value={"sub": str(user_id)}),
            ) as resolve_claims,
            patch(
                "sibyl.api.routes.logs.get_user_by_id",
                AsyncMock(return_value=SimpleNamespace(is_admin=True)),
            ) as get_user,
        ):
            assert await _validate_owner_websocket(websocket) is True

        resolve_claims.assert_awaited_once_with(websocket)
        get_user.assert_awaited_once_with(user_id)


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
            patch("sibyl.api.routes.logs._validate_owner_websocket", AsyncMock(return_value=True)),
            patch("sibyl.api.routes.logs.LogBuffer.get", return_value=buffer),
            client.websocket_connect(
                "/logs/stream",
                headers={"Authorization": "Bearer owner-token"},
            ) as websocket,
        ):
            assert websocket.receive_json() == entry

        assert buffer.unsubscribed == [queue]

    def test_non_owner_websocket_stream_is_rejected(self) -> None:
        with (
            self._create_client() as client,
            patch("sibyl.api.routes.logs._validate_owner_websocket", AsyncMock(return_value=False)),
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect(
                "/logs/stream",
                headers={"Authorization": "Bearer member-token"},
            ),
        ):
            pass

        assert exc_info.value.code == 1008

    def test_query_token_is_rejected(self) -> None:
        with (
            self._create_client() as client,
            patch(
                "sibyl.api.routes.logs.resolve_active_websocket_claims",
                AsyncMock(return_value=None),
            ),
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/logs/stream?token=must-not-be-read"),
        ):
            pass

        assert exc_info.value.code == 1008
