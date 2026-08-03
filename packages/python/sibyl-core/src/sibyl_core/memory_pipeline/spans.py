"""The cut plan an agent may supply for its own memory.

The mechanical cutter in :mod:`sibyl_core.projection.slicing` reads a body it
did not write and infers boundaries from Markdown structure. The agent that
authored the memory already knows where the seams are, and knows when there are
none. This module is the contract that lets it say so without the server ever
inventing anything: the agent hands over offsets, the server validates them and
either honors them exactly or refuses the write.

Offsets address the stored body, which is ``content.strip()`` -- the same string
the entity row holds and the same string the cutter would have sliced. A span is
a half-open ``[start, end)`` range into it.

Two properties are load-bearing and neither is negotiable.

Verbatim: a passage's text is the parent's bytes from ``start`` to ``end``, never
rewritten, reordered, or summarized. Structure is an index into the memory, not
a replacement for it.

Complete: the spans tile the whole body with no gap and no overlap. Retrieval
lets spans stand in for their parent only when the pack holds indices exactly
``range(total)`` (see ``_suppress_parents_of_passages``), so a plan that leaves
text in neither a span nor a served parent makes that text unreachable. Half-open
pairs can express a gap and an overlap, unlike a bare list of cut points, which
is why the form carries its own validation burden here rather than pushing the
error into retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# A leaf module on purpose. The projection reads this contract and the capture
# pipeline writes it, and both of those packages sit above heavyweight
# ``__init__`` modules that reach into the graph runtime. Importing any of them
# from here closes an import cycle, so the caps below restate their sources as
# literals and ``test_memory_spans.py`` pins each one to the constant it mirrors.

# The largest body one passage row may store, and the most rows one memory may
# be cut into. Both are the substrate's shape rather than the cutter's, so they
# live here where the agent contract and the mechanical cutter can share them.
MAX_PASSAGE_CONTENT_CHARS = 18_000
MAX_PASSAGES_PER_SOURCE = 64

MAX_AGENT_SPANS = MAX_PASSAGES_PER_SOURCE

# A label renders in the slot the mechanical cutter fills with a breadcrumb, so
# it inherits that slot's ceiling: ``slicing.HEADER_MAX_CHARS``.
MAX_SPAN_LABEL_CHARS = 120

# ``tools.helpers.MAX_TITLE_LENGTH``, the cap on the name a passage header
# restates.
MAX_PASSAGE_TITLE_CHARS = 200

# Every passage is rendered as header, then label, then span text. The header is
# the parent's name plus a position counter, so the framing a valid span must
# leave room for is bounded by the title cap plus the label cap plus the
# counter and its newlines. Validating the span against the remaining budget is
# what makes an accepted plan one the projection can store whole: a span that
# only overflows once framed would be dropped at projection time, and a dropped
# span is a silent hole in a tiling the agent was told was accepted.
MAX_SPAN_FRAMING_CHARS = MAX_PASSAGE_TITLE_CHARS + MAX_SPAN_LABEL_CHARS + 32
MAX_AGENT_SPAN_CHARS = MAX_PASSAGE_CONTENT_CHARS - MAX_SPAN_FRAMING_CHARS

# An atomic memory is a claim that the body is one retrievable unit. Past the
# ceiling one row can hold, no passage could carry it and the only copy is the
# fat parent, which is the outcome the slice substrate exists to prevent. The
# write is refused there rather than cut anyway, because cutting anyway would
# override an explicit instruction silently.
MAX_ATOMIC_CONTENT_CHARS = MAX_PASSAGE_CONTENT_CHARS

AGENT_SPANS_METADATA_KEY = "agent_spans"
AGENT_ATOMIC_METADATA_KEY = "agent_atomic"


class MemoryStructureError(ValueError):
    """An agent-supplied cut plan the server will not accept.

    Carries the field it faults so a surface can render a 422 that names what to
    fix, instead of a generic rejection the caller has to guess at.
    """

    def __init__(self, message: str, *, field: str = "spans") -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class AgentSpan:
    """One half-open ``[start, end)`` cut into a memory body."""

    start: int
    end: int
    label: str | None = None

    def slice_of(self, content: str) -> str:
        return content[self.start : self.end]

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"start": self.start, "end": self.end}
        if self.label:
            payload["label"] = self.label
        return payload


def coerce_agent_spans(value: object) -> tuple[AgentSpan, ...]:
    """Read a wire payload into spans, faulting anything that is not one.

    Surfaces hand over decoded JSON, so the shape is checked here rather than
    assumed. A malformed entry is a rejection and never a silent skip: dropping
    one span from a tiling turns a validated plan into a plan with a hole.
    """
    if value is None:
        return ()
    if isinstance(value, AgentSpan):
        return (value,)
    if isinstance(value, str) or not isinstance(value, list | tuple):
        msg = "spans must be a list of {start, end, label?} objects"
        raise MemoryStructureError(msg)
    spans: list[AgentSpan] = []
    for position, entry in enumerate(value):
        if isinstance(entry, AgentSpan):
            spans.append(entry)
            continue
        if not isinstance(entry, dict):
            msg = f"spans[{position}] must be an object with start and end"
            raise MemoryStructureError(msg)
        mapping = cast("Mapping[str, Any]", entry)
        for key in ("start", "end"):
            if key not in mapping:
                msg = f"spans[{position}] is missing {key!r}"
                raise MemoryStructureError(msg)
        start = mapping["start"]
        end = mapping["end"]
        # bool is an int subclass, and True as an offset is a payload bug rather
        # than a position, so it is refused before the range checks run.
        if isinstance(start, bool) or isinstance(end, bool):
            msg = f"spans[{position}] start and end must be integers"
            raise MemoryStructureError(msg)
        if not isinstance(start, int) or not isinstance(end, int):
            msg = f"spans[{position}] start and end must be integers"
            raise MemoryStructureError(msg)
        label = mapping.get("label")
        if label is not None and not isinstance(label, str):
            msg = f"spans[{position}] label must be a string"
            raise MemoryStructureError(msg)
        normalized_label = " ".join(label.split()) if label else None
        spans.append(AgentSpan(start=start, end=end, label=normalized_label or None))
    return tuple(spans)


def validate_agent_spans(content: str, spans: Sequence[AgentSpan]) -> tuple[AgentSpan, ...]:
    """Accept a span plan only if it tiles ``content`` exactly.

    Every rejection names the offending index and the numbers that made it
    invalid. The plan is never repaired and never partially honored, because
    both are ways of accepting a write while serving something the agent did
    not author.
    """
    if not spans:
        msg = "spans must contain at least one span"
        raise MemoryStructureError(msg)
    if len(spans) > MAX_AGENT_SPANS:
        msg = f"spans must contain at most {MAX_AGENT_SPANS} spans, got {len(spans)}"
        raise MemoryStructureError(msg)
    if len(spans) < 2:
        # One span is the parent again, so the projection would pay an embedding
        # for a duplicate row and retrieval would gain nothing.
        msg = "spans must contain at least 2 spans to be worth cutting"
        raise MemoryStructureError(msg)

    length = len(content)
    cursor = 0
    for index, span in enumerate(spans):
        if span.start < 0 or span.end > length:
            msg = (
                f"spans[{index}] [{span.start}, {span.end}) is out of bounds for "
                f"content of {length} characters"
            )
            raise MemoryStructureError(msg)
        if span.end <= span.start:
            msg = f"spans[{index}] must be non-empty, got [{span.start}, {span.end})"
            raise MemoryStructureError(msg)
        if span.start < cursor:
            msg = (
                f"spans[{index}] starts at {span.start} but the previous span ends "
                f"at {cursor}: spans must not overlap"
            )
            raise MemoryStructureError(msg)
        if span.start > cursor:
            msg = (
                f"spans[{index}] starts at {span.start} but the previous span ends "
                f"at {cursor}: spans must leave no gap"
            )
            raise MemoryStructureError(msg)
        span_chars = span.end - span.start
        if span_chars > MAX_AGENT_SPAN_CHARS:
            msg = (
                f"spans[{index}] is {span_chars} characters, over the "
                f"{MAX_AGENT_SPAN_CHARS} character limit for one passage"
            )
            raise MemoryStructureError(msg)
        if span.label is not None and len(span.label) > MAX_SPAN_LABEL_CHARS:
            msg = (
                f"spans[{index}] label is {len(span.label)} characters, over the "
                f"{MAX_SPAN_LABEL_CHARS} character limit"
            )
            raise MemoryStructureError(msg)
        cursor = span.end

    if cursor != length:
        msg = (
            f"spans must tile all {length} characters of the stored content, "
            f"but the last span ends at {cursor}"
        )
        raise MemoryStructureError(msg)
    return tuple(spans)


def validate_atomic_content(content: str) -> None:
    """Refuse an atomic claim the substrate could never serve as one unit."""
    if len(content) > MAX_ATOMIC_CONTENT_CHARS:
        msg = (
            f"atomic memories must be at most {MAX_ATOMIC_CONTENT_CHARS} characters "
            f"so one retrievable unit can hold the body, got {len(content)}; "
            "supply spans instead"
        )
        raise MemoryStructureError(msg, field="atomic")


def agent_spans_metadata(spans: Sequence[AgentSpan]) -> list[dict[str, Any]]:
    return [span.to_metadata() for span in spans]


def agent_spans_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[AgentSpan, ...]:
    """Read a stored plan back, treating anything malformed as absent.

    The read side runs against rows written by older code and by the storage
    round trip, so it degrades to the mechanical cutter rather than failing a
    projection. Only the write path rejects; the write path is what guarantees
    a stored plan is well formed in the first place.
    """
    if not metadata:
        return ()
    try:
        return coerce_agent_spans(metadata.get(AGENT_SPANS_METADATA_KEY))
    except MemoryStructureError:
        return ()


def agent_atomic_from_metadata(metadata: Mapping[str, Any] | None) -> bool:
    if not metadata:
        return False
    return bool(metadata.get(AGENT_ATOMIC_METADATA_KEY))


__all__ = [
    "AGENT_ATOMIC_METADATA_KEY",
    "AGENT_SPANS_METADATA_KEY",
    "MAX_AGENT_SPANS",
    "MAX_AGENT_SPAN_CHARS",
    "MAX_ATOMIC_CONTENT_CHARS",
    "MAX_PASSAGES_PER_SOURCE",
    "MAX_PASSAGE_CONTENT_CHARS",
    "MAX_PASSAGE_TITLE_CHARS",
    "MAX_SPAN_FRAMING_CHARS",
    "MAX_SPAN_LABEL_CHARS",
    "AgentSpan",
    "MemoryStructureError",
    "agent_atomic_from_metadata",
    "agent_spans_from_metadata",
    "agent_spans_metadata",
    "coerce_agent_spans",
    "validate_agent_spans",
    "validate_atomic_content",
]
