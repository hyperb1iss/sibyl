"""Models for reflecting raw session traces into durable memory candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import uuid4


class MemoryLifecycleState(StrEnum):
    ACTIVE = "active"
    CONTESTED = "contested"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MemoryLifecycleFlag(StrEnum):
    HIDDEN = "hidden"
    REDACTED = "redacted"
    SENSITIVE = "sensitive"


_LEGACY_LIFECYCLE_STATES: dict[str, MemoryLifecycleState] = {
    "pending": MemoryLifecycleState.ACTIVE,
    "deferred": MemoryLifecycleState.ACTIVE,
    "promoted": MemoryLifecycleState.ACTIVE,
    "restored": MemoryLifecycleState.ACTIVE,
    "duplicate": MemoryLifecycleState.CONTESTED,
    "stale": MemoryLifecycleState.CONTESTED,
    "wrong": MemoryLifecycleState.CONTESTED,
    "hidden": MemoryLifecycleState.ACTIVE,
    "redacted": MemoryLifecycleState.ACTIVE,
    "sensitive": MemoryLifecycleState.ACTIVE,
}
_LEGACY_LIFECYCLE_FLAGS: dict[str, MemoryLifecycleFlag] = {
    flag.value: flag for flag in MemoryLifecycleFlag
}


class ReflectionFindingKind(StrEnum):
    CLAIM = "claim"
    DUPLICATE = "duplicate"
    STALE = "stale"
    CONTRADICTION = "contradiction"
    SUPERSESSION = "supersession"
    PROMOTION = "promotion"
    CORRECTION = "correction"
    EXCEPTION = "exception"


_CORRECTION_FINDING_KIND: dict[str, ReflectionFindingKind] = {
    "mark_duplicate": ReflectionFindingKind.DUPLICATE,
    "mark_stale": ReflectionFindingKind.STALE,
    "supersede": ReflectionFindingKind.SUPERSESSION,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _state_value(value: MemoryLifecycleState | str | None) -> str:
    if isinstance(value, MemoryLifecycleState):
        return value.value
    text = str(value or MemoryLifecycleState.ACTIVE.value).strip().lower()
    if legacy_state := _LEGACY_LIFECYCLE_STATES.get(text):
        return legacy_state.value
    try:
        return MemoryLifecycleState(text).value
    except ValueError:
        return MemoryLifecycleState.CONTESTED.value


def _flag_values(value: object, *, legacy_state: object = None) -> list[str]:
    flags = _str_list(value)
    legacy_flag = _LEGACY_LIFECYCLE_FLAGS.get(str(legacy_state or "").strip().lower())
    if legacy_flag is not None:
        flags.append(legacy_flag.value)
    return list(dict.fromkeys(flag.strip().lower() for flag in flags if flag.strip()))


def _finding_kind_value(value: ReflectionFindingKind | str | None) -> str:
    if isinstance(value, ReflectionFindingKind):
        return value.value
    text = str(value or ReflectionFindingKind.CORRECTION.value).strip().lower()
    try:
        return ReflectionFindingKind(text).value
    except ValueError:
        return text or ReflectionFindingKind.CORRECTION.value


def _str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item).strip()]


def _dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {str(key): item_value for key, item_value in cast("dict[object, Any]", item).items()}
        for item in value
        if isinstance(item, dict)
    ]


def _dict_value(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item_value for key, item_value in cast("dict[object, Any]", value).items()}


@dataclass(frozen=True)
class ClaimRecord:
    content: str
    source_ids: list[str]
    confidence: float
    id: str = field(default_factory=lambda: f"claim_{uuid4().hex}")
    title: str = ""
    raw_source_ids: list[str] = field(default_factory=list)
    memory_scope: str | None = None
    scope_key: str | None = None
    validity: str = "active"
    freshness: str = "current"
    supports_source_ids: list[str] = field(default_factory=list)
    contradicts_source_ids: list[str] = field(default_factory=list)
    supersedes_source_ids: list[str] = field(default_factory=list)
    superseded_by_source_id: str | None = None
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "confidence": self.confidence,
            "source_ids": list(self.source_ids),
            "raw_source_ids": list(self.raw_source_ids),
            "memory_scope": self.memory_scope,
            "scope_key": self.scope_key,
            "validity": self.validity,
            "freshness": self.freshness,
            "supports_source_ids": list(self.supports_source_ids),
            "contradicts_source_ids": list(self.contradicts_source_ids),
            "supersedes_source_ids": list(self.supersedes_source_ids),
            "superseded_by_source_id": self.superseded_by_source_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ClaimRecord:
        return cls(
            id=str(value.get("id") or f"claim_{uuid4().hex}"),
            title=str(value.get("title") or ""),
            content=str(value.get("content") or ""),
            confidence=float(value.get("confidence") or 0.0),
            source_ids=_str_list(value.get("source_ids")),
            raw_source_ids=_str_list(value.get("raw_source_ids")),
            memory_scope=str(value["memory_scope"]) if value.get("memory_scope") else None,
            scope_key=str(value["scope_key"]) if value.get("scope_key") else None,
            validity=str(value.get("validity") or "active"),
            freshness=str(value.get("freshness") or "current"),
            supports_source_ids=_str_list(value.get("supports_source_ids")),
            contradicts_source_ids=_str_list(value.get("contradicts_source_ids")),
            supersedes_source_ids=_str_list(value.get("supersedes_source_ids")),
            superseded_by_source_id=str(value["superseded_by_source_id"])
            if value.get("superseded_by_source_id")
            else None,
            created_at=str(value.get("created_at") or _now_iso()),
            metadata=_dict_value(value.get("metadata")),
        )


@dataclass(frozen=True)
class ReflectionFinding:
    kind: ReflectionFindingKind | str
    target_source_id: str
    reason: str
    confidence: float = 1.0
    id: str = field(default_factory=lambda: f"finding_{uuid4().hex}")
    action: str | None = None
    lifecycle_state: MemoryLifecycleState | str | None = None
    source_ids: list[str] = field(default_factory=list)
    related_source_ids: list[str] = field(default_factory=list)
    policy_reasons: list[str] = field(default_factory=list)
    reversible: bool = True
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": _finding_kind_value(self.kind),
            "target_source_id": self.target_source_id,
            "reason": self.reason,
            "confidence": self.confidence,
            "action": self.action,
            "lifecycle_state": _state_value(self.lifecycle_state),
            "source_ids": list(self.source_ids),
            "related_source_ids": list(self.related_source_ids),
            "policy_reasons": list(self.policy_reasons),
            "reversible": self.reversible,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReflectionFinding:
        return cls(
            id=str(value.get("id") or f"finding_{uuid4().hex}"),
            kind=_finding_kind_value(value.get("kind")),
            target_source_id=str(value.get("target_source_id") or ""),
            reason=str(value.get("reason") or ""),
            confidence=float(value.get("confidence") or 0.0),
            action=str(value["action"]) if value.get("action") else None,
            lifecycle_state=_state_value(value.get("lifecycle_state")),
            source_ids=_str_list(value.get("source_ids")),
            related_source_ids=_str_list(value.get("related_source_ids")),
            policy_reasons=_str_list(value.get("policy_reasons")),
            reversible=bool(value.get("reversible", True)),
            created_at=str(value.get("created_at") or _now_iso()),
            metadata=_dict_value(value.get("metadata")),
        )


@dataclass(frozen=True)
class ReflectionRelationshipRecord:
    source_id: str
    target_id: str
    relationship_type: str
    reason: str
    confidence: float = 1.0
    source_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relationship_type": self.relationship_type,
            "reason": self.reason,
            "confidence": self.confidence,
            "source_ids": list(self.source_ids),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReflectionRelationshipRecord:
        return cls(
            source_id=str(value.get("source_id") or ""),
            target_id=str(value.get("target_id") or ""),
            relationship_type=str(value.get("relationship_type") or "RELATED_TO"),
            reason=str(value.get("reason") or ""),
            confidence=float(value.get("confidence") or 0.0),
            source_ids=_str_list(value.get("source_ids")),
            created_at=str(value.get("created_at") or _now_iso()),
            metadata=_dict_value(value.get("metadata")),
        )


@dataclass(frozen=True)
class MemoryLifecycle:
    state: MemoryLifecycleState | str
    source_id: str
    action: str
    reason: str
    prior_state: str | None = None
    replacement_source_id: str | None = None
    duplicate_of_source_id: str | None = None
    derived_ids: list[str] = field(default_factory=list)
    flags: list[MemoryLifecycleFlag | str] = field(default_factory=list)
    reversible: bool = True
    created_at: str = field(default_factory=_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": _state_value(self.state),
            "source_id": self.source_id,
            "action": self.action,
            "reason": self.reason,
            "prior_state": self.prior_state,
            "replacement_source_id": self.replacement_source_id,
            "duplicate_of_source_id": self.duplicate_of_source_id,
            "derived_ids": list(self.derived_ids),
            "flags": _flag_values(self.flags),
            "reversible": self.reversible,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MemoryLifecycle:
        raw_state = value.get("state")
        return cls(
            state=_state_value(raw_state),
            source_id=str(value.get("source_id") or ""),
            action=str(value.get("action") or ""),
            reason=str(value.get("reason") or ""),
            prior_state=str(value["prior_state"]) if value.get("prior_state") else None,
            replacement_source_id=str(value["replacement_source_id"])
            if value.get("replacement_source_id")
            else None,
            duplicate_of_source_id=str(value["duplicate_of_source_id"])
            if value.get("duplicate_of_source_id")
            else None,
            derived_ids=_str_list(value.get("derived_ids")),
            flags=_flag_values(value.get("flags"), legacy_state=raw_state),
            reversible=bool(value.get("reversible", True)),
            created_at=str(value.get("created_at") or _now_iso()),
            metadata=_dict_value(value.get("metadata")),
        )


@dataclass(frozen=True)
class ReflectionCandidate:
    kind: str
    title: str
    content: str
    reason: str
    confidence: float
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_source_ids: list[str] = field(default_factory=list)
    suggested_memory_scope: str | None = None
    suggested_scope_key: str | None = None
    review_state: str = "pending"
    persisted_id: str | None = None
    claim_records: list[ClaimRecord] = field(default_factory=list)
    reflection_findings: list[ReflectionFinding] = field(default_factory=list)
    relationship_records: list[ReflectionRelationshipRecord] = field(default_factory=list)
    sensitivity_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "reason": self.reason,
            "confidence": self.confidence,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "raw_source_ids": list(self.raw_source_ids),
            "suggested_memory_scope": self.suggested_memory_scope,
            "suggested_scope_key": self.suggested_scope_key,
            "review_state": self.review_state,
            "persisted_id": self.persisted_id,
            "claim_records": [claim.to_dict() for claim in self.claim_records],
            "reflection_findings": [finding.to_dict() for finding in self.reflection_findings],
            "relationship_records": [
                relationship.to_dict() for relationship in self.relationship_records
            ],
            "sensitivity_flags": list(self.sensitivity_flags),
        }


@dataclass(frozen=True)
class ReflectionPack:
    source_title: str
    source_id: str | None
    intent: str
    domain: str | None
    project: str | None
    candidates: list[ReflectionCandidate]
    total_candidates: int
    persisted_count: int = 0
    usage_hint: str = (
        "Review candidates, persist the durable ones, and keep raw session source as provenance."
    )


def claim_records_from_metadata(metadata: dict[str, object]) -> list[ClaimRecord]:
    return [ClaimRecord.from_dict(item) for item in _dict_list(metadata.get("claim_records"))]


def reflection_findings_from_metadata(metadata: dict[str, object]) -> list[ReflectionFinding]:
    return [
        ReflectionFinding.from_dict(item)
        for item in _dict_list(metadata.get("reflection_findings"))
    ]


def reflection_relationships_from_metadata(
    metadata: dict[str, object],
) -> list[ReflectionRelationshipRecord]:
    return [
        ReflectionRelationshipRecord.from_dict(item)
        for item in _dict_list(metadata.get("relationship_records"))
    ]


def memory_lifecycle_from_metadata(
    metadata: dict[str, object],
    *,
    source_id: str,
    review_state: str = "pending",
) -> MemoryLifecycle:
    lifecycle = _dict_value(metadata.get("memory_lifecycle"))
    if lifecycle:
        return MemoryLifecycle.from_dict(lifecycle)
    raw_state = metadata.get("lifecycle_state") or metadata.get("review_state") or review_state
    state = _state_value(str(raw_state) if raw_state else review_state)
    flags = _flag_values(metadata.get("lifecycle_flags"), legacy_state=raw_state)
    for flag in MemoryLifecycleFlag:
        if metadata.get(flag.value) or metadata.get(f"{flag.value}_at"):
            flags = list(dict.fromkeys([*flags, flag.value]))
    return MemoryLifecycle(
        state=state,
        source_id=source_id,
        action=str(metadata.get("lifecycle_action") or state),
        reason=str(metadata.get("lifecycle_reason") or ""),
        prior_state=str(metadata["prior_review_state"])
        if metadata.get("prior_review_state")
        else None,
        replacement_source_id=str(metadata["superseded_by_source_id"])
        if metadata.get("superseded_by_source_id")
        else None,
        duplicate_of_source_id=str(metadata["duplicate_of_source_id"])
        if metadata.get("duplicate_of_source_id")
        else None,
        flags=flags,
    )


def with_memory_lifecycle_metadata(
    metadata: dict[str, object],
    lifecycle: MemoryLifecycle,
) -> dict[str, object]:
    next_metadata = dict(metadata)
    snapshot = lifecycle.to_dict()
    next_metadata["memory_lifecycle"] = snapshot
    next_metadata["lifecycle_state"] = snapshot["state"]
    next_metadata["lifecycle_flags"] = snapshot["flags"]
    next_metadata["lifecycle_action"] = snapshot["action"]
    next_metadata["lifecycle_reason"] = snapshot["reason"]
    if snapshot.get("replacement_source_id"):
        next_metadata["superseded_by_source_id"] = snapshot["replacement_source_id"]
    if snapshot.get("duplicate_of_source_id"):
        next_metadata["duplicate_of_source_id"] = snapshot["duplicate_of_source_id"]
    return next_metadata


def with_reflection_finding_metadata(
    metadata: dict[str, object],
    finding: ReflectionFinding,
) -> dict[str, object]:
    next_metadata = dict(metadata)
    findings = [item.to_dict() for item in reflection_findings_from_metadata(next_metadata)]
    findings.append(finding.to_dict())
    next_metadata["reflection_findings"] = findings
    return next_metadata


def with_claim_record_metadata(
    metadata: dict[str, object],
    claim: ClaimRecord,
) -> dict[str, object]:
    next_metadata = dict(metadata)
    claims = [item.to_dict() for item in claim_records_from_metadata(next_metadata)]
    claims.append(claim.to_dict())
    next_metadata["claim_records"] = claims
    return next_metadata


def correction_finding_kind(action: str) -> ReflectionFindingKind:
    return _CORRECTION_FINDING_KIND.get(action, ReflectionFindingKind.CORRECTION)
