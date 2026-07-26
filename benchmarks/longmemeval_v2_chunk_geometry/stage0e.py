"""STAGE 0e - oracle exposure as a function of the slice window.

Extends stage0d: how many measurable questions are fully exposed by a window of
w adjacent slices, by any 2 slices drawn from the same state, and by the whole
state's slice set (the slice-substrate ceiling).
"""

from __future__ import annotations

import json
from collections import defaultdict
from itertools import combinations
from typing import Any

from corpus import OUT, build_units, eligible_questions, normalize_text, state_evidence_text

WINDOWS = (1, 2, 3, 5)


def analyse(domain: str) -> dict[str, Any]:
    units = build_units(domain)
    states = units["states"]
    by_state: dict[tuple[str, int], list[str]] = defaultdict(list)
    for row in units["slices"]:
        by_state[row["key"]].append(row["index_text"])
    norm_state = {k: normalize_text(state_evidence_text(e)) for k, e in states.items()}
    norm_slices = {k: [normalize_text(t) for t in v] for k, v in by_state.items()}

    results: dict[str, int] = defaultdict(int)
    measurable = 0
    losers: dict[str, list[str]] = defaultdict(list)

    for item in eligible_questions(domain):
        phrases = item["phrases"]
        question_id = item["question"]["id"]
        if not all(any(p in t for t in norm_state.values()) for p in phrases):
            continue
        measurable += 1
        carriers = [k for k in states if any(p in norm_state[k] for p in phrases)]
        hits: dict[str, bool] = {f"window_{w}": False for w in WINDOWS}
        hits["any_two_slices_same_state"] = False
        hits["all_slices_of_one_state"] = False
        hits["fat_state"] = False
        for k in carriers:
            if all(p in norm_state[k] for p in phrases):
                hits["fat_state"] = True
            texts = norm_slices.get(k, [])
            covers = [{i for i, p in enumerate(phrases) if p in t} for t in texts]
            union_all: set[int] = set()
            for cover in covers:
                union_all |= cover
            if len(union_all) == len(phrases):
                hits["all_slices_of_one_state"] = True
            for w in WINDOWS:
                for start in range(max(1, len(texts) - w + 1)):
                    merged: set[int] = set()
                    for cover in covers[start : start + w]:
                        merged |= cover
                    if len(merged) == len(phrases):
                        hits[f"window_{w}"] = True
                        break
            carrying = [index for index, cover in enumerate(covers) if cover]
            for a, b in combinations(carrying, 2):
                if len(covers[a] | covers[b]) == len(phrases):
                    hits["any_two_slices_same_state"] = True
                    break
        for name, value in hits.items():
            if value:
                results[name] += 1
            else:
                losers[name].append(question_id)
    return {
        "domain": domain,
        "measurable": measurable,
        "counts": dict(results),
        "rates": {k: round(v / measurable, 4) for k, v in results.items()},
        "losers": dict(losers),
    }


def main() -> None:
    report = {}
    for domain in ("enterprise", "web"):
        report[domain] = analyse(domain)
        print(json.dumps(report[domain], indent=2), flush=True)
    (OUT / "stage0e_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
