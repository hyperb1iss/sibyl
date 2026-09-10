"""Check captured source dependencies without claiming a global archive snapshot."""

import hashlib
from copy import deepcopy
from typing import Any

from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
    evidence_hash,
)
from sibyl_core.migrate.source_integrity import build_integrity_archive, validate_integrity_archive
from sibyl_core.services.graph_derivations import graph_target_digest
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.memory_derivations import observation_from_record
from sibyl_core.services.source_observations import graph_evidence


def seal_archive_lineage(
    graph_payload: dict[str, Any] | None,
    content_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, str]]]:
    """Quarantine mismatched captured dependencies before publishing the archive.

    Each namespace supplies its own coherent capture. Sources omitted from a
    standalone archive remain unresolved and protected, so a later coherent
    content restore may satisfy them. Present but contradictory source evidence
    retires the archived target association without rewriting its observations.
    """
    graph, content = deepcopy(graph_payload), deepcopy(content_payload)
    sections = {}
    observed = {}
    for kind, payload, current_version in (
        (SourceKind.GRAPH_ENTITY, graph, "3.0"),
        (SourceKind.RAW_CAPTURE, content, "2.0"),
    ):
        if payload is None or payload.get("version") not in (
            {"2.0", "2.1", "2.2", "2.3"} if kind is SourceKind.RAW_CAPTURE else {current_version}
        ):
            continue
        section = payload.get("source_integrity")
        organizations = section.get("organizations") if isinstance(section, dict) else None
        if not isinstance(organizations, list):
            raise ValueError("current archive has no complete source integrity section")
        rows, states, associations = validate_integrity_archive(
            section, kind=kind, organizations=organizations
        )
        sections[kind] = (payload, organizations, rows, states, associations)
        org_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
        state_map = {(state["organization_id"], state["source_id"]): state for state in states}
        for row in rows:
            source = SourceIdentity(row[org_field], kind, row["uuid"])
            state = state_map[(source.organization_id, source.id)]
            if state["deleted"]:
                continue
            digest = (
                graph_evidence(entity_from_surreal_row(row))
                if kind is SourceKind.GRAPH_ENTITY
                else evidence_hash({"version": 1, "raw_content": row["raw_content"]})
            )
            observed[source] = SourceObservation(
                source=source,
                generation=state["generation"],
                revision=state["revision"],
                incarnation=state["incarnation"],
                content_sha256=digest,
                durable=True,
            )
    report = [
        dict(row)
        for payload in (graph, content)
        if payload is not None
        for row in payload.get("lineage_validation", [])
        if isinstance(row, dict) and row.get("status") == "quarantined"
    ]
    for kind, (payload, organizations, rows, states, associations) in sections.items():
        org_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
        row_map = {(row[org_field], row["uuid"]): row for row in rows}
        for association in associations:
            if not association["active"]:
                continue
            target = row_map.get((association["organization_id"], association["target_id"]))
            target_digest = None
            if target is not None:
                target_digest = (
                    graph_target_digest(entity_from_surreal_row(target))
                    if kind is SourceKind.GRAPH_ENTITY
                    else hashlib.sha256(target["raw_content"].encode()).hexdigest()
                )
            mismatch = target_digest != association["body_sha256"]
            unresolved = False
            for value in association["observations"]:
                original = observation_from_record(value)
                if original.source.kind not in sections:
                    unresolved = True
                    continue
                current = observed.get(original.source)
                if current is None or not original.same_evidence(current):
                    mismatch = True
            if mismatch or unresolved:
                identity = association["target_id"]
                row = row_map.get((association["organization_id"], identity))
                if row is not None:
                    row["derivation_required"] = True
                if mismatch:
                    association["active"] = False
                report.append(
                    {
                        "organization_id": association["organization_id"],
                        "source_kind": kind.value,
                        "source_id": identity,
                        "status": "quarantined" if mismatch else "unresolved",
                        "reason": "captured_dependency_mismatch"
                        if mismatch
                        else "external_dependency_omitted",
                    }
                )
        payload["source_integrity"] = build_integrity_archive(
            kind=kind,
            organizations=organizations,
            source_rows=rows,
            source_states=states,
            derivations=associations,
        )
        payload["lineage_validation"] = [row for row in report if row["source_kind"] == kind.value]
    return graph, content, report
