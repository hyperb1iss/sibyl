"""Reflect raw notes into durable memory candidates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sibyl_core.services.dream_checkpoints import DreamSourceWork

import structlog

from sibyl_core.auth.memory_policy import (
    MemoryPolicyDecision,
    authorize_memory_reflect,
    authorize_memory_write,
)
from sibyl_core.models.reflection import ReflectionCandidate, ReflectionPack
from sibyl_core.services.reflection import (
    HeuristicReflectionExtractor,
    ReflectionExtractionRequest,
    ReflectionExtractor,
    apply_reflection_lifecycle_decisions,
    ephemeral_reflection_source_id,
    ground_reflection_candidate,
    validate_reflection_candidates,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    list_raw_memories_for_scope,
    raw_memory_recallable,
)
from sibyl_core.tools.responses import AddResponse

log = structlog.get_logger()


def _tags_for(kind: str, domain: str | None) -> list[str]:
    tags = ["reflection", kind]
    if domain:
        tags.append(domain.strip().lower().replace(" ", "-"))
    return tags


async def reflect_memory(
    content: str,
    *,
    source_title: str = "Session reflection",
    intent: str = "general",
    domain: str | None = None,
    project: str | None = None,
    related_to: list[str] | None = None,
    organization_id: str | None = None,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    writable_projects: set[str] | None = None,
    allowed_memory_scope_keys: frozenset[str] | None = None,
    memory_scope: str | MemoryScope | None = None,
    scope_key: str | None = None,
    suggested_memory_scope: str | MemoryScope | None = None,
    suggested_scope_key: str | None = None,
    persist: bool = False,
    persist_source: bool = True,
    persist_review: bool = False,
    existing_source_id: str | None = None,
    limit: int = 12,
    extractor: ReflectionExtractor | None = None,
    dream_work: DreamSourceWork | None = None,
) -> ReflectionPack:
    """Reflect raw notes into reviewable, optionally persisted memory candidates."""

    content = content.strip()
    if not content:
        msg = "content is required"
        raise ValueError(msg)
    if persist and organization_id is None:
        msg = "organization_id is required when persist=True"
        raise ValueError(msg)
    if persist and persist_review and principal_id is None:
        msg = "principal_id is required when persist_review=True"
        raise ValueError(msg)

    source_memory: RawMemory | None = None
    if persist and persist_review and existing_source_id is not None:
        source_memory = await _reflection_source_snapshot(
            organization_id=str(organization_id),
            principal_id=str(principal_id),
            source_id=existing_source_id,
            content=content,
            accessible_projects=accessible_projects,
        )

    limit = max(1, min(limit, 25))
    resolved_scope = _resolve_reflection_scope(memory_scope, project)
    resolved_scope_key = _resolve_reflection_scope_key(resolved_scope, scope_key, project)
    resolved_suggested_scope = (
        _resolve_reflection_scope(suggested_memory_scope, None)
        if suggested_memory_scope is not None
        else resolved_scope
    )
    resolved_suggested_scope_key = _resolve_reflection_scope_key(
        resolved_suggested_scope,
        suggested_scope_key,
        None,
    )
    extraction_prompt_metadata = _extraction_prompt_metadata(
        intent=intent,
        domain=domain,
        project=project,
        limit=limit,
    )
    active_extractor = extractor or HeuristicReflectionExtractor()

    async def extract_candidates():
        extracted = await active_extractor.extract(
            ReflectionExtractionRequest(
                content=content,
                source_title=source_title,
                intent=intent,
                domain=domain,
                project=project,
                limit=limit,
            )
        )
        validate_reflection_candidates(extracted, require_source_ids=False)
        return extracted

    candidates = await extract_candidates() if not persist or persist_review else None
    source_observations = ()
    source_authority = None

    persist_policy_metadata: dict[str, Any] = {}
    if persist:
        persist_decisions = _authorize_reflection_review_write(
            principal_id=principal_id,
            memory_scope=resolved_scope,
            scope_key=resolved_scope_key,
            accessible_projects=writable_projects
            if writable_projects is not None
            else accessible_projects,
        )
        persist_policy_metadata = _reflect_policy_metadata(persist_decisions)
        if any(not decision.allowed for decision in persist_decisions):
            if candidates is None:
                candidates = await extract_candidates()
            denied_candidates = [
                ground_reflection_candidate(
                    replace(candidate, metadata={**candidate.metadata, **persist_policy_metadata}),
                    raw_source_ids=[],
                    suggested_memory_scope=resolved_suggested_scope.value,
                    suggested_scope_key=resolved_suggested_scope_key,
                    extraction_prompt_metadata=extraction_prompt_metadata,
                    source_id=None,
                )
                for candidate in candidates
            ]
            return ReflectionPack(
                source_title=source_title,
                source_id=None,
                intent=intent,
                domain=domain,
                project=project,
                candidates=denied_candidates,
                total_candidates=len(candidates),
                persisted_count=0,
            )

    source_id: str | None = existing_source_id
    if persist and persist_source and source_id is None:
        if persist_review:
            source, source_memory = await _persist_reflection_source_review(
                title=source_title,
                content=content,
                organization_id=str(organization_id),
                principal_id=str(principal_id),
                domain=domain,
                project=project,
                related_to=related_to,
                memory_scope=resolved_scope,
                scope_key=resolved_scope_key,
                extraction_prompt_metadata=extraction_prompt_metadata,
                policy_metadata=persist_policy_metadata,
            )
        else:
            source = await _persist_reflection_source(
                title=source_title,
                content=content,
                organization_id=str(organization_id),
                principal_id=principal_id,
                domain=domain,
                project=project,
                related_to=related_to,
                accessible_projects=accessible_projects,
                writable_projects=writable_projects,
                memory_scope=memory_scope,
                scope_key=scope_key,
            )
        if source.success:
            source_id = source.id
        else:
            # A requested source is an evidence prerequisite. Never publish a
            # candidate after its source was denied or retired.
            if candidates is None:
                candidates = await extract_candidates()
            return ReflectionPack(
                source_title=source_title,
                source_id=source.id,
                intent=intent,
                domain=domain,
                project=project,
                candidates=[
                    ground_reflection_candidate(
                        replace(
                            candidate,
                            metadata={
                                **candidate.metadata,
                                **persist_policy_metadata,
                                "promotion_state": "denied",
                                "promotion_errors": [source.message],
                            },
                        ),
                        raw_source_ids=[],
                        suggested_memory_scope=resolved_suggested_scope.value,
                        suggested_scope_key=resolved_suggested_scope_key,
                        extraction_prompt_metadata=extraction_prompt_metadata,
                        source_id=None,
                    )
                    for candidate in candidates
                ],
                total_candidates=len(candidates),
                persisted_count=0,
            )

    if persist and not persist_review and source_id is not None:
        from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
        from sibyl_core.services.memory_derivations import validate_observations
        from sibyl_core.services.memory_source_validation import SourceReadAuthority
        from sibyl_core.services.observed_sources import load_authorized_source_snapshot
        from sibyl_core.services.source_observations import (
            GraphSourceSnapshot,
            SourceUnavailableError,
        )

        authority = SourceReadAuthority(
            str(principal_id or ""),
            projects=frozenset(accessible_projects or ()),
            scope_keys=(
                frozenset(allowed_memory_scope_keys)
                if allowed_memory_scope_keys is not None
                else None
            ),
        )
        snapshot = await load_authorized_source_snapshot(
            SourceIdentity(str(organization_id), SourceKind.GRAPH_ENTITY, source_id),
            authority,
            organization_id=str(organization_id),
        )
        if (
            not isinstance(snapshot, GraphSourceSnapshot)
            or not snapshot.observation.durable
            or (snapshot.entity.content or snapshot.entity.description).strip() != content
            or not await validate_observations(
                [snapshot.observation], authority, organization_id=str(organization_id)
            )
        ):
            raise SourceUnavailableError()
        content = snapshot.entity.content or snapshot.entity.description
        source_observations = (snapshot.observation,)
        source_authority = authority
    if candidates is None:
        candidates = await extract_candidates()

    source_anchor_id = source_id
    if persist and source_anchor_id is None and not persist_source:
        source_anchor_id = ephemeral_reflection_source_id(content)
    raw_source_ids = [source_anchor_id] if source_anchor_id else []
    candidates = [
        ground_reflection_candidate(
            candidate,
            raw_source_ids=raw_source_ids,
            suggested_memory_scope=resolved_suggested_scope.value,
            suggested_scope_key=resolved_suggested_scope_key,
            extraction_prompt_metadata=extraction_prompt_metadata,
            source_id=source_id,
        )
        for candidate in candidates
    ]
    if persist:
        prior_memories = await _load_reflection_decision_memories(
            organization_id=str(organization_id),
            principal_id=principal_id,
            memory_scope=resolved_scope,
            scope_key=resolved_scope_key,
            project=project,
        )
        candidates = apply_reflection_lifecycle_decisions(
            candidates,
            prior_memories=prior_memories,
        )
    validate_reflection_candidates(candidates, require_source_ids=persist)

    if dream_work is not None:
        from sibyl_core.services.dream_checkpoints import checkpoint_prepared_candidates

        candidates = await checkpoint_prepared_candidates(dream_work, candidates)
    persisted: list[ReflectionCandidate] = []
    for candidate_index, candidate in enumerate(candidates):
        if not persist:
            persisted.append(candidate)
            continue

        metadata = {
            **candidate.metadata,
            "organization_id": organization_id,
            "capture_mode": "reflect",
            "capture_surface": "reflection",
            "remember_kind": candidate.kind,
            "reflection_reason": candidate.reason,
            "confidence": candidate.confidence,
            **persist_policy_metadata,
        }
        if domain:
            metadata["domain"] = domain
        if source_id:
            metadata["reflection_source_id"] = source_id
        if persist_review:
            candidate_metadata = {**metadata, **persist_policy_metadata}
            dream_kwargs = {}
            if dream_work is not None:
                from sibyl_core.services.dream_checkpoints import DreamCandidateWrite

                dream_kwargs["dream_write"] = DreamCandidateWrite(dream_work, candidate_index)
            review = await _persist_reflection_candidate_review(
                **dream_kwargs,
                candidate=replace(candidate, metadata=candidate_metadata),
                organization_id=str(organization_id),
                principal_id=str(principal_id),
                raw_source_ids=raw_source_ids,
                source_id=source_id,
                source_memories=[source_memory] if source_memory is not None else [],
                accessible_projects=accessible_projects,
                memory_scope=resolved_scope,
                scope_key=resolved_scope_key,
                suggested_memory_scope=resolved_suggested_scope,
                suggested_scope_key=resolved_suggested_scope_key,
                extraction_prompt_metadata=extraction_prompt_metadata,
            )
            persisted.append(
                replace(
                    candidate,
                    metadata={**candidate_metadata, "review_state": review.review_state},
                    persisted_id=review.id,
                    review_state=review.review_state,
                )
            )
            continue
        candidate_result = await _persist_reflection_candidate(
            source_observations=source_observations,
            source_authority=source_authority,
            candidate=replace(candidate, metadata=metadata),
            organization_id=str(organization_id),
            principal_id=principal_id,
            domain=domain,
            project=project,
            source_id=source_id,
            related_to=related_to,
            accessible_projects=accessible_projects,
            writable_projects=writable_projects,
            memory_scope=memory_scope,
            scope_key=scope_key,
        )
        persisted.append(
            replace(
                candidate,
                metadata={**metadata, **candidate_result.metadata},
                persisted_id=candidate_result.response.id
                if candidate_result.response.success
                else None,
            )
        )

    return ReflectionPack(
        source_title=source_title,
        source_id=source_id,
        intent=intent,
        domain=domain,
        project=project,
        candidates=persisted,
        total_candidates=len(candidates),
        persisted_count=sum(1 for candidate in persisted if candidate.persisted_id),
    )


def reflection_pack_to_dict(pack: ReflectionPack) -> dict[str, Any]:
    return {
        "source_title": pack.source_title,
        "source_id": pack.source_id,
        "intent": pack.intent,
        "domain": pack.domain,
        "project": pack.project,
        "candidates": [candidate.to_dict() for candidate in pack.candidates],
        "total_candidates": pack.total_candidates,
        "persisted_count": pack.persisted_count,
        "usage_hint": pack.usage_hint,
    }


def reflection_pack_to_markdown(pack: ReflectionPack) -> str:
    lines = [
        f"# Sibyl Reflection: {pack.source_title}",
        f"Intent: {pack.intent}",
    ]
    if pack.source_id:
        lines.append(f"Source: `{pack.source_id}`")
    if pack.domain:
        lines.append(f"Domain: {pack.domain}")
    if pack.project:
        lines.append(f"Project: {pack.project}")

    for candidate in pack.candidates:
        persisted = f" `{candidate.persisted_id}`" if candidate.persisted_id else ""
        lines.extend(
            [
                "",
                f"## {candidate.kind.title()}: {candidate.title}{persisted}",
                f"- Confidence: {candidate.confidence:.2f}",
                f"- Why: {candidate.reason}",
                f"- Memory: {candidate.content}",
            ]
        )

    if pack.usage_hint:
        lines.extend(["", f"_Hint: {pack.usage_hint}_"])
    return "\n".join(lines)


__all__ = [
    "reflect_memory",
    "reflection_pack_to_dict",
    "reflection_pack_to_markdown",
]


def _resolve_reflection_scope(
    memory_scope: str | MemoryScope | None,
    project: str | None,
) -> MemoryScope:
    if memory_scope is not None:
        try:
            return MemoryScope(memory_scope)
        except ValueError:
            return MemoryScope.PRIVATE
    return MemoryScope.PROJECT if project else MemoryScope.PRIVATE


def _resolve_reflection_scope_key(
    memory_scope: MemoryScope,
    scope_key: str | None,
    project: str | None,
) -> str | None:
    if memory_scope is MemoryScope.PROJECT:
        return scope_key or project
    return scope_key


def _extraction_prompt_metadata(
    *,
    intent: str,
    domain: str | None,
    project: str | None,
    limit: int,
) -> dict[str, object]:
    return {
        "extractor": "sibyl_reflection_extractor",
        "extractor_version": "v0.12",
        "intent": intent,
        "domain": domain,
        "project": project,
        "limit": limit,
    }


def _authorize_reflection_review_write(
    *,
    principal_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    accessible_projects: set[str] | None,
) -> tuple[MemoryPolicyDecision, MemoryPolicyDecision]:
    reflect_decision = authorize_memory_reflect(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    write_decision = authorize_memory_write(
        principal_id=principal_id,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
    )
    return reflect_decision, write_decision


def _reflect_policy_metadata(decisions: tuple[MemoryPolicyDecision, ...]) -> dict[str, Any]:
    return {
        "memory_scope": decisions[0].memory_scope.value,
        "scope_key": decisions[0].scope_key,
        "policy_allowed": all(decision.allowed for decision in decisions),
        "policy_reasons": [decision.reason for decision in decisions],
        "policy_actions": [decision.action.value for decision in decisions],
    }


async def _load_reflection_decision_memories(
    *,
    organization_id: str,
    principal_id: str | None,
    memory_scope: MemoryScope,
    scope_key: str | None,
    project: str | None,
) -> list[RawMemory]:
    if principal_id is None:
        return []
    try:
        memories = await list_raw_memories_for_scope(
            organization_id=organization_id,
            principal_id=principal_id,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project,
            limit=100,
            include_lifecycle_hidden=True,
        )
    except Exception as exc:
        log.warning("reflection_decision_memory_lookup_failed", error=str(exc))
        return []
    return [memory for memory in memories if raw_memory_recallable(memory)]


async def _reflection_source_snapshot(
    *,
    organization_id: str,
    principal_id: str,
    source_id: str,
    content: str,
    accessible_projects: set[str] | None,
) -> RawMemory:
    from sibyl_core.services.memory_policy import _authorize_share_source_read

    source = await get_raw_memory(organization_id=organization_id, memory_id=source_id)
    if (
        source is None
        or not _authorize_share_source_read(
            memory=source,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
        ).allowed
        or not raw_memory_recallable(source)
        or source.raw_content.strip() != content
    ):
        raise ValueError("Reflection source is unavailable or changed")
    return source


async def _persist_reflection_source_review(**kwargs: Any) -> tuple[AddResponse, RawMemory]:
    from sibyl_core.services.surreal_content import remember_raw_memory

    policy_metadata = dict(kwargs.get("policy_metadata") or {})
    memory = await remember_raw_memory(
        organization_id=kwargs["organization_id"],
        principal_id=kwargs["principal_id"],
        source_id=f"reflection:{kwargs['title']}",
        raw_content=kwargs["content"],
        title=kwargs["title"],
        memory_scope=kwargs["memory_scope"],
        scope_key=kwargs["scope_key"],
        tags=_tags_for("session", kwargs.get("domain")),
        metadata={
            "organization_id": kwargs["organization_id"],
            "capture_mode": "reflect",
            "capture_surface": "reflection_source",
            "remember_kind": "session",
            "reflection_source": True,
            "project_id": kwargs.get("project"),
            "domain": kwargs.get("domain"),
            "related_to": list(kwargs.get("related_to") or []),
            "extraction_prompt_metadata": dict(kwargs["extraction_prompt_metadata"]),
            "review_state": "pending",
            **policy_metadata,
        },
        provenance={"capture_mode": "reflect"},
        capture_surface="reflection_source",
        entity_type="session",
    )
    return (
        AddResponse(
            success=True,
            id=memory.id,
            message=f"Stored reflection source for review: {memory.title}",
            timestamp=datetime.now(UTC),
        ),
        memory,
    )


async def _persist_reflection_source(**kwargs: Any) -> AddResponse:
    from sibyl_core.services.memory import persist_reflection_source

    result = await persist_reflection_source(**kwargs)
    return result.response


async def _persist_reflection_candidate_review(**kwargs: Any):
    from sibyl_core.services.surreal_content import remember_reflection_candidate_review

    return await remember_reflection_candidate_review(**kwargs)


async def _persist_reflection_candidate(**kwargs: Any):
    from sibyl_core.services.memory import persist_reflection_candidate

    return await persist_reflection_candidate(**kwargs)
