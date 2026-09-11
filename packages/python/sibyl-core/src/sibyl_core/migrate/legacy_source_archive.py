"""Translate incomplete legacy rows without inventing trusted historical lineage."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.source_integrity import build_integrity_archive

_WORK_ITEM_TYPES = frozenset({"project", "task", "epic", "team", "milestone"})


def legacy_memory_requires_quarantine(row: dict[str, Any], kind: SourceKind) -> bool:
    return (
        kind is SourceKind.RAW_CAPTURE
        or row.get("derivation_required") is True
        or row.get("entity_type") not in _WORK_ITEM_TYPES
    )


def build_legacy_source_archive(
    rows: list[dict[str, Any]], *, kind: SourceKind, organizations: list[str]
) -> tuple[dict[str, Any], set[str]]:
    """Preserve incoming content with fresh state and unavailable memory lineage.

    Legacy archives cannot prove whether an ordinary-looking memory previously
    had a protected association. The storage-owned marker preserves that
    uncertainty; an operator can reauthor content under a new capture identity.
    Existing destination rows are still governed by checked restore policy.
    """
    org_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
    table = "entity" if kind is SourceKind.GRAPH_ENTITY else "raw_captures"
    records = deepcopy(rows)
    states = []
    quarantined: set[str] = set()
    for row in records:
        identity = row.get("uuid")
        org = row.get(org_field)
        if not isinstance(identity, str) or not identity or org not in organizations:
            raise ValueError("legacy source identity is outside restore scope")
        revision = row.get("revision", 1)
        if type(revision) is not int or revision < 1:
            raise ValueError("legacy source revision is invalid")
        row["revision"] = revision
        row["archive_record_key"] = f"{table}:{uuid4().hex}"
        is_memory = kind is SourceKind.RAW_CAPTURE or row.get("entity_type") not in _WORK_ITEM_TYPES
        if is_memory or row.get("derivation_required") is True:
            row["derivation_required"] = True
            quarantined.add(identity)
        states.append(
            {
                "organization_id": org,
                "source_kind": kind.value,
                "source_id": identity,
                "generation": 1,
                "revision": revision,
                "deleted": kind is SourceKind.RAW_CAPTURE and row.get("deleted_at") is not None,
                "incarnation": str(uuid4()),
            }
        )
    return build_integrity_archive(
        kind=kind,
        organizations=organizations,
        source_rows=records,
        source_states=states,
        derivations=[],
    ), quarantined
