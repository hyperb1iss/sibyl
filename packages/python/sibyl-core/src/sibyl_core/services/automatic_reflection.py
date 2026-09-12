"""Automatically validate, correct and abstain ordinary reflection candidates."""

import json
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter

from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.services.automatic_correction import advance_correction
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    remember_reflection_candidate_review,
    save_raw_memory,
)
from sibyl_core.services.memory_source_validation import SourceAuthorityResolver
from sibyl_core.services.reflection_validation import (
    ORDINARY_SNAPSHOT,
    AuthorizedReflection,
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.validation_candidate import ValidationCandidateWrite
from sibyl_core.services.validation_execution import ValidationExecution, _query
from sibyl_core.services.validation_progress import ProgressContext
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation
from sibyl_core.tasks.procedure_review import ReviewSubmission
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult


@dataclass(frozen=True)
class AutomaticReflectionResult:
    candidate: RawMemory | None
    status: str
    executions: tuple[str, ...]
    reason: str | None = None
    candidate_ids: tuple[str, ...] = ()


async def _current(
    original: AuthorizedReflection, resolver: SourceAuthorityResolver
) -> AuthorizedReflection:
    memory = original.memory
    current = await prepare_stored_reflection(
        memory.organization_id, memory.principal_id, memory.id, resolver
    )
    if (
        current.snapshot_sha256 != original.snapshot_sha256
        or current.prepared.input_sha256 != original.prepared.input_sha256
    ):
        raise SourceUnavailableError()
    return current


async def _abstain(
    original: AuthorizedReflection,
    resolver: SourceAuthorityResolver,
    reason: str,
    executions: list[str],
) -> None:
    current = await _current(original, resolver)
    memory = current.memory
    await save_raw_memory(
        replace(
            memory,
            review_state="archived",
            metadata={
                **memory.metadata,
                "review_state": "archived",
                "autonomy_outcome": "abstained",
                "autonomy_recommended_action": "abstain",
                "automatic_validation_executions": executions,
                "archive_reason": reason,
            },
        ),
        expected_revision=memory.revision,
        source_observations=[memory, *current.sources],
    )


async def _persist_corrected(
    original: AuthorizedReflection,
    resolver: SourceAuthorityResolver,
    outcome: dict[str, Any],
    *,
    review_execution_id: str | None = None,
) -> RawMemory:
    execution_id = str(outcome["execution_id"])
    current = await _current(original, resolver)
    parent = current.memory
    execution = ValidationExecution(execution_id, parent.organization_id, parent.principal_id)
    row = await execution.load()
    if row is None or row["state"] != "returned" or row.get("purged"):
        raise SourceUnavailableError()
    correction = TypeAdapter(ReflectionCorrectionResult).validate_json(row["result_json"])
    if correction.status != "corrected" or not correction.content:
        raise SourceUnavailableError()
    if correction.parent_candidate_sha256 != current.snapshot_sha256:
        raise SourceUnavailableError()
    await execution._check_progress_history(json.loads(row["request_json"]))
    prior_guard = ""
    if review_execution_id is not None:
        from sibyl_core.services.validation_dependencies import (
            dependency_reference,
            resolve_dependencies,
        )

        async def load(identity):
            return await ValidationExecution(
                identity, parent.organization_id, parent.principal_id
            ).load()

        prior = await load(review_execution_id)
        if prior is None:
            raise SourceUnavailableError()
        value = await ValidationExecution(
            review_execution_id, parent.organization_id, parent.principal_id
        ).result()
        if (
            prior["parent_id"] != parent.id
            or ReviewSubmission.model_validate(value["submission"]) != correction.submission
        ):
            raise SourceUnavailableError()
        _, prior_guard = await resolve_dependencies(
            {"execution_dependencies": [dependency_reference(prior).model_dump(mode="json")]},
            execution_id=execution.id,
            org=parent.organization_id,
            principal=parent.principal_id,
            load=load,
        )
    write = ValidationCandidateWrite(
        execution.id,
        row["result_json"],
        prior_guard
        + execution.dispatch_guard
        + ORDINARY_SNAPSHOT
        + "IF $snapshot_digest != $expected { THROW 'Correction source changed'; };"
        + "LET $source_states_to_fence=$snapshot.states;"
        + SOURCE_STATE_WRITE_WITNESS,
        {
            "org": parent.organization_id,
            "principal": parent.principal_id,
            "parent": parent.id,
            "source_ids": current.source_ids,
            "expected": current.snapshot_sha256,
        },
    )
    candidate = replace(
        current.candidate,
        content=correction.content,
        claim_records=[],
        metadata={"automatic_correction": {"parent_id": parent.id, "execution_id": execution.id}},
    )
    suggested_scope = parent.metadata.get("suggested_memory_scope")
    suggested_key = parent.metadata.get("suggested_scope_key")
    if suggested_scope is not None and not isinstance(suggested_scope, str):
        raise SourceUnavailableError()
    if suggested_key is not None and not isinstance(suggested_key, str):
        raise SourceUnavailableError()
    return await remember_reflection_candidate_review(
        organization_id=parent.organization_id,
        principal_id=parent.principal_id,
        candidate=candidate,
        raw_source_ids=[source.id for source in current.sources],
        source_id=current.sources[0].id,
        memory_scope=parent.memory_scope,
        scope_key=parent.scope_key,
        suggested_memory_scope=suggested_scope,
        suggested_scope_key=suggested_key,
        source_memories=current.sources,
        source_observations=current.observations,
        validation_write=write,
        accessible_projects=current.authority.projects,
        accessible_teams=current.authority.teams,
        accessible_delegations=current.authority.delegations,
        allowed_memory_scope_keys=current.authority.scope_keys,
    )


async def _reflection_root(org: str, principal: str, candidate_id: str, resolver) -> str:
    seen = set()
    while True:
        if candidate_id in seen:
            raise SourceUnavailableError()
        seen.add(candidate_id)
        current = await prepare_stored_reflection(
            org, principal, candidate_id, resolver, publication=True
        )
        marker = current.memory.metadata.get("automatic_correction")
        if marker is None:
            return candidate_id
        if not isinstance(marker, dict) or not isinstance(marker.get("execution_id"), str):
            raise SourceUnavailableError()
        stage = ValidationExecution(marker["execution_id"], org, principal)
        await stage.result()
        row = await stage.load()
        if row is None or row["parent_id"] != marker.get("parent_id"):
            raise SourceUnavailableError()
        if candidate_id != str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + stage.id)):
            raise SourceUnavailableError()
        correction = TypeAdapter(ReflectionCorrectionResult).validate_json(row["result_json"])
        if correction.status != "corrected" or correction.content != current.memory.raw_content:
            raise SourceUnavailableError()
        parent = await prepare_stored_reflection(org, principal, row["parent_id"], resolver)
        if correction.parent_candidate_sha256 != parent.snapshot_sha256:
            raise SourceUnavailableError()
        if current.observations != parent.observations:
            raise SourceUnavailableError()
        candidate_id = row["parent_id"]


