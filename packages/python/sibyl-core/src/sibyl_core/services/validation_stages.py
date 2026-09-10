"""Resume evidence-bound model stages using one durable physical dispatch owner."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sibyl_core.ai.transport import observe_transport_attempts
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
    ValidationStageResult,
    _query,
)


async def _record_completed_validation(
    execution: ValidationExecution, result: ValidationStageResult
) -> None:
    try:
        await execution.record_result(result)
    except (Exception, asyncio.CancelledError) as failure:
        usage = result.usage.model_dump(mode="json")
        failure.__dict__["extraction_usage"] = usage
        details = getattr(failure, "details", None)
        if isinstance(details, dict):
            details["extraction_usage"] = usage
        try:
            recovered = await execution.reconcile_result(result)
        except (Exception, asyncio.CancelledError) as recovery_failure:
            raise failure from recovery_failure
        if not recovered or isinstance(failure, asyncio.CancelledError):
            raise


async def run_validation_stage(
    *,
    execution: ValidationExecution,
    parent_id: str,
    source_ids: list[str],
    request: dict[str, Any],
    policy: str,
    check_current: Callable[[], Awaitable[None]],
    run: Callable[[], Awaitable[ValidationStageResult]],
) -> dict[str, Any]:
    """Persist actual output before final fencing; resume recorded output, never resend."""
    claimed = await execution.begin(
        parent_id=parent_id, source_ids=source_ids, policy=policy, request=request
    )
    if not claimed:
        row = await execution.load()
        if row is None or row.get("state") not in {"recorded", "returned"}:
            return await execution.result()
    if claimed:
        try:
            await check_current()
            with observe_transport_attempts(execution):
                result = await run()
        except (Exception, asyncio.CancelledError) as failure:
            try:
                await execution.record_failure(failure)
            except Exception:
                if isinstance(failure, asyncio.CancelledError):
                    raise failure from None
                raise
            raise
        recording = asyncio.create_task(_record_completed_validation(execution, result))
        try:
            await asyncio.shield(recording)
        except asyncio.CancelledError as cancelled:
            try:
                await recording
            except Exception:
                raise cancelled from None
            raise
    try:
        await check_current()
        rows = await _query(
            "RETURN {"
            + execution.dispatch_guard
            + """
            RETURN (UPDATE memory_validation_executions SET state='returned'
                WHERE uuid=$uuid AND organization_id=$org AND principal_id=$principal
                    AND state IN ['recorded','returned'] AND purged=false RETURN AFTER);
            };""",
            **execution.params,
            **execution.guard_params,
        )
        if len(rows) != 1 or rows[0].get("state") != "returned":
            raise ValidationExecutionUnavailable("Validation stage final source fence failed")
    except Exception:
        await _query(
            "UPDATE memory_validation_executions SET state='fenced' WHERE uuid=$uuid "
            "AND organization_id=$org AND principal_id=$principal AND state='recorded';",
            **execution.params,
        )
        raise
    return await execution.result()
