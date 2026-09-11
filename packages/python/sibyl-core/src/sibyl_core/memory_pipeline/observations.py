"""Typed source observations, distinct from mutable citation metadata."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum


class SourceKind(StrEnum):
    RAW_CAPTURE = "raw_capture"
    GRAPH_ENTITY = "graph_entity"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    organization_id: str
    kind: SourceKind
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            raise ValueError("source kind must be explicit")
        for value in (self.organization_id, self.id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("source organization and id must be nonempty strings")

    @property
    def key(self) -> str:
        """Encode components without separator collisions or inferred prefixes."""
        return json.dumps([self.organization_id, self.kind.value, self.id], separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SourceObservation:
    source: SourceIdentity
    generation: int
    content_sha256: str
    revision: int
    durable: bool
    incarnation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceIdentity):
            raise ValueError("observation requires a typed source identity")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("source generation must be a nonnegative integer")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("source revision must be a positive integer")
        if not isinstance(self.content_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.content_sha256
        ):
            raise ValueError("source content hash must be a lowercase SHA-256 digest")
        if self.incarnation is not None and (
            not isinstance(self.incarnation, str) or not self.incarnation.strip()
        ):
            raise ValueError("source incarnation must be a nonempty string")
        if type(self.durable) is not bool:
            raise ValueError("observation durability must be explicit")

    @property
    def effective_incarnation(self) -> str:
        """Translate legacy observations without refreshing their evidence."""
        return self.incarnation or legacy_source_incarnation(self.source)

    def same_evidence(self, other: SourceObservation) -> bool:
        """Bookkeeping revisions do not manufacture a new source observation."""
        return (
            self.source == other.source
            and self.effective_incarnation == other.effective_incarnation
            and self.generation == other.generation
            and self.content_sha256 == other.content_sha256
            and self.durable == other.durable
        )


def evidence_hash(value: object) -> str:
    """Hash the versioned materialized representation, not arbitrary metadata."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def legacy_source_incarnation(source: SourceIdentity) -> str:
    """Identity assigned only to a ledger retained through the upgrade."""
    organization = hashlib.sha256(source.organization_id.encode()).hexdigest()
    identity = hashlib.sha256(source.id.encode()).hexdigest()
    return f"legacy-v1:{organization}:{source.kind.value}:{identity}"
