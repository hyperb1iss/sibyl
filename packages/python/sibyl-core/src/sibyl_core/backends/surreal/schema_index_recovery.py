"""Recover concurrent index builds while holding namespace schema ownership."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

from sibyl_core.backends.surreal.schema_ownership import SchemaOwnership
from sibyl_core.backends.surreal.schema_version import (
    ConcurrentIndexDefinition,
    IndexBuildFailedError,
    IndexBuildStalledError,
    IndexBuildStatus,
    _first_record,
    _with_concurrently,
    get_index_build_status,
    validate_identifier,
    wait_for_index_ready,
)


async def ensure_owned_concurrent_index(
    ownership: SchemaOwnership,
    definition: ConcurrentIndexDefinition,
    *,
    rebuild: bool = False,
    stall_timeout_seconds: float = 300.0,
) -> IndexBuildStatus:
    """Activate an index, recovering an unfinished build from a previous owner.

    Callers must hold the namespace lease for every schema mutation. An existing
    nonterminal build is observed while it progresses, and replaced only if it
    fails or stalls. The caller supplies the canonical definition for this name; explicit
    definition changes require ``rebuild=True`` even when the old index is ready.
    """
    validate_identifier(definition.name)
    validate_identifier(definition.table)
    if not isfinite(stall_timeout_seconds) or stall_timeout_seconds <= 0:
        raise ValueError("index stall timeout must be positive and finite")
    await ownership.heartbeat()
    info = _first_record(await ownership.read(f"INFO FOR TABLE {definition.table};"))
    indexes = info.get("indexes") if info is not None else None
    if not isinstance(indexes, Mapping):
        raise TypeError("index recovery expected table index definitions")
    exists = definition.name in indexes
    if exists and not rebuild:
        status = await get_index_build_status(
            ownership.read, name=definition.name, table=definition.table
        )
        if status is None:
            raise RuntimeError("existing index returned no build status")
        if status.status in {"ready", "built"}:
            return status
        if status.status != "error":
            try:
                return await _wait_for_owned_index(ownership, definition, stall_timeout_seconds)
            except (IndexBuildStalledError, IndexBuildFailedError):
                # A lost lease or failed status query must never trigger removal.
                pass

    body = _with_concurrently(definition.definition)
    if exists:
        body = f"REMOVE INDEX {definition.name} ON {definition.table};\n{body}"
    await ownership.mutate(body)
    return await _wait_for_owned_index(ownership, definition, stall_timeout_seconds)


async def _wait_for_owned_index(
    ownership: SchemaOwnership,
    definition: ConcurrentIndexDefinition,
    stall_timeout_seconds: float,
) -> IndexBuildStatus:
    status = await wait_for_index_ready(
        ownership.read,
        name=definition.name,
        table=definition.table,
        require_status=True,
        ownership=ownership,
        timeout_seconds=stall_timeout_seconds,
        track_progress=True,
    )
    if status is None:
        raise RuntimeError("index recovery completed without observed readiness")
    return status
