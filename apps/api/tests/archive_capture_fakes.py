"""Captures that write a fixed payload, for tests of the backup job's plumbing."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive_lineage import LineageSection

if TYPE_CHECKING:
    from pathlib import Path


class PayloadCapture:
    """Writes ``payload`` as the job writes a real capture, with nothing to seal."""

    def __init__(
        self,
        payload: dict[str, Any],
        *,
        kind: SourceKind = SourceKind.GRAPH_ENTITY,
        entity_count: int = 0,
        relationship_count: int = 0,
    ) -> None:
        self.payload = payload
        self.lineage = LineageSection(kind=kind, associations=[])
        self.quarantined: list[dict[str, str]] = []
        self.unpaged_scheme: str | None = None
        self.entity_count = entity_count
        self.relationship_count = relationship_count
        self.closed = False

    def write(self, path: Path, report: list[dict[str, str]]) -> int:
        del report
        path.write_text(json.dumps(self.payload, indent=2, default=str), encoding="utf-8")
        return path.stat().st_size

    def close(self) -> None:
        self.closed = True


def content_capture(payload: dict[str, Any]) -> PayloadCapture:
    return PayloadCapture(payload, kind=SourceKind.RAW_CAPTURE)