@dataclass
class _ReflectionAdapter:
    organization_id: str
    principal_id: str
    resolver: SourceAuthorityResolver
    visited: dict[str, AuthorizedReflection] = field(default_factory=dict)

    async def resolve(self, candidate_id: str) -> AuthorizedReflection:
        current = await prepare_stored_reflection(
            self.organization_id, self.principal_id, candidate_id, self.resolver, publication=True
        )
        self.visited[candidate_id] = current
        return current

    def prepared(self, candidate: AuthorizedReflection) -> PreparedMemoryValidation:
        return candidate.prepared

    async def critique(
        self, candidate: AuthorizedReflection, context: ProgressContext | None
    ) -> dict[str, Any]:
        if candidate.memory.review_state == "promoted":
            from sibyl_core.services.ordinary_publication import ordinary_promotion_binding
            from sibyl_core.services.validation_promotion import ValidationBinding

            rows = await _query(
                "SELECT * FROM memory_derivations WHERE organization_id=$org "
                "AND target_id=$parent AND validation_binding_json!=NONE;",
                org=self.organization_id,
                parent=candidate.memory.id,
            )
            if len(rows) != 1:
                raise SourceUnavailableError()
            binding = ValidationBinding.model_validate_json(rows[0]["validation_binding_json"])

            async def authorize():
                await prepare_stored_reflection(
                    self.organization_id,
                    self.principal_id,
                    candidate.memory.id,
                    self.resolver,
                    publication=True,
                )

            await ordinary_promotion_binding(
                self.organization_id,
                self.principal_id,
                candidate.memory.id,
                binding.execution_id,
                self.resolver,
                authorize,
            )
            return await ValidationExecution(
                binding.execution_id, self.organization_id, self.principal_id
            ).result()
        return await validate_reflection_stage(candidate, self.resolver, progress_context=context)

    async def correct(
        self, candidate: AuthorizedReflection, critique: dict[str, Any]
    ) -> tuple[str | None, str, str | None]:
        review = ReviewSubmission.model_validate(critique["submission"])
        outcome = await validate_reflection_stage(
            candidate, self.resolver, review, review_execution_id=str(critique["execution_id"])
        )
        execution_id = str(outcome["execution_id"])
        if outcome["status"] == "abstain":
            return None, execution_id, str(outcome["reason"])
        from sibyl_core.services.surreal_content import get_raw_memory

        child_id = str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + execution_id))
        existing = await get_raw_memory(organization_id=self.organization_id, memory_id=child_id)
        if existing is not None and existing.review_state == "promoted":
            child_root = await _reflection_root(
                self.organization_id, self.principal_id, child_id, self.resolver
            )
            parent_root = await _reflection_root(
                self.organization_id, self.principal_id, candidate.memory.id, self.resolver
            )
            if child_root != parent_root:
                raise SourceUnavailableError()
            published = await prepare_stored_reflection(
                self.organization_id, self.principal_id, child_id, self.resolver, publication=True
            )
            await self.critique(published, None)
            return child_id, execution_id, None
        child = await _persist_corrected(
            candidate, self.resolver, outcome, review_execution_id=str(critique["execution_id"])
        )
        return child.id, execution_id, None

    async def current(self, candidate: AuthorizedReflection) -> None:
        if candidate.memory.review_state == "promoted":
            await self.critique(candidate, None)
        else:
            await _current(candidate, self.resolver)


async def automatically_review_reflection(
    organization_id: str, principal_id: str, candidate_id: str, resolver: SourceAuthorityResolver
) -> AutomaticReflectionResult:
    """Advance verified children; unresolved progress remains pending without redispatch."""
    root = await _reflection_root(organization_id, principal_id, candidate_id, resolver)
    adapter = _ReflectionAdapter(organization_id, principal_id, resolver)
    frontier = await advance_correction(adapter, root)
    if frontier.status == "abstained":
        for original in adapter.visited.values():
            await _abstain(
                original,
                resolver,
                frontier.reason or "evidence_abstention",
                list(frontier.executions),
            )
        return AutomaticReflectionResult(None, "abstained", frontier.executions, frontier.reason)
    if frontier.status == "pending":
        return AutomaticReflectionResult(None, "pending", frontier.executions, frontier.reason)
    assert frontier.candidate is not None
    memory = frontier.candidate.memory
    return AutomaticReflectionResult(
        memory,
        "validated" if memory.id == root else "corrected",
        frontier.executions,
        candidate_ids=tuple(adapter.visited),
    )
