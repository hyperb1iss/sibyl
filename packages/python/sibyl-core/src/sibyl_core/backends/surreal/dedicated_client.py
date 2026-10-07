"""Shared client wrapper for dedicated SurrealDB namespaces."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import structlog

from sibyl_core.backends.surreal.connection import (
    SurrealConnectError,
    SurrealConnectTimeout,
    _can_retry_query,
    _can_retry_raw_query,
    _is_transient_connection_error,
    _query_tokens,
    cached_by_query_text,
    detach_url_secrets,
    withhold_url_secrets_in_envelope,
)
from sibyl_core.backends.surreal.observability import (
    elapsed_ms,
    log_query,
    query_start,
)
from sibyl_core.backends.surreal.protocols import QueryParams, SurrealClient
from sibyl_core.backends.surreal.query_guard import check_native_query_request
from sibyl_core.backends.surreal.url_schemes import (
    is_embedded_surreal_url,
    is_file_backed_surreal_url,
    is_websocket_surreal_url,
    normalize_surreal_url,
    safe_error_detail,
    split_surreal_url,
    surreal_url_scheme,
)

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.schema_version import SurrealExecute

logger = logging.getLogger(__name__)
log = structlog.get_logger()
_MAX_CLOSED_CONNECTION_RETRIES = 2
_MAX_TRANSACTION_CONFLICT_RETRIES = 8
_TRANSACTION_CONFLICT_RETRY_BASE_SECONDS = 0.05
_TRANSACTION_CONFLICT_RETRY_MAX_SECONDS = 1.0
_DEFAULT_POOL_SIZE = 4
# A write is preceded by a liveness probe only when its socket has not proven
# itself recently. A handshake or any answered query counts as proof, so a busy
# connection never pays the extra round trip; one that sat idle long enough
# for a keepalive or proxy to have silently closed it is probed first, so the
# write itself is never the statement that discovers the dead socket.
_WRITE_PREFLIGHT_IDLE_SECONDS = 5.0


def _connect_timeout_seconds(url: str) -> float | None:
    # Embedded stores open a local file rather than a socket, and a cold
    # SurrealKV directory can legitimately take longer than a handshake
    # budget, so only remote URLs get one.
    if is_embedded_surreal_url(url):
        return None
    from sibyl_core.config import core_config

    return core_config.surreal_connect_timeout_seconds


# A file-backed embedded engine keeps its own in-memory index over the files on
# disk, so two engines opened on one path in one process append to the same log
# and read each other's bytes back as corrupt values ("Invalid revision `N` for
# type `Value`"), and neither sees the other's writes. Every client on such a
# path therefore shares one engine. In-memory stores have no files behind
# them, and each connection there stays its own store.
def _shares_embedded_engine(url: str) -> bool:
    return is_file_backed_surreal_url(url)


def _embedded_engine_key(url: str) -> str:
    # Spellings of one directory (a symlinked /tmp, a trailing slash) must map
    # to one engine, or the registry would open the same files twice.
    parts = split_surreal_url(url)
    if parts is None or not parts[1] or "?" in parts[1]:
        return url
    scheme, location = parts
    return f"{scheme}://{os.path.realpath(os.path.expanduser(location))}"


def _surreal_ident(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


class _ConnectableSurrealClient(SurrealClient, Protocol):
    async def connect(self) -> None: ...


class _SharedEmbeddedEngine:
    """The one engine every client on a file-backed embedded URL runs through."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.client: SurrealClient | None = None
        self.leases = 0
        self.lock = asyncio.Lock()


_shared_embedded_engines: dict[str, _SharedEmbeddedEngine] = {}


