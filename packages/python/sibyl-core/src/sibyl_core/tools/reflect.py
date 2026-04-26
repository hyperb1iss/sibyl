"""Reflect raw notes into durable memory candidates."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from typing import Any

from sibyl_core.models.reflection import ReflectionCandidate, ReflectionPack
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
    persist: bool = False,
    persist_source: bool = True,
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

    limit = max(1, min(limit, 25))
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

    source_id: str | None = None
    if persist and persist_source:
        source_metadata: dict[str, Any] = {
            "organization_id": organization_id,
            "capture_mode": "reflect",
            "capture_surface": "reflection",
            "remember_kind": "session",
            "reflection_intent": intent,
            "reflection_source": True,
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
        }
        if domain:
            metadata["domain"] = domain
        if source_id:
            metadata["reflection_source_id"] = source_id
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
        persisted.append(replace(candidate, persisted_id=result.id if result.success else None))

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
