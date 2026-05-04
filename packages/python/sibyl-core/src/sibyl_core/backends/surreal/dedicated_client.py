"""Shared client wrapper for dedicated SurrealDB namespaces."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sibyl_core.backends.surreal.connection import (
    _can_retry_query,
    _can_retry_raw_query,
    _is_transient_connection_error,
)
from sibyl_core.backends.surreal.observability import (
    elapsed_ms,
    log_query,
    query_start,
)

logger = logging.getLogger(__name__)
_MAX_CLOSED_CONNECTION_RETRIES = 2


class DedicatedSurrealClient:
    def __init__(
        self,
        *,
        url: str,
        username: str = "",
        password: str = "",
        token: str = "",
        namespace: str,
        database: str,
        client_kind: str = "dedicated",
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._token = token
        self._namespace = namespace
        self._database = database
        self._client_kind = client_kind
        self._client: Any | None = None
        self._connect_lock = asyncio.Lock()
        self._query_lock = asyncio.Lock()

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def database(self) -> str:
        return self._database

    async def connect(self) -> Any:
        if self._client is not None:
            return self._client

        async with self._connect_lock:
            if self._client is not None:
                return self._client

            from surrealdb import AsyncSurreal

            client = AsyncSurreal(self._url)
            try:
                if self._requires_auth():
                    if self._token:
                        await client.authenticate(self._token)
                    elif self._username and self._password:
                        await client.signin(
                            {"username": self._username, "password": self._password}
                        )
                await client.use(self._namespace, self._database)
            except Exception:
                try:
                    await client.close()
                except Exception as exc:
                    logger.debug(
                        "SurrealDB dedicated client close after setup failure failed: %s", exc
                    )
                raise
            self._client = client
            return client

    async def execute_query(self, query: str, **params: Any) -> Any:
        return await self._execute(query, params=params, raw=False)

    async def execute_query_raw(self, query: str, **params: Any) -> Any:
        return await self._execute(query, params=params, raw=True)

    async def close(self) -> None:
        async with self._connect_lock:
            if self._client is not None:
                await self._client.close()
                self._client = None

    def _requires_auth(self) -> bool:
        return not self._url.startswith(("memory://", "surrealkv://"))

    async def _drop_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("SurrealDB dedicated client close after connection failure failed: %s", exc)

    async def _execute(self, query: str, *, params: dict[str, Any], raw: bool) -> Any:
        started_at = query_start()
        retry_count = 0
        try:
            async with self._query_lock:
                while True:
                    try:
                        client = await self.connect()
                        result = await self._send_query(client, query, params=params, raw=raw)
                        break
                    except Exception as exc:
                        if not _is_transient_connection_error(exc):
                            raise
                        await self._drop_client()
                        can_retry = _can_retry_raw_query(query) if raw else _can_retry_query(query)
                        if not can_retry or retry_count >= _MAX_CLOSED_CONNECTION_RETRIES:
                            raise
                        retry_count += 1
                        logger.warning(
                            "SurrealDB dedicated client connection failed during read; retrying "
                            "attempt=%s error=%s",
                            retry_count,
                            exc,
                        )
        except Exception as exc:
            log_query(
                query,
                client_kind=self._client_kind,
                namespace=self._namespace,
                database=self._database,
                raw=raw,
                elapsed=elapsed_ms(started_at),
                retry_count=retry_count,
                error=exc,
            )
            raise
        log_query(
            query,
            client_kind=self._client_kind,
            namespace=self._namespace,
            database=self._database,
            raw=raw,
            elapsed=elapsed_ms(started_at),
            retry_count=retry_count,
        )
        return result

    async def _send_query(
        self,
        client: Any,
        query: str,
        *,
        params: dict[str, Any],
        raw: bool,
    ) -> Any:
        bound_params = params if params else None
        if raw:
            return await client.query_raw(query, bound_params)
        return await client.query(query, bound_params)


__all__ = ["DedicatedSurrealClient"]
