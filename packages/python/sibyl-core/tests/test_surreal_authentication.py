from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from sibyl_core.backends.surreal import SurrealAuthClient, SurrealContentClient, SurrealDriver


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
