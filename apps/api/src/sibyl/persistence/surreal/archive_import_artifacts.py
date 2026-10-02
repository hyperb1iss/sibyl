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
    validate_saved_archive_pair,
)
from sibyl_core.migrate.personal_archive_plan import (
    CheckedArchivePlan,
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


def _validated_pair(
    run: Mapping[str, object],
    artifact: Mapping[str, object],
    run_id: str,
    organization_id: str,
    actor_id: str,
) -> SavedArchiveArtifact:
    pair = validate_saved_archive_pair(
        run, artifact, run_id=run_id, organization_id=organization_id, actor_id=actor_id
    )
    return SavedArchiveArtifact(
        pair.run_id,
        pair.artifact_id,
        pair.checked_plan_json,
        pair.checked_plan_sha256,
        pair.archive,
    )


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