async def _lease_shared_embedded_engine(
    url: str,
    authenticate: Callable[[SurrealClient], Awaitable[None]],
) -> _SharedEmbeddedEngine:
    key = _embedded_engine_key(url)
    engine = _shared_embedded_engines.get(key)
    if engine is None:
        engine = _SharedEmbeddedEngine(key)
        _shared_embedded_engines[key] = engine
    # Counted before the first await so a concurrent last release cannot close
    # the engine while this lease is still opening it.
    engine.leases += 1
    try:
        async with engine.lock:
            if engine.client is None:
                from surrealdb import AsyncSurreal

                client = cast(SurrealClient, AsyncSurreal(url))
                try:
                    # Open the store once, here, instead of lazily inside
                    # whichever scoped query happens to run first.
                    await cast("_ConnectableSurrealClient", client).connect()
                    await authenticate(client)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await client.close()
                    raise
                engine.client = client
    except BaseException:
        await _release_shared_embedded_engine(engine)
        raise
    return engine


async def _release_shared_embedded_engine(engine: _SharedEmbeddedEngine) -> None:
    engine.leases -= 1
    if engine.leases > 0:
        return
    async with engine.lock:
        if engine.leases > 0:
            return
        client = engine.client
        engine.client = None
        try:
            if client is not None:
                await _close_to_completion(client)
        finally:
            # Dropped only after the close finishes: a lease that arrives
            # meanwhile waits on this engine's lock and reopens it, rather than
            # opening a second engine on files that are still being closed.
            if engine.leases == 0 and _shared_embedded_engines.get(engine.key) is engine:
                del _shared_embedded_engines[engine.key]


async def _finish_despite_cancellation(awaitable: Awaitable[object]) -> None:
    """Run teardown to completion even when the awaiting task is cancelled.

    Cancellation is absorbed until the work finishes and then re-raised, so a
    caller that is torn down mid-close still releases what it held.
    """
    work = asyncio.ensure_future(awaitable)
    cancelled = False
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled = True
    work.result()
    if cancelled:
        raise asyncio.CancelledError


async def _close_to_completion(client: SurrealClient) -> None:
    """Finish closing an engine even when the releasing task is cancelled.

    The caller holds the engine's lock until this returns, so no new lease can
    open a second engine on the directory while the old one is still closing.
    """
    await _finish_despite_cancellation(client.close())


def _without_scope_result(response: object) -> object:
    """Drop the leading USE statement's envelope from a scoped response."""
    if not isinstance(response, dict):
        return response
    statements = response.get("result")
    if not isinstance(statements, list) or not statements:
        return response
    scope, *rest = statements
    if isinstance(scope, dict) and scope.get("status") == "ERR":
        from surrealdb.errors import parse_query_error

        raise parse_query_error(scope)
    return {**response, "result": rest}


class _EmbeddedNamespaceSession:
    """One namespace's handle on a shared embedded engine.

    The engine has a single session, and a USE on it would move every other
    client sharing it. Each query instead opens with its own USE statement,
    which scopes only that execution, so clients in different namespaces run
    concurrently on the one engine. The session itself never selects a
    namespace, so a query that somehow skipped the scope fails with "Specify a
    namespace to use" instead of landing in another tenant's namespace.
    """

    def __init__(self, engine: _SharedEmbeddedEngine, namespace: str, database: str) -> None:
        self._engine: _SharedEmbeddedEngine | None = engine
        self._scope = ""
        self._set_scope(namespace, database)

    def _set_scope(self, namespace: str, database: str) -> None:
        self._scope = f"USE NS {_surreal_ident(namespace)} DB {_surreal_ident(database)}; "

    def _client(self) -> SurrealClient:
        engine = self._engine
        if engine is None or engine.client is None:
            raise RuntimeError("SurrealDB embedded session is closed")
        return engine.client

    async def authenticate(self, token: str) -> None:
        await self._client().authenticate(token)

    async def signin(self, vars: QueryParams) -> str:
        return await self._client().signin(vars)

    async def use(self, namespace: str, database: str) -> None:
        self._set_scope(namespace, database)

    async def query(self, query: str, vars: QueryParams | None = None) -> object:
        return _checked_query_result(await self.query_raw(query, vars))

    async def query_raw(self, query: str, params: QueryParams | None = None) -> object:
        prepared_query = self._scope + query
        await check_native_query_request(prepared_query, params)
        response = await self._client().query_raw(prepared_query, params)
        return _without_scope_result(response)

    async def live(self, table: str, *, diff: bool = False) -> object:
        raise RuntimeError("SurrealDB live queries require a WebSocket URL")

    async def subscribe_live(self, query_uuid: object) -> AsyncIterator[object]:
        raise RuntimeError("SurrealDB live queries require a WebSocket URL")

    async def kill(self, query_uuid: object) -> object:
        raise RuntimeError("SurrealDB live queries require a WebSocket URL")

    async def close(self) -> None:
        engine = self._engine
        self._engine = None
        if engine is not None:
            await _release_shared_embedded_engine(engine)


