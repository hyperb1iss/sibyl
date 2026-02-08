"""Tests for sandbox exec WebSocket proxy."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.agents.sandbox_exec import SandboxExecProxy


class TestSandboxExecProxy:
    """WebSocket exec proxy unit tests."""

    def test_init_default_namespace(self):
        """Default namespace is 'default'."""
        proxy = SandboxExecProxy()
        assert proxy._namespace == "default"

    def test_init_custom_namespace(self):
        """Custom namespace is respected."""
        proxy = SandboxExecProxy(namespace="sibyl")
        assert proxy._namespace == "sibyl"

    def test_init_streams_none(self):
        """Stream and client start as None."""
        proxy = SandboxExecProxy()
        assert proxy._k8s_stream is None
        assert proxy._ws_client is None

    async def test_relay_raises_without_connect(self):
        """relay() raises if connect() hasn't been called."""
        proxy = SandboxExecProxy()
        with pytest.raises(RuntimeError, match="not connected"):
            await proxy.relay(AsyncMock())

    async def test_close_idempotent(self):
        """close() is safe to call multiple times."""
        proxy = SandboxExecProxy()
        await proxy.close()
        await proxy.close()
        assert proxy._k8s_stream is None
        assert proxy._ws_client is None

    async def test_close_cleans_up_stream(self):
        """close() cleans up stream and client."""
        proxy = SandboxExecProxy()
        mock_stream = MagicMock()
        mock_client = AsyncMock()
        proxy._k8s_stream = mock_stream
        proxy._ws_client = mock_client

        await proxy.close()
        assert proxy._k8s_stream is None
        assert proxy._ws_client is None
        mock_stream.close.assert_called_once()
        mock_client.close.assert_called_once()

    async def test_close_handles_stream_error(self):
        """close() handles exceptions from stream.close()."""
        proxy = SandboxExecProxy()
        mock_stream = MagicMock()
        mock_stream.close.side_effect = RuntimeError("already closed")
        proxy._k8s_stream = mock_stream
        proxy._ws_client = None

        # Should not raise
        await proxy.close()
        assert proxy._k8s_stream is None

    async def test_close_handles_client_error(self):
        """close() handles exceptions from client.close()."""
        proxy = SandboxExecProxy()
        mock_client = AsyncMock()
        mock_client.close.side_effect = RuntimeError("already closed")
        proxy._k8s_stream = None
        proxy._ws_client = mock_client

        # Should not raise
        await proxy.close()
        assert proxy._ws_client is None

    async def test_resize_noop_without_stream(self):
        """resize() is no-op when stream is None."""
        proxy = SandboxExecProxy()
        # Should not raise
        await proxy.resize(80, 24)

    async def test_resize_sends_to_channel_4(self):
        """resize() sends JSON to K8s exec channel 4."""
        proxy = SandboxExecProxy()
        mock_stream = AsyncMock()
        proxy._k8s_stream = mock_stream

        await proxy.resize(120, 40)

        mock_stream.write_channel.assert_called_once()
        args = mock_stream.write_channel.call_args
        assert args[0][0] == 4  # Channel 4 = resize
        payload = json.loads(args[0][1])
        assert payload["Width"] == 120
        assert payload["Height"] == 40

    async def test_resize_handles_error(self):
        """resize() swallows exceptions from stream."""
        proxy = SandboxExecProxy()
        mock_stream = AsyncMock()
        mock_stream.write_channel.side_effect = RuntimeError("stream broken")
        proxy._k8s_stream = mock_stream

        # Should not raise -- resize failure is non-fatal
        await proxy.resize(80, 24)
