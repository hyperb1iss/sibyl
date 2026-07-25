"""Invariant checks that validate a schema plane independently of its recorded version.

`execute_schema_statement` swallows a `DEFINE INDEX ... UNIQUE` that fails because the
table already holds duplicate rows, and `apply_schema_migrations` then records the
migration as applied. Nothing ever retries the index, so a plane can sit at its target
version with deduplication permanently unenforced. These checks re-derive what the
migrations promised and compare it against what the database actually has, so a missing
guarantee is reported instead of assumed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from sibyl_core.backends.surreal.schema_version import SchemaMigration, SurrealExecute

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"

_DEFINE_INDEX_RE = re.compile(
    rf"^\s*DEFINE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?"
    rf"(?P<name>{_IDENTIFIER})\s+ON\s+(?:TABLE\s+)?"
    rf"(?P<table>{_IDENTIFIER})\s+FIELDS\s+(?P<body>.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_REMOVE_INDEX_RE = re.compile(
    rf"^\s*REMOVE\s+INDEX\s+(?:IF\s+EXISTS\s+)?"
    rf"(?P<name>{_IDENTIFIER})\s+ON\s+(?:TABLE\s+)?"
    rf"(?P<table>{_IDENTIFIER})\s*;?\s*$",
    re.IGNORECASE,
)
_UNIQUE_TAIL_RE = re.compile(r"\bUNIQUE\b\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class UniqueIndexRequirement:
    """A `UNIQUE` index the migrations promise, plus the statement that rebuilds it."""

    name: str
    table: str
    fields: tuple[str, ...]
    statement: str


@dataclass(frozen=True, slots=True)
class SchemaInvariantViolation:
    kind: str
    table: str
    detail: str

    def describe(self) -> str:
        return f"{self.table}: {self.detail}"


@dataclass(frozen=True, slots=True)
class SchemaInvariantPlan:
    """What a schema plane must look like once its migrations have all landed."""

    schemafull_tables: tuple[str, ...] = ()
    relation_tables: tuple[str, ...] = ()
    unique_indexes: tuple[UniqueIndexRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class SchemaInvariantReport:
    violations: tuple[SchemaInvariantViolation, ...] = ()
    repaired_indexes: tuple[str, ...] = ()
    unrepairable_indexes: tuple[tuple[str, str], ...] = ()
    skipped_tables: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.violations


def expected_unique_indexes(
    migrations: Sequence[SchemaMigration],
) -> tuple[UniqueIndexRequirement, ...]:
    """Replay the migration statements to derive the surviving set of UNIQUE indexes.

    Replay order matters: several migrations drop a unique index and redefine it over
    different fields, so only the last definition of a name counts.
    """
    surviving: dict[tuple[str, str], UniqueIndexRequirement] = {}
    for migration in sorted(migrations, key=lambda item: item.version):
        for statement in migration.statements:
            removal = _REMOVE_INDEX_RE.match(statement)
            if removal is not None:
                surviving.pop(
                    (removal.group("table").lower(), removal.group("name").lower()),
                    None,
                )
                continue
            definition = _DEFINE_INDEX_RE.match(statement)
            if definition is None:
                continue
            key = (definition.group("table").lower(), definition.group("name").lower())
            requirement = _unique_requirement(definition, statement)
            if requirement is None:
                # A redefinition that is no longer UNIQUE retires the guarantee.
                surviving.pop(key, None)
                continue
            surviving[key] = requirement
    return tuple(surviving.values())


def _unique_requirement(
    definition: re.Match[str],
    statement: str,
) -> UniqueIndexRequirement | None:
    body = definition.group("body")
    unique = _UNIQUE_TAIL_RE.search(body)
    if unique is None:
        return None
    fields = tuple(part.strip() for part in body[: unique.start()].split(",") if part.strip())
    if not fields:
        return None
    return UniqueIndexRequirement(
        name=definition.group("name"),
        table=definition.group("table"),
        fields=fields,
        statement=statement,
    )


async def fetch_table_definitions(execute_query: SurrealExecute) -> dict[str, str]:
    result = await execute_query("INFO FOR DB;")
    info = _as_mapping(result)
    tables = info.get("tables") if info else None
    if not isinstance(tables, Mapping):
        return {}
    typed = cast(Mapping[str, object], tables)
    return {str(name): str(definition) for name, definition in typed.items()}


async def fetch_table_indexes(execute_query: SurrealExecute, table: str) -> dict[str, str]:
    result = await execute_query(f"INFO FOR TABLE {table};")
    info = _as_mapping(result)
    indexes = info.get("indexes") if info else None
    if not isinstance(indexes, Mapping):
        return {}
    typed = cast(Mapping[str, object], indexes)
    return {str(name): str(definition) for name, definition in typed.items()}


async def fetch_declared_fields(execute_query: SurrealExecute, table: str) -> frozenset[str]:
    """Return the top-level field names a table declares.

    Nested declarations (`errors.*`, `metadata.foo`) are folded into their top-level
    parent because a declared `object FLEXIBLE` field accepts arbitrary keys beneath it,
    so only the top level of an incoming record needs checking. An empty result means the
    table declares nothing and therefore constrains nothing.
    """
    result = await execute_query(f"INFO FOR TABLE {table};")
    info = _as_mapping(result)
    fields = info.get("fields") if info else None
    if not isinstance(fields, Mapping):
        return frozenset()
    typed = cast(Mapping[str, object], fields)
    return frozenset(str(name).split(".", 1)[0] for name in typed)


_IMPLICIT_FIELDS = frozenset({"id", "in", "out"})


def drop_undeclared_fields(
    record: Mapping[str, object],
    declared: frozenset[str],
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Conform a record to a SCHEMAFULL table, returning it with the names it shed.

    A SCHEMAFULL table hard-errors on any undeclared key, and the error names one field
    at a time, so a record carrying historical drift can never be written. Shedding the
    unmapped keys loses strictly less than losing the whole row, which is what a restore
    would otherwise do. The caller is expected to report the names.
    """
    if not declared:
        return dict(record), ()
    keep = declared | _IMPLICIT_FIELDS
    kept = {key: value for key, value in record.items() if key in keep}
    dropped = tuple(sorted(key for key in record if key not in keep))
    return kept, dropped


