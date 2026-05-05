"""Structural contracts for the SurrealDB Python client."""

from __future__ import annotations

from typing import Protocol

type QueryParams = dict[str, object]


class SurrealClient(Protocol):
    async def authenticate(self, token: str) -> None: ...

    async def signin(self, vars: QueryParams) -> str: ...

    async def use(self, namespace: str, database: str) -> None: ...

    async def query(self, query: str, vars: QueryParams | None = None) -> object: ...

    async def query_raw(self, query: str, params: QueryParams | None = None) -> object: ...

    async def close(self) -> None: ...


__all__ = ["QueryParams", "SurrealClient"]
