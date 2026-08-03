"""Session grouping, rank recovery, and exposure-to-feedback attribution.

Three facts about the v1.1 usage schema shape everything here, and each one is
measured by the extractor rather than assumed:

1. An exposure session_key and a citation session_key can never be equal. Both
   are sha256 digests, but the citation digest folds `cited_ids` into its
   payload (tools/usage_citation.py:311-322) while the exposure digest does not
   (tools/usage_exposure.py:476-486), and the two families use disjoint
   source_surface prefixes. So the natural (session_key, message_key) join is
   empty by construction, and feedback has to be attributed to an exposure by
   item identity plus time.

2. The served rank is not a column, but it is recoverable within one item kind.
   record_memory_usage stamps every event with its own datetime.now(UTC)
   (services/usage.py:245) and the emitter builds the event list in served order
   (tools/usage_exposure.py:366-383), so event_at increases monotonically with
   rank at microsecond granularity.

3. That recovery does not cross item kinds. The emitter records raw_capture
   targets and graph_entity targets in two separate record_memory_usage calls
   (tools/usage_exposure.py:174 and :207), so every raw event in a session
   precedes every graph event regardless of true interleaved rank. Ranks are
   therefore only meaningful within a kind, which is why ExposedItem carries
   rank_within_kind and never a global rank.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from events import CITATION, EXPOSURE, FEEDBACK_SIGNALS, MISLED, UsageEventRow

# Surfaces that only a benchmark run can produce. context_pack_eval is NOT here:
# it posts to /context/pack without record_exposure=false
# (sibyl_core/evals/runtime.py:302), so its rows are indistinguishable from
# interactive context packs by surface alone. Burst detection bounds that
# instead.
DEFAULT_EVAL_SURFACES: frozenset[str] = frozenset()

DEFAULT_ATTRIBUTION_WINDOW_SECONDS = 86_400.0
DEFAULT_BURST_WINDOW_SECONDS = 60.0
DEFAULT_BURST_THRESHOLD = 6

ATTRIBUTED = "attributed"
ITEM_NEVER_EXPOSED = "item_never_exposed"
NO_PRECEDING_EXPOSURE = "no_preceding_exposure"
OUTSIDE_WINDOW = "outside_window"

ORIGIN_INTERACTIVE = "interactive"
ORIGIN_EVAL_SURFACE = "eval_surface"
ORIGIN_BURST_SUSPECT = "burst_suspect"


@dataclass(frozen=True, slots=True)
class ExposedItem:
    """One item served inside one exposure session."""

    item_kind: str
    item_id: str
    rank_within_kind: int
    event_at: datetime
    project_id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.item_kind, self.item_id)


@dataclass(frozen=True, slots=True)
class ExposureSession:
    """All items served by one retrieval request.

    The exposure session_key is a digest over the request plus the returned id
    list, so it is effectively a per-request identifier: one session is one
    served result page.
    """

    organization_id: str
    session_key: str
    source_surface: str
    started_at: datetime
    ended_at: datetime
    items: tuple[ExposedItem, ...]

    @property
    def item_count(self) -> int:
        return len(self.items)

    @property
    def item_kinds(self) -> tuple[str, ...]:
        return tuple(sorted({item.item_kind for item in self.items}))

    @property
    def is_mixed_kind(self) -> bool:
        return len(self.item_kinds) > 1

    def items_of_kind(self, item_kind: str) -> tuple[ExposedItem, ...]:
        return tuple(item for item in self.items if item.item_kind == item_kind)

    @property
    def has_contiguous_kind_blocks(self) -> bool:
        """Whether each item kind occupies one unbroken run of timestamps.

        Rank recovery assumes a session's events were written by one batched call
        per kind. A kind that appears in two separate runs means the writes
        interleaved, so the timestamp order is not the served order and the
        recovered ranks for that session are not trustworthy.
        """
        sequence = [item.item_kind for item in self.items]
        blocks = [
            kind for index, kind in enumerate(sequence) if index == 0 or sequence[index - 1] != kind
        ]
        return len(blocks) == len(set(blocks))


@dataclass(frozen=True, slots=True)
class FeedbackAttribution:
    """The outcome of attributing one feedback event to an exposure session."""

    signal_type: str
    item_kind: str
    item_id: str
    event_at: datetime
    source_surface: str
    outcome: str
    session_key: str | None = None
    gap_seconds: float | None = None

    @property
    def attributed(self) -> bool:
        return self.outcome == ATTRIBUTED


@dataclass(frozen=True, slots=True)
class LabeledSession:
    """An exposure session with its attributed feedback labels."""

    session: ExposureSession
    cited_keys: frozenset[tuple[str, str]]
    misled_keys: frozenset[tuple[str, str]]
    origin: str

    @property
    def positive_count(self) -> int:
        return len(self.cited_keys)

    @property
    def negative_count(self) -> int:
        labeled = self.cited_keys | self.misled_keys
        return sum(1 for item in self.session.items if item.key not in labeled)

    def is_contrastive(self, item_kind: str | None = None) -> bool:
        """True when this session has both a cited item and an uncited one.

        A rerank what-if needs both: something to promote and something to
        promote it past. Restricted to one item kind when asked, because
        recovered ranks are only comparable within a kind.
        """
        items = self.session.items if item_kind is None else self.session.items_of_kind(item_kind)
        if not items:
            return False
        cited = sum(1 for item in items if item.key in self.cited_keys)
        return 0 < cited < len(items)


@dataclass(frozen=True, slots=True)
class SessionKeyOverlap:
    """Receipt for the structural join-key finding."""

    exposure_sessions: int
    feedback_sessions: int
    overlapping_sessions: int
    overlapping_message_keys: int

    def to_json(self) -> dict[str, Any]:
        return {
            "exposure_session_keys": self.exposure_sessions,
            "feedback_session_keys": self.feedback_sessions,
            "overlapping_session_keys": self.overlapping_sessions,
            "overlapping_message_keys": self.overlapping_message_keys,
            "session_key_join_viable": self.overlapping_sessions > 0,
        }


def group_exposure_sessions(rows: Iterable[UsageEventRow]) -> tuple[ExposureSession, ...]:
    """Group exposure events into per-request sessions with within-kind ranks."""
    grouped: dict[tuple[str, str], list[UsageEventRow]] = defaultdict(list)
    for row in rows:
        if row.signal_type != EXPOSURE:
            continue
        grouped[(row.organization_id, row.session_key)].append(row)

    sessions: list[ExposureSession] = []
    for (organization_id, session_key), session_rows in grouped.items():
        ordered = sorted(session_rows, key=lambda row: (row.event_at, row.item_id))
        rank_by_kind: Counter[str] = Counter()
        items: list[ExposedItem] = []
        seen: set[tuple[str, str]] = set()
        for row in ordered:
            key = (row.item_kind, row.item_id)
            if key in seen:
                continue
            seen.add(key)
            rank_by_kind[row.item_kind] += 1
            items.append(
                ExposedItem(
                    item_kind=row.item_kind,
                    item_id=row.item_id,
                    rank_within_kind=rank_by_kind[row.item_kind],
                    event_at=row.event_at,
                    project_id=row.project_id,
                )
            )
        sessions.append(
            ExposureSession(
                organization_id=organization_id,
                session_key=session_key,
                source_surface=ordered[0].source_surface,
                started_at=ordered[0].event_at,
                ended_at=ordered[-1].event_at,
                items=tuple(items),
            )
        )
    return tuple(sorted(sessions, key=lambda session: (session.started_at, session.session_key)))


def measure_session_key_overlap(rows: Iterable[UsageEventRow]) -> SessionKeyOverlap:
    """Measure whether the (session_key, message_key) join can ever match."""
    exposure_sessions: set[str] = set()
    feedback_sessions: set[str] = set()
    exposure_messages: set[str] = set()
    feedback_messages: set[str] = set()
    for row in rows:
        if row.signal_type == EXPOSURE:
            exposure_sessions.add(row.session_key)
            exposure_messages.add(row.message_key)
        elif row.signal_type in FEEDBACK_SIGNALS:
            feedback_sessions.add(row.session_key)
            feedback_messages.add(row.message_key)
    return SessionKeyOverlap(
        exposure_sessions=len(exposure_sessions),
        feedback_sessions=len(feedback_sessions),
        overlapping_sessions=len(exposure_sessions & feedback_sessions),
        overlapping_message_keys=len(exposure_messages & feedback_messages),
    )


def attribute_feedback(
    sessions: Sequence[ExposureSession],
    rows: Iterable[UsageEventRow],
    *,
    window_seconds: float = DEFAULT_ATTRIBUTION_WINDOW_SECONDS,
) -> tuple[FeedbackAttribution, ...]:
    """Attach each feedback event to the last exposure that served the item.

    Attribution walks backwards from the feedback timestamp because a citation
    is feedback on the most recent time the agent saw the item, and takes the
    latest qualifying session so that a repeatedly-exposed item is credited to
    the exposure that actually preceded the citation.
    """
    sessions_by_item: dict[tuple[str, str], list[ExposureSession]] = defaultdict(list)
    for session in sessions:
        for item in session.items:
            sessions_by_item[item.key].append(session)
    for candidates in sessions_by_item.values():
        candidates.sort(key=lambda session: session.started_at)

    window = timedelta(seconds=window_seconds)
    attributions: list[FeedbackAttribution] = []
    for row in sorted(rows, key=lambda row: row.event_at):
        if row.signal_type not in FEEDBACK_SIGNALS:
            continue
        key = (row.item_kind, row.item_id)
        candidates = sessions_by_item.get(key)
        if not candidates:
            attributions.append(_unattributed(row, ITEM_NEVER_EXPOSED))
            continue
        preceding = [session for session in candidates if session.started_at <= row.event_at]
        if not preceding:
            # The item was served, but only after this feedback. Distinct from a
            # gap that is merely too large, and conflating the two hides which
            # kind of attribution failure a store actually has.
            attributions.append(_unattributed(row, NO_PRECEDING_EXPOSURE))
            continue
        chosen = preceding[-1]
        gap = row.event_at - chosen.started_at
        if gap > window:
            attributions.append(_unattributed(row, OUTSIDE_WINDOW, gap_seconds=gap.total_seconds()))
            continue
        attributions.append(
            FeedbackAttribution(
                signal_type=row.signal_type,
                item_kind=row.item_kind,
                item_id=row.item_id,
                event_at=row.event_at,
                source_surface=row.source_surface,
                outcome=ATTRIBUTED,
                session_key=chosen.session_key,
                gap_seconds=gap.total_seconds(),
            )
        )
    return tuple(attributions)


def flag_eval_suspect_sessions(
    sessions: Sequence[ExposureSession],
    *,
    eval_surfaces: frozenset[str] = DEFAULT_EVAL_SURFACES,
    burst_threshold: int = DEFAULT_BURST_THRESHOLD,
    burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
) -> dict[str, str]:
    """Classify each session as interactive or eval-suspect.

    No column marks an event as benchmark-origin, so exact separation is
    impossible and this returns a deliberately generous upper bound: any group
    of `burst_threshold` or more same-surface sessions that returned the same
    number of items inside one `burst_window_seconds` bucket looks like a
    programmatic sweep. Interactive agent work can also burst, so a
    burst_suspect count is a ceiling on contamination, not an estimate of it.
    """
    buckets: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    origins: dict[str, str] = {}
    for session in sessions:
        if session.source_surface in eval_surfaces:
            origins[session.session_key] = ORIGIN_EVAL_SURFACE
            continue
        origins[session.session_key] = ORIGIN_INTERACTIVE
        bucket = int(session.started_at.timestamp() // burst_window_seconds)
        buckets[(session.source_surface, session.item_count, bucket)].append(session.session_key)

    for session_keys in buckets.values():
        if len(session_keys) >= burst_threshold:
            for session_key in session_keys:
                origins[session_key] = ORIGIN_BURST_SUSPECT
    return origins


def build_labeled_sessions(
    sessions: Sequence[ExposureSession],
    attributions: Sequence[FeedbackAttribution],
    origins: Mapping[str, str],
) -> tuple[LabeledSession, ...]:
    """Attach attributed labels and an origin classification to each session."""
    cited: dict[str, set[tuple[str, str]]] = defaultdict(set)
    misled: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for attribution in attributions:
        if not attribution.attributed or attribution.session_key is None:
            continue
        key = (attribution.item_kind, attribution.item_id)
        if attribution.signal_type == CITATION:
            cited[attribution.session_key].add(key)
        elif attribution.signal_type == MISLED:
            misled[attribution.session_key].add(key)

    return tuple(
        LabeledSession(
            session=session,
            cited_keys=frozenset(cited.get(session.session_key, frozenset())),
            misled_keys=frozenset(misled.get(session.session_key, frozenset())),
            origin=origins.get(session.session_key, ORIGIN_INTERACTIVE),
        )
        for session in sessions
    )


def attribution_window_sweep(
    sessions: Sequence[ExposureSession],
    rows: Sequence[UsageEventRow],
    windows_seconds: Sequence[float],
) -> dict[str, int]:
    """Count attributed feedback events at several windows.

    A single window is a free parameter, so the sweep is what makes the chosen
    default auditable: if attribution barely moves between one hour and one
    week, the window is not driving the result.
    """
    sweep: dict[str, int] = {}
    for window in windows_seconds:
        attributions = attribute_feedback(sessions, rows, window_seconds=window)
        sweep[f"{int(window)}s"] = sum(1 for item in attributions if item.attributed)
    return sweep


def gap_summary(attributions: Sequence[FeedbackAttribution]) -> dict[str, float | int | None]:
    """Summarize the exposure-to-feedback delay of attributed events."""
    gaps = [
        attribution.gap_seconds
        for attribution in attributions
        if attribution.attributed and attribution.gap_seconds is not None
    ]
    if not gaps:
        return {"count": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "count": len(gaps),
        "min": round(min(gaps), 3),
        "median": round(statistics.median(gaps), 3),
        "mean": round(statistics.fmean(gaps), 3),
        "max": round(max(gaps), 3),
    }


def rank_recovery_audit(
    rows: Iterable[UsageEventRow],
    sessions: Sequence[ExposureSession],
) -> dict[str, Any]:
    """Check that event_at really does recover the served order.

    Rank recovery rests on an implementation detail rather than a contract, so
    it gets audited instead of trusted. Two things have to hold: timestamps
    within one kind must be strictly increasing (a tie means two items share a
    rank and the order is arbitrary), and in a mixed-kind session each kind must
    occupy one contiguous timestamp block, which is the fingerprint of the
    two-call emitter and the reason a global rank cannot be reconstructed.
    """
    by_session: dict[str, list[UsageEventRow]] = defaultdict(list)
    for row in rows:
        if row.signal_type == EXPOSURE:
            by_session[row.session_key].append(row)

    strictly_ordered = 0
    duplicate_timestamps = 0
    contiguous_kind_blocks = 0
    mixed_kind = 0
    non_contiguous_sessions: list[str] = []
    for session_key, session_rows in by_session.items():
        ordered = sorted(session_rows, key=lambda row: (row.event_at, row.item_id))
        per_kind: dict[str, list[datetime]] = defaultdict(list)
        for row in ordered:
            per_kind[row.item_kind].append(row.event_at)
        if all(
            all(earlier < later for earlier, later in pairwise(stamps))
            for stamps in per_kind.values()
        ):
            strictly_ordered += 1
        else:
            duplicate_timestamps += 1
        if len(per_kind) > 1:
            mixed_kind += 1
            kind_sequence = [row.item_kind for row in ordered]
            blocks = [
                kind
                for index, kind in enumerate(kind_sequence)
                if index == 0 or kind_sequence[index - 1] != kind
            ]
            if len(blocks) == len(set(blocks)):
                contiguous_kind_blocks += 1
            else:
                non_contiguous_sessions.append(session_key)

    return {
        "sessions_audited": len(by_session),
        "sessions_strictly_ordered": strictly_ordered,
        "sessions_with_tied_timestamps": duplicate_timestamps,
        "mixed_kind_sessions": mixed_kind,
        "mixed_kind_sessions_with_contiguous_kind_blocks": contiguous_kind_blocks,
        "mixed_kind_sessions_with_interleaved_kinds": len(non_contiguous_sessions),
        "interleaved_session_keys": sorted(non_contiguous_sessions),
        "global_rank_recoverable": mixed_kind == 0,
        "sessions_reported": len(sessions),
    }


def _unattributed(
    row: UsageEventRow,
    outcome: str,
    *,
    gap_seconds: float | None = None,
) -> FeedbackAttribution:
    return FeedbackAttribution(
        signal_type=row.signal_type,
        item_kind=row.item_kind,
        item_id=row.item_id,
        event_at=row.event_at,
        source_surface=row.source_surface,
        outcome=outcome,
        gap_seconds=gap_seconds,
    )


__all__ = [
    "ATTRIBUTED",
    "DEFAULT_ATTRIBUTION_WINDOW_SECONDS",
    "DEFAULT_BURST_THRESHOLD",
    "DEFAULT_BURST_WINDOW_SECONDS",
    "DEFAULT_EVAL_SURFACES",
    "ITEM_NEVER_EXPOSED",
    "NO_PRECEDING_EXPOSURE",
    "ORIGIN_BURST_SUSPECT",
    "ORIGIN_EVAL_SURFACE",
    "ORIGIN_INTERACTIVE",
    "OUTSIDE_WINDOW",
    "ExposedItem",
    "ExposureSession",
    "FeedbackAttribution",
    "LabeledSession",
    "SessionKeyOverlap",
    "attribute_feedback",
    "attribution_window_sweep",
    "build_labeled_sessions",
    "flag_eval_suspect_sessions",
    "gap_summary",
    "group_exposure_sessions",
    "measure_session_key_overlap",
    "rank_recovery_audit",
]
