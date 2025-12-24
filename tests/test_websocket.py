"""Tests for WebSocket org scoping and auth integration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.api.websocket import (
    Connection,
    ConnectionManager,
    _extract_org_from_token,
)


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


class TestExtractOrgFromToken:
    """Tests for JWT org extraction."""

    def test_missing_cookie(self) -> None:
        """Missing cookie should return None."""
        ws = MagicMock()
        ws.cookies = {}
        result = _extract_org_from_token(ws)
        assert result is None

    def test_valid_jwt_with_org(self) -> None:
        """Valid JWT with org claim should extract org_id."""
        import base64
        import json

        # Create a mock JWT payload
        payload = {"sub": "user123", "org": "org_abc123"}
        payload_json = json.dumps(payload)
        payload_b64 = base64.b64encode(payload_json.encode()).decode()
        # JWT format: header.payload.signature (we only care about payload)
        fake_jwt = f"header.{payload_b64}.signature"

        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": fake_jwt}

        result = _extract_org_from_token(ws)
        assert result == "org_abc123"

    def test_malformed_jwt(self) -> None:
        """Malformed JWT should return None."""
        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": "not-a-valid-jwt"}

        result = _extract_org_from_token(ws)
        assert result is None

    def test_jwt_without_org_claim(self) -> None:
        """JWT without org claim should return None."""
        import base64
        import json

        payload = {"sub": "user123"}  # No org claim
        payload_json = json.dumps(payload)
        payload_b64 = base64.b64encode(payload_json.encode()).decode()
        fake_jwt = f"header.{payload_b64}.signature"

        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": fake_jwt}

        result = _extract_org_from_token(ws)
        assert result is None
