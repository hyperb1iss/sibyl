"""Private shadow dispatch accounting with durable cohort retirement fences."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sibyl_core.ai.decisions import DecisionObservation, DecisionRequest
from sibyl_core.services import content_client
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest


class DecisionReceiptUnavailable(ValueError):
    """The shadow invocation no longer has an authorized current receipt."""


async def _query(query: str, **params: Any) -> list[dict[str, Any]]:
    async with content_client.surreal_content_client() as client:
        return content_client.normalize_records(await client.execute_query(query, **params))


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    organization_id: str
    principal_id: str
    project_id: str | None
    epoch: int
    enabled: bool
    policy_version: str
    route_policy_sha256: str

    @property
    def key(self) -> str:
        return review_digest([self.organization_id, self.principal_id, self.project_id])


def _policy(row: dict[str, Any]) -> DecisionPolicy:
    return DecisionPolicy(
        organization_id=row["organization_id"],
        principal_id=row["principal_id"],
        project_id=row.get("project_id"),
        epoch=row["epoch"],
        enabled=row["enabled"],
        policy_version=row["policy_version"],
        route_policy_sha256=row["route_policy_sha256"],
    )


async def load_policy(
    organization_id: str, principal_id: str, project_id: str | None
) -> DecisionPolicy | None:
    key = review_digest([organization_id, principal_id, project_id])
    rows = await _query("SELECT * FROM semantic_decision_policies WHERE uuid=$key;", key=key)
    return _policy(rows[0]) if rows else None


async def configure_policy(
    *,
    organization_id: str,
    principal_id: str,
    project_id: str | None,
    expected_epoch: int,
    enabled: bool,
    policy_version: str,
    route_policy_sha256: str,
) -> DecisionPolicy:
    """Set an internal cohort policy using an optimistic, monotonically increasing epoch."""
    if expected_epoch < 0 or not organization_id or not principal_id or not policy_version:
        raise ValueError("Invalid shadow policy identity")
    if len(route_policy_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in route_policy_sha256
    ):
        raise ValueError("Invalid shadow route digest")
    key = review_digest([organization_id, principal_id, project_id])
    rows = await _query(
        """RETURN {
            LET $id=type::record(string::concat('semantic_decision_policies:', $key));
            LET $previous=(SELECT * FROM $id)[0];
            IF ($previous.epoch ?? 0)!=$expected { THROW 'Shadow policy epoch changed'; };
            UPSERT $id CONTENT $row;
            RETURN (SELECT * FROM $id)[0];
        };""",
        key=key,
        expected=expected_epoch,
        row={
            "uuid": key,
            "organization_id": organization_id,
            "principal_id": principal_id,
            "project_id": project_id,
            "epoch": expected_epoch + 1,
            "enabled": enabled,
            "policy_version": policy_version,
            "route_policy_sha256": route_policy_sha256,
        },
    )
    return _policy(rows[0])


_POLICY_GUARD = """
LET $policy=(SELECT * FROM semantic_decision_policies WHERE uuid=$policy_key)[0];
IF $policy=NONE OR $policy.enabled=false OR $policy.epoch!=$policy_epoch
    OR $policy.organization_id!=$org OR $policy.principal_id!=$principal
    OR $policy.route_policy_sha256!=$route OR $policy.policy_version!=$policy_version {
    THROW 'Shadow policy unavailable';
};
"""
_SOURCE_GUARD = """
FOR $source IN $decision_sources {
    LET $raw=(SELECT * FROM raw_captures WHERE uuid=$source AND organization_id=$org)[0];
    IF $raw=NONE OR $raw.deleted_at!=NONE { THROW 'Shadow source unavailable'; };
};
"""

# Only insertion needs this independent fence. Once the receipt exists, its own
# row arbitrates completion against purge, without writing critic source state.
_SOURCE_INSERT_FENCE = """
FOR $source IN $decision_sources {
    LET $fences=(SELECT * FROM semantic_decision_source_fences
        WHERE organization_id=$org AND source_id=$source);
    IF array::len($fences)!=1 { THROW 'Shadow source fence unavailable'; };
    UPDATE $fences[0].id SET witness=type::string(rand::uuid());
};
"""


class DecisionReceipt:
    """One physical shadow invocation; callers supply the existing source owner's fence."""

    def __init__(
        self,
        organization_id: str,
        principal_id: str,
        *,
        authorize: Callable[[], Awaitable[None]],
        dispatch_guard: str,
        guard_params: dict[str, Any] | None = None,
        receipt_id: str | None = None,
    ) -> None:
        if not dispatch_guard.strip():
            raise ValueError("Shadow receipt requires a source dispatch guard")
        self.id = receipt_id or uuid4().hex
        self._specified_id = receipt_id
        self.org = organization_id
        self.principal = principal_id
        self.authorize = authorize
        self.dispatch_guard = dispatch_guard
        self.guard_params = guard_params or {}
        self._binding: dict[str, Any] = {}

    @property
    def params(self) -> dict[str, Any]:
        return (
            self.guard_params
            | self._binding
            | {
                "uuid": self.id,
                "org": self.org,
                "principal": self.principal,
            }
        )

    async def begin(
        self,
        request: DecisionRequest,
        *,
        parent_id: str,
        source_ids: list[str],
        policy: DecisionPolicy,
    ) -> str:
        await self.authorize()
        if self._specified_id is not None and self._specified_id != request.request_id:
            raise DecisionReceiptUnavailable("Shadow invocation identity differs")
        self.id = request.request_id
        if (
            request.org_id != self.org
            or policy.organization_id != self.org
            or policy.principal_id != self.principal
            or request.project_id != policy.project_id
            or request.policy_epoch != policy.epoch
            or request.caller_policy_version != policy.policy_version
            or request.route_policy_sha256 != policy.route_policy_sha256
        ):
            raise DecisionReceiptUnavailable("Shadow request policy differs")
        sources = sorted(set(source_ids) | {parent_id})
        if not {ref.source.id for ref in request.source_refs}.issubset(sources):
            raise DecisionReceiptUnavailable("Shadow source inventory differs")
        self._binding = {
            "policy_key": policy.key,
            "policy_epoch": policy.epoch,
            "policy_version": policy.policy_version,
            "route": policy.route_policy_sha256,
            "decision_sources": sources,
        }
        rows = await _query(
            "RETURN {"
            + self.dispatch_guard
            + _POLICY_GUARD
            + _SOURCE_GUARD
            + _SOURCE_INSERT_FENCE
            + "CREATE semantic_decision_receipts CONTENT $row; RETURN {begun:true}; };",
            **self.params,
            row={
                "uuid": self.id,
                "organization_id": self.org,
                "principal_id": self.principal,
                "parent_id": parent_id,
                "source_ids": sources,
                "policy_key": policy.key,
                "policy_epoch": policy.epoch,
                "request_digest": request.request_digest,
                "semantic_digest": request.semantic_input_sha256,
                "state": "pending",
            },
        )
        if len(rows) != 1 or rows[0].get("begun") is not True:
            raise DecisionReceiptUnavailable("Shadow receipt begin was not committed")
        return self.id

    async def before_dispatch(self) -> None:
        await self.authorize()
        rows = await _query(
            "RETURN {"
            + self.dispatch_guard
            + _POLICY_GUARD
            + _SOURCE_GUARD
            + """LET $updated=(UPDATE semantic_decision_receipts SET attempt_count=1
                WHERE uuid=$uuid AND organization_id=$org AND principal_id=$principal
                    AND state='pending' AND purged=false AND attempt_count=0 RETURN AFTER);
                IF array::len($updated)!=1 { THROW 'Shadow dispatch unavailable'; };
                RETURN {begun:true}; };""",
            **self.params,
        )
        if len(rows) != 1 or rows[0].get("begun") is not True:
            raise DecisionReceiptUnavailable("Shadow dispatch was not committed")

    async def finish(self, observation: DecisionObservation) -> bool:
        """Retain safe accounting even when authorization or a commit fence has changed."""
        usage = canonical(
            {
                "input_tokens": observation.input_tokens,
                "output_tokens": observation.output_tokens,
                "observed_cost_usd": observation.observed_cost_usd,
                "usage_status": observation.usage_status,
                "attempt_count": observation.attempt_count,
                "elapsed_ms": observation.elapsed_ms,
            }
        )
        binding = {
            "request_digest": observation.request_digest,
            "semantic_digest": observation.semantic_input_sha256,
        }
        try:
            await self.authorize()
            rows = await _query(
                "RETURN {"
                + self.dispatch_guard
                + _POLICY_GUARD
                + _SOURCE_GUARD
                + """RETURN (UPDATE semantic_decision_receipts
                    SET state=$state, observation_json=$observation, usage_json=$usage
                    WHERE uuid=$uuid AND organization_id=$org AND principal_id=$principal
                        AND request_digest=$request_digest AND semantic_digest=$semantic_digest
                        AND purged=false AND state='pending' AND attempt_count=1 RETURN AFTER); };""",
                **self.params,
                **binding,
                state="completed" if observation.execution_status == "completed" else "unavailable",
                observation=observation.model_dump_json(),
                usage=usage,
            )
            return len(rows) == 1
        except Exception:
            await _query(
                """UPDATE semantic_decision_receipts SET state='stale', usage_json=$usage,
                    observation_json=NONE WHERE uuid=$uuid AND organization_id=$org
                    AND principal_id=$principal AND request_digest=$request_digest
                    AND semantic_digest=$semantic_digest AND state IN ['pending','stale'];""",
                **self.params,
                **binding,
                usage=usage,
            )
            return False

    async def cancel(self) -> None:
        await self._stop("cancelled")

    async def fail(self) -> None:
        await self._stop("failed")

    async def _stop(self, state: str) -> None:
        await _query(
            """UPDATE semantic_decision_receipts SET state=$state
                WHERE uuid=$uuid AND organization_id=$org AND principal_id=$principal
                    AND state='pending' AND purged=false;""",
            **self.params,
            state=state,
        )

    async def load(self, request: DecisionRequest) -> dict[str, Any] | None:
        """Read a receipt only for a freshly authorized reconstruction of its exact request."""
        await self.authorize()
        if request.org_id != self.org or request.request_id != self.id:
            return None
        rows = await _query(
            """SELECT * FROM semantic_decision_receipts WHERE uuid=$uuid
                AND organization_id=$org AND principal_id=$principal AND purged=false
                AND request_digest=$digest AND semantic_digest=$semantic;""",
            uuid=self.id,
            org=self.org,
            principal=self.principal,
            digest=request.request_digest,
            semantic=request.semantic_input_sha256,
        )
        if not rows:
            return None
        row = rows[0]
        self._binding = {
            "policy_key": row["policy_key"],
            "policy_epoch": row["policy_epoch"],
            "policy_version": request.caller_policy_version,
            "route": request.route_policy_sha256,
            "decision_sources": row["source_ids"],
        }
        rows = await _query(
            "RETURN {"
            + self.dispatch_guard
            + _POLICY_GUARD
            + _SOURCE_GUARD
            + """RETURN (SELECT * FROM semantic_decision_receipts WHERE uuid=$uuid
                AND organization_id=$org AND principal_id=$principal AND purged=false
                AND request_digest=$digest AND semantic_digest=$semantic)[0]; };""",
            **self.params,
            digest=request.request_digest,
            semantic=request.semantic_input_sha256,
        )
        return rows[0] if rows else None
