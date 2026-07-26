"""STAGE 0b - straddle characterisation at scale.

The real-gold denominator is tiny (5 phrase-eligible + source-complete questions
per domain), so this widens the boundary measurement three ways:

  PROBE A  every (question, phrase, carrier state) triple from real gold
  PROBE B  synthetic multi-line evidence spans (k consecutive tree lines)
  PROBE C  parent/child adjacency - does a node stay with its own children?
  PROBE D  where do gold phrases actually live: tree vs thought/action/url
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from paths import OUT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunkparse import EVAL_ROOT
from longmemeval_v2_diagnostics import (
    answer_evidence_phrases,
    is_exact_evidence_eligible,
    normalize_text,
)
from slicer import render_slice, slice_body, slice_header
from stage0 import load_states

SPAN_SIZES = (1, 2, 3, 5, 8)
SPAN_SAMPLES = 4000
random.seed(20260724)


def slices_for(entry: dict[str, Any]) -> list[dict[str, Any]]:
    pieces, _ = slice_body(entry["tree"])
    total = len(pieces)
    return [
        {
            "line_indices": piece.line_indices,
            "rendered": render_slice(
                slice_header(entry["state_index"], entry["url"], index, total),
                piece.breadcrumb,
                piece.content,
            ),
        }
        for index, piece in enumerate(pieces, start=1)
    ]


def analyse(domain: str) -> dict[str, Any]:
    states = load_states(domain)
    with (EVAL_ROOT / domain / "runtime_inputs" / "questions.json").open() as handle:
        questions = json.load(handle)
    eligible = []
    for question in questions:
        phrases = answer_evidence_phrases(question)
        if phrases and all(is_exact_evidence_eligible(p) for p in phrases):
            eligible.append((question, phrases))

    # ---------- PROBE D: where gold phrases live ----------
    normalized_trees = {key: normalize_text(entry["tree"]) for key, entry in states.items()}
    normalized_meta = {
        key: normalize_text("\n".join((entry["url"], entry["action"], entry["thought"])))
        for key, entry in states.items()
    }
    live_counts = Counter()
    phrase_carrier_rows: list[tuple[str, str, tuple[str, int]]] = []
    for question, phrases in eligible:
        for phrase in phrases:
            found_tree = 0
            found_meta_only = 0
            for key in states:
                if phrase in normalized_trees[key]:
                    found_tree += 1
                    phrase_carrier_rows.append((question["id"], phrase, key))
                elif phrase in normalized_meta[key]:
                    found_meta_only += 1
            if found_tree:
                live_counts["phrase_in_tree"] += 1
            elif found_meta_only:
                live_counts["phrase_only_in_thought_action_url"] += 1
            else:
                live_counts["phrase_absent_from_corpus"] += 1

    # ---------- PROBE A: per (question, phrase, carrier state) ----------
    probe_a = {"pairs": 0, "single_slice_hit": 0, "pair_slice_hit": 0, "misses": []}
    grouped_carriers: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
    for question_id, phrase, key in phrase_carrier_rows:
        grouped_carriers[key].append((question_id, phrase))
    for key, items in grouped_carriers.items():
        pieces = slices_for(states[key])
        normalized = [normalize_text(p["rendered"]) for p in pieces]
        joined = [
            normalize_text(pieces[i]["rendered"] + "\n" + pieces[i + 1]["rendered"])
            for i in range(len(pieces) - 1)
        ]
        for question_id, phrase in items:
            probe_a["pairs"] += 1
            if any(phrase in text for text in normalized):
                probe_a["single_slice_hit"] += 1
                probe_a["pair_slice_hit"] += 1
                continue
            if any(phrase in text for text in joined):
                probe_a["pair_slice_hit"] += 1
                probe_a["misses"].append(
                    {
                        "question_id": question_id,
                        "state": f"{key[0]}:{key[1]}",
                        "phrase": phrase[:120],
                        "recovered_by": "neighbour",
                    }
                )
            else:
                probe_a["misses"].append(
                    {
                        "question_id": question_id,
                        "state": f"{key[0]}:{key[1]}",
                        "phrase": phrase[:120],
                        "recovered_by": "none",
                    }
                )

    # ---------- PROBE B / C: synthetic spans + parent-child adjacency ----------
    keys = sorted(states)
    sampled = random.sample(keys, min(600, len(keys)))
    span_stats = {k: {"n": 0, "single": 0, "pair": 0} for k in SPAN_SIZES}
    parent_child = {"n": 0, "same_slice": 0, "adjacent_slice": 0}
    slice_span_chars: list[int] = []
    for key in sampled:
        entry = states[key]
        lines = entry["tree"].split("\n")
        pieces = slices_for(entry)
        if len(lines) < 2 or not pieces:
            continue
        owner = {}
        for index, piece in enumerate(pieces):
            for line_index in piece["line_indices"]:
                owner[line_index] = index
        for k in SPAN_SIZES:
            if len(lines) <= k:
                continue
            for _ in range(max(1, SPAN_SAMPLES // len(sampled))):
                start = random.randrange(0, len(lines) - k)
                span = list(range(start, start + k))
                owners = {owner[i] for i in span if i in owner}
                span_stats[k]["n"] += 1
                if len(owners) == 1:
                    span_stats[k]["single"] += 1
                    span_stats[k]["pair"] += 1
                elif len(owners) == 2 and max(owners) - min(owners) == 1:
                    span_stats[k]["pair"] += 1
        # parent -> immediate children adjacency
        depths = [len(line) - len(line.lstrip("\t")) for line in lines]
        for index in range(len(lines) - 1):
            if depths[index + 1] > depths[index]:
                parent_child["n"] += 1
                a, b = owner.get(index), owner.get(index + 1)
                if a == b:
                    parent_child["same_slice"] += 1
                elif a is not None and b is not None and abs(a - b) == 1:
                    parent_child["adjacent_slice"] += 1
        slice_span_chars.extend(len(p["rendered"]) for p in pieces)

    return {
        "domain": domain,
        "eligible_questions": len(eligible),
        "probe_d_phrase_home": dict(live_counts),
        "probe_a": {
            "question_phrase_state_triples": probe_a["pairs"],
            "single_slice_hit": probe_a["single_slice_hit"],
            "pair_slice_hit": probe_a["pair_slice_hit"],
            "single_slice_rate": round(probe_a["single_slice_hit"] / probe_a["pairs"], 5)
            if probe_a["pairs"]
            else None,
            "straddle_rate": round(1 - probe_a["single_slice_hit"] / probe_a["pairs"], 5)
            if probe_a["pairs"]
            else None,
            "misses_sample": probe_a["misses"][:20],
            "miss_total": len(probe_a["misses"]),
        },
        "probe_b_spans": {
            str(k): {
                "samples": v["n"],
                "within_single_slice": round(v["single"] / v["n"], 4) if v["n"] else None,
                "within_slice_plus_neighbour": round(v["pair"] / v["n"], 4) if v["n"] else None,
            }
            for k, v in span_stats.items()
        },
        "probe_c_parent_child": {
            "adjacent_pairs": parent_child["n"],
            "same_slice_rate": round(parent_child["same_slice"] / parent_child["n"], 4)
            if parent_child["n"]
            else None,
            "adjacent_slice_rate": round(parent_child["adjacent_slice"] / parent_child["n"], 4)
            if parent_child["n"]
            else None,
        },
        "rendered_slice_chars": {
            "mean": round(statistics.mean(slice_span_chars), 1) if slice_span_chars else None,
            "p90": sorted(slice_span_chars)[int(0.9 * len(slice_span_chars))]
            if slice_span_chars
            else None,
        },
        "sampled_states": len(sampled),
    }


def main() -> None:
    report = {}
    for domain in ("enterprise", "web"):
        report[domain] = analyse(domain)
        print(json.dumps(report[domain], indent=2)[:3000], flush=True)
    (OUT / "stage0b_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
