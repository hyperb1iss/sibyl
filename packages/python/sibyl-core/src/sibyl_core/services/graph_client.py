"""SurrealDB graph client cache and schema preparation helpers."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, cast

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
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
        )

    @property
    def group_id(self) -> str:
        return self._group_id


_prepared_groups: set[str] = set()
_prepare_lock = asyncio.Lock()
_client_lock = asyncio.Lock()
_clients: OrderedDict[str, SurrealGraphClient] = OrderedDict()


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
            client = SurrealGraphClient(
                group_id=group_id,
                url=settings.resolved_surreal_url,
                username=settings.surreal_username,
                password=settings.surreal_password.get_secret_value(),
                token=settings.surreal_token.get_secret_value(),
                namespace_prefix=settings.surreal_namespace_prefix,
                database=settings.surreal_database,
            )
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
    group_id = client.group_id
    if group_id in _prepared_groups:
        return
    async with _prepare_lock:
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
    "close_graph_clients",
    "get_surreal_graph_client",
    "mark_graph_schema_dirty",
    "prepare_graph_schema",
    "validate_native_embedding_dimensions",
]
