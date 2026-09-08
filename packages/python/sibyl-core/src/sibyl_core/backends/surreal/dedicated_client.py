"""Shared client wrapper for dedicated SurrealDB namespaces."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from sibyl_core.backends.surreal.connection import (
    _can_retry_query,
    _can_retry_raw_query,
    _is_transient_connection_error,
    _query_tokens,
)
from sibyl_core.backends.surreal.observability import (
    elapsed_ms,
    log_query,
    query_start,
)
from sibyl_core.backends.surreal.protocols import QueryParams, SurrealClient

logger = logging.getLogger(__name__)
_MAX_CLOSED_CONNECTION_RETRIES = 2
_MAX_TRANSACTION_CONFLICT_RETRIES = 8
_TRANSACTION_CONFLICT_RETRY_BASE_SECONDS = 0.05
_TRANSACTION_CONFLICT_RETRY_MAX_SECONDS = 1.0
_DEFAULT_POOL_SIZE = 4


_EMBEDDED_URL_SCHEMES = ("memory://", "surrealkv://", "rocksdb://", "file://")


def _is_embedded_url(url: str) -> bool:
    # Embedded stores are single-writer and `memory://` hands out a fresh empty
    # database per connection, so a pool there would fragment state.
    return url.startswith(_EMBEDDED_URL_SCHEMES)


def _checked_query_result(response: object) -> object:
    """Validate every statement before returning the SDK's first-result shape."""
    from surrealdb.errors import SurrealError, parse_query_error, parse_rpc_error

    if not isinstance(response, dict):
        raise SurrealError("Invalid SurrealDB query response envelope")
    if (error := response.get("error")) is not None:
        if not isinstance(error, dict):
            raise SurrealError("Invalid SurrealDB RPC error envelope")
        raise parse_rpc_error(error)
    statements = response.get("result")
    if not isinstance(statements, list) or not statements:
        raise SurrealError("Missing SurrealDB query statement results")
    errors: list[dict[str, object]] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("status") not in {"OK", "ERR"}:
            raise SurrealError("Invalid SurrealDB query statement envelope")
        if "result" not in statement:
            raise SurrealError("Missing SurrealDB query statement result")
        if statement["status"] == "ERR":
            errors.append(statement)
    if errors:
        # An aborted transaction marks preceding statements NotExecuted. Surface
        # the causal error so callers retain its structured type and retry signal.
        for error in errors:
            details = error.get("details")
            message = error.get("result")
            if isinstance(message, str) and _is_retryable_transaction_conflict(message):
                raise parse_query_error(error)
            if isinstance(message, str) and message in {
                "The query was not executed due to a failed transaction",
                "The query was not executed due to a cancelled transaction",
            }:
                continue
            if not isinstance(details, dict) or details.get("kind") not in {
                "NotExecuted",
                "Cancelled",
            }:
                raise parse_query_error(error)
        raise parse_query_error(errors[0])
    return statements[0]["result"]


def _is_retryable_transaction_conflict(exc: BaseException | str) -> bool:
    message = str(exc).lower()
    return "transaction conflict" in message and "can be retried" in message


def _can_replay_query(query: str, response: object = None) -> bool:
    """Require one atomic unit before replaying a conflict."""
    if isinstance(response, dict):
        results = response.get("result")
        # The server emits one result per statement. This also recognizes an
        # implicit transaction containing semicolons inside strings or blocks.
        if isinstance(results, list) and len(results) == 1:
            return True
    statements = [part.strip().upper() for part in query.split(";") if part.strip()]
    if len(statements) == 1:
        return True
    if not statements or statements[0] not in {"BEGIN", "BEGIN TRANSACTION"}:
        return False
    if statements[-1] not in {"COMMIT", "COMMIT TRANSACTION"}:
        return False
    # Count conservatively, including comments and strings: extra transaction
    # words may suppress a retry, but cannot hide an intervening commit.
    tokens = _query_tokens(query)
    return tokens.count("BEGIN") == 1 and tokens.count("COMMIT") == 1 and "CANCEL" not in tokens


