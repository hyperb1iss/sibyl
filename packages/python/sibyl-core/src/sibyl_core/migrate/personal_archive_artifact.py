"""Rehydrate immutable staged members without adopting foreign authority."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TypedDict
from uuid import UUID

from sibyl_core.migrate.archive import ArchiveManifest, LoadedArchive
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeBudget,
    ArchiveIntakeError,
    ParsedPersonalArchive,
    decode_personal_archive_json,
    validate_personal_archive_members,
)
from sibyl_core.migrate.personal_archive_plan import (
    ArchiveSourceOrigin,
    CheckedArchivePlan,
    archive_digest,
    canonical_json,
    verify_checked_plan,
)

_MEMBER_NAMES = frozenset({"manifest.json", "metadata.json", "graph.json", "content.json"})
_MEASUREMENTS = frozenset(
    {
        "compressed_bytes",
        "inflated_bytes",
        "logical_members",
        "physical_tar_headers",
        "json_nodes",
        "parsed_rows",
        "encoded_artifact_bytes",
        "resource_budget",
    }
)
_BUDGET_FIELDS = frozenset(field.name for field in fields(ArchiveIntakeBudget))


class _InventoryEntry(TypedDict):
    sha256: str
    size_bytes: int


class ArchiveArtifactIntegrityError(ArchiveIntakeError):
    """Saved inert metadata does not match its checked immutable binding."""


@dataclass(frozen=True)
class RehydratedPersonalArchive:
    """Immutable validated bytes; materialization returns a fresh owned snapshot."""

    archive_sha256: str
    artifact_sha256: str
    origin: ArchiveSourceOrigin
    original_budget: ArchiveIntakeBudget
    members: tuple[tuple[str, bytes], ...]
    manifest_json: str
    graph_json: str | None
    content_json: str | None
    member_inventory_json: str
    staged_payload_json: str
    measured_sizes_json: str

    def materialize(self) -> ParsedPersonalArchive:
        """Create caller-owned payloads on a worker before expensive consumers."""
        # Only canonical strings generated after full typed validation enter
        # this snapshot. Repeated consumers need independent maps, without
        # rerunning foreign journal/protection validation on every access.
        return ParsedPersonalArchive(
            archive=LoadedArchive(
                source=Path("staged-personal-archive"),
                manifest=ArchiveManifest.from_dict(json.loads(self.manifest_json)),
                files={
                    name: data
                    for name, data in self.members
                    if name in {"graph.json", "content.json"}
                },
            ),
            origin=self.origin,
            archive_sha256=self.archive_sha256,
            artifact_sha256=self.artifact_sha256,
            member_inventory_json=self.member_inventory_json,
            staged_payload_json=self.staged_payload_json,
            measured_sizes_json=self.measured_sizes_json,
            graph=json.loads(self.graph_json) if self.graph_json is not None else None,
            content=json.loads(self.content_json) if self.content_json is not None else None,
        )


def _canonical_control(
    encoded: str, *, depth: int, nodes: int, scalar_bytes: int | None = None
) -> dict[str, object]:
    if not isinstance(encoded, str) or not encoded.isascii():
        raise ArchiveArtifactIntegrityError("archive staging metadata must be ASCII JSON")
    maximum = max(1, len(encoded))
    budget = ArchiveIntakeBudget(
        compressed_bytes=maximum,
        inflated_bytes=maximum,
        member_bytes=maximum,
        members=len(_MEMBER_NAMES),
        json_depth=depth,
        json_scalar_bytes=scalar_bytes or maximum,
        json_nodes=nodes,
        parsed_rows=maximum,
        encoded_artifact_bytes=maximum,
        encoded_plan_bytes=maximum,
        metadata_transaction_bytes=maximum,
    )
    payload = decode_personal_archive_json(encoded.encode("ascii"), budget)
    if canonical_json(payload) != encoded:
        raise ArchiveArtifactIntegrityError("archive staging metadata must be canonical JSON")
    return payload


def _saved_measurements(encoded: str) -> tuple[dict[str, int], ArchiveIntakeBudget]:
    sizes = _canonical_control(
        encoded, depth=2, nodes=2 * (len(_MEASUREMENTS) + len(_BUDGET_FIELDS)) + 2
    )
    resource = sizes.get("resource_budget")
    if sizes.keys() != _MEASUREMENTS or not isinstance(resource, dict):
        raise ArchiveArtifactIntegrityError("archive admission measurements are malformed")
    if resource.keys() != _BUDGET_FIELDS:
        raise ArchiveArtifactIntegrityError("archive original resource budget is malformed")
    budget_values: dict[str, int] = {}
    for name, value in resource.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ArchiveArtifactIntegrityError("archive original resource budget is malformed")
        budget_values[name] = value
    budget = ArchiveIntakeBudget(**budget_values)
    measurements: dict[str, int] = {}
    for name, value in sizes.items():
        if name == "resource_budget":
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ArchiveArtifactIntegrityError("archive admission measurements must be integers")
        measurements[name] = value
    for measured, maximum in (
        ("compressed_bytes", budget.compressed_bytes),
        ("inflated_bytes", budget.inflated_bytes),
        ("logical_members", budget.members),
        ("physical_tar_headers", budget.members),
        ("json_nodes", budget.json_nodes),
        ("parsed_rows", budget.parsed_rows),
        ("encoded_artifact_bytes", budget.encoded_artifact_bytes),
    ):
        if measurements[measured] > maximum:
            raise ArchiveArtifactIntegrityError(
                "archive admission measurements exceed original budget"
            )
    if (
        measurements["compressed_bytes"] == 0
        or measurements["inflated_bytes"] == 0
        or measurements["logical_members"] == 0
        or measurements["physical_tar_headers"] < measurements["logical_members"]
    ):
        raise ArchiveArtifactIntegrityError("archive admission measurements are inconsistent")
    return measurements, budget


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ArchiveArtifactIntegrityError("archive digest must be lowercase SHA256")
    return value


def _member_inventory(encoded: str, budget: ArchiveIntakeBudget) -> dict[str, _InventoryEntry]:
    maximum_inventory = canonical_json(
        {name: {"sha256": "f" * 64, "size_bytes": budget.member_bytes} for name in _MEMBER_NAMES}
    )
    if len(encoded) > len(maximum_inventory):
        raise ArchiveArtifactIntegrityError("archive member inventory exceeds its bounded shape")
    inventory = _canonical_control(encoded, depth=2, nodes=1 + 6 * len(_MEMBER_NAMES))
    if (
        not inventory
        or inventory.keys() - _MEMBER_NAMES
        or len(set(inventory) & {"manifest.json", "metadata.json"}) != 1
        or not set(inventory) & {"graph.json", "content.json"}
        or len(inventory) > budget.members
    ):
        raise ArchiveArtifactIntegrityError("archive member inventory is unsupported")
    result: dict[str, _InventoryEntry] = {}
    for name, entry in inventory.items():
        if not isinstance(entry, dict) or entry.keys() != {"sha256", "size_bytes"}:
            raise ArchiveArtifactIntegrityError("archive member inventory is malformed")
        size, digest = entry["size_bytes"], entry["sha256"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= budget.member_bytes
        ):
            raise ArchiveArtifactIntegrityError("archive member length exceeds original budget")
        result[name] = {"size_bytes": size, "sha256": _digest(digest)}
    return result


def rehydrate_personal_archive(
    *,
    archive_sha256: str,
    artifact_sha256: str,
    member_inventory_json: str,
    staged_payload_json: str,
    measured_sizes_json: str,
) -> RehydratedPersonalArchive:
    """Verify original admitted members; tar bytes and authority stay unproven."""
    try:
        return _rehydrate(
            archive_sha256,
            artifact_sha256,
            member_inventory_json,
            staged_payload_json,
            measured_sizes_json,
        )
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ArchiveArtifactIntegrityError):
            raise
        raise ArchiveArtifactIntegrityError("saved archive artifact is invalid") from exc


def _rehydrate(
    archive_sha256: str,
    artifact_sha256: str,
    inventory_json: str,
    staged_json: str,
    sizes_json: str,
) -> RehydratedPersonalArchive:
    _digest(archive_sha256)
    _digest(artifact_sha256)
    sizes, budget = _saved_measurements(sizes_json)
    inventory = _member_inventory(inventory_json, budget)
    if archive_digest("sibyl-archive-artifact-v1", inventory) != artifact_sha256:
        raise ArchiveArtifactIntegrityError("archive member inventory digest mismatch")
    if not isinstance(staged_json, str) or len(staged_json) > budget.encoded_artifact_bytes:
        raise ArchiveArtifactIntegrityError("archive staged bytes exceed original budget")
    if len(staged_json) != sizes["encoded_artifact_bytes"]:
        raise ArchiveArtifactIntegrityError("archive staged byte measurement mismatch")
    staged = _canonical_control(
        staged_json,
        depth=1,
        nodes=1 + 2 * len(inventory),
        scalar_bytes=1 + max(4 * ((entry["size_bytes"] + 2) // 3) for entry in inventory.values()),
    )
    if staged.keys() != inventory.keys():
        raise ArchiveArtifactIntegrityError("archive staged member inventory mismatch")
    members: dict[str, bytes] = {}
    total = 0
    for name, entry in inventory.items():
        encoded, expected_size = staged[name], entry["size_bytes"]
        if (
            not isinstance(encoded, str)
            or not encoded.isascii()
            or len(encoded) != 4 * ((expected_size + 2) // 3)
        ):
            raise ArchiveArtifactIntegrityError("archive encoded member length mismatch")
        total += expected_size
        if total > budget.inflated_bytes or total > sizes["inflated_bytes"]:
            raise ArchiveArtifactIntegrityError("archive logical bytes exceed original admission")
        data = base64.b64decode(encoded, validate=True)
        if base64.b64encode(data).decode("ascii") != encoded:
            raise ArchiveArtifactIntegrityError("archive member base64 is noncanonical")
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ArchiveArtifactIntegrityError("archive staged member digest or length mismatch")
        members[name] = data
    validated = validate_personal_archive_members(members, budget)
    if (
        len(members) != sizes["logical_members"]
        or validated.parsed_rows != sizes["parsed_rows"]
        or validated.json_nodes != sizes["json_nodes"]
    ):
        raise ArchiveArtifactIntegrityError("archive logical admission measurements mismatch")
    return RehydratedPersonalArchive(
        archive_sha256=archive_sha256,
        artifact_sha256=artifact_sha256,
        origin=validated.origin,
        original_budget=budget,
        members=tuple(sorted(members.items())),
        manifest_json=canonical_json(validated.archive.manifest.to_dict()),
        graph_json=canonical_json(validated.graph) if validated.graph is not None else None,
        content_json=canonical_json(validated.content) if validated.content is not None else None,
        member_inventory_json=inventory_json,
        staged_payload_json=staged_json,
        measured_sizes_json=sizes_json,
    )


@dataclass(frozen=True)
class ValidatedArchiveCheckedPair:
    """Detached original checked plan and admitted artifact, never an apply grant."""

    run_id: str
    artifact_id: str
    checked_plan_json: str
    checked_plan_sha256: str
    archive: RehydratedPersonalArchive

    @property
    def plan(self) -> CheckedArchivePlan:
        """Return a fresh parsed plan while retaining its original serialized bytes."""
        return verify_checked_plan(self.checked_plan_json, self.checked_plan_sha256)


def _saved_pair_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ArchiveArtifactIntegrityError("archive identity must be a canonical UUID")
    try:
        canonical = str(UUID(value))
    except ValueError as exc:
        raise ArchiveArtifactIntegrityError("archive identity must be a canonical UUID") from exc
    if canonical != value:
        raise ArchiveArtifactIntegrityError("archive identity must be a canonical UUID")
    return value


def _saved_pair_string(record: Mapping[str, object], field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise ArchiveArtifactIntegrityError("archive binding must be a string")
    return value


def validate_saved_archive_pair(
    run: Mapping[str, object],
    artifact: Mapping[str, object],
    *,
    run_id: str,
    organization_id: str,
    actor_id: str,
) -> ValidatedArchiveCheckedPair:
    """Verify exact saved bytes and original admission, without authorizing apply."""
    try:
        archive = rehydrate_personal_archive(
            archive_sha256=_saved_pair_string(artifact, "archive_sha256"),
            artifact_sha256=_saved_pair_string(artifact, "artifact_sha256"),
            member_inventory_json=_saved_pair_string(artifact, "member_inventory_json"),
            staged_payload_json=_saved_pair_string(artifact, "staged_payload_json"),
            measured_sizes_json=_saved_pair_string(artifact, "measured_sizes_json"),
        )
        encoded, digest = (
            _saved_pair_string(run, "checked_plan_json"),
            _saved_pair_string(run, "checked_plan_sha256"),
        )
        maximum = archive.original_budget.encoded_plan_bytes
        if len(encoded) > maximum:
            raise ArchiveArtifactIntegrityError("saved archive plan exceeds original admission")
        encoded_bytes = 0
        for offset in range(0, len(encoded), 65536):
            encoded_bytes += len(encoded[offset : offset + 65536].encode("utf-8"))
            if encoded_bytes > maximum:
                raise ArchiveArtifactIntegrityError("saved archive plan exceeds original admission")
        plan = verify_checked_plan(encoded, digest)
        artifact_id = _saved_pair_uuid(run["artifact_id"])
        expected_run = {
            "uuid": run_id,
            "organization_id": organization_id,
            "actor_id": actor_id,
            "archive_sha256": plan.archive_sha256,
            "artifact_sha256": plan.artifact_sha256,
            "origin_json": canonical_json(plan.origin),
            "mappings_json": canonical_json(plan.mappings),
            "mappings_sha256": archive_digest("sibyl-archive-mappings-v1", plan.mappings),
            "conflict_policy": plan.conflict_policy,
            "credential_kind": plan.credential.credential_kind,
            "original_api_key_id": plan.credential.api_key_id,
            "original_ceiling_json": canonical_json(plan.credential),
            "preview_counts_json": canonical_json(
                {kind: count.model_dump(mode="json") for kind, count in plan.counts.items()}
            ),
        }
        expected_artifact = {
            "uuid": artifact_id,
            "run_id": run_id,
            "organization_id": organization_id,
            "actor_id": actor_id,
            "archive_sha256": plan.archive_sha256,
            "artifact_sha256": plan.artifact_sha256,
        }
        if (
            any(run.get(field) != value for field, value in expected_run.items())
            or any(artifact.get(field) != value for field, value in expected_artifact.items())
            or type(run.get("contract_version")) is not int
            or type(artifact.get("contract_version")) is not int
            or run["contract_version"] != plan.contract_version
            or artifact["contract_version"] != plan.contract_version
            or plan.organization_id != organization_id
            or plan.actor_id != actor_id
            or archive.origin != plan.origin
        ):
            raise ArchiveArtifactIntegrityError("saved archive pair binding mismatch")
        return ValidatedArchiveCheckedPair(run_id, artifact_id, encoded, digest, archive)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ArchiveArtifactIntegrityError):
            raise
        raise ArchiveArtifactIntegrityError("saved archive pair is invalid") from exc
