"""Reflect raw notes into durable memory candidates."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from sibyl_core.auth.memory_policy import (
    MemoryPolicyDecision,
    authorize_memory_reflect,
    authorize_memory_write,
)
from sibyl_core.models.reflection import ReflectionCandidate, ReflectionPack
from sibyl_core.services.surreal_content import MemoryScope
from sibyl_core.tools.add import add as default_add
from sibyl_core.tools.responses import AddResponse

AddFn = Callable[..., Awaitable[AddResponse]]

_KIND_RULES: tuple[tuple[str, tuple[str, ...], str, float], ...] = (
    (
        "decision",
        (
            "decided",
            "decision",
            "we will",
            "we'll",
            "chosen",
            "choose",
            "keep",
            "use ",
        ),
        "captures a choice or direction future agents should preserve",
        0.86,
    ),
    (
        "plan",
        (
            "plan",
            "next",
            "todo",
            "build",
            "implement",
            "migrate",
            "phase",
            "workstream",
        ),
        "captures sequencing, scope, or intended work",
        0.80,
    ),
    (
        "idea",
        (
            "idea",
            "maybe",
            "could",
            "what if",
            "explore",
            "brainstorm",
            "possible",
        ),
        "captures a possibility before it becomes a decision",
        0.72,
    ),
    (
        "claim",
        (
            "confirmed",
            "validated",
            "observed",
            "because",
            "means",
            "fact",
            "latest",
        ),
        "captures an assertion that may need provenance or contradiction handling",
        0.78,
    ),
    (
        "procedure",
        (
            "workflow",
            "steps",
            "run ",
            "command",
            "use `",
            "verify",
            "test",
        ),
        "captures repeatable process knowledge",
        0.76,
    ),
)

_ARTIFACT_PATTERN = re.compile(
    r"(?i)(https?://\S+|(?:[\w.-]+/)+[\w.-]+\.\w+|[\w.-]+\.(?:md|py|ts|tsx|json|ya?ml|toml|rs))"
)
_SPLIT_PATTERN = re.compile(r"(?:\n+|(?<=[.!?])\s+)")


def _compact(value: str, max_chars: int = 500) -> str:
    compact = " ".join(value.strip().split())
    if len(compact) <= max_chars:
        return compact
    cutoff = compact.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return compact[:cutoff].rstrip() + "..."


def _derive_title(text: str, *, prefix: str | None = None, max_chars: int = 86) -> str:
    title = _compact(re.sub(r"^[\-*#>\d.)\s]+", "", text), max_chars=max_chars)
    if prefix and not title.lower().startswith(prefix.lower()):
        return f"{prefix}: {title}"[:max_chars].rstrip()
    return title or "Reflected memory"


def _segments(content: str) -> list[str]:
    raw_segments = [_compact(part, max_chars=900) for part in _SPLIT_PATTERN.split(content)]
    return [part for part in raw_segments if len(part) >= 12]


def _tags_for(kind: str, domain: str | None) -> list[str]:
    tags = ["reflection", kind]
    if domain:
        tags.append(domain.strip().lower().replace(" ", "-"))
    return tags


def _candidate_for_segment(
    segment: str,
    *,
    source_title: str,
    intent: str,
    domain: str | None,
    project: str | None,
    index: int,
) -> ReflectionCandidate | None:
    lower = segment.lower()
    kind = ""
    reason = ""
    confidence = 0.0

    if _ARTIFACT_PATTERN.search(segment):
        kind = "artifact"
        reason = "mentions a concrete file, URL, document, or generated output"
        confidence = 0.82
    elif lower.startswith(("maybe ", "what if ", "could ")):
        kind = "idea"
        reason = "captures a possibility before it becomes a decision"
        confidence = 0.82
    elif lower.startswith(("next ", "todo ", "phase ", "workstream ")):
        kind = "plan"
        reason = "captures sequencing, scope, or intended work"
        confidence = 0.84
    else:
        for candidate_kind, markers, candidate_reason, score in _KIND_RULES:
            if any(marker in lower for marker in markers):
                kind = candidate_kind
                reason = candidate_reason
                confidence = score
                break

    if not kind:
        return None

    metadata: dict[str, Any] = {
        "reflection_source_title": source_title,
        "reflection_intent": intent,
        "reflection_index": index,
    }
    if project:
        metadata["project_id"] = project

    return ReflectionCandidate(
        kind=kind,
        title=_derive_title(segment, prefix=kind.capitalize()),
        content=segment,
        reason=reason,
        confidence=confidence,
        tags=_tags_for(kind, domain),
        metadata=metadata,
    )


def _fallback_session_candidate(
    *,
    source_title: str,
    content: str,
    intent: str,
    domain: str | None,
    project: str | None,
) -> ReflectionCandidate:
    metadata: dict[str, Any] = {
        "reflection_source_title": source_title,
        "reflection_intent": intent,
        "reflection_index": 0,
    }
    if project:
        metadata["project_id"] = project
    return ReflectionCandidate(
        kind="session",
        title=_derive_title(source_title, prefix="Session"),
        content=_compact(content, max_chars=1200),
        reason="preserves the raw session checkpoint when no finer candidate is obvious",
        confidence=0.60,
        tags=_tags_for("session", domain),
        metadata=metadata,
    )


def _dedupe(candidates: list[ReflectionCandidate], limit: int) -> list[ReflectionCandidate]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ReflectionCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        key = (candidate.kind, candidate.content.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= limit:
            break
    return deduped


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
    memory_scope: str | MemoryScope | None = None,
    scope_key: str | None = None,
    persist: bool = False,
    persist_source: bool = True,
    persist_review: bool = False,
    limit: int = 12,
    add_fn: AddFn = default_add,
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

    limit = max(1, min(limit, 25))
    resolved_scope = _resolve_reflection_scope(memory_scope, project)
    resolved_scope_key = _resolve_reflection_scope_key(resolved_scope, scope_key, project)
    extraction_prompt_metadata = _extraction_prompt_metadata(
        intent=intent,
        domain=domain,
        project=project,
        limit=limit,
    )
    candidates = [
        candidate
        for index, segment in enumerate(_segments(content))
        if (
            candidate := _candidate_for_segment(
                segment,
                source_title=source_title,
                intent=intent,
                domain=domain,
                project=project,
                index=index,
            )
        )
        is not None
    ]
    if not candidates:
        candidates = [
            _fallback_session_candidate(
                source_title=source_title,
                content=content,
                intent=intent,
                domain=domain,
                project=project,
            )
        ]
    candidates = _dedupe(candidates, limit)

    persist_policy_metadata: dict[str, Any] = {}
    if persist:
        persist_decisions = _authorize_reflection_review_write(
            principal_id=principal_id,
            memory_scope=resolved_scope,
            scope_key=resolved_scope_key,
            accessible_projects=accessible_projects,
        )
        persist_policy_metadata = _reflect_policy_metadata(persist_decisions)
        if any(not decision.allowed for decision in persist_decisions):
            denied_candidates = [
                _candidate_with_review_metadata(
                    replace(candidate, metadata={**candidate.metadata, **persist_policy_metadata}),
                    raw_source_ids=[],
                    suggested_memory_scope=resolved_scope,
                    suggested_scope_key=resolved_scope_key,
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

    source_id: str | None = None
    use_native_write = persist and _native_reflection_write_enabled()
    if persist and persist_source:
        if persist_review:
            source = await _persist_reflection_source_review(
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
        elif use_native_write:
            source = await _persist_reflection_source_native(
                title=source_title,
                content=content,
                organization_id=str(organization_id),
                principal_id=principal_id,
                domain=domain,
                project=project,
                related_to=related_to,
                accessible_projects=accessible_projects,
                memory_scope=memory_scope,
                scope_key=scope_key,
            )
        else:
            source_metadata: dict[str, Any] = {
                "organization_id": organization_id,
                "capture_mode": "reflect",
                "capture_surface": "reflection",
                "remember_kind": "session",
                "reflection_intent": intent,
                "reflection_source": True,
                **persist_policy_metadata,
            }
            if domain:
                source_metadata["domain"] = domain
            if project:
                source_metadata["project_id"] = project
            source = await add_fn(
                title=source_title,
                content=content,
                entity_type="session",
                category=domain,
                tags=_tags_for("session", domain),
                related_to=related_to,
                metadata=source_metadata,
                sync=True,
                check_conflicts=False,
            )
        if source.success:
            source_id = source.id

    raw_source_ids = [source_id] if source_id else []
    candidates = [
        _candidate_with_review_metadata(
            candidate,
            raw_source_ids=raw_source_ids,
            suggested_memory_scope=resolved_scope,
            suggested_scope_key=resolved_scope_key,
            extraction_prompt_metadata=extraction_prompt_metadata,
            source_id=source_id,
        )
        for candidate in candidates
    ]

    persisted: list[ReflectionCandidate] = []
    for candidate in candidates:
        if not persist:
            persisted.append(candidate)
            continue

        candidate_related_to = list(related_to or [])
        if source_id:
            candidate_related_to.append(source_id)
        metadata = {
            **candidate.metadata,
            "organization_id": organization_id,
            "capture_mode": "reflect",
            "capture_surface": "reflection",
            "remember_kind": candidate.kind,
            "reflection_reason": candidate.reason,
            "reflection_confidence": candidate.confidence,
            **persist_policy_metadata,
        }
        if domain:
            metadata["domain"] = domain
        if source_id:
            metadata["reflection_source_id"] = source_id
        if persist_review:
            candidate_metadata = {**metadata, **persist_policy_metadata}
            review = await _persist_reflection_candidate_review(
                candidate=replace(candidate, metadata=candidate_metadata),
                organization_id=str(organization_id),
                principal_id=str(principal_id),
                raw_source_ids=raw_source_ids,
                source_id=source_id,
                memory_scope=resolved_scope,
                scope_key=resolved_scope_key,
                suggested_memory_scope=resolved_scope,
                suggested_scope_key=resolved_scope_key,
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
        if use_native_write:
            native_result = await _persist_reflection_candidate_native(
                candidate=replace(candidate, metadata=metadata),
                organization_id=str(organization_id),
                principal_id=principal_id,
                domain=domain,
                project=project,
                source_id=source_id,
                related_to=related_to,
                accessible_projects=accessible_projects,
                memory_scope=memory_scope,
                scope_key=scope_key,
            )
            persisted.append(
                replace(
                    candidate,
                    metadata={**metadata, **native_result.metadata},
                    persisted_id=native_result.response.id
                    if native_result.response.success
                    else None,
                )
            )
            continue

        result = await add_fn(
            title=candidate.title,
            content=candidate.content,
            entity_type=candidate.kind,
            category=domain,
            tags=candidate.tags,
            related_to=candidate_related_to or None,
            metadata=metadata,
            sync=True,
            check_conflicts=True,
        )
        persisted.append(
            replace(
                candidate, metadata=metadata, persisted_id=result.id if result.success else None
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
    return asdict(pack)


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


def _native_reflection_write_enabled() -> bool:
    from sibyl_core.services.native_memory import native_reflection_write_enabled

    return native_reflection_write_enabled()


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
        "extractor": "sibyl_reflect_heuristic",
        "extractor_version": "v0.7",
        "intent": intent,
        "domain": domain,
        "project": project,
        "limit": limit,
    }


def _candidate_with_review_metadata(
    candidate: ReflectionCandidate,
    *,
    raw_source_ids: list[str],
    suggested_memory_scope: MemoryScope,
    suggested_scope_key: str | None,
    extraction_prompt_metadata: dict[str, object],
    source_id: str | None,
) -> ReflectionCandidate:
    metadata = {
        **candidate.metadata,
        "raw_source_ids": list(raw_source_ids),
        "source_ids": list(raw_source_ids),
        "suggested_memory_scope": suggested_memory_scope.value,
        "suggested_scope_key": suggested_scope_key,
        "review_state": candidate.review_state,
        "extraction_prompt_metadata": dict(extraction_prompt_metadata),
    }
    if source_id:
        metadata["reflection_source_id"] = source_id
    return replace(
        candidate,
        metadata=metadata,
        raw_source_ids=list(raw_source_ids),
        suggested_memory_scope=suggested_memory_scope.value,
        suggested_scope_key=suggested_scope_key,
    )


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


async def _persist_reflection_source_review(**kwargs: Any) -> AddResponse:
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
    return AddResponse(
        success=True,
        id=memory.id,
        message=f"Stored reflection source for review: {memory.title}",
        timestamp=datetime.now(UTC),
    )


async def _persist_reflection_source_native(**kwargs: Any) -> AddResponse:
    from sibyl_core.services.native_memory import persist_reflection_source_native

    result = await persist_reflection_source_native(**kwargs)
    return result.response


async def _persist_reflection_candidate_review(**kwargs: Any):
    from sibyl_core.services.surreal_content import remember_reflection_candidate_review

    return await remember_reflection_candidate_review(**kwargs)


async def _persist_reflection_candidate_native(**kwargs: Any):
    from sibyl_core.services.native_memory import persist_reflection_candidate_native

    return await persist_reflection_candidate_native(**kwargs)