def _checked_query_result(response: object, *, all_results: bool = False) -> object:
    """Validate every statement before selecting the requested result shape."""
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
            if isinstance(message, str) and is_retryable_transaction_conflict(message):
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
    if all_results:
        return [statement["result"] for statement in statements]
    return statements[0]["result"]


# A commit that lost a race says so in two wordings. A SurrealDB 3.x server
# reports "Transaction conflict: <reason>. This transaction can be retried";
# the engine the Python SDK embeds (surrealdb-core 2.3) reports "Failed to
# commit transaction due to a read or write conflict. This transaction can be
# retried". Both markers require the retry suffix, so an error that merely
# mentions a conflict is never replayed.
_TRANSACTION_CONFLICT_MARKERS = ("transaction conflict", "read or write conflict")
_TRANSACTION_RETRY_MARKER = "can be retried"


def is_retryable_transaction_conflict(exc: BaseException | str) -> bool:
    """Whether SurrealDB rejected a commit only because another commit won."""
    message = str(exc).lower()
    return _TRANSACTION_RETRY_MARKER in message and any(
        marker in message for marker in _TRANSACTION_CONFLICT_MARKERS
    )


def _can_replay_query(query: str, response: object = None) -> bool:
    """Require one atomic unit before replaying a conflict."""
    if isinstance(response, dict):
        results = response.get("result")
        # The server emits one result per statement. This also recognizes an
        # implicit transaction containing semicolons inside strings or blocks.
        if isinstance(results, list) and len(results) == 1:
            return True
    return _query_text_is_atomic(query)


@cached_by_query_text
def _query_text_is_atomic(query: str) -> bool:
    statements = _top_level_statements(query)
    if len(statements) == 1:
        # One statement, which includes a lone RETURN { ... } block: its inner
        # semicolons sit at brace depth one, so the block runs as a single
        # statement in its own transaction and replays like one. The response
        # shortcut above already said so; this answers before a response
        # exists, which is what a conflict raised as an exception needs.
        return True
    if not statements or statements[0] not in {"BEGIN", "BEGIN TRANSACTION"}:
        return False
    if statements[-1] not in {"COMMIT", "COMMIT TRANSACTION"}:
        return False
    # Count conservatively, including comments and strings: extra transaction
    # words may suppress a retry, but cannot hide an intervening commit.
    tokens = _query_tokens(query)
    return tokens.count("BEGIN") == 1 and tokens.count("COMMIT") == 1 and "CANCEL" not in tokens


def _top_level_statements(query: str) -> list[str]:
    """Split on the semicolons at brace depth zero, upper-cased and stripped.

    A RETURN { ... } block carries its own statements inside the braces; they
    belong to the block, not to the query. Braces are counted without parsing
    strings or comments, so a brace quoted inside a literal can unbalance the
    count; an unbalanced query falls back to the plain split, which only ever
    reports more statements and therefore never replays what it should not.
    """
    statements: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(query):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                break
        elif char == ";" and depth == 0:
            statements.append(query[start:index])
            start = index + 1
    else:
        if depth == 0:
            statements.append(query[start:])
            return [part.strip().upper() for part in statements if part.strip()]
    return [part.strip().upper() for part in query.split(";") if part.strip()]


