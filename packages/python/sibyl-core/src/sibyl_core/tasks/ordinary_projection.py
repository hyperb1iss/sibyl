"""Complete ordinary controller evidence shared by proposal and validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.episode_evidence import (
    EvidenceCitation,
    encode_episode_views,
    episode_projection_receipt,
    project_episode,
)
from sibyl_core.tasks.ordinary_evidence import OrdinarySource

VERSION = "ordinary_complete_controller_projection_v1"
INSTRUCTIONS = (
    "These are complete semantic views of the listed controller observations. "
    "Resolve $ref through values and $literal as literal object entries. Evidence IDs "
    "identify exact original UTF-8 byte ranges, not offsets into this view. Transport "
    "and audit fields remain bound by the projection receipt but are not assertion "
    "evidence. Source reports do not establish verified effectiveness, causality or "
    "environment compatibility. Preserve uncertainty and contradictory observations."
)


@dataclass(frozen=True)
class OrdinaryEvidenceProjection:
    binding_json: str
    payload_json: str
    citations: dict[str, EvidenceCitation]

    @property
    def binding(self) -> dict:
        return json.loads(self.binding_json)

    def permits(self, source_id: str, start: int, end: int) -> bool:
        return any(
            citation.episode_id == source_id and left <= start < end <= right
            for citation in self.citations.values()
            for left, right in citation.ranges
        )


class ProjectionReuse:
    """Retain one validated projection within a synchronous preparation lifetime."""

    def __init__(self) -> None:
        self._key: str | None = None
        self._snapshot: tuple[str, str, tuple[tuple[str, EvidenceCitation], ...]] | None = None

    def _read(self, key: str) -> OrdinaryEvidenceProjection | None:
        if self._key != key or self._snapshot is None:
            return None
        binding, payload, citations = self._snapshot
        return OrdinaryEvidenceProjection(binding, payload, dict(citations))

    def _retain(self, key: str, projection: OrdinaryEvidenceProjection) -> None:
        self._key = key
        self._snapshot = (
            projection.binding_json,
            projection.payload_json,
            tuple(projection.citations.items()),
        )


def prepare_ordinary_projection(
    episodes: Sequence[tuple[str, bytes]],
    observations: Sequence[OrdinarySource],
    *,
    reuse: ProjectionReuse | None = None,
) -> OrdinaryEvidenceProjection:
    """Bind complete views and citation ranges to distinct original observations."""
    ordered = sorted(episodes)
    sources = {source.source_id: source for source in observations}
    if (
        not ordered
        or len(sources) != len(observations)
        or len({identifier for identifier, _ in ordered}) != len(ordered)
        or set(sources) != {identifier for identifier, _ in ordered}
    ):
        raise ValueError("ordinary projection source identities differ")
    for identifier, artifact in ordered:
        if hashlib.sha256(artifact).hexdigest() != sources[identifier].content_sha256:
            raise ValueError("ordinary projection source bytes differ")
    source_observations = [sources[identifier].model_dump(mode="json") for identifier, _ in ordered]
    key = canonical(source_observations)
    if reuse is not None and (retained := reuse._read(key)) is not None:
        return retained
    projections = [
        project_episode(identifier, artifact, prefix=f"s{index}")
        for index, (identifier, artifact) in enumerate(ordered)
    ]
    view = encode_episode_views(projections)
    citations = {
        key: value for projection in projections for key, value in projection.citations.items()
    }
    binding = {
        "version": VERSION,
        "source_observations": source_observations,
        "projection": episode_projection_receipt(ordered, projections, view),
    }
    payload = {
        "version": VERSION,
        "evidence_view": view,
        "binding": binding,
        "citations": {
            key: {"source_id": citation.episode_id, "ranges": citation.ranges}
            for key, citation in citations.items()
        },
    }
    prepared = OrdinaryEvidenceProjection(canonical(binding), canonical(payload), citations)
    if reuse is not None:
        reuse._retain(key, prepared)
    return prepared


def reconstruct_ordinary_projection(
    episodes: Sequence[tuple[str, bytes]], binding: dict, *, reuse: ProjectionReuse | None = None
) -> OrdinaryEvidenceProjection:
    """Reject changed modes, bytes, coverage and original observation bindings."""
    if binding.get("version") != VERSION:
        raise ValueError("unsupported ordinary projection version")
    observations = [
        OrdinarySource.model_validate(value) for value in binding["source_observations"]
    ]
    prepared = prepare_ordinary_projection(episodes, observations, reuse=reuse)
    if prepared.binding_json != canonical(binding):
        raise ValueError("ordinary projection differs from protected binding")
    return prepared
