"""Actor-scoped loading of validated, inert checked archive artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from anyio import CancelScope
from anyio.lowlevel import checkpoint

from sibyl.persistence.surreal.archive_import_runs import (
    ArchiveMetadataClient,
    SurrealArchiveImportRunRepository,
)
from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.migrate.personal_archive_artifact import (
    ArchiveArtifactIntegrityError,
    RehydratedPersonalArchive,
    rehydrate_personal_archive,
)
from sibyl_core.migrate.personal_archive_plan import (
    CheckedArchivePlan,
    archive_digest,
    canonical_json,
    verify_checked_plan,
)


@dataclass(frozen=True)
class SavedArchiveArtifact:
    """Validated immutable intake data, without granting permission to apply."""

    run_id: str
    artifact_id: str
    checked_plan_json: str
    checked_plan_sha256: str
    archive: RehydratedPersonalArchive

    @property
    def plan(self) -> CheckedArchivePlan:
        """Return a fresh plan so nested maps cannot mutate the stored snapshot."""
        return verify_checked_plan(self.checked_plan_json, self.checked_plan_sha256)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ArchiveArtifactIntegrityError("archive identity must be a canonical UUID")
    try:
        canonical = str(UUID(value))
    except ValueError as exc:
        raise ArchiveArtifactIntegrityError("archive identity must be a canonical UUID") from exc
    if canonical != value:
        raise ArchiveArtifactIntegrityError("archive identity must be a canonical UUID")
    return value


def _string(record: Mapping[str, object], field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise ArchiveArtifactIntegrityError("archive binding must be a string")
    return value


def _validated_pair(
    run: Mapping[str, object],
    artifact: Mapping[str, object],
    run_id: str,
    organization_id: str,
    actor_id: str,
) -> SavedArchiveArtifact:
    try:
        archive = rehydrate_personal_archive(
            archive_sha256=_string(artifact, "archive_sha256"),
            artifact_sha256=_string(artifact, "artifact_sha256"),
            member_inventory_json=_string(artifact, "member_inventory_json"),
            staged_payload_json=_string(artifact, "staged_payload_json"),
            measured_sizes_json=_string(artifact, "measured_sizes_json"),
        )
        encoded, digest = _string(run, "checked_plan_json"), _string(run, "checked_plan_sha256")
        maximum = archive.original_budget.encoded_plan_bytes
        if len(encoded) > maximum:
            raise ArchiveArtifactIntegrityError("saved archive plan exceeds original admission")
        encoded_bytes = 0
        for offset in range(0, len(encoded), 65536):
            encoded_bytes += len(encoded[offset : offset + 65536].encode("utf-8"))
            if encoded_bytes > maximum:
                raise ArchiveArtifactIntegrityError("saved archive plan exceeds original admission")
        plan = verify_checked_plan(encoded, digest)
        artifact_id = _canonical_uuid(run["artifact_id"])
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
        return SavedArchiveArtifact(run_id, artifact_id, encoded, digest, archive)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ArchiveArtifactIntegrityError):
            raise
        raise ArchiveArtifactIntegrityError("saved archive pair is invalid") from exc


async def _joined_validation(
    run: Mapping[str, object],
    artifact: Mapping[str, object],
    run_id: str,
    organization_id: str,
    actor_id: str,
) -> SavedArchiveArtifact:
    work = asyncio.create_task(
        asyncio.to_thread(_validated_pair, run, artifact, run_id, organization_id, actor_id)
    )
    try:
        result = await asyncio.shield(work)
        await checkpoint()
        return result
    except asyncio.CancelledError as cancellation:
        # Cancellation must retain ownership until member expansion and all
        # eager validators have finished, including explicit Task.cancel().
        with CancelScope(shield=True):
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    raise cancellation from exc
            try:
                work.result()
            except Exception as exc:
                raise cancellation from exc
        raise


class SurrealArchiveImportArtifactRepository:
    """Load one immutable pair through its authenticated caller-owned scope."""

    def __init__(self, client: ArchiveMetadataClient) -> None:
        self._client = client
        self._runs = SurrealArchiveImportRunRepository(client)

    async def load(
        self, run_id: str, *, organization_id: str, actor_id: str
    ) -> SavedArchiveArtifact | None:
        """Validate saved input only; callers must separately refresh authority."""
        run_id, organization_id, actor_id = (
            _canonical_uuid(value) for value in (run_id, organization_id, actor_id)
        )
        run = await self._runs.load(run_id, organization_id=organization_id, actor_id=actor_id)
        if run is None:
            return None
        # Native row-shaped metadata has scalar fields. Copy the returned map
        # before another await; immutable strings remain safe across workers.
        run = dict(run)
        artifact_id = _canonical_uuid(run.get("artifact_id"))
        query = (
            "SELECT * FROM archive_import_artifacts WHERE uuid=$artifact_id "
            "AND run_id=$run_id AND organization_id=$organization_id "
            "AND actor_id=$actor_id LIMIT 2;"
        )
        result = await self._client.execute_query(
            query,
            artifact_id=artifact_id,
            run_id=run_id,
            organization_id=organization_id,
            actor_id=actor_id,
        )
        raise_on_error(result, query=query)
        records = normalize_records(result)
        if len(records) != 1:
            raise ArchiveArtifactIntegrityError("saved archive pair is unavailable")
        return await _joined_validation(run, dict(records[0]), run_id, organization_id, actor_id)
