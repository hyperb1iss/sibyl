"""Advance source-authorized correction stages using their existing receipts."""

import json
from dataclasses import dataclass
from typing import Any, Protocol

from sibyl_core.services.validation_progress import ProgressContext
from sibyl_core.tasks.memory_progress import semantic_candidate_digest
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation


class CorrectionAdapter[T](Protocol):
    organization_id: str
    principal_id: str

    async def resolve(self, candidate_id: str) -> T: ...
    def prepared(self, candidate: T) -> PreparedMemoryValidation: ...
    async def critique(self, candidate: T, context: ProgressContext | None) -> dict[str, Any]: ...
    async def correct(
        self, candidate: T, critique: dict[str, Any]
    ) -> tuple[str | None, str, str | None]: ...
    async def current(self, candidate: T) -> None: ...


@dataclass(frozen=True)
class CorrectionFrontier[T]:
    status: str
    candidate: T | None
    executions: tuple[str, ...]
    reason: str | None = None


async def advance_correction[T](
    adapter: CorrectionAdapter[T], root_id: str
) -> CorrectionFrontier[T]:
    """Replay the verified root forward; only the first absent stage can dispatch."""
    candidate_id = root_id
    context: ProgressContext | None = None
    executions: list[str] = []
    ancestors: tuple[str, ...] = ()
    while True:
        candidate = await adapter.resolve(candidate_id)
        prepared = adapter.prepared(candidate)
        if context is not None:
            prepared, _ = await context.prepare(
                prepared, adapter.organization_id, adapter.principal_id
            )
        critique = await adapter.critique(candidate, context)
        critic_id = str(critique["execution_id"])
        executions.append(critic_id)
        if critique["status"] == "no_findings":
            await adapter.current(candidate)
            return CorrectionFrontier("validated", candidate, tuple(executions))
        progress = critique.get("progress")
        if critique["status"] == "abstain" or progress in {"no_progress", "abstain"}:
            return CorrectionFrontier(
                "abstained",
                candidate,
                tuple(executions),
                str(critique.get("reason") or progress or "evidence_abstention"),
            )
        if context is not None and progress != "advance":
            # A completed unresolved review is not a license to resend the same
            # request or discard a supported partial correction.
            return CorrectionFrontier(
                "pending", candidate, tuple(executions), "unresolved_progress"
            )
        next_id, correction_id, reason = await adapter.correct(candidate, critique)
        executions.append(correction_id)
        if next_id is None:
            return CorrectionFrontier("abstained", candidate, tuple(executions), reason)
        context = ProgressContext(prepared, critic_id, correction_id, ancestors)
        ancestors = (*ancestors, semantic_candidate_digest(json.loads(prepared.payload_json)))
        candidate_id = next_id
