"""SurrealDB repository for durable source-import run state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from sibyl.persistence.surreal.content import surreal_content_client
from sibyl_core.backends.surreal.records import normalize_records, raise_on_error

type SourceImportRecord = dict[str, object]


class SourceImportRevisionConflictError(RuntimeError):
    """Raised when a source-import state transition loses a revision race."""


class SourceImportQueryClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


class SurrealSourceImportRunRepository:
    """Persist source-import runs with checked compare-and-swap updates."""

    def __init__(self, client: SourceImportQueryClient) -> None:
        self._client = client

    async def load(
        self,
        import_id: str,
        *,
        organization_id: str,
    ) -> SourceImportRecord | None:
        query = (
            "SELECT * FROM source_imports "
            "WHERE uuid = $import_id AND organization_id = $organization_id LIMIT 1;"
        )
        result = await self._client.execute_query(
            query,
            import_id=import_id,
            organization_id=organization_id,
        )
        raise_on_error(result, query=query)
        records = normalize_records(result)
        return records[0] if records else None

    async def save(
        self,
        record: Mapping[str, object],
        *,
        expected_revision: int | None,
    ) -> SourceImportRecord:
        persisted = {str(key): value for key, value in record.items()}
        import_id = str(persisted.get("uuid") or "")
        organization_id = str(persisted.get("organization_id") or "")
        if not import_id or not organization_id:
            raise ValueError("source_import_identity_required")

        if expected_revision is None:
            query = "CREATE source_imports CONTENT $record;"
            params: dict[str, object] = {"record": persisted}
        else:
            query = (
                "UPDATE source_imports CONTENT $record "
                "WHERE uuid = $import_id "
                "AND organization_id = $organization_id "
                "AND revision = $expected_revision;"
            )
            params = {
                "record": persisted,
                "import_id": import_id,
                "organization_id": organization_id,
                "expected_revision": expected_revision,
            }

        result = await self._client.execute_query(query, **params)
        raise_on_error(result, query=query)
        records = normalize_records(result)
        if records:
            return records[0]
        if expected_revision is not None:
            raise SourceImportRevisionConflictError(
                f"source import {import_id} revision {expected_revision} is stale"
            )
        raise RuntimeError(f"source import {import_id} was not persisted")


async def load_source_import_run_record(
    import_id: str,
    *,
    organization_id: str,
) -> SourceImportRecord | None:
    async with surreal_content_client() as client:
        return await SurrealSourceImportRunRepository(client).load(
            import_id,
            organization_id=organization_id,
        )


async def save_source_import_run_record(
    record: Mapping[str, object],
    *,
    expected_revision: int | None,
) -> SourceImportRecord:
    async with surreal_content_client() as client:
        return await SurrealSourceImportRunRepository(client).save(
            record,
            expected_revision=expected_revision,
        )


__all__ = [
    "SourceImportRecord",
    "SourceImportRevisionConflictError",
    "SurrealSourceImportRunRepository",
    "load_source_import_run_record",
    "save_source_import_run_record",
]
