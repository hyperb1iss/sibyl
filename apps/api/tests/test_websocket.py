"""Tests for WebSocket org scoping and auth integration."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import sibyl.api.websocket as websocket_module
from sibyl.api.websocket import (
    Connection,
    ConnectionManager,
    _extract_org_from_token,
    entity_change_payload,
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


class TestEntityBroadcastPayload:
    """A broadcast fans out to an organization, not to a reader.

    A connection authenticates as an org and carries no principal, so there
    is no identity here to run the scope rule against. The channel therefore
    carries no memory: clients learn a row changed and refetch it through the
    REST endpoint, which authorizes them individually.
    """

    def test_entity_change_payload_carries_identifiers_only(self) -> None:
        payload = entity_change_payload("decision_private", "episode")

        assert payload == {"id": "decision_private", "entity_type": "episode"}
        for content_field in ("content", "description", "name", "metadata"):
            assert content_field not in payload

    def test_entity_change_payload_drops_prose_but_keeps_workflow_signals(self) -> None:
        """Task and epic events pass a field bag straight through.

        Those rows are reachable only by project members at REST but their
        events fan out to the whole org, so the title and body have to be
        dropped here rather than at each call site.
        """
        payload = entity_change_payload(
            "task-1",
            "task",
            action="update_task",
            status="doing",
            name="Acquisition target is Initech",
            title="Acquisition target is Initech",
            description="board voted 5-2, do not disclose",
            summary="do not disclose",
            content="do not disclose",
            metadata={"memory_scope": "private"},
        )

        assert payload == {
            "id": "task-1",
            "entity_type": "task",
            "action": "update_task",
            "status": "doing",
        }
        assert "do not disclose" not in str(payload)
        assert "Acquisition" not in str(payload)

    @pytest.mark.asyncio
    async def test_broadcast_reaches_every_connection_in_the_org(self) -> None:
        """Which is exactly why the payload may not carry memory."""
        manager = ConnectionManager()
        sockets = [MagicMock(), MagicMock()]
        for socket in sockets:
            socket.accept = AsyncMock()
            socket.send_json = AsyncMock()
            await manager.connect(socket, org_id="org-1")

        await manager.broadcast(
            "entity_created",
            entity_change_payload("decision_private", "episode"),
            org_id="org-1",
        )

        for socket in sockets:
            sent = socket.send_json.await_args.args[0]
            assert sent["data"] == {"id": "decision_private", "entity_type": "episode"}
            assert "do not disclose" not in str(sent)


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
    async def test_broadcast_respects_topic_subscriptions(
        self,
        manager: ConnectionManager,
    ) -> None:
        """Subscribed connections should only receive matching event topics."""
        ws_all = MagicMock()
        ws_all.send_json = AsyncMock()
        ws_raw = MagicMock()
        ws_raw.send_json = AsyncMock()
        ws_entity = MagicMock()
        ws_entity.send_json = AsyncMock()

        manager.active_connections = [
            Connection(websocket=ws_all, org_id="org_a"),
            Connection(
                websocket=ws_raw,
                org_id="org_a",
                topics=frozenset({"raw_capture_changed"}),
            ),
            Connection(
                websocket=ws_entity,
                org_id="org_a",
                topics=frozenset({"entity_updated"}),
            ),
        ]

        await manager.broadcast("raw_capture_changed", {"raw_memory_ids": ["raw-a"]}, "org_a")

        assert ws_all.send_json.called
        assert ws_raw.send_json.called
        assert not ws_entity.send_json.called

    @pytest.mark.asyncio
    async def test_subscribe_normalizes_supported_topics(
        self,
        manager: ConnectionManager,
    ) -> None:
        """Subscribe should store valid broadcast topics and ignore unknowns."""
        ws = MagicMock()
        manager.active_connections = [Connection(websocket=ws, org_id="org_a")]

        topics = await manager.subscribe(
            ws,
            ["raw_capture_changed", "entity_updated", "raw_capture_changed", "surprise", 1],
        )

        assert topics == ["entity_updated", "raw_capture_changed"]
        assert manager.active_connections[0].topics == frozenset(topics)

        assert await manager.subscribe(ws, []) == []
        assert manager.active_connections[0].topics is None

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_hold_lock_while_sending(
        self,
        manager: ConnectionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Heartbeat sends should not block connect/disconnect operations."""
        manager.HEARTBEAT_INTERVAL = 0
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

    @pytest.mark.asyncio
    async def test_heartbeat_disconnects_only_after_timeout(
        self,
        manager: ConnectionManager,
    ) -> None:
        """Pending heartbeat connections should expire by elapsed timeout."""
        ws = MagicMock()
        now = datetime.now(UTC)
        manager.active_connections = [
            Connection(
                websocket=ws,
                org_id="org_a",
                last_activity=now - timedelta(seconds=30),
                last_heartbeat_sent_at=now - timedelta(seconds=manager.PONG_TIMEOUT - 1),
                pending_pong=True,
            )
        ]

        heartbeat_connections, dead_connections = await manager._prepare_heartbeat_batch(
            now=now,
            send_heartbeats=False,
        )
        assert heartbeat_connections == []
        assert dead_connections == []

        heartbeat_connections, dead_connections = await manager._prepare_heartbeat_batch(
            now=now + timedelta(seconds=manager.PONG_TIMEOUT + 1),
            send_heartbeats=False,
        )
        assert heartbeat_connections == []
        assert dead_connections == [ws]

    def test_heartbeat_ack_clears_pending_state(self, manager: ConnectionManager) -> None:
        """mark_activity should clear pending heartbeat state."""
        ws = MagicMock()
        now = datetime.now(UTC)
        connection = Connection(
            websocket=ws,
            org_id="org_a",
            last_activity=now - timedelta(seconds=5),
            last_heartbeat_sent_at=now - timedelta(seconds=1),
            pending_pong=True,
        )
        manager.active_connections = [connection]

        manager.mark_activity(ws)

        assert connection.pending_pong is False
        assert connection.last_heartbeat_sent_at is None
        assert connection.last_activity is not None
        assert connection.last_activity >= now


