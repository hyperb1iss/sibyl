"""sibylnet channel client operations."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4


class ClientNetMixin:
    """sibylnet channel client operations."""

    async def post_net_message(
        self,
        *,
        channel: str,
        body: str,
        thread: str | None = None,
        decision: bool = False,
        project_id: str | None = None,
        agent: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Post a message, or a decision that is also remembered, to a channel."""
        data: dict[str, Any] = {
            "channel": channel,
            "body": body,
            "kind": "decision" if decision else "message",
        }
        if thread:
            data["thread"] = thread
        if project_id:
            data["project_id"] = project_id
        if agent:
            data["agent"] = agent
        if session:
            data["session"] = session
        # A channel message is live coordination. Replaying it from the write
        # buffer after the conversation moved on would hand its receivers a
        # stale instruction, so a failed post fails now and the caller retries.
        # The key makes the transport's own retry (after a token refresh) land
        # once, and a decision remember its memory once.
        return await self._request(
            "POST",
            "/net/messages",
            json=data,
            _buffer_pending=False,
            _idempotency_key=str(uuid4()),
        )

    async def net_feed(
        self,
        *,
        since: str | None = None,
        channels: Sequence[str] = (),
        thread: str | None = None,
        limit: int | None = None,
        wait: int = 0,
        agent: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Read messages after a cursor, or the newest ``limit`` without one.

        ``agent`` and ``session`` label the reader in the channel's presence.
        """
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        if channels:
            params["channel"] = list(channels)
        if thread:
            params["thread"] = thread
        if limit is not None:
            params["limit"] = limit
        if wait:
            params["wait"] = wait
        if agent:
            params["agent"] = agent
        if session:
            params["session"] = session
        return await self._request("GET", "/net/feed", params=params)

    async def list_net_channels(self) -> dict[str, Any]:
        """List channels by most recent activity."""
        return await self._request("GET", "/net/channels")

    async def list_net_threads(self, channel: str) -> dict[str, Any]:
        """List a channel's threads by most recent activity."""
        return await self._request("GET", "/net/threads", params={"channel": channel})

    async def net_presence(self, channel: str | None = None) -> dict[str, Any]:
        """Who is watching and who posted recently, in one channel or the whole organization."""
        params = {"channel": channel} if channel else None
        return await self._request("GET", "/net/presence", params=params)
