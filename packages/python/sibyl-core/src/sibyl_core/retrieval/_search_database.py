"""Small checked-query boundary shared by native retrieval stages."""

from __future__ import annotations

from typing import Any

from sibyl_core.backends.surreal.records import SurrealQueryError, query_error
from sibyl_core.services.graph import get_surreal_graph_runtime, normalize_records


async def _get_read_only_graph_runtime(organization_id: str) -> Any:
    return await get_surreal_graph_runtime(organization_id, ensure_schema=False)


async def _execute_query_records(
    client: Any,
    query: str,
    **params: object,
) -> list[dict[str, object]]:
    result = await client.execute_query(query, **params)
    if error := query_error(result):
        raise SurrealQueryError(query, error)
    return normalize_records(result)
