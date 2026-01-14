"""WebSocket connection management for Sibyl Runner."""

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from sibyl_runner.config import RunnerConfig

log = structlog.get_logger()

# Message handler type
MessageHandler = Callable[[dict], Coroutine[Any, Any, None]]


class RunnerClient:
    """WebSocket client for connecting to Sibyl Core.

    Handles:
    - Connection establishment with authentication
    - Automatic reconnection with exponential backoff
    - Heartbeat response
    - Message routing to handlers
    """

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self._ws: ClientConnection | None = None
        self._handlers: dict[str, MessageHandler] = {}
        self._connected = False
        self._shutdown_requested = False
        self._reconnect_count = 0
        self._last_heartbeat = datetime.now(UTC)

    @property
    def connected(self) -> bool:
        """Check if currently connected."""
        return self._connected and self._ws is not None

    def register_handler(self, msg_type: str, handler: MessageHandler) -> None:
        """Register a handler for a message type.

        Args:
            msg_type: Message type to handle (e.g., "task_assign").
            handler: Async function to call with message data.
        """
        self._handlers[msg_type] = handler

    async def connect(self) -> bool:
        """Establish WebSocket connection to server.

        Returns:
            True if connected successfully, False otherwise.
        """
        ws_url = self._build_ws_url()
        headers = self._build_headers()

        try:
            self._ws = await websockets.connect(
                ws_url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
            )
            self._connected = True
            self._reconnect_count = 0
            log.info("connected_to_server", url=ws_url)
            return True
        except Exception as e:
            log.warning("connection_failed", url=ws_url, error=str(e))
            return False

    async def disconnect(self) -> None:
        """Gracefully disconnect from server."""
        import contextlib

        self._connected = False
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def send(self, message: dict) -> bool:
        """Send a message to the server.

        Args:
            message: Message dict to send.

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._ws:
            return False

        try:
            await self._ws.send(json.dumps(message))
            return True
        except Exception as e:
            log.warning("send_failed", error=str(e))
            return False

    async def send_status(self, status: str, agent_count: int = 0) -> bool:
        """Send status update to server.

        Args:
            status: Runner status ("online", "busy", "draining").
            agent_count: Current number of running agents.
        """
        return await self.send({
            "type": "status",
            "status": status,
            "agent_count": agent_count,
        })

    async def send_project_register(
        self,
        project_id: str,
        worktree_path: str,
        worktree_branch: str | None = None,
    ) -> bool:
        """Register a warm worktree with server.

        Args:
            project_id: Project UUID.
            worktree_path: Path to worktree on this runner.
            worktree_branch: Git branch checked out in worktree.
        """
        return await self.send({
            "type": "project_register",
            "project_id": project_id,
            "worktree_path": worktree_path,
            "worktree_branch": worktree_branch,
        })

    async def send_agent_update(
        self,
        agent_id: str,
        status: str,
        progress: int | None = None,
        activity: str | None = None,
    ) -> bool:
        """Send agent execution update.

        Args:
            agent_id: Agent graph ID.
            status: Agent status.
            progress: Progress percentage (0-100).
            activity: Current activity description.
        """
        return await self.send({
            "type": "agent_update",
            "agent_id": agent_id,
            "status": status,
            "progress": progress,
            "activity": activity,
        })

    async def send_task_complete(
        self,
        task_id: str,
        result: dict,
    ) -> bool:
        """Report task completion.

        Args:
            task_id: Task ID that was completed.
            result: Task result data.
        """
        return await self.send({
            "type": "task_complete",
            "task_id": task_id,
            "result": result,
        })

    async def run_message_loop(self) -> None:
        """Run the message receive loop.

        Receives messages and routes to registered handlers.
        Handles heartbeats automatically.
        """
        if not self._ws:
            return

        try:
            async for message in self._ws:
                if self._shutdown_requested:
                    break

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    log.warning("invalid_json_message", message=message[:100])
                    continue

                await self._handle_message(data)

        except ConnectionClosed as e:
            log.info("connection_closed", code=e.code, reason=e.reason)
            self._connected = False

    async def run_with_reconnect(self) -> None:
        """Run connection loop with automatic reconnection.

        Connects to server and handles messages. Automatically
        reconnects on disconnection with exponential backoff.
        """
        while not self._shutdown_requested:
            # Try to connect
            if not await self.connect():
                await self._handle_reconnect_delay()
                continue

            # Run message loop until disconnected
            await self.run_message_loop()

            if self._shutdown_requested:
                break

            # Handle reconnection
            await self._handle_reconnect_delay()

        await self.disconnect()

    def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_requested = True

    async def _handle_message(self, data: dict) -> None:
        """Route incoming message to appropriate handler."""
        msg_type = data.get("type")

        if not isinstance(msg_type, str):
            log.warning("message_missing_type", data=data)
            return

        if msg_type == "heartbeat":
            # Respond to server heartbeat
            self._last_heartbeat = datetime.now(UTC)
            await self.send({"type": "heartbeat_ack"})
            return

        if msg_type == "error":
            log.warning("server_error", message=data.get("message"))
            return

        # Route to registered handler
        handler = self._handlers.get(msg_type)
        if handler:
            try:
                await handler(data)
            except Exception as e:
                log.exception("handler_error", msg_type=msg_type, error=str(e))
        else:
            log.debug("unhandled_message_type", msg_type=msg_type)

    async def _handle_reconnect_delay(self) -> None:
        """Handle reconnection delay with exponential backoff."""
        self._reconnect_count += 1

        if self._reconnect_count > self.config.max_reconnect_attempts:
            log.error("max_reconnect_attempts_exceeded")
            self._shutdown_requested = True
            return

        # Exponential backoff: 5s, 10s, 20s, 40s, ... capped at 5 minutes
        delay = min(
            self.config.reconnect_interval * (2 ** (self._reconnect_count - 1)),
            300,  # Max 5 minutes
        )

        log.info(
            "reconnecting",
            attempt=self._reconnect_count,
            max_attempts=self.config.max_reconnect_attempts,
            delay_seconds=delay,
        )

        await asyncio.sleep(delay)

    def _build_ws_url(self) -> str:
        """Build WebSocket URL from config."""
        base = self.config.server_url.rstrip("/")

        # Convert http(s) to ws(s)
        if base.startswith("https://"):
            base = "wss://" + base[8:]
        elif base.startswith("http://"):
            base = "ws://" + base[7:]

        return f"{base}/api/runners/ws/{self.config.runner_id}"

    def _build_headers(self) -> dict:
        """Build request headers with authentication."""
        headers = {}

        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"

        return headers
