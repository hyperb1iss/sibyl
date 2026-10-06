"""SurrealDB graph client cache and schema preparation helpers."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakValueDictionary

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM, bootstrap_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.config import settings
from sibyl_core.embeddings.providers import EmbeddingProvider


class SurrealGraphClient(DedicatedSurrealClient):
    """Dedicated SurrealDB graph client scoped to one organization namespace."""

    def __init__(
        self,
        *,
        group_id: str,
        url: str,
        username: str = "",
        password: str = "",
        token: str = "",
        namespace_prefix: str = "org_",
        database: str = "graph",
        pool_size: int | None = None,
    ) -> None:
        self._group_id = group_id
        super().__init__(
            url=url,
            username=username,
            password=password,
            token=token,
            namespace=_namespace_for_group(namespace_prefix, group_id),
            database=database,
            client_kind="graph",
            pool_size=pool_size,
        )

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def is_embedded(self) -> bool:
        """Whether this client runs an in-process engine rather than a server."""
        return is_embedded_surreal_url(self._url)


_prepared_groups: set[str] = set()
_prepare_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()
_client_lock = asyncio.Lock()
_clients: OrderedDict[str, SurrealGraphClient] = OrderedDict()
# Clients evicted from the LRU wait here until they have been idle long enough
# to close. Closing drains a pool behind its in-flight queries, so doing it in
# the evicting request would charge one org's tail latency to another, and a
# request still holding the evicted client keeps working meanwhile.
_retired: list[tuple[SurrealGraphClient, float]] = []
RETIRED_CLIENT_IDLE_SECONDS = 60.0
# Answers about an organization's graph schema that hold until the schema is
# marked dirty: whether the sweep migration has run, the recorded embedding
# dimension. Background passes ask every minute; the schema moves only through
# a bootstrap or a migration, which mark it dirty the same way they already
# invalidate ``_prepared_groups``.
_schema_facts: dict[str, dict[str, Any]] = {}


@dataclass
class _BackgroundLease:
    client: SurrealGraphClient
    users: int = 0
    closing: asyncio.Task[None] | None = None
    # Teardown scheduled while the lease sits idle; the next user cancels it.
    idle_close: asyncio.TimerHandle | None = None


_background_clients: dict[str, _BackgroundLease] = {}
_background_lock = asyncio.Lock()


def _new_graph_client(group_id: str) -> SurrealGraphClient:
    return SurrealGraphClient(
        group_id=group_id,
        url=settings.require_serviceable_surreal_url(),
        username=settings.surreal_username,
        password=settings.surreal_password.get_secret_value(),
        token=settings.surreal_token.get_secret_value(),
        namespace_prefix=settings.surreal_namespace_prefix,
        database=settings.surreal_database,
        pool_size=settings.surreal_client_pool_size("graph"),
    )


async def _close_background_lease(group_id: str, lease: _BackgroundLease) -> None:
    try:
        await lease.client.close()
    finally:
        async with _background_lock:
            if _background_clients.get(group_id) is lease:
                del _background_clients[group_id]


async def _finish_background_cleanup(task: asyncio.Task[None]) -> None:
    """Complete owned socket teardown even if shutdown cancels its waiter again."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


def _start_background_close(group_id: str, lease: _BackgroundLease) -> asyncio.Task[None]:
    """Begin tearing an idle lease down; the caller holds ``_background_lock``."""
    if lease.idle_close is not None:
        lease.idle_close.cancel()
        lease.idle_close = None
    lease.closing = asyncio.create_task(_close_background_lease(group_id, lease))
    return lease.closing


async def _expire_background_lease(group_id: str, lease: _BackgroundLease) -> None:
    """Close a lease nobody has used for the idle window, unless a user came back."""
    async with _background_lock:
        lease.idle_close = None
        if (
            lease.users
            or lease.closing is not None
            or _background_clients.get(group_id) is not lease
        ):
            return
        closing = _start_background_close(group_id, lease)
    await closing


async def _release_background_lease(group_id: str, lease: _BackgroundLease) -> None:
    async with _background_lock:
        lease.users -= 1
        if lease.users == 0:
            idle = settings.surreal_background_client_idle_seconds
            if idle > 0:
                # The pool stays warm for the next pass: the scheduled repairs
                # come back every minute, and a torn-down pool costs a socket
                # handshake per slot before the first query runs.
                lease.idle_close = asyncio.get_running_loop().call_later(
                    idle,
                    lambda: asyncio.ensure_future(_expire_background_lease(group_id, lease)),
                )
            else:
                _start_background_close(group_id, lease)
        closing = lease.closing
    if closing is not None:
        await closing


