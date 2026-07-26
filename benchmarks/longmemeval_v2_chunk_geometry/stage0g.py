"""STAGE 0g - v1 vs v2 boundary rules: band adherence, count, oracle exposure."""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from corpus import OUT, eligible_questions, normalize_text, state_evidence_text
from slicer import HARD_MAX, TARGET_HI, TARGET_LO, render_slice, slice_body, slice_header
from slicer_v2 import slice_body_v2
from stage0 import load_states

WINDOWS = (1, 2, 3)


def summarise(domain: str, cutter) -> dict[str, Any]:
    states = load_states(domain)
    sizes: list[int] = []
    band = Counter()
    per_state: list[int] = []
    crumbs: list[int] = []
    stats_total = Counter()
    rendered_by_state: dict[tuple[str, int], list[str]] = {}
    for key, entry in states.items():
        pieces, stats = cutter(entry["tree"])
        for name, value in stats.items():
            stats_total[name] += value
        per_state.append(len(pieces))
        rendered = []
        for order, piece in enumerate(pieces, start=1):
            size = len(piece.content)
            sizes.append(size)
            crumbs.append(len(piece.breadcrumb))
            if size > HARD_MAX:
                band["over_hard_max"] += 1
            elif size < TARGET_LO:
                band["under_600"] += 1
            elif size <= TARGET_HI:
                band["in_band"] += 1
            else:
                band["1200_to_2000"] += 1
            rendered.append(
                render_slice(
                    slice_header(entry["state_index"], entry["url"], order, len(pieces)),
                    piece.breadcrumb,
                    piece.content,
                )
            )
        rendered_by_state[key] = rendered

    norm_state = {k: normalize_text(state_evidence_text(e)) for k, e in states.items()}
    norm_slices = {k: [normalize_text(t) for t in v] for k, v in rendered_by_state.items()}
    exposure = {f"window_{w}": 0 for w in WINDOWS}
    exposure["fat_state"] = 0
    triples = {"n": 0, "single": 0}
    measurable = 0
    for item in eligible_questions(domain):
        phrases = item["phrases"]
        if not all(any(p in t for t in norm_state.values()) for p in phrases):
            continue
        measurable += 1
        carriers = [k for k in states if any(p in norm_state[k] for p in phrases)]
        hits = dict.fromkeys(exposure, False)
        for k in carriers:
            if all(p in norm_state[k] for p in phrases):
                hits["fat_state"] = True
            texts = norm_slices[k]
            covers = [{i for i, p in enumerate(phrases) if p in t} for t in texts]
            for w in WINDOWS:
                for start in range(max(1, len(texts) - w + 1)):
                    merged: set[int] = set()
                    for cover in covers[start : start + w]:
                        merged |= cover
                    if len(merged) == len(phrases):
                        hits[f"window_{w}"] = True
                        break
            tree_norm = normalize_text(states[k]["tree"])
            for phrase in phrases:
                if phrase not in tree_norm:
                    continue
                triples["n"] += 1
                if any(phrase in t for t in texts):
                    triples["single"] += 1
        for name, value in hits.items():
            if value:
                exposure[name] += 1

    total = len(sizes)
    return {
        "slices": total,
        "size": {
            "mean": round(statistics.mean(sizes), 1),
            "p50": sorted(sizes)[total // 2],
            "p90": sorted(sizes)[int(0.9 * total)],
            "p99": sorted(sizes)[int(0.99 * total)],
            "max": max(sizes),
        },
        "band_pct": {k: round(100 * v / total, 2) for k, v in band.items()},
        "slices_per_state_mean": round(statistics.mean(per_state), 2),
        "breadcrumb_mean": round(statistics.mean(crumbs), 1),
        "stats": dict(stats_total),
        "measurable": measurable,
        "exposure_counts": exposure,
        "exposure_rates": {k: round(v / measurable, 4) for k, v in exposure.items()},
        "phrase_triples": triples["n"],
        "phrase_single_slice_rate": round(triples["single"] / triples["n"], 6)
        if triples["n"]
        else None,
    }


def main() -> None:
    report: dict[str, Any] = {}
    for domain in ("enterprise", "web"):
        report[domain] = {
            "v1": summarise(domain, slice_body),
            "v2": summarise(domain, slice_body_v2),
        }
        print(f"== {domain}", flush=True)
        for name, data in report[domain].items():
            print(
                f"  {name}: slices={data['slices']} mean={data['size']['mean']} "
                f"max={data['size']['max']} band={data['band_pct']} "
                f"exposure={data['exposure_rates']} straddle0={data['phrase_single_slice_rate']}",
                flush=True,
            )
    (OUT / "stage0g_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
