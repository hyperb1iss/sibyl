"""Atomic, actor-scoped persistence of immutable checked archive metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from anyio import to_thread
from surrealdb.request_message.message import RequestMessage
from surrealdb.request_message.methods import RequestMethod

from sibyl_core.backends.surreal.protocols import QueryParams
from sibyl_core.backends.surreal.query_guard import guard_native_query_requests
from sibyl_core.backends.surreal.records import normalize_records, raise_on_error
from sibyl_core.migrate.personal_archive_intake import ArchiveIntakeCapacityError
from sibyl_core.migrate.personal_archive_plan import (
    CheckedArchivePlan,
    archive_digest,
    canonical_json,
    checked_plan_bytes,
    checked_plan_digest,
    verify_checked_plan,
)

_CREATE_CHECKED_QUERY = (
    "BEGIN TRANSACTION; "
    "CREATE archive_import_artifacts CONTENT $artifact RETURN NONE; "
    "CREATE archive_import_runs CONTENT $run RETURN NONE; "
    "COMMIT TRANSACTION;"
)


def _metadata_request(query: str, params: QueryParams) -> RequestMessage:
    return RequestMessage(RequestMethod.QUERY, query=query, params=params)


def _metadata_request_size(query: str, params: QueryParams) -> int:
    # The pinned SDK sends this same CBOR envelope over HTTP and WebSocket.
    # Prepared namespace SQL, IDs, timestamps and all native bindings are included.
    return len(_metadata_request(query, params).WS_CBOR_DESCRIPTOR)


def _validate_unprepared_capacity(
    query: str, params: QueryParams, metadata_transaction_bytes: int
) -> None:
    # Direct SDK-backed repository adapters need a preflight too. Namespace
    # preparation may enlarge this lower bound; the final native guard then
    # checks the complete prepared request at its actual dispatch boundary.
    if len(_metadata_request(query, params).WS_CBOR_DESCRIPTOR) > metadata_transaction_bytes:
        raise ArchiveIntakeCapacityError("archive metadata transaction-byte budget exceeded")


class ArchiveCheckConflictError(ValueError):
    """An operation binding already owns a different immutable request."""


class ArchiveMetadataClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


@dataclass(frozen=True)
class CheckedArchiveArtifact:
    archive_sha256: str
    artifact_sha256: str
    member_inventory_json: str
    staged_payload_json: str
    measured_sizes_json: str


@dataclass(frozen=True)
class SavedArchiveCheck:
    record: Mapping[str, object]
    replayed: bool

    @property
    def plan(self) -> CheckedArchivePlan:
        return verify_checked_plan(
            str(self.record["checked_plan_json"]),
            str(self.record["checked_plan_sha256"]),
        )


class SurrealArchiveImportRunRepository:
    """Create artifact and run in one native transaction, without a detached claim."""

    def __init__(self, client: ArchiveMetadataClient) -> None:
        self._client = client

    async def load(
        self, run_id: str, *, organization_id: str, actor_id: str
    ) -> Mapping[str, object] | None:
        return await self._lookup(
            "uuid",
            run_id,
            organization_id=organization_id,
            actor_id=actor_id,
        )

    async def load_operation(
        self, intake_identity: str, *, organization_id: str, actor_id: str
    ) -> Mapping[str, object] | None:
        return await self._lookup(
            "intake_identity",
            intake_identity,
            organization_id=organization_id,
            actor_id=actor_id,
        )

    async def _lookup(
        self, field: str, value: str, *, organization_id: str, actor_id: str
    ) -> Mapping[str, object] | None:
        if field not in {"uuid", "intake_identity"}:
            raise ValueError("unsupported archive metadata lookup")
        query = {
            "uuid": (
                "SELECT * FROM archive_import_runs WHERE uuid=$value "
                "AND organization_id=$organization_id AND actor_id=$actor_id LIMIT 1;"
            ),
            "intake_identity": (
                "SELECT * FROM archive_import_runs WHERE intake_identity=$value "
                "AND organization_id=$organization_id AND actor_id=$actor_id LIMIT 1;"
            ),
        }[field]
        result = await self._client.execute_query(
            query,
            value=value,
            organization_id=organization_id,
            actor_id=actor_id,
        )
        raise_on_error(result, query=query)
        records = normalize_records(result)
        return records[0] if records else None

    async def create_checked(
        self,
        *,
        plan: CheckedArchivePlan,
        artifact: CheckedArchiveArtifact,
        intake_identity: str,
        request_sha256: str,
        metadata_transaction_bytes: int | None = None,
    ) -> SavedArchiveCheck:
        if metadata_transaction_bytes is not None and (
            type(metadata_transaction_bytes) is not int or metadata_transaction_bytes <= 0
        ):
            raise ValueError("archive metadata byte budget must be a positive integer")
        # Frozen models still contain mutable maps. Validate an isolated snapshot
        # before native I/O, then serialize only that snapshot across awaits.
        plan = CheckedArchivePlan.model_validate(plan.model_dump(mode="python"))
        if (
            artifact.archive_sha256 != plan.archive_sha256
            or artifact.artifact_sha256 != plan.artifact_sha256
        ):
            raise ValueError("archive artifact and plan binding mismatch")
        existing = await self.load_operation(
            intake_identity,
            organization_id=plan.organization_id,
            actor_id=plan.actor_id,
        )
        if existing is not None:
            return self._replay(existing, request_sha256)

        run_id, artifact_id = str(uuid4()), str(uuid4())
        now = datetime.now(UTC)
        record = {
            "uuid": run_id,
            "organization_id": plan.organization_id,
            "actor_id": plan.actor_id,
            "intake_identity": intake_identity,
            "request_sha256": request_sha256,
            "contract_version": plan.contract_version,
            "archive_sha256": plan.archive_sha256,
            "artifact_id": artifact_id,
            "artifact_sha256": plan.artifact_sha256,
            "origin_json": canonical_json(plan.origin),
            "mappings_json": canonical_json(plan.mappings),
            "mappings_sha256": archive_digest("sibyl-archive-mappings-v1", plan.mappings),
            "conflict_policy": plan.conflict_policy,
            "credential_kind": plan.credential.credential_kind,
            "original_api_key_id": plan.credential.api_key_id,
            "original_ceiling_json": canonical_json(plan.credential),
            "checked_plan_json": checked_plan_bytes(plan),
            "checked_plan_sha256": checked_plan_digest(plan),
            "preview_counts_json": canonical_json(
                {kind: counts.model_dump(mode="json") for kind, counts in plan.counts.items()}
            ),
            "created_at": now,
            "status": "checked",
            "revision": 0,
            "updated_at": now,
        }
        artifact_record = {
            "uuid": artifact_id,
            "organization_id": plan.organization_id,
            "actor_id": plan.actor_id,
            "run_id": run_id,
            "archive_sha256": artifact.archive_sha256,
            "artifact_sha256": artifact.artifact_sha256,
            "contract_version": plan.contract_version,
            "member_inventory_json": artifact.member_inventory_json,
            "staged_payload_json": artifact.staged_payload_json,
            "measured_sizes_json": artifact.measured_sizes_json,
            "created_at": now,
        }
        query = _CREATE_CHECKED_QUERY

        if metadata_transaction_bytes is not None:
            await to_thread.run_sync(
                _validate_unprepared_capacity,
                query,
                {"artifact": artifact_record, "run": record},
                metadata_transaction_bytes,
            )

        async def enforce_capacity(prepared_query: str, params: QueryParams | None) -> None:
            if metadata_transaction_bytes is None or params is None:
                return
            # Write preflight queries have no archive bindings. The actual
            # transaction reaches this guard after embedded USE preparation.
            if "artifact" not in params or "run" not in params:
                return
            measured_bytes = await to_thread.run_sync(
                _metadata_request_size, prepared_query, params
            )
            if measured_bytes > metadata_transaction_bytes:
                raise ArchiveIntakeCapacityError(
                    "archive metadata transaction-byte budget exceeded"
                )

        try:
            with guard_native_query_requests(enforce_capacity):
                result = await self._client.execute_query(
                    query, artifact=artifact_record, run=record
                )
            raise_on_error(result, query=query)
        except Exception as exc:
            if "archive_import_runs_intake" not in str(exc):
                raise
            winner = await self.load_operation(
                intake_identity,
                organization_id=plan.organization_id,
                actor_id=plan.actor_id,
            )
            if winner is None:
                raise
            return self._replay(winner, request_sha256)
        saved = await self.load(
            run_id, organization_id=plan.organization_id, actor_id=plan.actor_id
        )
        if saved is None:
            raise RuntimeError("checked archive metadata was not committed")
        return SavedArchiveCheck(record=saved, replayed=False)

    @staticmethod
    def _replay(record: Mapping[str, object], request_sha256: str) -> SavedArchiveCheck:
        if record["request_sha256"] != request_sha256:
            raise ArchiveCheckConflictError("archive operation binding owns another request")
        saved = SavedArchiveCheck(record=record, replayed=True)
        _ = saved.plan
        return saved