@asynccontextmanager
async def background_graph_client(group_id: str) -> AsyncIterator[SurrealGraphClient]:
    """Share one network pool across overlapping background operations per org.

    The active-lease registry is separate from the foreground LRU. The pool
    outlives its last user by ``surreal_background_client_idle_seconds`` so
    the next background operation reuses its sockets; a new operation waits
    for any old pool teardown to finish. Embedded stores must share their
    original single writer and memory identity.
    """
    if is_embedded_surreal_url(settings.resolved_surreal_url):
        yield await get_surreal_graph_client(group_id)
        return
    while True:
        async with _background_lock:
            lease = _background_clients.get(group_id)
            if lease is None:
                lease = _BackgroundLease(_new_graph_client(group_id))
                _background_clients[group_id] = lease
            if lease.closing is None:
                if lease.idle_close is not None:
                    lease.idle_close.cancel()
                    lease.idle_close = None
                lease.users += 1
                break
            closing = lease.closing
        await asyncio.shield(closing)
    try:
        yield lease.client
    finally:
        cleanup = asyncio.create_task(_release_background_lease(group_id, lease))
        await _finish_background_cleanup(cleanup)


async def close_idle_background_clients() -> None:
    """Close every background pool with no current user, without waiting for its idle window."""
    async with _background_lock:
        closing = [
            _start_background_close(group_id, lease)
            for group_id, lease in list(_background_clients.items())
            if lease.users == 0 and lease.closing is None
        ]
    if closing:
        await asyncio.gather(*closing, return_exceptions=True)


def validate_native_embedding_dimensions(
    embedding_provider: EmbeddingProvider | None,
) -> None:
    if embedding_provider is None:
        return
    dimensions = embedding_provider.metadata.dimensions
    if dimensions != EMBEDDING_DIM:
        raise ValueError(
            "native embedding provider dimensions "
            f"({dimensions}) must match Surreal graph schema ({EMBEDDING_DIM})"
        )


async def get_surreal_graph_client(group_id: str) -> SurrealGraphClient:
    async with _client_lock:
        client = _clients.get(group_id)
        if client is None:
            client = _new_graph_client(group_id)
            _clients[group_id] = client
            retired_at = time.monotonic()
            while len(_clients) > settings.surreal_graph_client_cache_size:
                evicted_group_id, evicted_client = _clients.popitem(last=False)
                mark_graph_schema_dirty(evicted_group_id)
                _retired.append((evicted_client, retired_at))
        else:
            _clients.move_to_end(group_id)
    return client


async def reap_retired_graph_clients(*, idle_seconds: float = RETIRED_CLIENT_IDLE_SECONDS) -> int:
    """Close evicted clients that have been idle for the whole window.

    Called from the pool health sweep. A retired client still in use keeps
    its sockets until it goes quiet, so no request ever waits on a close.
    """
    now = time.monotonic()
    async with _client_lock:
        due = [
            client
            for client, retired_at in _retired
            if now - retired_at >= idle_seconds and client.idle_seconds >= idle_seconds
        ]
        _retired[:] = [entry for entry in _retired if entry[0] not in due]
    if due:
        await asyncio.gather(*(client.close() for client in due), return_exceptions=True)
    return len(due)


async def close_graph_clients() -> None:
    async with _client_lock:
        clients = list(_clients.values())
        clients.extend(client for client, _ in _retired)
        _clients.clear()
        _retired.clear()
        _prepared_groups.clear()
        _schema_facts.clear()
    await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
    await close_idle_background_clients()


async def prepare_graph_schema(client: SurrealGraphClient) -> None:
    """Prepare once per org, coordinating embedded DDL across its shared store."""
    group_id = client.group_id
    if group_id in _prepared_groups:
        return
    scope = ("store", client._url) if is_embedded_surreal_url(client._url) else ("org", group_id)
    lock = _prepare_locks.setdefault(scope, asyncio.Lock())
    async with lock:
        if group_id in _prepared_groups:
            return
        await bootstrap_schema(cast("Any", client))
        _prepared_groups.add(group_id)


def mark_graph_schema_dirty(group_id: str) -> None:
    _prepared_groups.discard(group_id)
    _schema_facts.pop(group_id, None)


def reset_graph_schema_facts() -> None:
    """Forget every remembered schema answer; tests sharing a process call this."""
    _schema_facts.clear()


def graph_schema_fact(group_id: str, key: str) -> Any:
    """A remembered answer about this organization's graph schema, or None."""
    facts = _schema_facts.get(group_id)
    return facts.get(key) if facts is not None else None


def remember_graph_schema_fact(group_id: str, key: str, value: Any) -> None:
    """Keep an answer until the organization's schema is marked dirty.

    Only answers a schema change can move belong here, and only ones the
    caller has just read from the database; a bootstrap, a migration or an
    evicted client marks the schema dirty and forgets them.
    """
    _schema_facts.setdefault(group_id, {})[key] = value


def _namespace_for_group(prefix: str, group_id: str) -> str:
    if not group_id:
        msg = "group_id is required to resolve a SurrealDB namespace"
        raise ValueError(msg)
    sanitized = group_id.replace("-", "").lower()
    return f"{prefix}{sanitized}"


__all__ = [
    "RETIRED_CLIENT_IDLE_SECONDS",
    "SurrealGraphClient",
    "background_graph_client",
    "close_graph_clients",
    "close_idle_background_clients",
    "get_surreal_graph_client",
    "graph_schema_fact",
    "mark_graph_schema_dirty",
    "prepare_graph_schema",
    "reap_retired_graph_clients",
    "remember_graph_schema_fact",
    "reset_graph_schema_facts",
    "validate_native_embedding_dimensions",
]
