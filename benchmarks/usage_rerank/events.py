"""Row model for memory_usage_events.

Mirrors the schema defined in
packages/python/sibyl-core/src/sibyl_core/backends/surreal/content_schema.py:408-433
and written by sibyl_core.services.usage.record_memory_usage.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

EXPOSURE = "exposure"
CITATION = "citation"
MISLED = "misled"

FEEDBACK_SIGNALS = (CITATION, MISLED)

GRAPH_ENTITY = "graph_entity"
RAW_CAPTURE = "raw_capture"

# Metadata keys that would carry the retrieval query if it were persisted.
# Checked rather than assumed: the emitters pass a `query`/`goal` into
# request_metadata but only fold it into a digest, so these keys are expected to
# be absent. The harness measures that instead of asserting it.
QUERY_METADATA_KEYS = ("query", "goal", "request_query", "search_query", "prompt")

# Metadata keys that would carry the served rank if it were persisted.
RANK_METADATA_KEYS = ("rank", "position", "result_rank", "ordinal")

# Metadata keys that would carry the fused retrieval score if it were persisted.
SCORE_METADATA_KEYS = ("score", "fused_score", "relevance_score", "rrf_score")

_FRACTIONAL_SECONDS = re.compile(r"\.(\d+)")


@dataclass(frozen=True, slots=True)
class UsageEventRow:
    """One row of memory_usage_events, normalized."""

    organization_id: str
    session_key: str
    message_key: str
    source_surface: str
    item_kind: str
    item_id: str
    signal_type: str
    event_at: datetime
    principal_id: str | None = None
    project_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_exposure(self) -> bool:
        return self.signal_type == EXPOSURE

    @property
    def is_feedback(self) -> bool:
        return self.signal_type in FEEDBACK_SIGNALS

    def metadata_text(self, keys: tuple[str, ...]) -> str | None:
        """Return the first non-empty metadata value among `keys`."""
        for key in keys:
            value = self.metadata.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @property
    def recoverable_query(self) -> str | None:
        return self.metadata_text(QUERY_METADATA_KEYS)

    @property
    def recorded_rank(self) -> str | None:
        return self.metadata_text(RANK_METADATA_KEYS)

    @property
    def recorded_score(self) -> str | None:
        return self.metadata_text(SCORE_METADATA_KEYS)

    def to_json(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "session_key": self.session_key,
            "message_key": self.message_key,
            "source_surface": self.source_surface,
            "item_kind": self.item_kind,
            "item_id": self.item_id,
            "signal_type": self.signal_type,
            "event_at": self.event_at.isoformat(),
            "principal_id": self.principal_id,
            "project_id": self.project_id,
            "metadata": dict(self.metadata),
        }


def parse_event_datetime(value: Any) -> datetime:
    """Parse a SurrealDB datetime into an aware UTC datetime.

    Sub-microsecond precision is truncated rather than rejected: the store can
    answer with more fractional digits than datetime.fromisoformat accepts, and
    the harness only needs microsecond ordering.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        raise ValueError("event_at is required")
    normalized = text.replace("Z", "+00:00")

    def _truncate(match: re.Match[str]) -> str:
        return "." + match.group(1)[:6]

    normalized = _FRACTIONAL_SECONDS.sub(_truncate, normalized)
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_event_row(raw: Mapping[str, Any]) -> UsageEventRow:
    """Build a UsageEventRow from a store row or a JSONL line."""
    metadata = raw.get("metadata")
    return UsageEventRow(
        organization_id=_text(raw.get("organization_id"), "organization_id"),
        session_key=_text(raw.get("session_key"), "session_key"),
        message_key=_text(raw.get("message_key"), "message_key"),
        source_surface=_text(raw.get("source_surface"), "source_surface"),
        item_kind=_text(raw.get("item_kind"), "item_kind"),
        item_id=_text(raw.get("item_id"), "item_id"),
        signal_type=_text(raw.get("signal_type"), "signal_type"),
        event_at=parse_event_datetime(raw.get("event_at")),
        principal_id=_optional_text(raw.get("principal_id")),
        project_id=_optional_text(raw.get("project_id")),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def normalize_event_rows(raws: Any) -> tuple[UsageEventRow, ...]:
    if not isinstance(raws, list):
        return ()
    return tuple(normalize_event_row(raw) for raw in raws if isinstance(raw, Mapping))


def _text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "CITATION",
    "EXPOSURE",
    "FEEDBACK_SIGNALS",
    "GRAPH_ENTITY",
    "MISLED",
    "QUERY_METADATA_KEYS",
    "RANK_METADATA_KEYS",
    "RAW_CAPTURE",
    "SCORE_METADATA_KEYS",
    "UsageEventRow",
    "normalize_event_row",
    "normalize_event_rows",
    "parse_event_datetime",
]