async def check_schema_invariants(
    execute_query: SurrealExecute,
    plan: SchemaInvariantPlan,
) -> tuple[SchemaInvariantViolation, ...]:
    definitions = await fetch_table_definitions(execute_query)
    violations: list[SchemaInvariantViolation] = []

    for table in plan.schemafull_tables:
        definition = definitions.get(table)
        if definition is None:
            continue
        if "SCHEMAFULL" not in definition.upper():
            violations.append(
                SchemaInvariantViolation(
                    kind="table_mode",
                    table=table,
                    detail="table is SCHEMALESS but the schema declares it SCHEMAFULL",
                )
            )

    for table in plan.relation_tables:
        definition = definitions.get(table)
        if definition is None:
            continue
        if "TYPE RELATION" not in definition.upper():
            violations.append(
                SchemaInvariantViolation(
                    kind="table_type",
                    table=table,
                    detail="edge table is missing TYPE RELATION metadata",
                )
            )

    index_cache: dict[str, dict[str, str]] = {}
    for requirement in plan.unique_indexes:
        if requirement.table not in definitions:
            continue
        if requirement.table not in index_cache:
            index_cache[requirement.table] = await fetch_table_indexes(
                execute_query,
                requirement.table,
            )
        if requirement.name not in index_cache[requirement.table]:
            violations.append(
                SchemaInvariantViolation(
                    kind="unique_index",
                    table=requirement.table,
                    detail=(
                        f"missing UNIQUE index {requirement.name} on "
                        f"({', '.join(requirement.fields)})"
                    ),
                )
            )
    return tuple(violations)


async def ensure_schema_invariants(
    execute_query: SurrealExecute,
    plan: SchemaInvariantPlan,
) -> SchemaInvariantReport:
    """Rebuild any missing UNIQUE index, then report what is still unmet.

    Rebuilding is safe to attempt unconditionally: creating an index never deletes rows,
    and a table that still holds duplicates simply refuses again and stays a violation.
    """
    repaired: list[str] = []
    unrepairable: list[tuple[str, str]] = []

    definitions = await fetch_table_definitions(execute_query)
    for requirement in plan.unique_indexes:
        if requirement.table not in definitions:
            continue
        existing = await fetch_table_indexes(execute_query, requirement.table)
        if requirement.name in existing:
            continue
        try:
            await execute_query(requirement.statement)
        except Exception as exc:
            unrepairable.append((requirement.name, str(exc)))
            continue
        repaired.append(requirement.name)

    violations = await check_schema_invariants(execute_query, plan)
    return SchemaInvariantReport(
        violations=violations,
        repaired_indexes=tuple(repaired),
        unrepairable_indexes=tuple(unrepairable),
    )


def _as_mapping(result: object) -> Mapping[str, object] | None:
    if isinstance(result, Mapping):
        return cast(Mapping[str, object], result)
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, Mapping):
            return cast(Mapping[str, object], first)
    return None


__all__ = [
    "SchemaInvariantPlan",
    "SchemaInvariantReport",
    "SchemaInvariantViolation",
    "UniqueIndexRequirement",
    "check_schema_invariants",
    "drop_undeclared_fields",
    "ensure_schema_invariants",
    "expected_unique_indexes",
    "fetch_declared_fields",
    "fetch_table_definitions",
    "fetch_table_indexes",
]