def _transaction_conflict_retry_delay(retry_count: int) -> float:
    ceiling = min(
        _TRANSACTION_CONFLICT_RETRY_MAX_SECONDS,
        _TRANSACTION_CONFLICT_RETRY_BASE_SECONDS * (2 ** (retry_count - 1)),
    )
    return random.uniform(0.0, ceiling)


class _PooledConnection:
    """One independent SurrealDB socket, used by at most one query at a time."""

    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        token: str,
        namespace: str,
        database: str,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._token = token
        self._namespace = namespace
        self._database = database
        self._client: SurrealClient | None = None
        self._connect_lock = asyncio.Lock()

    async def connect(self) -> SurrealClient:
        if self._client is not None:
            return self._client

        async with self._connect_lock:
            if self._client is not None:
                return self._client

            from surrealdb import AsyncSurreal

            client = cast(SurrealClient, AsyncSurreal(self._url))
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
                with contextlib.suppress(Exception):
                    await client.close()
                raise
            self._client = client
            return client

    def _requires_auth(self) -> bool:
        return not self._url.startswith(("memory://", "surrealkv://"))

    async def drop(self) -> None:
        async with self._connect_lock:
            await self._close_locked()

    async def close(self) -> None:
        async with self._connect_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("SurrealDB pooled connection close failed: %s", exc)


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
        pool_size: int | None = None,
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._token = token
        self._namespace = namespace
        self._database = database
        self._client_kind = client_kind
        # Embedded URLs are hard-clamped to one connection regardless of an
        # explicit pool_size: a pool over memory:// would fragment tenant state.
        if _is_embedded_url(url):
            self._pool_size = 1
        else:
            requested = pool_size if pool_size is not None else _DEFAULT_POOL_SIZE
            self._pool_size = max(1, requested)
        self._pool: list[_PooledConnection] = [
            self._new_connection() for _ in range(self._pool_size)
        ]
        self._available: asyncio.Queue[_PooledConnection] = asyncio.Queue()
        for connection in self._pool:
            self._available.put_nowait(connection)
        self._close_lock = asyncio.Lock()

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def database(self) -> str:
        return self._database

    @property
    def supports_live_queries(self) -> bool:
        return self._url.startswith(("ws://", "wss://"))

    def _new_connection(self) -> _PooledConnection:
        return _PooledConnection(
            url=self._url,
            username=self._username,
            password=self._password,
            token=self._token,
            namespace=self._namespace,
            database=self._database,
        )

    async def connect(self) -> SurrealClient:
        connection = await self._available.get()
        try:
            return await connection.connect()
        finally:
            self._available.put_nowait(connection)

    async def execute_query(self, query: str, **params: object) -> object:
        query_label = _pop_query_label(params)
        return await self._execute(
            query,
            params=params,
            raw=False,
            query_label=query_label,
            query_origin=_caller_origin(),
        )

    async def execute_query_raw(self, query: str, **params: object) -> object:
        query_label = _pop_query_label(params)
        return await self._execute(
            query,
            params=params,
            raw=True,
            query_label=query_label,
            query_origin=_caller_origin(),
        )

    async def close(self) -> None:
        async with self._close_lock:
            # Drain the pool before closing so a checked-out connection is never
            # closed mid-query: each get() blocks until an in-flight query
            # returns its connection. Closed connections go back in the queue so
            # a later query reconnects them lazily.
            drained = [await self._available.get() for _ in range(self._pool_size)]
            try:
                await asyncio.gather(
                    *(connection.close() for connection in drained),
                    return_exceptions=True,
                )
            finally:
                for connection in drained:
                    self._available.put_nowait(connection)

    async def warm_pool(self) -> None:
        drained = [await self._available.get() for _ in range(self._pool_size)]
        try:
            await asyncio.gather(*(connection.connect() for connection in drained))
        except Exception:
            await asyncio.gather(
                *(connection.drop() for connection in drained),
                return_exceptions=True,
            )
            raise
        finally:
            for connection in drained:
                self._available.put_nowait(connection)

    async def ping(self) -> None:
        connection = await self._available.get()
        try:
            client = await connection.connect()
            await self._send_query(client, "RETURN true;", params={}, raw=False)
        except Exception as exc:
            if _is_transient_connection_error(exc):
                await connection.drop()
            raise
        finally:
            self._available.put_nowait(connection)

    @asynccontextmanager
    async def live_table(
        self, table: str, *, diff: bool = False
    ) -> AsyncIterator[AsyncIterator[object]]:
        if not self.supports_live_queries:
            raise RuntimeError("SurrealDB live queries require a WebSocket URL")

        connection = self._new_connection()
        client = await connection.connect()
        query_uuid = await client.live(table, diff=diff)
        try:
            yield await client.subscribe_live(query_uuid)
        finally:
            with contextlib.suppress(Exception):
                await client.kill(query_uuid)
            await connection.close()

    async def _execute(
        self,
        query: str,
        *,
        params: QueryParams,
        raw: bool,
        query_label: str | None,
        query_origin: str | None,
    ) -> object:
        started_at = query_start()
        connection_retry_count = 0
        transaction_retry_count = 0
        result: object = None
        can_retry = _can_retry_raw_query(query) if raw else _can_retry_query(query)
        connection = await self._available.get()
        try:
            while True:
                transaction_retry_allowed = _can_replay_query(query)
                try:
                    if not can_retry:
                        while True:
                            try:
                                client = await connection.connect()
                                await self._send_query(
                                    client,
                                    "RETURN true;",
                                    params={},
                                    raw=False,
                                )
                                break
                            except Exception as exc:
                                if not _is_transient_connection_error(exc):
                                    raise
                                await connection.drop()
                                if connection_retry_count >= _MAX_CLOSED_CONNECTION_RETRIES:
                                    raise
                                connection_retry_count += 1
                                logger.warning(
                                    "SurrealDB dedicated client connection failed during "
                                    "write preflight; retrying attempt=%s error=%s",
                                    connection_retry_count,
                                    exc,
                                )
                    client = await connection.connect()
                    response = await self._send_query(client, query, params=params, raw=True)
                    if raw:
                        result = response
                    else:
                        transaction_retry_allowed = _can_replay_query(query, response)
                        result = _checked_query_result(response)
                    break
                except Exception as exc:
                    if transaction_retry_allowed and _is_retryable_transaction_conflict(exc):
                        if transaction_retry_count >= _MAX_TRANSACTION_CONFLICT_RETRIES:
                            raise
                        transaction_retry_count += 1
                        delay = _transaction_conflict_retry_delay(transaction_retry_count)
                        logger.warning(
                            "SurrealDB transaction conflict; retrying attempt=%s delay=%.3fs "
                            "error=%s",
                            transaction_retry_count,
                            delay,
                            exc,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if not _is_transient_connection_error(exc):
                        raise
                    await connection.drop()
                    if not can_retry or connection_retry_count >= _MAX_CLOSED_CONNECTION_RETRIES:
                        raise
                    connection_retry_count += 1
                    logger.warning(
                        "SurrealDB dedicated client connection failed during read; retrying "
                        "attempt=%s error=%s",
                        connection_retry_count,
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
                param_keys=sorted(params),
                query_label=query_label,
                query_origin=query_origin,
                retry_count=connection_retry_count + transaction_retry_count,
                error=exc,
            )
            raise
        finally:
            self._available.put_nowait(connection)
        log_query(
            query,
            client_kind=self._client_kind,
            namespace=self._namespace,
            database=self._database,
            raw=raw,
            elapsed=elapsed_ms(started_at),
            param_keys=sorted(params),
            query_label=query_label,
            query_origin=query_origin,
            retry_count=connection_retry_count + transaction_retry_count,
        )
        return result

    async def _send_query(
        self,
        client: SurrealClient,
        query: str,
        *,
        params: QueryParams,
        raw: bool,
    ) -> object:
        bound_params = params if params else None
        if raw:
            return await client.query_raw(query, bound_params)
        return _checked_query_result(await client.query_raw(query, bound_params))


def _caller_origin() -> str | None:
    try:
        frame = sys._getframe(2)
    except ValueError:
        return None
    module = frame.f_globals.get("__name__")
    function = frame.f_code.co_name
    if not isinstance(module, str) or not module:
        return None
    return f"{module}:{function}:{frame.f_lineno}"


def _pop_query_label(params: QueryParams) -> str | None:
    value = params.pop("_query_label", None)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


__all__ = ["DedicatedSurrealClient"]
