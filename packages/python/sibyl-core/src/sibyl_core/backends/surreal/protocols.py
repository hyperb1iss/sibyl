"""Structural contracts for the SurrealDB Python client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.schema_version import SurrealExecute

type QueryParams = dict[str, object]


class SurrealClient(Protocol):
    async def authenticate(self, token: str) -> None: ...

    async def signin(self, vars: QueryParams) -> str: ...

    async def use(self, namespace: str, database: str) -> None: ...

    async def query(self, query: str, vars: QueryParams | None = None) -> object: ...

    async def query_raw(self, query: str, params: QueryParams | None = None) -> object: ...

    async def live(self, table: str, *, diff: bool = False) -> object: ...

    async def subscribe_live(self, query_uuid: object) -> AsyncIterator[object]: ...

    async def kill(self, query_uuid: object) -> object: ...

    async def close(self) -> None: ...


class SchemaDriver(Protocol):
    """Minimal contract schema bootstrap needs: connection URL, org scope, query execution."""

    _url: str

    @property
    def group_id(self) -> str: ...

    def schema_lease_executor(self) -> AbstractAsyncContextManager[SurrealExecute]: ...

    async def execute_query(self, query: str, **params: object) -> object: ...


__all__ = ["QueryParams", "SchemaDriver", "SurrealClient"]