class TestExtractOrgFromToken:
    """Tests for JWT org extraction."""

    @pytest.mark.asyncio
    async def test_missing_cookie(self) -> None:
        """Missing cookie should return None."""
        ws = MagicMock()
        ws.cookies = {}
        ws.headers = {}
        result = await _extract_org_from_token(ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_jwt_with_org(self, monkeypatch) -> None:
        """Valid signed JWT with org claim should extract org_id."""
        monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")
        validate_access_session = AsyncMock(return_value=True)
        monkeypatch.setattr(websocket_module, "validate_access_session", validate_access_session)

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        org_id = uuid4()
        token = create_access_token(user_id=uuid4(), organization_id=org_id)
        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": token}
        ws.headers = {}

        result = await _extract_org_from_token(ws)
        assert result == str(org_id)
        validate_access_session.assert_awaited_once_with(token)

    @pytest.mark.asyncio
    async def test_malformed_jwt(self) -> None:
        """Malformed JWT should return None."""
        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": "not-a-valid-jwt"}
        ws.headers = {}

        result = await _extract_org_from_token(ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_jwt_without_org_claim(self, monkeypatch) -> None:
        """JWT without org claim should return None (org-scoped WS required)."""
        monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")
        validate_access_session = AsyncMock(return_value=True)
        monkeypatch.setattr(websocket_module, "validate_access_session", validate_access_session)

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        token = create_access_token(user_id=uuid4(), organization_id=None)

        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": token}
        ws.headers = {}

        result = await _extract_org_from_token(ws)
        assert result is None
        validate_access_session.assert_awaited_once_with(token)

    @pytest.mark.asyncio
    async def test_revoked_jwt_returns_none(self, monkeypatch) -> None:
        """Revoked JWT sessions should not connect to org-scoped WS."""
        monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")
        validate_access_session = AsyncMock(return_value=False)
        monkeypatch.setattr(websocket_module, "validate_access_session", validate_access_session)

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        token = create_access_token(user_id=uuid4(), organization_id=uuid4())

        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": token}
        ws.headers = {}

        result = await _extract_org_from_token(ws)
        assert result is None
        validate_access_session.assert_awaited_once_with(token)

    @pytest.mark.asyncio
    async def test_auth_store_timeout_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")
        monkeypatch.setenv("SIBYL_JWT_ALGORITHM", "HS256")
        validate_access_session = AsyncMock(side_effect=TimeoutError)
        monkeypatch.setattr(websocket_module, "validate_access_session", validate_access_session)

        from sibyl import config as config_module

        config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

        token = create_access_token(user_id=uuid4(), organization_id=uuid4())

        ws = MagicMock()
        ws.cookies = {"sibyl_access_token": token}
        ws.headers = {}

        result = await _extract_org_from_token(ws)
        assert result is None
        validate_access_session.assert_awaited_once_with(token)
