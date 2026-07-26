"""STAGE 0c - band adherence, hard-max overflow, subtree survival."""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

from paths import OUT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slicer import HARD_MAX, TARGET_HI, TARGET_LO, build_forest, slice_body
from stage0 import load_states

random.seed(20260724)


def analyse(domain: str) -> dict:
    states = load_states(domain)
    band = Counter()
    overflow: list[int] = []
    sizes: list[int] = []
    subtree_whole = 0
    subtree_total = 0
    keys = sorted(states)
    sample = set(random.sample(keys, min(600, len(keys))))
    for key, entry in states.items():
        pieces, _ = slice_body(entry["tree"])
        for piece in pieces:
            size = len(piece.content)
            sizes.append(size)
            if size > HARD_MAX:
                overflow.append(size)
                band["over_hard_max"] += 1
            elif size < TARGET_LO:
                band["under_600"] += 1
            elif size <= TARGET_HI:
                band["in_600_1200_band"] += 1
            else:
                band["1200_to_2000"] += 1
        if key in sample:
            lines = entry["tree"].split("\n")
            owner = {}
            for index, piece in enumerate(pieces):
                for line_index in piece.line_indices:
                    owner[line_index] = index

            def walk(nodes, owner=owner):
                nonlocal subtree_whole, subtree_total
                for node in nodes:
                    if node.children:
                        subtree_total += 1
                        indices = []
                        stack = [node]
                        while stack:
                            current = stack.pop()
                            indices.append(current.index)
                            stack.extend(current.children)
                        if len({owner.get(i) for i in indices}) == 1:
                            subtree_whole += 1
                        walk(node.children, owner)

            walk(build_forest(lines))
    total = len(sizes)
    return {
        "domain": domain,
        "slices": total,
        "band_adherence": {k: {"n": v, "pct": round(100 * v / total, 2)} for k, v in band.items()},
        "hard_max_overflow": {
            "n": len(overflow),
            "pct": round(100 * len(overflow) / total, 3),
            "max": max(overflow) if overflow else 0,
            "mean_overflow_chars": round(sum(o - HARD_MAX for o in overflow) / len(overflow), 1)
            if overflow
            else 0,
        },
        "internal_subtrees_sampled": subtree_total,
        "internal_subtree_intact_in_one_slice_pct": round(100 * subtree_whole / subtree_total, 2)
        if subtree_total
        else None,
    }


def main() -> None:
    report = {domain: analyse(domain) for domain in ("enterprise", "web")}
    (OUT / "stage0c_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
