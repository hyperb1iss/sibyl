"""Recheck correction receipts under the credential reading their stored result."""

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.services.graph import get_surreal_graph_runtime
from sibyl_core.services.memory_policy import _authorize_share_source_read
from sibyl_core.services.surreal_content import get_raw_memory


async def filter_correction_disclosure(
    payload: dict[str, Any],
    *,
    organization_id: str,
    principal_id: str | None,
    accessible_projects: set[str] | None,
    accessible_teams: set[str] | None,
    allowed_memory_scope_keys: Iterable[str] | None,
) -> dict[str, Any]:
    """Retain mutation identity while checking current access to referenced rows.

    Cached correction results belong to a principal, so a later narrower key
    may replay the same mutation. The stored receipt is evidence of a write,
    not a durable grant to read every row named by the original credential.
    """
    allowed_memory_scope_keys = (
        None if allowed_memory_scope_keys is None else frozenset(allowed_memory_scope_keys)
    )
    result = deepcopy(payload)
    decisions: dict[tuple[str, str], bool] = {}
    runtime = None

    async def visible(kind: str, identifier: str) -> bool:
        nonlocal runtime
        key = (kind, identifier)
        if key in decisions:
            return decisions[key]
        allowed = False
        try:
            if kind == "raw_captures":
                row = await get_raw_memory(organization_id=organization_id, memory_id=identifier)
                if row is not None:
                    allowed = _authorize_share_source_read(
                        memory=row,
                        principal_id=principal_id,
                        accessible_projects=accessible_projects,
                        accessible_teams=accessible_teams,
                        allowed_memory_scope_keys=allowed_memory_scope_keys,
                    ).allowed
            elif kind == "entity":
                if runtime is None:
                    runtime = await get_surreal_graph_runtime(organization_id)
                row = await runtime.entity_manager.get(identifier)
                if row is not None:
                    allowed = memory_metadata_read_allowed(
                        row.metadata,
                        row_project_id=memory_row_project_id(
                            row.metadata,
                            entity_type=str(getattr(row, "entity_type", "") or ""),
                            entity_id=str(getattr(row, "id", "") or ""),
                        ),
                        principal_id=principal_id,
                        accessible_projects=accessible_projects,
                        private_scope_granted=private_scope_granted_for(
                            allowed_memory_scope_keys, principal_id=principal_id
                        ),
                        allowed_memory_scope_keys=allowed_memory_scope_keys,
                    )
        except Exception:
            # An unavailable row cannot authorize disclosure; the mutation
            # receipt still describes the original completed operation.
            allowed = False
        decisions[key] = allowed
        return allowed

    impact = result.get("recall_impact")
    if isinstance(impact, dict):
        for field, kind in (
            ("graph_entity_ids", "entity"),
            ("refused_entity_ids", "entity"),
            ("derived_raw_memory_ids", "raw_captures"),
        ):
            if isinstance(impact.get(field), list):
                impact[field] = [
                    value
                    for value in impact[field]
                    if isinstance(value, str) and await visible(kind, value)
                ]
    for container, field in (
        (result, "affected_derived_ids"),
        (result.get("lifecycle"), "derived_ids"),
    ):
        if isinstance(container, dict) and isinstance(container.get(field), list):
            container[field] = [
                value
                for value in container[field]
                if isinstance(value, str) and await visible("entity", value)
            ]
    receipt = result.get("mutation_receipt")
    if isinstance(receipt, dict) and isinstance(receipt.get("affected_records"), list):
        receipt["affected_records"] = [
            value
            for value in receipt["affected_records"]
            if isinstance(value, str) and ":" in value and await visible(*value.split(":", 1))
        ]
    for field in ("metadata", "lifecycle", "reflection_finding"):
        metadata = result.get(field)
        if isinstance(metadata, dict):
            for key in ("replacement_source_id", "duplicate_of_source_id"):
                value = metadata.get(key)
                if isinstance(value, str) and not await visible("raw_captures", value):
                    metadata.pop(key)
    finding = result.get("reflection_finding")
    if isinstance(finding, dict) and isinstance(finding.get("related_source_ids"), list):
        finding["related_source_ids"] = [
            value
            for value in finding["related_source_ids"]
            if isinstance(value, str) and await visible("raw_captures", value)
        ]
    return result
