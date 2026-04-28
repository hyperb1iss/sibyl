"""Shared helpers for SurrealDB operations modules.

Graphiti's record-parser contracts expect dict-shaped records with specific
keys. SurrealDB returns nearly-identical shapes; these helpers smooth the
remaining edges (RecordID unwrapping, attribute merge, date normalization)
so the call sites in individual ops modules stay short.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graphiti_core.driver.query_executor import QueryExecutor, Transaction


def _check_identifier(value: str) -> str:
    if not value or not all(char.isalnum() or char == "_" for char in value):
        msg = f"invalid SurrealDB identifier: {value!r}"
        raise ValueError(msg)
    return value


async def run_query(
    executor: QueryExecutor,
    tx: Transaction | None,
    query: str,
    **params: Any,
) -> Any:
    if tx is not None:
        return await tx.run(query, **params)
    return await executor.execute_query(query, **params)


async def resolve_record_id(
    executor: QueryExecutor,
    tx: Transaction | None,
    table: str,
    uuid: str,
) -> Any | None:
    result = await run_query(
        executor,
        tx,
        f"SELECT id FROM {_check_identifier(table)} WHERE uuid = $uuid LIMIT 1;",
        uuid=uuid,
    )
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    return first.get("id")


def build_relation_save_query(
    edge_table: str,
    fields: Sequence[str],
    *,
    source_binding: str | None = None,
    target_binding: str | None = None,
) -> str:
    table = _check_identifier(edge_table)
    checked_fields = tuple(_check_identifier(field) for field in fields)
    update_fields = ",\n    ".join(
        ("in = $src", "out = $tgt", *(f"{field} = ${field}" for field in checked_fields))
    )
    relate_fields = ",\n        ".join(f"{field} = ${field}" for field in checked_fields)
    bindings = []
    if source_binding is not None:
        bindings.append(f"LET $src = {source_binding};")
    if target_binding is not None:
        bindings.append(f"LET $tgt = {target_binding};")
    bindings_text = "\n".join(bindings)
    if bindings_text:
        bindings_text += "\n"
    return f"""{bindings_text}LET $rel = type::thing('{table}', $uuid);
DELETE FROM {table} WHERE uuid = $uuid AND (in != $src OR out != $tgt);
LET $updated = (UPDATE {table} SET
    {update_fields}
    WHERE uuid = $uuid RETURN id);
IF array::len($updated) = 0 THEN
    RELATE $src->$rel->$tgt SET
        {relate_fields};
END;
"""


def build_node_upsert_query(table: str, fields: Sequence[str]) -> str:
    checked_table = _check_identifier(table)
    checked_fields = tuple(_check_identifier(field) for field in fields)
    assignments = ",\n    ".join(f"{field} = ${field}" for field in checked_fields)
    return f"""UPSERT {checked_table} SET
    {assignments}
WHERE uuid = $uuid;
"""


def normalize_record(record: Any) -> dict[str, Any] | None:
    """Coerce a SurrealDB row into the dict shape Graphiti parsers expect.

    SurrealDB returns records as Python dicts keyed by field name, with an
    ``id`` field holding a ``RecordID`` object. Graphiti parsers don't use
    ``id`` directly (they key off the ``uuid`` string field) so we can drop
    it. Returns ``None`` when the input is empty so callers short-circuit.
    """
    if record is None:
        return None
    if not isinstance(record, dict):
        return None
    out = dict(record)
    out.pop("id", None)
    # Graphiti's entity_node_from_record mutates attributes in place, so we
    # always return a fresh mutable dict. Missing optional fields default
    # to the right empty value.
    if "attributes" not in out or out["attributes"] is None:
        out["attributes"] = {}
    if "labels" not in out or out["labels"] is None:
        out["labels"] = []
    return out


def normalize_records(result: Any) -> list[dict[str, Any]]:
    """Coerce an ``execute_query`` result into a list of record dicts.

    SurrealDB SELECT statements return a list of dicts directly. This helper
    tolerates nested shapes (list-of-list, dict-with-result) so callers can
    stay forgiving.
    """
    if result is None:
        return []
    if isinstance(result, dict):
        single = normalize_record(result)
        return [single] if single is not None else []
    if isinstance(result, list):
        out: list[dict[str, Any]] = []
        for item in result:
            if isinstance(item, list):
                # Nested list (eg. multi-statement leak); flatten one level.
                out.extend(n for r in item if (n := normalize_record(r)) is not None)
            elif (n := normalize_record(item)) is not None:
                out.append(n)
        return out
    return []


__all__ = [
    "build_node_upsert_query",
    "build_relation_save_query",
    "normalize_record",
    "normalize_records",
    "resolve_record_id",
    "run_query",
]
