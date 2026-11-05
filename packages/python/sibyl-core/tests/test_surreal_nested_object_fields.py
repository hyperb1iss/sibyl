"""Every object-bearing field names what may live inside it.

SurrealDB 3.x stored undeclared keys of an object nested in a SCHEMAFULL
table; 4.0 refuses the write ("Found field 'metrics[58].avg', but no such
field exists"). The embedded engine the unit tests run on is lenient like
3.x, so this reads the schema sources instead: a field whose type holds an
object either is FLEXIBLE, or has a FLEXIBLE element definition, or declares
the keys beneath it.
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "src" / "sibyl_core" / "backends" / "surreal"

_DEFINE_FIELD = re.compile(
    r"DEFINE FIELD (?:IF NOT EXISTS |OVERWRITE )?(?P<name>[\w.*\[\]]+) ON (?:TABLE )?(?P<table>\w+)"
    r"(?P<rest>[^;\"]*)",
)


def _definitions() -> list[tuple[str, str, str, str]]:
    found = []
    for path in sorted([*SCHEMA_ROOT.rglob("*.py"), *SCHEMA_ROOT.rglob("*.surql")]):
        text = path.read_text(encoding="utf-8")
        for match in _DEFINE_FIELD.finditer(text):
            found.append((path.name, match["table"], match["name"], match["rest"]))
    return found


def test_the_scan_sees_the_schema() -> None:
    tables = {table for _, table, _, _ in _definitions()}
    assert {"entity", "users", "raw_captures", "telemetry_rollups", "schema_version"} <= tables


def test_every_object_field_declares_its_contents() -> None:
    definitions = _definitions()
    by_table: dict[str, list[tuple[str, str]]] = {}
    for _, table, name, rest in definitions:
        by_table.setdefault(table, []).append((name, rest))

    undeclared = []
    for source, table, name, rest in definitions:
        kind = re.search(r"\bTYPE\s+(\S+)", rest)
        if kind is None or "object" not in kind[1] or "FLEXIBLE" in rest:
            continue
        children = [
            (child, child_rest)
            for child, child_rest in by_table[table]
            if child.startswith((f"{name}.", f"{name}[*]"))
        ]
        flexible_element = any(
            child in (f"{name}.*", f"{name}[*]") and "FLEXIBLE" in child_rest
            for child, child_rest in children
        )
        declared_keys = any(child.count(".") > name.count(".") + 1 for child, _ in children)
        if not (flexible_element or declared_keys):
            undeclared.append(f"{source}: {table}.{name} {kind[1]}")

    assert undeclared == []
