"""P5 phase 1 - offline usage-prior rerank what-if.

Replays every contrastive exposure session (one that served both an eventually
cited item and at least one item nobody cited), reweights the served order by a
point-in-time usage prior, and reports what that reordering would have done to
the cited item's rank. This is a feasibility signal about how much headroom the
usage loop has, not a gate: it reorders a proxy score derived from the served
rank, because the real fused score is never persisted on the event.

Usage:
    uv run python benchmarks/usage_rerank/whatif.py
    uv run python benchmarks/usage_rerank/whatif.py --interactive-only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths
from extract import load_events
from join import (
    DEFAULT_ATTRIBUTION_WINDOW_SECONDS,
    DEFAULT_BURST_THRESHOLD,
    DEFAULT_BURST_WINDOW_SECONDS,
    ORIGIN_INTERACTIVE,
    attribute_feedback,
    build_labeled_sessions,
    flag_eval_suspect_sessions,
    group_exposure_sessions,
)
from prior import (
    CITATION_WEIGHT,
    MISLED_WEIGHT,
    RETRIEVAL_WEIGHT,
    PointInTimeCounts,
    RerankOutcome,
    permutation_null,
    run_whatif,
    summarize_outcomes,
)

# Sensitivity arms. The middle arm is the production retention curve; the others
# bracket it so the verdict is not an artifact of one weight choice.
WEIGHT_ARMS: tuple[tuple[str, float, float, float], ...] = (
    ("citation_only", 0.0, CITATION_WEIGHT, MISLED_WEIGHT),
    ("production_retention_shape", RETRIEVAL_WEIGHT, CITATION_WEIGHT, MISLED_WEIGHT),
    ("retrieval_heavy", 0.06, CITATION_WEIGHT, MISLED_WEIGHT),
    ("citation_heavy", RETRIEVAL_WEIGHT, 0.36, MISLED_WEIGHT),
)


def print_report(report: dict[str, Any]) -> None:
    print("\n=== P5 usage-prior rerank what-if ===")
    print(f"events                {report['events']}")
    print(f"sessions              {report['exposure_sessions']}")
    print(f"contrastive sessions  {report['contrastive_sessions']}")
    print(f"origin filter         {report['origin_filter']}")
    print()
    header = f"{'arm':<28}{'cited':>7}{'up':>6}{'flat':>6}{'down':>6}{'mean_d':>9}{'mrr_d':>10}"
    print(header)
    print("-" * len(header))
    for arm in report["arms"]:
        summary = arm["summary"]
        print(
            f"{arm['name']:<28}"
            f"{summary['cited_items_evaluated']:>7}"
            f"{summary['improved']:>6}"
            f"{summary['unchanged']:>6}"
            f"{summary['worsened']:>6}"
            f"{_fmt(summary['mean_rank_delta']):>9}"
            f"{_fmt(summary['mrr_delta']):>10}"
        )
    print()
    baseline = (
        report["arms"][1]["summary"] if len(report["arms"]) > 1 else report["arms"][0]["summary"]
    )
    print(f"informative prior share  {baseline['informative_prior_share']}")
    print(f"baseline MRR             {baseline['baseline_mrr']}")
    print(f"reweighted MRR           {baseline['reweighted_mrr']}")
    null = report["permutation_null"]
    print("\n-- permutation null (shuffled prior, same strength) --")
    print(f"trials {null['trials']}  mean {_fmt(null['mean'])}  stdev {null['stdev']}")
    print(f"95th pct |MRR delta|     {null['p95_abs']}")
    print("  an observed |MRR delta| below that is indistinguishable from a meaningless prior")
    print("\n-- verdict per arm --")
    for verdict in report["verdicts"]:
        if verdict.get("verdict") == "no_data":
            print(f"{verdict['name']:<28} no_data")
            continue
        print(f"{verdict['name']:<28}{verdict['verdict']:<32}z_vs_null={verdict['z_vs_null_mean']}")
    print()


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def adjudicate(arms: Sequence[dict[str, Any]], null: dict[str, Any]) -> list[dict[str, Any]]:
    """Score each arm against the noise floor.

    Two comparisons matter and they answer different questions. Against the
    served baseline: did the reweighting help at all, and by more than a
    meaningless prior of the same strength would have moved things. Against the
    null mean: does the prior carry information, even if it is not enough to
    beat the current ranker. An arm can pass the second and fail the first,
    which is the difference between "the signal is real" and "ship it".
    """
    p95 = null.get("p95_abs")
    null_mean = null.get("mean")
    null_stdev = null.get("stdev") or 0.0
    verdicts: list[dict[str, Any]] = []
    for arm in arms:
        observed = arm["summary"]["mrr_delta"]
        if observed is None or p95 is None:
            verdicts.append({"name": arm["name"], "verdict": "no_data"})
            continue
        above_floor = abs(observed) > p95
        z_vs_null = (
            round((observed - null_mean) / null_stdev, 3)
            if null_mean is not None and null_stdev
            else None
        )
        if observed > 0 and above_floor:
            verdict = "improves_above_noise"
        elif observed < 0 and above_floor:
            verdict = "harms_above_noise"
        else:
            verdict = "indistinguishable_from_noise"
        verdicts.append(
            {
                "name": arm["name"],
                "mrr_delta": observed,
                "null_p95_abs": p95,
                "exceeds_noise_floor": above_floor,
                "z_vs_null_mean": z_vs_null,
                "verdict": verdict,
            }
        )
    return verdicts


def outcome_records(outcomes: Sequence[RerankOutcome]) -> list[dict[str, Any]]:
    return [outcome.to_json() for outcome in outcomes]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events",
        type=Path,
        default=paths.EVENTS_JSONL,
        help="JSONL produced by extract.py.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=paths.OUT,
        help="Directory for the what-if receipt.",
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
        "--interactive-only",
        action="store_true",
        help="Drop burst-suspect sessions, giving the contamination-free lower bound.",
    )
    parser.add_argument(
        "--null-trials",
        type=int,
        default=200,
        help="Permutation trials used to build the MRR-delta noise floor.",
    )
    parser.add_argument(
        "--null-seed",
        type=int,
        default=20260803,
        help="Seed for the permutation null, so the noise floor reproduces.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    events_path = Path(args.events)
    if not events_path.exists():
        print(f"no extraction found at {events_path}; run extract.py first")
        return 1

    rows = load_events(events_path)
    sessions = group_exposure_sessions(rows)
    attributions = attribute_feedback(sessions, rows, window_seconds=args.window_seconds)
    origins = flag_eval_suspect_sessions(
        sessions,
        burst_threshold=args.burst_threshold,
        burst_window_seconds=args.burst_window_seconds,
    )
    labeled_sessions = build_labeled_sessions(sessions, attributions, origins)
    if args.interactive_only:
        labeled_sessions = tuple(
            labeled for labeled in labeled_sessions if labeled.origin == ORIGIN_INTERACTIVE
        )

    counts = PointInTimeCounts(rows)
    arms: list[dict[str, Any]] = []
    for name, retrieval_weight, citation_weight, misled_weight in WEIGHT_ARMS:
        outcomes = run_whatif(
            labeled_sessions,
            counts,
            retrieval_weight=retrieval_weight,
            citation_weight=citation_weight,
            misled_weight=misled_weight,
        )
        arms.append(
            {
                "name": name,
                "weights": {
                    "retrieval": retrieval_weight,
                    "citation": citation_weight,
                    "misled": misled_weight,
                },
                "summary": summarize_outcomes(outcomes),
                "outcomes": outcome_records(outcomes),
            }
        )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "events_path": str(events_path),
        "events": len(rows),
        "exposure_sessions": len(sessions),
        "contrastive_sessions": sum(1 for labeled in labeled_sessions if labeled.is_contrastive()),
        "origin_filter": ORIGIN_INTERACTIVE if args.interactive_only else "all",
        "attribution_window_seconds": args.window_seconds,
        "method": {
            "baseline_score": "rrf_score(recovered rank within item kind), k=60",
            "reweighted_score": "baseline * usage_prior_multiplier(point-in-time counts)",
            "leakage_guard": "counts use only events strictly before the session start",
            "rank_scope": "within item kind; the emitter's two-call batching loses global rank",
        },
        "arms": arms,
    }
    report["permutation_null"] = permutation_null(
        labeled_sessions,
        counts,
        trials=args.null_trials,
        seed=args.null_seed,
    )
    report["verdicts"] = adjudicate(arms, report["permutation_null"])

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The origin filter is part of the receipt's identity, so the two runs do not
    # overwrite each other and a committed report always says which it is.
    stem = paths.WHATIF_REPORT_JSON.stem
    suffix = "" if not args.interactive_only else "_interactive_only"
    report_path = out_dir / f"{stem}{suffix}{paths.WHATIF_REPORT_JSON.suffix}"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print_report(report)
    print(f"report -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
