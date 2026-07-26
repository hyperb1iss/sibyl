"""STAGE 1b - is the free ride the trajectory-goal preamble? (BM25 only, CPU)

If the fat unit's lexical advantage comes from the duplicated goal preamble that
rides along in every chunk of a trajectory, then stripping the preamble from the
fat arm should collapse its BM25 recall toward the slice arm's. Confirmatory
test of the mechanism, not a substitute for Stage 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from corpus import OUT, build_units, eligible_questions, normalize_text, state_evidence_text
from stage1 import CHAR_BUDGETS, K_GRID, bm25_scores, build_bm25, recall_curve


def run(domain: str) -> dict[str, Any]:
    units = build_units(domain)
    states = units["states"]
    norm_state = {k: normalize_text(state_evidence_text(e)) for k, e in states.items()}
    measurable = [
        item
        for item in eligible_questions(domain)
        if all(any(p in t for t in norm_state.values()) for p in item["phrases"])
    ]
    arms = {
        "FAT_with_preamble": [r["index_text"] for r in units["fat"]],
        "FAT_no_preamble": [r["exposure_text"] for r in units["fat"]],
        "SLICE_bare": [r["index_text"] for r in units["slices"]],
        "SLICE_with_preamble": [r["preamble"] + r["index_text"] for r in units["slices"]],
    }
    exposure = {
        "FAT_with_preamble": [normalize_text(r["exposure_text"]) for r in units["fat"]],
        "FAT_no_preamble": [normalize_text(r["exposure_text"]) for r in units["fat"]],
        "SLICE_bare": [normalize_text(r["exposure_text"]) for r in units["slices"]],
        "SLICE_with_preamble": [normalize_text(r["exposure_text"]) for r in units["slices"]],
    }
    chars = {
        "FAT_with_preamble": np.asarray([len(r["exposure_text"]) for r in units["fat"]]),
        "FAT_no_preamble": np.asarray([len(r["exposure_text"]) for r in units["fat"]]),
        "SLICE_bare": np.asarray([len(r["exposure_text"]) for r in units["slices"]]),
        "SLICE_with_preamble": np.asarray([len(r["exposure_text"]) for r in units["slices"]]),
    }
    out: dict[str, Any] = {"domain": domain, "measurable": len(measurable), "arms": {}}
    for name, documents in arms.items():
        weights, vocabulary = build_bm25(documents)
        hits_k = dict.fromkeys(K_GRID, 0)
        hits_b = dict.fromkeys(CHAR_BUDGETS, 0)
        ranks = []
        for item in measurable:
            masks = [
                np.asarray([p in text for text in exposure[name]], dtype=bool)
                for p in item["phrases"]
            ]
            scores = bm25_scores(weights, vocabulary, item["question"]["question"])
            curve = recall_curve(np.argsort(-scores, kind="stable"), masks, chars[name])
            for k in K_GRID:
                hits_k[k] += curve["hit_at_k"][str(k)]
            for b in CHAR_BUDGETS:
                hits_b[b] += curve["hit_at_budget"][str(b)]
            ranks.append(curve["complete_rank"] or 10**6)
        out["arms"][name] = {
            "units": len(documents),
            "recall_at_k": {str(k): round(v / len(measurable), 4) for k, v in hits_k.items()},
            "recall_at_char_budget": {
                str(b): round(v / len(measurable), 4) for b, v in hits_b.items()
            },
            "median_complete_rank": float(np.median(ranks)),
        }
        print(f"[{domain}] {name:22s} {out['arms'][name]['recall_at_k']}", flush=True)
        print(f"      budget {out['arms'][name]['recall_at_char_budget']}", flush=True)
    return out


def main() -> None:
    report = {}
    for domain in ("enterprise", "web"):
        report[domain] = run(domain)
        (OUT / "stage1b_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
