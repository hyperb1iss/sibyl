"""True item age, read from the rows that own it.

The usage-event table has no creation timestamp, so the first version of this
harness used an item's earliest usage event as an age proxy. That proxy is
badly asymmetric: an item already present when event recording began has its
history truncated, and older items are exactly the ones more likely to be
truncated, so the proxy compresses the long tail it most needs to measure. Any
claim about age has to come from the owning row instead.

Graph entities carry `created_at` in the per-org graph namespace, and raw
captures carry it in the shared content namespace, so both item kinds are
resolvable and neither has to be dropped from the comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from events import GRAPH_ENTITY, RAW_CAPTURE, parse_event_datetime
from store import ReadOnlySurrealStore

# Matches settings.surreal_namespace_prefix in
# packages/python/sibyl-core/src/sibyl_core/config.py:77.
NAMESPACE_PREFIX = "org_"
GRAPH_DATABASE = "graph"

_CHUNK = 400


def graph_namespace(organization_id: str, *, prefix: str = NAMESPACE_PREFIX) -> str:
    """Derive an org's graph namespace from its uuid, as the runtime does."""
    return f"{prefix}{organization_id.replace('-', '').lower()}"


def fetch_created_at(
    organization_id: str,
    items: Iterable[tuple[str, str]],
    *,
    graph_store: ReadOnlySurrealStore | None = None,
    content_store: ReadOnlySurrealStore | None = None,
) -> dict[tuple[str, str], datetime]:
    """Resolve creation timestamps for (item_kind, item_id) pairs.

    Unresolvable items are simply absent from the result, so a caller can report
    coverage rather than silently substituting a wrong age.
    """
    wanted = set(items)
    graph_ids = sorted({item_id for kind, item_id in wanted if kind == GRAPH_ENTITY})
    raw_ids = sorted({item_id for kind, item_id in wanted if kind == RAW_CAPTURE})

    resolved: dict[tuple[str, str], datetime] = {}
    if graph_ids:
        store = graph_store or ReadOnlySurrealStore(
            namespace=graph_namespace(organization_id),
            database=GRAPH_DATABASE,
        )
        for item_id, created_at in _fetch_table(store, "entity", graph_ids).items():
            resolved[(GRAPH_ENTITY, item_id)] = created_at
    if raw_ids:
        store = content_store or ReadOnlySurrealStore()
        for item_id, created_at in _fetch_table(store, "raw_captures", raw_ids).items():
            resolved[(RAW_CAPTURE, item_id)] = created_at
    return resolved


def _fetch_table(
    store: ReadOnlySurrealStore,
    table: str,
    item_ids: Sequence[str],
) -> dict[str, datetime]:
    found: dict[str, datetime] = {}
    for start in range(0, len(item_ids), _CHUNK):
        chunk = item_ids[start : start + _CHUNK]
        literals = ", ".join(_quote(item_id) for item_id in chunk)
        rows = store.query(
            f"SELECT uuid, created_at FROM {table} WHERE uuid IN [{literals}];"  # noqa: S608
        )
        for row in rows:
            if not isinstance(row, dict):
                continue
            uuid = str(row.get("uuid") or "")
            stamp = row.get("created_at")
            if not uuid or stamp is None:
                continue
            found[uuid] = parse_event_datetime(stamp)
    return found


def _quote(value: str) -> str:
    """Single-quote a SurrealQL string literal.

    Item ids are uuids and slug-style keys in practice, so this only has to be
    correct rather than clever, and a rejected quote is better than an injected
    one.
    """
    if "'" in value or "\\" in value:
        raise ValueError(f"refusing to interpolate a quoted item id: {value!r}")
    return f"'{value}'"


__all__ = [
    "GRAPH_DATABASE",
    "NAMESPACE_PREFIX",
    "fetch_created_at",
    "graph_namespace",
]
