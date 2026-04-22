"""Dedicated SurrealDB client for Sibyl content storage."""

from __future__ import annotations

from typing import Any


class SurrealContentClient:
    """Small wrapper around AsyncSurreal for the shared content namespace."""

    def __init__(
        self,
        *,
        url: str,
        username: str = "",
        password: str = "",
        namespace: str = "sibyl_content",
        database: str = "content",
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._namespace = namespace
        self._database = database
        self._client: Any | None = None

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def database(self) -> str:
        return self._database

    async def connect(self) -> Any:
        if self._client is not None:
            return self._client

        from surrealdb import AsyncSurreal

        client = AsyncSurreal(self._url)
        if self._requires_auth() and self._username and self._password:
            await client.signin({"username": self._username, "password": self._password})
        await client.use(self._namespace, self._database)
        self._client = client
        return client

    async def execute_query(self, query: str, **params: Any) -> Any:
        client = await self.connect()
        return await client.query(query, params if params else None)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _requires_auth(self) -> bool:
        return not self._url.startswith(("memory://", "surrealkv://"))
