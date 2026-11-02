"""Check captured source dependencies without claiming a global archive snapshot."""

import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
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


@dataclass
class LineageSection:
    """What sealing reads from one kind's capture, without the source bodies.

    Rows arrive one at a time through ``add_row``, so a capture that streams
    its rows to disk keeps only these digests. Sealing retires associations in
    place and names the rows that must be marked ``derivation_required``.
    """

    kind: SourceKind
    associations: list[dict[str, Any]]
    observed: dict[SourceIdentity, SourceObservation] = field(default_factory=dict)
    target_digests: dict[tuple[str, str], str] = field(default_factory=dict)
    flagged: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._targets = {
            (association["organization_id"], association["target_id"])
            for association in self.associations
        }

    def add_row(self, row: dict[str, Any], state: dict[str, Any]) -> None:
        org_field = "group_id" if self.kind is SourceKind.GRAPH_ENTITY else "organization_id"
        key = (row[org_field], row["uuid"])
        if state["deleted"] and key not in self._targets:
            return
        entity = entity_from_surreal_row(row) if self.kind is SourceKind.GRAPH_ENTITY else None
        if not state["deleted"]:
            source = SourceIdentity(key[0], self.kind, key[1])
            self.observed[source] = SourceObservation(
                source=source,
                generation=state["generation"],
                revision=state["revision"],
                incarnation=state["incarnation"],
                content_sha256=graph_evidence(entity)
                if entity is not None
                else evidence_hash({"version": 1, "raw_content": row["raw_content"]}),
                durable=True,
            )
        if key in self._targets:
            self.target_digests[key] = (
                graph_target_digest(entity)
                if entity is not None
                else hashlib.sha256(row["raw_content"].encode()).hexdigest()
            )


def lineage_section(
    kind: SourceKind,
    rows: list[dict[str, Any]],
    states: list[dict[str, Any]],
    associations: list[dict[str, Any]],
) -> LineageSection:
    section = LineageSection(kind=kind, associations=associations)
    state_map = {(state["organization_id"], state["source_id"]): state for state in states}
    org_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
    for row in rows:
        section.add_row(row, state_map[(row[org_field], row["uuid"])])
    return section


def seal_lineage(
    sections: list[LineageSection], prior: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Retire contradicted associations and report every unresolved dependency.

    Each namespace supplies its own coherent capture. Sources omitted from a
    standalone archive remain unresolved and protected, so a later coherent
    content restore may satisfy them. Present but contradictory source evidence
    retires the archived target association without rewriting its observations.
    """
    observed: dict[SourceIdentity, SourceObservation] = {}
    for section in sections:
        observed.update(section.observed)
    kinds = {section.kind for section in sections}
    report = [dict(row) for row in prior]
    for section in sections:
        for association in section.associations:
            if not association["active"]:
                continue
            key = (association["organization_id"], association["target_id"])
            mismatch = section.target_digests.get(key) != association["body_sha256"]
            unresolved = False
            for value in association["observations"]:
                original = observation_from_record(value)
                if original.source.kind not in kinds:
                    unresolved = True
                    continue
                current = observed.get(original.source)
                if current is None or not original.same_evidence(current):
                    mismatch = True
            if mismatch or unresolved:
                if key in section.target_digests:
                    section.flagged.add(key)
                if mismatch:
                    association["active"] = False
                report.append(
                    {
                        "organization_id": association["organization_id"],
                        "source_kind": section.kind.value,
                        "source_id": association["target_id"],
                        "status": "quarantined" if mismatch else "unresolved",
                        "reason": "captured_dependency_mismatch"
                        if mismatch
                        else "external_dependency_omitted",
                    }
                )
    return report


def seal_archive_lineage(
    graph_payload: dict[str, Any] | None,
    content_payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, str]]]:
    """Quarantine mismatched captured dependencies before publishing the archive."""
    graph, content = deepcopy(graph_payload), deepcopy(content_payload)
    captured = []
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
        captured.append(
            (
                payload,
                organizations,
                rows,
                states,
                lineage_section(kind, rows, states, associations),
            )
        )
    report = seal_lineage(
        [lineage for *_, lineage in captured],
        [
            row
            for payload in (graph, content)
            if payload is not None
            for row in payload.get("lineage_validation", [])
            if isinstance(row, dict) and row.get("status") == "quarantined"
        ],
    )
    for payload, organizations, rows, states, lineage in captured:
        org_field = "group_id" if lineage.kind is SourceKind.GRAPH_ENTITY else "organization_id"
        for row in rows:
            if (row[org_field], row["uuid"]) in lineage.flagged:
                row["derivation_required"] = True
        payload["source_integrity"] = build_integrity_archive(
            kind=lineage.kind,
            organizations=organizations,
            source_rows=rows,
            source_states=states,
            derivations=lineage.associations,
        )
        payload["lineage_validation"] = [
            row for row in report if row["source_kind"] == lineage.kind.value
        ]
    return graph, content, report
