"""Verify persisted procedure artifacts against their immutable receipt and sources."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.content_models import RawMemory
from sibyl_core.tasks import consolidation as c


@dataclass(frozen=True, slots=True)
class ProcedureArtifact:
    group: c.ConsolidationGroup
    candidate: ReflectionCandidate


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate artifact member")
        result[key] = value
    return result


def _without_object_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_object_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_object_nulls(item) for item in value]
    return value


def _artifact_payload(memory: RawMemory) -> dict[str, Any]:
    payload = memory.metadata.get(c.METADATA_KEY)
    if not isinstance(payload, dict):
        raise ValueError("procedure audit is not an object")
    stored_payload = payload
    if payload.get("render_version") is None:
        marker = "## Evidence and build receipt\n\n```json\n"
        if memory.raw_content.count(marker) != 1 or not memory.raw_content.endswith("\n```"):
            raise ValueError("legacy procedure audit is not uniquely framed")
        encoded = memory.raw_content.split(marker, 1)[1][:-4]
        payload = json.loads(encoded, object_pairs_hook=_strict_object)
        if not isinstance(payload, dict) or "render_version" in payload:
            raise ValueError("legacy procedure audit has an invalid shape")
        if stored_payload != payload and stored_payload != _without_object_nulls(payload):
            raise ValueError("legacy procedure audit differs from stored metadata")
    return payload


def publication_build_receipt_json(memory: RawMemory) -> str | None:
    """Bind final publication to the exact receipt retained in the candidate audit.

    This extracts an observation, not an artifact validity or authority verdict.
    Full reconstruction must still precede publication.
    """
    try:
        receipt = _artifact_payload(memory)["build_receipt"]
        if not isinstance(receipt, dict):
            return None
        return json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def resolve_procedure_artifact(
    memory: RawMemory, ledger: dict[str, Any], captures: list[dict[str, Any]]
) -> ProcedureArtifact | None:
    """Check artifact agreement without granting source or publication authority."""
    from sibyl_core.services.eval_publication import ConsolidationConflict, _decode_build_receipt

    try:
        if (
            ledger.get("candidate_id") != memory.id
            or ledger.get("organization_id") != memory.organization_id
            or ledger.get("principal_id") != memory.principal_id
            or ledger.get("uuid") != memory.metadata.get("eval_consolidation")
        ):
            return None
        payload = _artifact_payload(memory)
        receipt = _decode_build_receipt(ledger)
        if receipt is None or payload.get("build_receipt") != receipt:
            return None
        group_data = deepcopy(payload["group"])
        by_id = {row["uuid"]: row for row in captures}
        for episode in group_data["episodes"]:
            sources = episode["stored_sources"]
            if len(sources) != 1:
                return None
            row = by_id[sources[0]["source_id"]]
            episode["artifact"] = row["raw_content"]
        group = c.ConsolidationGroup.model_validate_json(json.dumps(group_data))
        if (group.organization_id, group.owner_principal_id) != (
            memory.organization_id,
            memory.principal_id,
        ):
            return None
        candidate = c.reconstruct_candidate_artifact(group, payload)
        matches = (
            memory.entity_type == candidate.kind
            and memory.title == candidate.title
            and memory.raw_content == candidate.content
            and all(
                (payload if key == c.METADATA_KEY else memory.metadata.get(key)) == value
                for key, value in candidate.metadata.items()
            )
        )
        return ProcedureArtifact(group, candidate) if matches else None
    except (ValueError, TypeError, KeyError, RecursionError, ConsolidationConflict):
        return None
