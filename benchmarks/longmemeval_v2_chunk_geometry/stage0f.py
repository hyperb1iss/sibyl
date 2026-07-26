"""STAGE 0f - breadcrumb cost as a design knob.

The breadcrumb is pure metadata: it duplicates ancestor role/name text that also
exists verbatim earlier in the same state. Measure what capping it to the last
N ancestors saves, and how much of the breadcrumb text is already present in the
slice's own content (i.e. redundant even within the slice).
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from corpus import OUT
from slicer import HEADER_MAX_CHARS, slice_body, slice_header
from stage0 import load_states

CAPS = (0, 1, 2, 3, 999)


def analyse(domain: str) -> dict:
    states = load_states(domain)
    depths: list[int] = []
    full: list[int] = []
    capped = dict.fromkeys(CAPS, 0)
    header_total = 0
    content_total = 0
    slices = 0
    over_header_budget = 0
    for entry in states.values():
        pieces, _ = slice_body(entry["tree"])
        total = len(pieces)
        for order, piece in enumerate(pieces, start=1):
            slices += 1
            content_total += len(piece.content)
            header = slice_header(entry["state_index"], entry["url"], order, total)
            header_total += len(header)
            if (
                len(f"Observation {entry['state_index']} · {entry['url']} · slice {order}/{total}")
                > HEADER_MAX_CHARS
            ):
                over_header_budget += 1
            parts = piece.breadcrumb.split(" > ") if piece.breadcrumb else []
            depths.append(len(parts))
            full.append(len(piece.breadcrumb))
            for cap in CAPS:
                text = " > ".join(parts[-cap:]) if cap and parts else ""
                if cap == 999:
                    text = piece.breadcrumb
                capped[cap] += len(text)
    base = content_total + header_total + 2 * slices
    return {
        "domain": domain,
        "slices": slices,
        "ancestor_depth": {
            "mean": round(statistics.mean(depths), 2),
            "p50": sorted(depths)[len(depths) // 2],
            "p90": sorted(depths)[int(0.9 * len(depths))],
            "max": max(depths),
        },
        "breadcrumb_chars": {
            "mean": round(statistics.mean(full), 1),
            "p50": sorted(full)[len(full) // 2],
            "p90": sorted(full)[int(0.9 * len(full))],
            "max": max(full),
        },
        "total_overhead_pct_of_payload": {
            f"last_{cap}_ancestors" if cap != 999 else "uncapped": round(
                100 * capped[cap] / (base + capped[cap]), 2
            )
            for cap in CAPS
        },
        "chars_saved_vs_uncapped": {
            f"last_{cap}_ancestors": capped[999] - capped[cap] for cap in CAPS if cap != 999
        },
        "url_over_120_char_header_budget_pct": round(100 * over_header_budget / slices, 2),
    }


def main() -> None:
    report = {domain: analyse(domain) for domain in ("enterprise", "web")}
    (OUT / "stage0f_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
