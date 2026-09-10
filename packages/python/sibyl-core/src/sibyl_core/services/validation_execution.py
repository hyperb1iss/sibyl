"""Durable validation dispatch and outcomes, independent of publication fences."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import TypeAdapter

from sibyl_core.ai.llm.extractor import ExtractionUsage
from sibyl_core.ai.transport import FailedExtractionUsage, TransportAttempt
from sibyl_core.services import content_client, validation_receipts
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import MemoryValidationResult
from sibyl_core.tasks.procedure_correction_result import ProcedureCorrectionResult
from sibyl_core.tasks.procedure_review import review_digest
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult

ValidationStageResult = (
    MemoryValidationResult | ReflectionCorrectionResult | ProcedureCorrectionResult
)


class ValidationExecutionUnavailable(ValueError):
    """The execution cannot currently supply an authorized completed result."""


async def _query(query: str, **params: Any) -> list[dict[str, Any]]:
    async with content_client.surreal_content_client() as client:
        return content_client.normalize_records(await client.execute_query(query, **params))


class ValidationExecution:
    """One immutable request identity with separately committed physical attempts."""

    def __init__(
        self,
        execution_id: str,
        organization_id: str,
        principal_id: str,
        *,
        authorize: Callable[[], Awaitable[None]] | None = None,
        dispatch_guard: str = "",
        guard_params: dict[str, Any] | None = None,
    ) -> None:
        self.id = execution_id
        self.org = organization_id
        self.principal = principal_id
        self.authorize = authorize
        self.dispatch_guard = dispatch_guard
        self.guard_params = guard_params or {}

    @property
    def params(self) -> dict[str, str]:
        return {"uuid": self.id, "org": self.org, "principal": self.principal}

    async def load(self) -> dict[str, Any] | None:
        rows = await _query(
            "SELECT * FROM memory_validation_executions WHERE uuid = $uuid "
            "AND organization_id = $org AND principal_id = $principal LIMIT 1;",
            **self.params,
        )
        return rows[0] if rows else None

    async def begin(
        self, *, parent_id: str, source_ids: list[str], policy: str, request: dict[str, Any]
    ) -> bool:
        # The UUID unique index arbitrates only identical operations, not global work.
        await asyncio.to_thread(validation_receipts.ready)
        nonce = uuid4().hex
        recovery_key = Fernet.generate_key().decode()
        if review_digest(request) != self.id:
            raise ValidationExecutionUnavailable("Execution request identity differs")
        rows = await _query(
            """RETURN {
                LET $key = type::record(string::concat('memory_validation_executions:', $uuid));
                LET $previous = (SELECT * FROM memory_validation_executions WHERE uuid = $uuid);
                IF array::len($previous) = 0 AND record::exists($key) = false { CREATE $key CONTENT $row; };
                RETURN (SELECT * FROM memory_validation_executions WHERE uuid = $uuid)[0];
            };""",
            uuid=self.id,
            row={
                "uuid": self.id,
                "organization_id": self.org,
                "principal_id": self.principal,
                "parent_id": parent_id,
                "source_ids": source_ids,
                "request_sha256": self.id,
                "policy_json": policy,
                "state": "running",
                "claim_id": nonce,
                "recovery_key": recovery_key,
                "request_json": canonical(request),
            },
        )
        if (
            len(rows) != 1
            or rows[0].get("organization_id") != self.org
            or rows[0].get("principal_id") != self.principal
        ):
            raise ValidationExecutionUnavailable("Execution identity conflict")
        if not self._matches_request(rows[0]):
            raise ValidationExecutionUnavailable("Execution request identity differs")
        self._request_json = rows[0]["request_json"]
        self._recovery_key = rows[0].get("recovery_key")
        return rows[0].get("claim_id") == nonce

    async def before_dispatch(self) -> str:
        if self.authorize is not None:
            await self.authorize()
        attempt_id = uuid4().hex
        rows = await _query(
            "RETURN {"
            + self.dispatch_guard
            + """
                LET $execution = (SELECT * FROM memory_validation_executions WHERE uuid = $uuid
                    AND organization_id = $org AND principal_id = $principal)[0];
                IF $execution = NONE OR $execution.state != 'running' OR $execution.purged {
                    THROW 'Validation dispatch is unavailable';
                };
                CREATE memory_validation_attempts CONTENT $row;
                RETURN { begun: true };
            };""",
            **self.params,
            **self.guard_params,
            row={
                "uuid": attempt_id,
                "execution_id": self.id,
                "organization_id": self.org,
                "principal_id": self.principal,
            },
        )
        if len(rows) != 1 or rows[0].get("begun") is not True:
            raise ValidationExecutionUnavailable("Dispatch begin was not committed")
        return attempt_id

    async def after_dispatch(self, attempt_id: str, outcome: TransportAttempt) -> None:
        # HTTP status is not a token or cost receipt. All physical rows stay usage-unknown.
        encoded = canonical(outcome.model_dump(mode="json"))
        rows = await _query(
            """UPDATE memory_validation_attempts SET outcome_json = $outcome
                WHERE uuid = $attempt AND execution_id = $uuid AND organization_id = $org
                    AND principal_id = $principal AND outcome_json = NONE RETURN AFTER;""",
            **self.params,
            attempt=attempt_id,
            outcome=encoded,
        )
        if len(rows) != 1:
            raise ValidationExecutionUnavailable("Dispatch outcome was not committed")

    @staticmethod
    def _result_value(result: ValidationStageResult) -> dict[str, Any]:
        return TypeAdapter(ValidationStageResult).dump_python(result, mode="json")

    async def record_result(self, result: ValidationStageResult) -> None:
        value = self._result_value(result)
        if self._recovery_key is None:
            raise ValidationExecutionUnavailable("Execution has no durable receipt key")
        await asyncio.to_thread(
            validation_receipts.retain, self._request_json, self._recovery_key, value
        )
        await self._finish(
            "recorded",
            usage=value["usage"],
            result=canonical(value),
            request_json=self._request_json,
        )
        if not await self.reconcile_result(result):
            row = await self.load()
            erased_by_purge = bool(
                row is not None
                and row.get("purged") is True
                and row.get("recovery_key") is None
                and row.get("request_json") == self._request_json
                and row.get("request_sha256") == self.id
                and row.get("result_json") is None
                and row.get("usage_json") == canonical(value["usage"])
            )
            if not erased_by_purge:
                raise ValidationExecutionUnavailable("Completed receipt retention differs")
        await asyncio.to_thread(validation_receipts.discard, self._request_json)

    async def recover_completed_receipt(self) -> bool:
        """Recover only this authenticated request; never create a new dispatch claim."""
        if self.authorize is not None:
            await self.authorize()
        row = await self.load()
        if not self._matches_request(row) or row is None or not row.get("recovery_key"):
            return False
        value = await asyncio.to_thread(
            validation_receipts.read, row["request_json"], row["recovery_key"]
        )
        if value is None:
            return False
        result = TypeAdapter(ValidationStageResult).validate_python(value)
        recovered = await self.reconcile_result(result)
        if recovered:
            await asyncio.to_thread(validation_receipts.discard, row["request_json"])
        return recovered

    def _matches_request(self, row: dict[str, Any] | None) -> bool:
        if row is None or row.get("purged") or row.get("request_sha256") != self.id:
            return False
        encoded = row.get("request_json")
        if not isinstance(encoded, str):
            return False
        try:
            request = json.loads(encoded)
            return (
                isinstance(request, dict)
                and canonical(request) == encoded
                and review_digest(request) == self.id
                and request.get("org") == self.org
                and request.get("principal") == self.principal
            )
        except (ValueError, TypeError):
            return False

    async def reconcile_result(self, result: ValidationStageResult) -> bool:
        """Recover a lost result acknowledgment without replacing terminal history."""
        value = self._result_value(result)
        encoded, usage = canonical(value), canonical(value["usage"])
        row = await self.load()
        if not self._matches_request(row):
            return False
        assert row is not None
        request_json = row["request_json"]
        if row.get("state") == "running":
            await _query(
                """UPDATE memory_validation_executions SET state = 'recorded',
                        usage_json = $usage, result_json = $result, error_type = NONE
                    WHERE uuid = $uuid AND organization_id = $org AND principal_id = $principal
                        AND request_sha256 = $uuid AND state = 'running' AND purged = false
                        AND request_json = $request_json RETURN AFTER;""",
                **self.params,
                usage=usage,
                result=encoded,
                request_json=request_json,
            )
            row = await self.load()
        return bool(
            self._matches_request(row)
            and row is not None
            and row.get("request_json") == request_json
            and row.get("state") in {"recorded", "returned"}
            and row.get("result_json") == encoded
            and row.get("usage_json") == usage
        )

    async def record_failure(self, failure: BaseException) -> None:
        details = getattr(failure, "details", None) or failure.__dict__
        reported = details.get("extraction_usage")
        if isinstance(reported, ExtractionUsage):
            usage = reported.model_dump(mode="json")
        else:
            usage = FailedExtractionUsage.model_validate(reported or {}).model_dump(mode="json")
        await self._finish(
            "cancelled" if isinstance(failure, asyncio.CancelledError) else "failed",
            usage=usage,
            error=type(failure).__name__,
        )

    async def _finish(
        self,
        state: str,
        *,
        usage: object,
        result: str | None = None,
        error: str | None = None,
        request_json: str | None = None,
    ) -> None:
        request_guard = (
            " AND request_json = $expected_request AND request_sha256 = $uuid"
            if request_json is not None
            else ""
        )
        rows = await _query(
            """UPDATE memory_validation_executions SET state = $state, usage_json = $usage,
                    result_json = IF purged THEN NONE ELSE $result END, error_type = $error
                WHERE uuid = $uuid AND organization_id = $org AND principal_id = $principal
                    AND state = 'running'"""
            + request_guard
            + " RETURN AFTER;",
            **self.params,
            expected_request=request_json,
            state=state,
            usage=canonical(usage),
            result=result,
            error=error,
        )
        if len(rows) != 1:
            raise ValidationExecutionUnavailable("Validation outcome was not committed")

    async def result(self) -> dict[str, Any]:
        row = await self.load()
        if (
            row is None
            or row.get("purged")
            or row.get("state") != "returned"
            or not row.get("result_json")
        ):
            raise ValidationExecutionUnavailable(
                f"Validation has no available completed result: {row.get('state') if row else 'missing'}"
            )
        TypeAdapter(ValidationStageResult).validate_json(row["result_json"])
        return {"execution_id": self.id, **json.loads(row["result_json"])}


def _archive_request(record: dict[str, Any]) -> dict[str, Any]:
    """Check persisted JSON shape before comparing its immutable bindings."""
    for field in (
        "uuid",
        "organization_id",
        "principal_id",
        "parent_id",
        "policy_json",
        "request_sha256",
        "request_json",
    ):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"Validation archive {field} must be a nonempty string")
    source_ids = record.get("source_ids")
    if not isinstance(source_ids, list) or any(
        not isinstance(value, str) or not value for value in source_ids
    ):
        raise ValueError("Validation archive source IDs must be strings")
    request = json.loads(record["request_json"])
    if not isinstance(request, dict):
        raise ValueError("Validation archive request must be an object")
    for field in ("org", "principal", "parent", "policy"):
        if not isinstance(request.get(field), str) or not request[field]:
            raise ValueError(f"Validation archive request {field} must be a nonempty string")
    bindings = request.get("source_bindings")
    if not isinstance(bindings, list):
        raise ValueError("Validation archive source bindings must be a list")
    for binding in bindings:
        if (
            not isinstance(binding, dict)
            or any(
                not isinstance(binding.get(field), str) or not binding[field]
                for field in ("source_id", "incarnation")
            )
            or type(binding.get("generation")) is not int
        ):
            raise ValueError("Validation archive source binding is malformed")
    if record.get("result_json") is not None and not isinstance(record["result_json"], str):
        raise ValueError("Validation archive result must be encoded JSON")
    return request


def validation_archive_guard(table: str, record: dict[str, Any]) -> str:
    """Validate private history before the existing atomic archive writer runs."""
    if not isinstance(record, dict):
        raise ValueError("Validation archive row must be an object")
    if table == "memory_validation_attempts":
        for field in ("execution_id", "organization_id", "principal_id"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValueError(f"Validation attempt {field} must be a nonempty string")
        if record.get("outcome_json") is not None and not isinstance(record["outcome_json"], str):
            raise ValueError("Validation attempt outcome must be encoded JSON")
        if record.get("outcome_json") is not None:
            outcome = TransportAttempt.model_validate_json(record["outcome_json"])
            if outcome.usage_known:
                raise ValueError("Physical validation usage cannot be inferred from HTTP status")
        return """LET $parent_execution = (SELECT * FROM memory_validation_executions
            WHERE uuid = $record.execution_id AND organization_id = $record.organization_id
                AND principal_id = $record.principal_id LIMIT 1)[0];
            IF $parent_execution = NONE { THROW 'Validation attempt execution is absent'; };"""
    if table != "memory_validation_executions":
        raise ValueError("Unknown validation history table")
    if type(record.get("purged")) is not bool or record.get("request_sha256") != record.get("uuid"):
        raise ValueError("Validation archive retention identity differs")
    request = _archive_request(record)
    if canonical(request) != record["request_json"] or review_digest(request) != record["uuid"]:
        raise ValueError("Validation archive identity differs")
    if (
        request["org"] != record["organization_id"]
        or request["principal"] != record["principal_id"]
        or request["parent"] != record["parent_id"]
        or request["policy"] != record["policy_json"]
    ):
        raise ValueError("Validation archive owner or policy differs")
    bindings = request["source_bindings"]
    if record["parent_id"] not in record["source_ids"]:
        raise ValueError("Validation archive omits parent source")
    if sorted(binding["source_id"] for binding in bindings) != sorted(record["source_ids"]):
        raise ValueError("Validation archive source set differs")
    if record.get("result_json") is not None:
        TypeAdapter(ValidationStageResult).validate_json(record["result_json"])
        if record.get("purged"):
            raise ValueError("Purged validation cannot retain readable output")
    clauses = []
    for binding in bindings:
        if (
            not isinstance(binding["incarnation"], str)
            or not binding["incarnation"]
            or type(binding["generation"]) is not int
            or binding["generation"] < 1
        ):
            raise ValueError("Invalid validation source incarnation")
        # Validated data is serialized as JSON scalar literals, never raw SQL.
        clauses.append(f"""LET $state = (SELECT * FROM source_states WHERE organization_id = $record.organization_id
            AND source_kind = 'raw_capture' AND source_id = {canonical(binding["source_id"])} LIMIT 1)[0];
            IF $state = NONE OR $state.deleted != false OR $state.incarnation != {canonical(binding["incarnation"])}
                OR $state.generation < {binding["generation"]} {{ THROW 'Validation archive source was revoked'; }};""")
    return "IF $record.purged = false { " + "".join(clauses) + " };"
