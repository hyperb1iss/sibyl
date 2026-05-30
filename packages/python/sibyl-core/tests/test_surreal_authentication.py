from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sibyl_core.backends.surreal import SurrealAuthClient, SurrealContentClient
from sibyl_core.backends.surreal.connection import _can_retry_raw_query
from sibyl_core.services.graph import SurrealGraphClient


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

    class FakeRecordID:
        pass

    monkeypatch.setitem(
        sys.modules,
        "surrealdb",
        SimpleNamespace(AsyncSurreal=FakeAsyncSurreal, RecordID=FakeRecordID),
    )
    return calls


@pytest.mark.asyncio
async def test_graph_client_prefers_token_auth(fake_surreal) -> None:
    client = SurrealGraphClient(
        group_id="org-123",
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
        token="token-123",
    )

    await client.execute_query("RETURN true;")

    assert ("authenticate", "token-123") in fake_surreal
    assert not any(call[0] == "signin" for call in fake_surreal)
    assert ("use", ("org_org123", "graph")) in fake_surreal


@pytest.mark.asyncio
async def test_graph_client_falls_back_to_username_password(fake_surreal) -> None:
    client = SurrealGraphClient(
        group_id="org-123",
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
    )

    await client.execute_query("RETURN true;")

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
async def test_surreal_dedicated_client_pools_connections_for_concurrent_queries(
    monkeypatch,
) -> None:
    """Concurrent queries fan out across a connection pool, not one socket (H3)."""
    clients: list[FakeAsyncSurreal] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url
            self.signin_count = 0
            self.use_count = 0
            self.query_count = 0
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials
            self.signin_count += 1
            await asyncio.sleep(0.01)

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database
            self.use_count += 1
            await asyncio.sleep(0.01)

        async def query(self, query: str, params: object | None = None) -> list[dict[str, int]]:
            self.query_count += 1
            await asyncio.sleep(0)
            return [{"query_count": self.query_count}]

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
    client = SurrealAuthClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
    )

    results = await asyncio.gather(
        *(client.execute_query("SELECT * FROM user_sessions;") for _ in range(8))
    )

    # Audit H3: concurrent queries fan out across the connection pool instead of
    # serializing onto one socket. Server URLs pool to 4, so 8 concurrent reads
    # open more than one connection; each connection authenticates once.
    assert len(results) == 8
    assert all(result and result[0]["query_count"] >= 1 for result in results)
    assert 1 < len(clients) <= 4
    assert all(fake.signin_count == 1 and fake.use_count == 1 for fake in clients)
    assert sum(fake.query_count for fake in clients) == 8


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
            self.closed = False
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
async def test_surreal_dedicated_client_drops_query_id_keyerror_on_write(monkeypatch) -> None:
    clients: list[FakeAsyncSurreal] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.closed = False
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query(self, query: str, params: object | None = None) -> list[dict[str, str]]:
            raise KeyError("c87ffcce-66d3-4c07-aa06-7e40f3a9e67f")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
    client = SurrealAuthClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
    )

    with pytest.raises(KeyError):
        await client.execute_query("CREATE audit_logs CONTENT $record;", record={})

    assert len(clients) == 1
    assert clients[0].closed is True


@pytest.mark.asyncio
async def test_surreal_content_client_retries_closed_socket_during_connect(monkeypatch) -> None:
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
            if len(clients) == 1:
                raise ConnectionClosedError("sent 1011 keepalive ping timeout")

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, Any]:
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
    assert clients[0].closed is True
    assert clients[1].namespace == "sibyl_content"


@pytest.mark.asyncio
async def test_surreal_content_client_retries_opening_handshake_timeout(monkeypatch) -> None:
    clients: list[FakeAsyncSurreal] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            clients.append(self)

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials
            if len(clients) == 1:
                raise TimeoutError("timed out during opening handshake")

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query_raw(self, query: str, params: object | None = None) -> dict[str, Any]:
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
    assert clients[0].closed is True
    assert clients[1].namespace == "sibyl_content"


@pytest.mark.asyncio
async def test_surreal_content_client_allows_two_closed_raw_read_retries(monkeypatch) -> None:
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
            if len(clients) <= 2:
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
    assert len(clients) == 3
    assert clients[0].closed is True
    assert clients[1].closed is True


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


@pytest.mark.asyncio
async def test_surreal_content_client_emits_query_telemetry(monkeypatch) -> None:
    telemetry: list[dict[str, Any]] = []

    class FakeAsyncSurreal:
        def __init__(self, url: str) -> None:
            self.url = url

        async def signin(self, credentials: dict[str, str]) -> None:
            self.credentials = credentials

        async def use(self, namespace: str, database: str) -> None:
            self.namespace = namespace
            self.database = database

        async def query(self, query: str, params: object | None = None) -> list[Any]:
            return []

        async def close(self) -> None:
            return None

    def fake_log_query(query: str, **fields: Any) -> None:
        telemetry.append({"query": query, **fields})

    monkeypatch.setitem(sys.modules, "surrealdb", SimpleNamespace(AsyncSurreal=FakeAsyncSurreal))
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.dedicated_client.query_start",
        lambda: 10.0,
    )
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.dedicated_client.elapsed_ms",
        lambda started_at: 12.3,
    )
    monkeypatch.setattr(
        "sibyl_core.backends.surreal.dedicated_client.log_query",
        fake_log_query,
    )
    client = SurrealContentClient(
        url="ws://localhost:8000/rpc",
        username="root",
        password="root",
    )

    await client.execute_query("SELECT * FROM crawl_sources;")

    assert len(telemetry) == 1
    event = telemetry[0]
    assert {
        "query": event["query"],
        "client_kind": event["client_kind"],
        "namespace": event["namespace"],
        "database": event["database"],
        "raw": event["raw"],
        "elapsed": event["elapsed"],
        "retry_count": event["retry_count"],
    } == {
        "query": "SELECT * FROM crawl_sources;",
        "client_kind": "content",
        "namespace": "sibyl_content",
        "database": "content",
        "raw": False,
        "elapsed": 12.3,
        "retry_count": 0,
    }
    assert event["param_keys"] == []
    assert event["query_label"] is None
    assert str(event["query_origin"]).startswith(__name__ + ":")


def test_surreal_raw_retry_predicate_allows_let_reads() -> None:
    assert _can_retry_raw_query("LET $ids = []; SELECT * FROM document_chunks;")
    assert not _can_retry_raw_query("LET $record = {}; CREATE document_chunks CONTENT $record;")
    assert not _can_retry_raw_query(
        "LET $created = (CREATE document_chunks CONTENT $record); SELECT * FROM document_chunks;"
    )
