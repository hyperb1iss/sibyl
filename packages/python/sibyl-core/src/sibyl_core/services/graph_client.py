"""SurrealDB graph client cache and schema preparation helpers."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from weakref import WeakValueDictionary

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient, _is_embedded_url
from sibyl_core.backends.surreal.schema import EMBEDDING_DIM, bootstrap_schema
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


_prepared_groups: set[str] = set()
_prepare_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()
_client_lock = asyncio.Lock()
_clients: OrderedDict[str, SurrealGraphClient] = OrderedDict()


@dataclass
class _BackgroundLease:
    client: SurrealGraphClient
    users: int = 0
    closing: asyncio.Task[None] | None = None


_background_clients: dict[str, _BackgroundLease] = {}
_background_lock = asyncio.Lock()


def _new_graph_client(group_id: str) -> SurrealGraphClient:
    return SurrealGraphClient(
        group_id=group_id,
        url=settings.resolved_surreal_url,
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


async def _release_background_lease(group_id: str, lease: _BackgroundLease) -> None:
    async with _background_lock:
        lease.users -= 1
        if lease.users == 0:
            lease.closing = asyncio.create_task(_close_background_lease(group_id, lease))
        closing = lease.closing
    if closing is not None:
        await closing


@asynccontextmanager
async def background_graph_client(group_id: str) -> AsyncIterator[SurrealGraphClient]:
    """Share one network pool across overlapping background operations per org.

    The active-lease registry is separate from the foreground LRU. Its last user
    closes the pool; a new operation waits for any old pool teardown to finish.
    Embedded stores must share their original single writer and memory identity.
    """
    if _is_embedded_url(settings.resolved_surreal_url):
        yield await get_surreal_graph_client(group_id)
        return
    while True:
        async with _background_lock:
            lease = _background_clients.get(group_id)
            if lease is None:
                lease = _BackgroundLease(_new_graph_client(group_id))
                _background_clients[group_id] = lease
            if lease.closing is None:
                lease.users += 1
                break
            closing = lease.closing
        await asyncio.shield(closing)
    try:
        yield lease.client
    finally:
        cleanup = asyncio.create_task(_release_background_lease(group_id, lease))
        await _finish_background_cleanup(cleanup)


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
    evicted: list[SurrealGraphClient] = []
    async with _client_lock:
        client = _clients.get(group_id)
        if client is None:
            client = _new_graph_client(group_id)
            _clients[group_id] = client
            while len(_clients) > settings.surreal_graph_client_cache_size:
                evicted_group_id, evicted_client = _clients.popitem(last=False)
                mark_graph_schema_dirty(evicted_group_id)
                evicted.append(evicted_client)
        else:
            _clients.move_to_end(group_id)
    if evicted:
        await asyncio.gather(*(client.close() for client in evicted), return_exceptions=True)
        return client
    return client


async def close_graph_clients() -> None:
    async with _client_lock:
        clients = list(_clients.values())
        _clients.clear()
        _prepared_groups.clear()
    await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


async def prepare_graph_schema(client: SurrealGraphClient) -> None:
    """Prepare once per org, coordinating embedded DDL across its shared store."""
    group_id = client.group_id
    if group_id in _prepared_groups:
        return
    scope = ("store", client._url) if _is_embedded_url(client._url) else ("org", group_id)
    lock = _prepare_locks.setdefault(scope, asyncio.Lock())
    async with lock:
        if group_id in _prepared_groups:
            return
        await bootstrap_schema(cast("Any", client))
        _prepared_groups.add(group_id)


def mark_graph_schema_dirty(group_id: str) -> None:
    _prepared_groups.discard(group_id)


def _namespace_for_group(prefix: str, group_id: str) -> str:
    if not group_id:
        msg = "group_id is required to resolve a SurrealDB namespace"
        raise ValueError(msg)
    sanitized = group_id.replace("-", "").lower()
    return f"{prefix}{sanitized}"


__all__ = [
    "SurrealGraphClient",
    "background_graph_client",
    "close_graph_clients",
    "get_surreal_graph_client",
    "mark_graph_schema_dirty",
    "prepare_graph_schema",
    "validate_native_embedding_dimensions",
]
