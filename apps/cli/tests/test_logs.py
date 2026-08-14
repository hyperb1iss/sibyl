from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
import typer

from sibyl_cli.logs import _stream_logs


class _FakeConnectionClosed(Exception):
    pass


class _FakeWebSocket:
    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def recv(self) -> str:
        raise _FakeConnectionClosed


def test_stream_logs_requires_resolved_client_token(capsys: pytest.CaptureFixture[str]) -> None:
    client = SimpleNamespace(base_url="https://sibyl.example/api", auth_token=None)

    with pytest.raises(typer.Exit):
        import asyncio

        asyncio.run(_stream_logs(client, None, None))

    assert "Authentication required" in capsys.readouterr().out


def test_stream_logs_uses_resolved_client_url_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_urls: list[str] = []

    def connect(url: str) -> _FakeWebSocket:
        connect_urls.append(url)
        return _FakeWebSocket()

    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=connect, ConnectionClosed=_FakeConnectionClosed),
    )
    client = SimpleNamespace(
        base_url="https://sibyl.example/api",
        auth_token="scoped-access-token",
    )

    import asyncio

    # The stream ends only when the server or the network ends it, so a
    # supervised follow that loses its connection has to exit nonzero.
    with pytest.raises(typer.Exit) as exc:
        asyncio.run(_stream_logs(client, None, None))

    assert exc.value.exit_code == 1
    assert connect_urls == ["wss://sibyl.example/api/logs/stream?token=scoped-access-token"]


def test_stream_logs_exits_nonzero_when_the_connection_drops(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dropped stream must not look like a clean finish to a supervisor."""
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(
            connect=lambda _url: _FakeWebSocket(),
            ConnectionClosed=_FakeConnectionClosed,
        ),
    )
    client = SimpleNamespace(base_url="https://sibyl.example/api", auth_token="tok")

    import asyncio

    with pytest.raises(typer.Exit) as exc:
        asyncio.run(_stream_logs(client, None, None))

    assert exc.value.exit_code == 1
    out = capsys.readouterr().out
    assert "Connection closed" in out
    # The broad handler below must not relabel the clean exit.
    assert "WebSocket error" not in out
