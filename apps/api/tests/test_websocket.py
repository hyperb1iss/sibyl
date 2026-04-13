"""Tests for WebSocket org scoping and auth integration."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import sibyl.api.websocket as websocket_module
from sibyl.api.websocket import (
    Connection,
    ConnectionManager,
    _extract_org_from_token,
)
from sibyl.auth.jwt import create_access_token
from sibyl.config import Settings


class TestConnection:
    """Tests for Connection dataclass."""

    def test_connection_with_org(self) -> None:
        """Connection should store org_id."""
        ws = MagicMock()
        conn = Connection(websocket=ws, org_id="org_123")
        assert conn.org_id == "org_123"
        assert conn.websocket == ws

    def test_connection_without_org(self) -> None:
        """Connection should allow None org_id."""
        ws = MagicMock()
        conn = Connection(websocket=ws)
        assert conn.org_id is None


class TestConnectionManagerOrgScoping:
    """Tests for org-scoped broadcasting."""

    @pytest.fixture
    def manager(self) -> ConnectionManager:
        return ConnectionManager()

    @pytest.fixture
    def mock_websocket(self) -> MagicMock:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_broadcast_to_specific_org(self, manager: ConnectionManager) -> None:
        """Broadcast with org_id should only reach that org's connections."""
        ws1 = MagicMock()
        ws1.send_json = AsyncMock()
        ws2 = MagicMock()
        ws2.send_json = AsyncMock()
        ws3 = MagicMock()
        ws3.send_json = AsyncMock()

        # Add connections for different orgs
        manager.active_connections = [
            Connection(websocket=ws1, org_id="org_a"),
            Connection(websocket=ws2, org_id="org_b"),
            Connection(websocket=ws3, org_id="org_a"),
        ]

        # Broadcast to org_a only
        await manager.broadcast("test_event", {"key": "value"}, org_id="org_a")

        # Only org_a connections should receive
        assert ws1.send_json.called
        assert ws3.send_json.called
        assert not ws2.send_json.called

    @pytest.mark.asyncio
    async def test_broadcast_without_org_reaches_all(self, manager: ConnectionManager) -> None:
        """Broadcast without org_id should reach all connections."""
        ws1 = MagicMock()
        ws1.send_json = AsyncMock()
        ws2 = MagicMock()
        ws2.send_json = AsyncMock()

        manager.active_connections = [
            Connection(websocket=ws1, org_id="org_a"),
            Connection(websocket=ws2, org_id="org_b"),
        ]

        # Broadcast to all (system event)
        await manager.broadcast("health_update", {"status": "ok"}, org_id=None)

        assert ws1.send_json.called
        assert ws2.send_json.called

    @pytest.mark.asyncio
    async def test_broadcast_to_empty_org(self, manager: ConnectionManager) -> None:
        """Broadcast to org with no connections should succeed without error."""
        ws1 = MagicMock()
        ws1.send_json = AsyncMock()

        manager.active_connections = [
            Connection(websocket=ws1, org_id="org_a"),
        ]

        # Broadcast to org with no connections
        await manager.broadcast("test_event", {"key": "value"}, org_id="org_nonexistent")

        # Should not error, just not send anything
        assert not ws1.send_json.called

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_hold_lock_while_sending(
        self,
        manager: ConnectionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Heartbeat sends should not block connect/disconnect operations."""
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def slow_send(_message: dict[str, object]) -> None:
            send_started.set()
            await release_send.wait()

        slow_ws = MagicMock()
        slow_ws.send_json = AsyncMock(side_effect=slow_send)
        manager.active_connections = [Connection(websocket=slow_ws, org_id="org_a")]

        sleep_calls = 0

        async def fake_sleep(_interval: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                return
            raise asyncio.CancelledError

        monkeypatch.setattr(websocket_module.asyncio, "sleep", fake_sleep)

        heartbeat_task = asyncio.create_task(manager._heartbeat_loop())
        manager._heartbeat_task = heartbeat_task

        await asyncio.wait_for(send_started.wait(), timeout=1)

        new_ws = MagicMock()
        new_ws.accept = AsyncMock()
        await asyncio.wait_for(manager.connect(new_ws, org_id="org_b"), timeout=0.1)

        assert any(conn.websocket == new_ws for conn in manager.active_connections)

        release_send.set()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat_task


class TestExtractOrgFromToken:
    """Tests for JWT org extraction."""

    def test_missing_cookie(self) -> None:
        """Missing cookie should return None."""
        ws = MagicMock()
        ws.cookies = {}
        ws.headers = {}
        result = _extract_org_from_token(ws)
        assert result is None

    def test_valid_jwt_with_org(self, monkeypatch) -> None:
        """Valid signed JWT with org claim should extract org_id."""
        monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        org_id = uuid4()
        token = create_access_token(user_id=uuid4(), organization_id=org_id)
        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": token}
        ws.headers = {}

        result = _extract_org_from_token(ws)
        assert result == str(org_id)

    def test_malformed_jwt(self) -> None:
        """Malformed JWT should return None."""
        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": "not-a-valid-jwt"}
        ws.headers = {}

        result = _extract_org_from_token(ws)
        assert result is None

    def test_jwt_without_org_claim(self, monkeypatch) -> None:
        """JWT without org claim should return None (org-scoped WS required)."""
        monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        token = create_access_token(user_id=uuid4(), organization_id=None)

        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": token}
        ws.headers = {}

        result = _extract_org_from_token(ws)
        assert result is None
