"""P5 phase 1 - extract usage signal from a live content store.

Pulls memory_usage_events read-only, groups exposures into per-request sessions,
attributes citation and misled feedback back to the exposure that served the
item, and writes a tidy JSONL plus a summary receipt. The summary is the point:
it answers how much labeled ranking signal actually exists, whether the query
that produced an exposure is recoverable, and how much of the signal a benchmark
run could have manufactured.

Usage:
    uv run python benchmarks/usage_rerank/extract.py
    uv run python benchmarks/usage_rerank/extract.py --limit 2000 --out /tmp/p5
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths
from events import (
    CITATION,
    EXPOSURE,
    MISLED,
    QUERY_METADATA_KEYS,
    RANK_METADATA_KEYS,
    SCORE_METADATA_KEYS,
    UsageEventRow,
    normalize_event_rows,
)
from join import (
    DEFAULT_ATTRIBUTION_WINDOW_SECONDS,
    DEFAULT_BURST_THRESHOLD,
    DEFAULT_BURST_WINDOW_SECONDS,
    ORIGIN_BURST_SUSPECT,
    ORIGIN_EVAL_SURFACE,
    ORIGIN_INTERACTIVE,
    ExposureSession,
    FeedbackAttribution,
    LabeledSession,
    attribute_feedback,
    attribution_window_sweep,
    build_labeled_sessions,
    flag_eval_suspect_sessions,
    gap_summary,
    group_exposure_sessions,
    measure_session_key_overlap,
    rank_recovery_audit,
)
from store import ReadOnlySurrealStore

PAGE_SIZE = 5000

WINDOW_SWEEP_SECONDS = (300.0, 3600.0, 21_600.0, 86_400.0, 604_800.0)

_EVENT_FIELDS = (
    "uuid",
    "organization_id",
    "session_key",
    "message_key",
    "source_surface",
    "item_kind",
    "item_id",
    "signal_type",
    "principal_id",
    "project_id",
    "metadata",
    "event_at",
)


def fetch_events(
    store: ReadOnlySurrealStore,
    *,
    limit: int | None = None,
    page_size: int = PAGE_SIZE,
) -> tuple[UsageEventRow, ...]:
    """Page through memory_usage_events in a stable order.

    Both ordering fields stay in the projection because SurrealDB 3.x rejects an
    ORDER BY on a field the non-star SELECT does not return.
    """
    projection = ", ".join(_EVENT_FIELDS)
    rows: list[UsageEventRow] = []
    start = 0
    while True:
        remaining = None if limit is None else limit - len(rows)
        if remaining is not None and remaining <= 0:
            break
        size = page_size if remaining is None else min(page_size, remaining)
        # Interpolation is safe here and unavoidable: SurrealDB does not bind
        # LIMIT/START, projection is a module constant, and both bounds are ints.
        statement = (
            f"SELECT {projection} FROM memory_usage_events "  # noqa: S608
            f"ORDER BY event_at ASC, uuid ASC LIMIT {int(size)} START {int(start)};"
        )
        page = normalize_event_rows(store.query(statement))
        rows.extend(page)
        if len(page) < size:
            break
        start += size
    return tuple(rows)


def load_events(path: Path) -> tuple[UsageEventRow, ...]:
    """Read a previously extracted JSONL back into rows."""
    raws = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return normalize_event_rows(raws)


def metadata_key_coverage(rows: Sequence[UsageEventRow], keys: tuple[str, ...]) -> dict[str, Any]:
    """Measure how many events carry any of `keys` in their metadata."""
    present = sum(1 for row in rows if row.metadata_text(keys) is not None)
    total = len(rows)
    return {
        "events": total,
        "with_value": present,
        "share": round(present / total, 6) if total else None,
    }


def observed_metadata_keys(rows: Sequence[UsageEventRow]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(str(key) for key in row.metadata)
    return dict(sorted(counter.items(), key=lambda entry: (-entry[1], entry[0])))


def build_summary(
    rows: Sequence[UsageEventRow],
    sessions: Sequence[ExposureSession],
    attributions: Sequence[FeedbackAttribution],
    labeled_sessions: Sequence[LabeledSession],
    *,
    target: str,
    window_seconds: float,
    burst_threshold: int,
    burst_window_seconds: float,
) -> dict[str, Any]:
    """Assemble the extraction receipt."""
    exposures = [row for row in rows if row.signal_type == EXPOSURE]
    citations = [row for row in rows if row.signal_type == CITATION]
    misleds = [row for row in rows if row.signal_type == MISLED]

    by_signal = Counter(row.signal_type for row in rows)
    by_surface = Counter(f"{row.signal_type}/{row.source_surface}" for row in rows)
    by_kind = Counter(f"{row.item_kind}/{row.signal_type}" for row in rows)

    exposed_items = {(row.item_kind, row.item_id) for row in exposures}
    cited_items = {(row.item_kind, row.item_id) for row in citations}
    misled_items = {(row.item_kind, row.item_id) for row in misleds}

    origins = Counter(labeled.origin for labeled in labeled_sessions)
    interactive = [labeled for labeled in labeled_sessions if labeled.origin == ORIGIN_INTERACTIVE]
    contrastive = [labeled for labeled in labeled_sessions if labeled.is_contrastive()]
    contrastive_clean = [labeled for labeled in interactive if labeled.is_contrastive()]

    timestamps = [row.event_at for row in rows]
    span_days = None
    if timestamps:
        span_days = round((max(timestamps) - min(timestamps)).total_seconds() / 86_400.0, 3)

    outcome_counts = Counter(attribution.outcome for attribution in attributions)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "store_target": target,
        "attribution_window_seconds": window_seconds,
        "burst_threshold": burst_threshold,
        "burst_window_seconds": burst_window_seconds,
        "events": {
            "total": len(rows),
            "by_signal": dict(sorted(by_signal.items())),
            "by_signal_and_surface": dict(sorted(by_surface.items())),
            "by_item_kind_and_signal": dict(sorted(by_kind.items())),
            "organizations": len({row.organization_id for row in rows}),
            "principals": len({row.principal_id for row in rows if row.principal_id}),
            "projects": len({row.project_id for row in rows if row.project_id}),
        },
        "time_span": {
            "first_event_at": min(timestamps).isoformat() if timestamps else None,
            "last_event_at": max(timestamps).isoformat() if timestamps else None,
            "span_days": span_days,
        },
        "items": {
            "distinct_exposed": len(exposed_items),
            "distinct_cited": len(cited_items),
            "distinct_misled": len(misled_items),
            "cited_and_exposed": len(cited_items & exposed_items),
            "cited_never_exposed": len(cited_items - exposed_items),
            "positive_item_share_of_exposed": (
                round(len(cited_items & exposed_items) / len(exposed_items), 6)
                if exposed_items
                else None
            ),
        },
        "query_recoverability": {
            "checked_metadata_keys": list(QUERY_METADATA_KEYS),
            "exposure": metadata_key_coverage(exposures, QUERY_METADATA_KEYS),
            "all_events": metadata_key_coverage(rows, QUERY_METADATA_KEYS),
            "observed_exposure_metadata_keys": observed_metadata_keys(exposures),
            "observed_feedback_metadata_keys": observed_metadata_keys(citations + misleds),
        },
        "rank_recoverability": {
            "checked_metadata_keys": list(RANK_METADATA_KEYS),
            "recorded_rank": metadata_key_coverage(exposures, RANK_METADATA_KEYS),
            "recorded_score": metadata_key_coverage(exposures, SCORE_METADATA_KEYS),
            **rank_recovery_audit(exposures, sessions),
        },
        "session_key_join": measure_session_key_overlap(rows).to_json(),
        "sessions": {
            "exposure_sessions": len(sessions),
            "items_per_session_mean": (
                round(sum(session.item_count for session in sessions) / len(sessions), 3)
                if sessions
                else None
            ),
            "items_per_session_max": (
                max(session.item_count for session in sessions) if sessions else None
            ),
            "mixed_kind_sessions": sum(1 for session in sessions if session.is_mixed_kind),
            "by_origin": dict(sorted(origins.items())),
            "contrastive_sessions": len(contrastive),
            "contrastive_sessions_interactive_only": len(contrastive_clean),
        },
        "attribution": {
            "feedback_events": len(citations) + len(misleds),
            "outcomes": dict(sorted(outcome_counts.items())),
            "gap_seconds": gap_summary(attributions),
            "window_sweep_attributed": attribution_window_sweep(
                sessions, rows, WINDOW_SWEEP_SECONDS
            ),
        },
        "labels": {
            "sessions_with_positive": sum(
                1 for labeled in labeled_sessions if labeled.positive_count
            ),
            "positive_labels": sum(labeled.positive_count for labeled in labeled_sessions),
            "misled_labels": sum(len(labeled.misled_keys) for labeled in labeled_sessions),
            "negative_labels_in_contrastive_sessions": sum(
                labeled.negative_count for labeled in contrastive
            ),
        },
        "contamination": {
            "note": (
                "No column marks an event as benchmark-origin, so eval and interactive "
                "rows are not exactly separable. burst_suspect is a generous upper "
                "bound on contamination, not an estimate."
            ),
            "clean_lower_bound_sessions": len(interactive),
            "eval_surface_sessions": origins.get(ORIGIN_EVAL_SURFACE, 0),
            "burst_suspect_sessions": origins.get(ORIGIN_BURST_SUSPECT, 0),
        },
    }


def write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def session_records(labeled_sessions: Sequence[LabeledSession]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for labeled in labeled_sessions:
        session = labeled.session
        records.append(
            {
                "session_key": session.session_key,
                "organization_id": session.organization_id,
                "source_surface": session.source_surface,
                "started_at": session.started_at.isoformat(),
                "ended_at": session.ended_at.isoformat(),
                "origin": labeled.origin,
                "item_count": session.item_count,
                "item_kinds": list(session.item_kinds),
                "contrastive": labeled.is_contrastive(),
                "items": [
                    {
                        "item_kind": item.item_kind,
                        "item_id": item.item_id,
                        "rank_within_kind": item.rank_within_kind,
                        "project_id": item.project_id,
                        "label": (
                            "cited"
                            if item.key in labeled.cited_keys
                            else "misled"
                            if item.key in labeled.misled_keys
                            else "exposed"
                        ),
                    }
                    for item in session.items
                ],
            }
        )
    return records


def print_report(summary: dict[str, Any]) -> None:
    events = summary["events"]
    items = summary["items"]
    print("\n=== P5 usage-signal extraction ===")
    print(f"store              {summary['store_target']}")
    print(f"events             {events['total']}")
    for key, value in events["by_signal"].items():
        print(f"  {key:<16} {value}")
    print(f"span               {summary['time_span']['span_days']} days")
    print(f"distinct exposed   {items['distinct_exposed']}")
    print(
        f"distinct cited     {items['distinct_cited']} (also exposed: {items['cited_and_exposed']})"
    )
    print(f"distinct misled    {items['distinct_misled']}")

    join = summary["session_key_join"]
    print("\n-- session_key join --")
    print(f"exposure keys      {join['exposure_session_keys']}")
    print(f"feedback keys      {join['feedback_session_keys']}")
    print(
        f"overlap            {join['overlapping_session_keys']} (viable: {join['session_key_join_viable']})"
    )

    query = summary["query_recoverability"]["exposure"]
    print("\n-- query recoverability --")
    print(
        f"exposure events with query text  {query['with_value']}/{query['events']} ({query['share']})"
    )

    rank = summary["rank_recoverability"]
    print("\n-- rank recoverability --")
    print(
        f"recorded rank column             {rank['recorded_rank']['with_value']}/{rank['recorded_rank']['events']}"
    )
    print(
        f"sessions with strict order       {rank['sessions_strictly_ordered']}/{rank['sessions_audited']}"
    )
    print(
        f"mixed-kind sessions              {rank['mixed_kind_sessions']} (global rank unrecoverable)"
    )
    print(
        f"  of those, kinds interleave in  {rank['mixed_kind_sessions_with_interleaved_kinds']}"
        " (recovered rank untrustworthy, dropped from the what-if)"
    )

    sessions = summary["sessions"]
    attribution = summary["attribution"]
    labels = summary["labels"]
    print("\n-- sessions and labels --")
    print(f"exposure sessions                {sessions['exposure_sessions']}")
    print(f"contrastive sessions             {sessions['contrastive_sessions']}")
    print(f"  interactive-only               {sessions['contrastive_sessions_interactive_only']}")
    print(f"feedback events                  {attribution['feedback_events']}")
    for key, value in attribution["outcomes"].items():
        print(f"  {key:<30} {value}")
    print(f"positive labels                  {labels['positive_labels']}")
    print(f"misled labels                    {labels['misled_labels']}")
    print(f"attribution gap median (s)       {attribution['gap_seconds']['median']}")
    print(f"window sweep                     {attribution['window_sweep_attributed']}")

    contamination = summary["contamination"]
    print("\n-- contamination bound --")
    print(f"clean lower bound sessions       {contamination['clean_lower_bound_sessions']}")
    print(f"burst suspect sessions           {contamination['burst_suspect_sessions']}")
    print()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=paths.OUT,
        help="Directory for the JSONL and summary receipts.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many events (debugging aid).",
    )
    parser.add_argument(
        "--from-jsonl",
        type=Path,
        default=None,
        help="Re-summarize a previous extraction instead of touching the store.",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=DEFAULT_ATTRIBUTION_WINDOW_SECONDS,
        help="Maximum exposure-to-feedback delay treated as attributable.",
    )
    parser.add_argument(
        "--burst-threshold",
        type=int,
        default=DEFAULT_BURST_THRESHOLD,
        help="Same-surface same-size sessions in one bucket that look programmatic.",
    )
    parser.add_argument(
        "--burst-window-seconds",
        type=float,
        default=DEFAULT_BURST_WINDOW_SECONDS,
        help="Bucket width for burst detection.",
    )
    parser.add_argument(
        "--eval-surface",
        action="append",
        default=[],
        help="Treat this source_surface as benchmark-origin (repeatable).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out)

    if args.from_jsonl is not None:
        rows = load_events(Path(args.from_jsonl))
        target = f"jsonl:{args.from_jsonl}"
    else:
        store = ReadOnlySurrealStore()
        target = store.target
        rows = fetch_events(store, limit=args.limit)

    if not rows:
        print(f"no memory_usage_events found at {target}")
        return 1

    sessions = group_exposure_sessions(rows)
    attributions = attribute_feedback(sessions, rows, window_seconds=args.window_seconds)
    origins = flag_eval_suspect_sessions(
        sessions,
        eval_surfaces=frozenset(args.eval_surface),
        burst_threshold=args.burst_threshold,
        burst_window_seconds=args.burst_window_seconds,
    )
    labeled_sessions = build_labeled_sessions(sessions, attributions, origins)

    summary = build_summary(
        rows,
        sessions,
        attributions,
        labeled_sessions,
        target=target,
        window_seconds=args.window_seconds,
        burst_threshold=args.burst_threshold,
        burst_window_seconds=args.burst_window_seconds,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / paths.EVENTS_JSONL.name
    sessions_path = out_dir / paths.SESSIONS_JSONL.name
    summary_path = out_dir / paths.EXTRACT_SUMMARY_JSON.name

    write_jsonl(events_path, [row.to_json() for row in rows])
    write_jsonl(sessions_path, session_records(labeled_sessions))
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print_report(summary)
    print(f"events   -> {events_path}")
    print(f"sessions -> {sessions_path}")
    print(f"summary  -> {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