@dataclass(frozen=True, slots=True)
class PoolHealth:
    """Outcome of one pool sweep: slots checked, slots dropped, and why."""

    checked: int
    reaped: int
    failures: tuple[str, ...] = ()


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
        self._verified_at: float | None = None

    def mark_verified(self) -> None:
        """Record that the server just answered on this socket."""
        self._verified_at = time.monotonic()

    def verified_within(self, seconds: float) -> bool:
        """Whether the server answered on this socket inside the last window."""
        verified_at = self._verified_at
        return verified_at is not None and time.monotonic() - verified_at < seconds

    @property
    def connected(self) -> bool:
        """Whether this slot currently holds a socket (or an embedded session)."""
        return self._client is not None

    async def connect(self, *, attempt: int = 1) -> SurrealClient:
        if self._client is not None:
            return self._client

        async with self._connect_lock:
            if self._client is not None:
                return self._client

            if _shares_embedded_engine(self._url):
                return await self._connect_shared_embedded()

            from surrealdb import AsyncSurreal

            started_at = query_start()
            budget = _connect_timeout_seconds(self._url)
            connect_failure: SurrealConnectError | None = None
            try:
                client = cast(SurrealClient, AsyncSurreal(self._url))
            except Exception as exc:
                connect_failure = self._log_connect_error(exc, attempt, started_at, budget)
            if connect_failure is not None:
                raise connect_failure
            timeout_failure: SurrealConnectTimeout | None = None
            try:
                async with asyncio.timeout(budget):
                    await self._handshake(client)
            except asyncio.CancelledError:
                # A warm or query cancelled mid-handshake would otherwise strand
                # the half-open client, since it was never stored on the slot.
                with contextlib.suppress(Exception):
                    await _finish_despite_cancellation(client.close())
                raise
            except TimeoutError as exc:
                with contextlib.suppress(Exception):
                    await client.close()
                elapsed = elapsed_ms(started_at)
                # The SDK opens the socket lazily inside the first RPC, so a
                # handshake stall surfaces here rather than on the statement
                # that was about to run. Name it so the receipt cannot blame
                # an innocent query.
                timeout = budget if budget is not None else elapsed / 1000
                failure = SurrealConnectTimeout(
                    url=self._url, attempt=attempt, timeout_seconds=timeout
                )
                log.warning(
                    "surreal_connect_failed",
                    attempt=attempt,
                    elapsed_ms=elapsed,
                    timeout_seconds=budget,
                    url_scheme=surreal_url_scheme(self._url) or "unknown",
                    namespace=self._namespace,
                    database=self._database,
                    # The raised class, so this line joins the query receipt
                    # on error_type; the inner asyncio error is the cause.
                    error_type=type(failure).__name__,
                    cause_type=type(exc).__name__,
                    error_category="connect_timeout",
                )
                timeout_failure = failure
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await client.close()
                connect_failure = self._log_connect_error(exc, attempt, started_at, budget)
            # Raised here, outside the handlers, so neither failure carries the
            # SDK error (whose message can quote the URL) as cause or context.
            if timeout_failure is not None:
                raise timeout_failure
            if connect_failure is not None:
                raise connect_failure
            self._client = client
            # The handshake just signed in and selected a namespace, which is
            # all the proof a preflight would gather.
            self.mark_verified()
            return client

    def _log_connect_error(
        self,
        cause: Exception,
        attempt: int,
        started_at: float,
        budget: float | None,
    ) -> SurrealConnectError:
        """Log a failed connect and build the redacted error to raise.

        The caller raises it after leaving its except block, so the SDK error,
        whose message can quote the full URL, is not chained as its context.
        """
        failure = SurrealConnectError(url=self._url, cause=cause)
        log.warning(
            "surreal_connect_failed",
            attempt=attempt,
            elapsed_ms=elapsed_ms(started_at),
            timeout_seconds=budget,
            url_scheme=failure.url_scheme,
            namespace=self._namespace,
            database=self._database,
            # The raised class, so this line joins the query receipt on
            # error_type; the SDK error's class is the cause.
            error_type=type(failure).__name__,
            cause_type=failure.cause_type,
            error_category="connect_error",
        )
        return failure

    async def _connect_shared_embedded(self) -> SurrealClient:
        started_at = query_start()
        connect_failure: SurrealConnectError | None = None
        try:
            engine = await _lease_shared_embedded_engine(self._url, self._authenticate)
        except Exception as exc:
            connect_failure = self._log_connect_error(exc, 1, started_at, None)
        if connect_failure is not None:
            raise connect_failure
        session = _EmbeddedNamespaceSession(engine, self._namespace, self._database)
        self._client = session
        self.mark_verified()
        return session

    async def _handshake(self, client: SurrealClient) -> None:
        await self._authenticate(client)
        await client.use(self._namespace, self._database)

    async def _authenticate(self, client: SurrealClient) -> None:
        if self._requires_auth():
            if self._token:
                await client.authenticate(self._token)
            elif self._username and self._password:
                await client.signin({"username": self._username, "password": self._password})

    def _requires_auth(self) -> bool:
        # An in-process engine has no users to sign in as.
        return not is_embedded_surreal_url(self._url)

    async def drop(self) -> None:
        async with self._connect_lock:
            await self._close_locked()

    async def close(self) -> None:
        async with self._connect_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        client = self._client
        self._client = None
        self._verified_at = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.debug(
                    "SurrealDB pooled connection close failed: %s", _loggable(exc, self._url)
                )


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
        # The SDK's embedded engine rejects an uppercase scheme, so every URL
        # is normalized once here, where it enters the client.
        url = normalize_surreal_url(url)
        self._url = url
        self._username = username
        self._password = password
        self._token = token
        self._namespace = namespace
        self._database = database
        self._client_kind = client_kind
        # Embedded URLs are hard-clamped to one connection regardless of any
        # configured pool size, and this is a correctness boundary. The engine
        # the Python SDK embeds (surrealdb-core 2.3) misses write-write
        # conflicts, even inside BEGIN/COMMIT: two queries in flight can both
        # commit a read-modify-write from the same stale read. The clamp keeps
        # this client to one query in flight. It does not make a namespace
        # single-writer: that comes from the builders handing out one shared
        # client per namespace (auth, content, and the per-org graph LRU), and
        # two clients on one namespace still lose updates even at one
        # connection each. The compare-and-set fences in dream checkpoints,
        # source states, revisions, schema leases, and write witnesses depend
        # on both. Sites that build a second client on a namespace are tracked
        # in Sibyl task 192bf5f7. (In-memory stores also hand every connection
        # a fresh store, so a pool there would fragment it.)
        # packages/python/sibyl-core/tests/test_embedded_shared_engine.py
        # enforces the clamp and pins the lost updates with a strict xfail.
        if is_embedded_surreal_url(url):
            self._pool_size = 1
        else:
            requested = pool_size if pool_size is not None else _DEFAULT_POOL_SIZE
            self._pool_size = max(1, requested)
        self._pool: list[_PooledConnection] = [
            self._new_connection() for _ in range(self._pool_size)
        ]
        # Slots connect lazily, and the slot a query returns goes back on top,
        # so sequential queries keep reusing the one socket they warmed. A
        # first-in-first-out queue rotated every query across every slot, which
        # opened a socket (TCP, WebSocket, signin, USE) per slot before any real
        # concurrency asked for it. Cold slots still connect the moment a burst
        # needs them, so capacity is unchanged.
        self._available: asyncio.LifoQueue[_PooledConnection] = asyncio.LifoQueue()
        for connection in self._pool:
            self._available.put_nowait(connection)
        self._close_lock = asyncio.Lock()
        self._last_used_at = time.monotonic()

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def database(self) -> str:
        return self._database

    @property
    def supports_live_queries(self) -> bool:
        return is_websocket_surreal_url(self._url)

    @property
    def idle_seconds(self) -> float:
        """Seconds since a query last started on this client."""
        return time.monotonic() - self._last_used_at

    def _new_connection(self) -> _PooledConnection:
        return _PooledConnection(
            url=self._url,
            username=self._username,
            password=self._password,
            token=self._token,
            namespace=self._namespace,
            database=self._database,
        )

    @asynccontextmanager
    async def schema_lease_executor(self) -> AsyncIterator[SurrealExecute]:
        """Reserve renewal capacity without competing with graph query sockets."""
        if is_embedded_surreal_url(self._url):
            yield self.execute_query
            return
        control = DedicatedSurrealClient(
            url=self._url,
            username=self._username,
            password=self._password,
            token=self._token,
            namespace=self._namespace,
            database=self._database,
            client_kind=self._client_kind,
            pool_size=1,
        )
        try:
            yield control.execute_query
        finally:
            await control.close()

    async def connect(self) -> SurrealClient:
        return await _at_boundary(self._url, self._connect_one())

    async def _connect_one(self) -> SurrealClient:
        connection = await self._available.get()
        try:
            return await connection.connect()
        finally:
            self._available.put_nowait(connection)

    @property
    def pool_size(self) -> int:
        """Return the existing query capacity, including embedded-mode clamping."""
        return self._pool_size

    async def execute_query(self, query: str, **params: object) -> object:
        query_label = _pop_query_label(params)
        return await _at_boundary(
            self._url,
            self._execute(
                query,
                params=params,
                raw=False,
                query_label=query_label,
                query_origin=_caller_origin(),
            ),
        )

    async def execute_query_batch(self, query: str, **params: object) -> object:
        """Return every statement result after checking the complete response."""
        query_label = _pop_query_label(params)
        return await _at_boundary(
            self._url,
            self._execute(
                query,
                params=params,
                raw=False,
                all_results=True,
                query_label=query_label,
                query_origin=_caller_origin(),
            ),
        )

    async def execute_query_raw(self, query: str, **params: object) -> object:
        query_label = _pop_query_label(params)
        response = await _at_boundary(
            self._url,
            self._execute(
                query,
                params=params,
                raw=True,
                query_label=query_label,
                query_origin=_caller_origin(),
            ),
        )
        # Raw envelopes leave the client unraised, and callers raise their ERR
        # text later, so that text passes the same URL check here.
        return withhold_url_secrets_in_envelope(response, self._url)

    async def close(self) -> None:
        await _at_boundary(self._url, self._close_pool())

    async def _close_pool(self) -> None:
        async with self._close_lock:
            # Drain the pool before closing so a checked-out connection is never
            # closed mid-query: each get() blocks until an in-flight query
            # returns its connection. Closed connections go back in the queue so
            # a later query reconnects them lazily.
            drained: list[_PooledConnection] = []
            try:
                for _ in range(self._pool_size):
                    drained.append(await self._available.get())
                # Shielded so a caller cancelled mid-close cannot cancel a
                # connection's close before it runs, which would leave that
                # connection holding its socket or shared-engine lease.
                await _finish_despite_cancellation(
                    asyncio.gather(
                        *(connection.close() for connection in drained),
                        return_exceptions=True,
                    )
                )
            finally:
                for connection in drained:
                    self._available.put_nowait(connection)

    async def warm_pool(self) -> None:
        await _at_boundary(self._url, self._warm_pool())

    async def _warm_pool(self) -> None:
        drained: list[_PooledConnection] = []
        try:
            # Drained inside the try, so a warm cancelled while it waits on a
            # busy slot returns the slots it already took instead of shrinking
            # the pool for good.
            for _ in range(self._pool_size):
                drained.append(await self._available.get())
            try:
                await asyncio.gather(*(connection.connect() for connection in drained))
            except BaseException:
                # BaseException, not Exception: a cancelled warm would otherwise
                # leave the sockets it already opened behind on a client nobody
                # holds.
                await asyncio.gather(
                    *(connection.drop() for connection in drained),
                    return_exceptions=True,
                )
                raise
        finally:
            for connection in drained:
                self._available.put_nowait(connection)

    async def ping_pool(self) -> PoolHealth:
        """Check every idle slot that holds a socket, dropping the ones whose socket is gone.

        The idle slots come off the pool together, so none is checked twice,
        and each goes back the moment its own check ends, so a query arriving
        mid-sweep waits for at most one ping. A busy slot is being proved
        healthy by the query holding it, and a slot that never connected has
        no socket to lose; neither is touched, so the sweep spends no
        handshake. A dropped slot reconnects lazily on its next query rather
        than on a request path.
        """
        checked = 0
        reaped = 0
        failures: list[str] = []
        idle: list[_PooledConnection] = []
        while True:
            try:
                idle.append(self._available.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Coldest first, so the slot queries warmed most recently is back on
        # top of the pool when the sweep ends.
        remaining = list(idle)
        try:
            while remaining:
                connection = remaining.pop()
                try:
                    if not connection.connected:
                        continue
                    checked += 1
                    try:
                        client = await connection.connect()
                        await self._send_query(client, "RETURN true;", params={}, raw=False)
                        connection.mark_verified()
                    except Exception as exc:
                        failures.append(type(exc).__name__)
                        reaped += 1
                        await connection.drop()
                finally:
                    self._available.put_nowait(connection)
        finally:
            for connection in remaining:
                self._available.put_nowait(connection)
        return PoolHealth(checked=checked, reaped=reaped, failures=tuple(failures))

    @asynccontextmanager
    async def live_table(
        self, table: str, *, diff: bool = False
    ) -> AsyncIterator[AsyncIterator[object]]:
        if not self.supports_live_queries:
            raise RuntimeError("SurrealDB live queries require a WebSocket URL")

        connection = self._new_connection()
        client = await _at_boundary(self._url, connection.connect())
        query_uuid = await _at_boundary(self._url, client.live(table, diff=diff))
        try:
            yield await _at_boundary(self._url, client.subscribe_live(query_uuid))
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
        all_results: bool = False,
        query_label: str | None,
        query_origin: str | None,
    ) -> object:
        connection_retry_count = 0
        transaction_retry_count = 0
        result: object = None
        can_retry = _can_retry_raw_query(query) if raw else _can_retry_query(query)
        # Time parked on the queue is pool pressure, not query time, and is
        # reported on its own so a saturated pool never reads as slow queries.
        wait_started = query_start()
        connection = await self._available.get()
        pool_wait = elapsed_ms(wait_started)
        slot_held = True
        self._last_used_at = time.monotonic()
        started_at = query_start()
        # Backoff sleeps and re-checkouts happen inside the timed window and
        # are subtracted so elapsed stays the socket time.
        off_socket_ms = 0.0
        try:
            while True:
                transaction_retry_allowed = _can_replay_query(query)
                try:
                    if not can_retry:
                        while True:
                            try:
                                client = await connection.connect(
                                    attempt=connection_retry_count + 1
                                )
                                # A fresh handshake or a recent answer already
                                # proved this socket; only an idle one is probed.
                                if connection.verified_within(_WRITE_PREFLIGHT_IDLE_SECONDS):
                                    break
                                await self._send_query(
                                    client,
                                    "RETURN true;",
                                    params={},
                                    raw=False,
                                )
                                connection.mark_verified()
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
                                    _loggable(exc, self._url),
                                )
                    client = await connection.connect(attempt=connection_retry_count + 1)
                    response = await self._send_query(client, query, params=params, raw=True)
                    # Any envelope, OK or ERR, came back over this socket.
                    connection.mark_verified()
                    transaction_retry_allowed = _can_replay_query(query, response)
                    if raw:
                        # Raw callers retain statement envelopes, but an atomic
                        # server-declared conflict still belongs to this owner.
                        if transaction_retry_allowed and isinstance(response, dict):
                            statements = response.get("result")
                            if isinstance(statements, list) and any(
                                isinstance(statement, dict)
                                and statement.get("status") == "ERR"
                                and isinstance(statement.get("result"), str)
                                and is_retryable_transaction_conflict(statement["result"])
                                for statement in statements
                            ):
                                _checked_query_result(response)
                        result = response
                    else:
                        result = _checked_query_result(response, all_results=all_results)
                    break
                except Exception as exc:
                    if transaction_retry_allowed and is_retryable_transaction_conflict(exc):
                        if transaction_retry_count >= _MAX_TRANSACTION_CONFLICT_RETRIES:
                            raise
                        transaction_retry_count += 1
                        delay = _transaction_conflict_retry_delay(transaction_retry_count)
                        logger.warning(
                            "SurrealDB transaction conflict; retrying attempt=%s delay=%.3fs "
                            "error=%s",
                            transaction_retry_count,
                            delay,
                            _loggable(exc, self._url),
                        )
                        # Hand the slot back while backing off: a conflict storm
                        # is exactly when the rest of the namespace needs it.
                        self._available.put_nowait(connection)
                        slot_held = False
                        backoff_started = query_start()
                        await asyncio.sleep(delay)
                        reacquire_started = query_start()
                        connection = await self._available.get()
                        slot_held = True
                        pool_wait += elapsed_ms(reacquire_started)
                        off_socket_ms += elapsed_ms(backoff_started)
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
                        _loggable(exc, self._url),
                    )
        except Exception as exc:
            log_query(
                query,
                client_kind=self._client_kind,
                namespace=self._namespace,
                database=self._database,
                raw=raw,
                elapsed=max(0.0, elapsed_ms(started_at) - off_socket_ms),
                param_keys=sorted(params),
                query_label=query_label,
                query_origin=query_origin,
                retry_count=connection_retry_count + transaction_retry_count,
                error=exc,
                pool_wait_ms=pool_wait,
            )
            raise
        finally:
            if slot_held:
                self._available.put_nowait(connection)
        log_query(
            query,
            client_kind=self._client_kind,
            namespace=self._namespace,
            database=self._database,
            raw=raw,
            elapsed=max(0.0, elapsed_ms(started_at) - off_socket_ms),
            param_keys=sorted(params),
            query_label=query_label,
            query_origin=query_origin,
            retry_count=connection_retry_count + transaction_retry_count,
            pool_wait_ms=pool_wait,
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
        if not isinstance(client, _EmbeddedNamespaceSession):
            await check_native_query_request(query, bound_params)
        if raw:
            return await client.query_raw(query, bound_params)
        return _checked_query_result(await client.query_raw(query, bound_params))


def _loggable(error: BaseException, url: str) -> str:
    """An error for a log line: its message when it quotes nothing from the URL."""
    return safe_error_detail(error, url) or type(error).__name__


async def _at_boundary[T](url: str, operation: Awaitable[T]) -> T:
    """Await an operation, replacing any error that quotes the URL on the way out.

    Every exception leaving a DedicatedSurrealClient passes through here. A
    clean error is re-raised unchanged, so callers keep matching on its type
    and message. One that quotes a secret piece of the URL, in any spelling,
    anywhere in its chain, is replaced by detach_url_secrets() and raised
    after the handler exits, with no cause or context. Cancellation passes
    straight through.
    """
    failure: Exception | None = None
    try:
        return await operation
    except Exception as exc:
        failure = detach_url_secrets(exc, url)
        if failure is None:
            raise
    raise failure


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


__all__ = ["DedicatedSurrealClient", "PoolHealth", "is_retryable_transaction_conflict"]
