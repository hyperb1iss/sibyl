from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sibyl_core.backends.surreal import SurrealAuthClient, SurrealContentClient, SurrealDriver
from sibyl_core.backends.surreal.connection import _can_retry_raw_query


@pytest.fixture
def fake_surreal(monkeypatch) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            calls.append(("init", url))

        async def authenticate(self, token: str) -> None:
            calls.append(("authenticate", token))

        async def signin(self, credentials: dict[str, str]) -> None:
            calls.append(("signin", credentials))

        async def use(self, namespace: str, database: str) -> None:
            calls.append(("use", (namespace, database)))

        async def query(self, query: str, params: object | None = None) -> list[Any]:
            calls.append(("query", (query, params)))
            return []

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, Any]:
            calls.append(("query_raw", (query, params)))
            return {"result": []}

        async def close(self) -> None:
            calls.append(("close", None))

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
    return calls


@pytest.mark.asyncio
async def test_surreal_driver_prefers_token_auth(fake_surreal) -> None:
    driver = SurrealDriver(
        "ws://localhost:8000/rpc",
        username="root",
        password="root",
        token="token-123",
    ).clone("org-123")

    await driver.execute_query("RETURN true;")

    assert ("authenticate", "token-123") in fake_surreal
    assert not any(call[0] == "signin" for call in fake_surreal)
    assert ("use", ("org_org123", "graph")) in fake_surreal


@pytest.mark.asyncio
async def test_surreal_driver_falls_back_to_username_password(fake_surreal) -> None:
    driver = SurrealDriver(
        "ws://localhost:8000/rpc",
        username="root",
        password="root",
    ).clone("org-123")

    await driver.execute_query("RETURN true;")

    assert ("signin", {"username": "root", "password": "root"}) in fake_surreal
    assert not any(call[0] == "authenticate" for call in fake_surreal)
    assert ("use", ("org_org123", "graph")) in fake_surreal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "namespace", "database"),
    [
        (
            SurrealAuthClient(
                url="ws://localhost:8000/rpc",
                username="root",
                password="root",
                token="token-123",
            ),
            "sibyl_auth",
            "auth",
        ),
        (
            SurrealContentClient(
                url="ws://localhost:8000/rpc",
                username="root",
                password="root",
                token="token-123",
            ),
            "sibyl_content",
            "content",
        ),
    ],
)
async def test_surreal_dedicated_clients_prefer_token_auth(
    fake_surreal,
    client: SurrealAuthClient | SurrealContentClient,
    namespace: str,
    database: str,
) -> None:
    await client.execute_query("RETURN true;")

    assert ("authenticate", "token-123") in fake_surreal
    assert not any(call[0] == "signin" for call in fake_surreal)
    assert ("use", (namespace, database)) in fake_surreal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "namespace", "database"),
    [
        (
            SurrealAuthClient(
                url="ws://localhost:8000/rpc",
                username="root",
                password="root",
            ),
            "sibyl_auth",
            "auth",
        ),
        (
            SurrealContentClient(
                url="ws://localhost:8000/rpc",
                username="root",
                password="root",
            ),
            "sibyl_content",
            "content",
        ),
    ],
)
async def test_surreal_dedicated_clients_retry_closed_read_socket(
    monkeypatch,
    client: SurrealAuthClient | SurrealContentClient,
    namespace: str,
    database: str,
) -> None:
    class ConnectionClosedError(RuntimeError):
        pass

    ConnectionClosedError.__module__ = "websockets.exceptions"
    clients: list[FakeAsyncSurreal] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace_: str, database_: str) -> None:
            self.namespace = namespace_
            self.database = database_

        async def query(self, query: str, params: object | None = None) -> list[dict[str, str]]:
            if len(clients) == 1:
                raise ConnectionClosedError("sent 1011 keepalive ping timeout")
            return [{"ok": "yes"}]

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))

    result = await client.execute_query("SELECT * FROM system_settings;")

    assert result == [{"ok": "yes"}]
    assert len(clients) == 2
    assert clients[0].closed is True
    assert clients[1].namespace == namespace
    assert clients[1].database == database


@pytest.mark.asyncio
async def test_surreal_content_client_retries_closed_raw_let_read(monkeypatch) -> None:
    class ConnectionClosedError(RuntimeError):
        pass

    ConnectionClosedError.__module__ = "websockets.exceptions"
    clients: list[FakeAsyncSurreal] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, Any]:
            if len(clients) == 1:
                raise ConnectionClosedError("sent 1011 keepalive ping timeout")
            return {"result": [{"status": "OK", "result": None}, {"status": "OK", "result": []}]}

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
    client = SurrealContentClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
    )

    result = await client.execute_query_raw(
        "LET $document_ids = []; SELECT * FROM document_chunks;",
    )

    assert result == {"result": [{"status": "OK", "result": None}, {"status": "OK", "result": []}]}
    assert len(clients) == 2


@pytest.mark.asyncio
async def test_surreal_content_client_does_not_retry_closed_write(monkeypatch) -> None:
    class ConnectionClosedError(RuntimeError):
        pass

    ConnectionClosedError.__module__ = "websockets.exceptions"
    clients: list[FakeAsyncSurreal] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query(self, query: str, params: object | None = None) -> list[Any]:
            raise ConnectionClosedError("sent 1011 keepalive ping timeout")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
    client = SurrealContentClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
    )

    with pytest.raises(ConnectionClosedError):
        await client.execute_query("CREATE system_settings CONTENT $record;", record={"key": "x"})

    assert len(clients) == 1


def test_surreal_raw_retry_predicate_allows_let_reads() -> None:
    assert _can_retry_raw_query("LET $ids = []; SELECT * FROM document_chunks;")
    assert not _can_retry_raw_query("LET $record = {}; CREATE document_chunks CONTENT $record;")
    assert not _can_retry_raw_query(
        "LET $created = (CREATE document_chunks CONTENT $record); SELECT * FROM document_chunks;"
    )
