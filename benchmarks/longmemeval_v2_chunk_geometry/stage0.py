"""STAGE 0 - slicer distributions + oracle exposure ceiling.

Reads the frozen chunk catalogs from the two-typed run, reconstructs whole
accessibility-tree states from their parts, slices them, and measures:
  * slice size / count / header-tax / depth-behaviour distributions
  * oracle exposure: can a single slice (or slice + 1 neighbour) carry every
    gold phrase for a phrase-eligible question?
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from paths import OUT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chunkparse import EVAL_ROOT, iter_chunks, split_chunk
from longmemeval_v2_diagnostics import (
    answer_evidence_phrases,
    is_exact_evidence_eligible,
    normalize_text,
)
from slicer import render_slice, slice_body, slice_header

OUT.mkdir(parents=True, exist_ok=True)

_FIELD = re.compile(r"^(Thought|Action|Accessibility tree):", re.MULTILINE)


def parse_header_fields(header: str) -> dict[str, str]:
    """Pull Action / Thought text out of the per-part header block."""
    fields: dict[str, str] = {}
    lines = header.split("\n")
    current: str | None = None
    buffer: list[str] = []
    for line in lines:
        match = _FIELD.match(line)
        if match is not None:
            if current:
                fields[current] = "\n".join(buffer).strip()
            name = match.group(1)
            if name == "Accessibility tree":
                current = None
                buffer = []
                continue
            current = name.lower()
            buffer = [line[len(name) + 1 :].strip()]
            continue
        if current:
            buffer.append(line)
    if current:
        fields[current] = "\n".join(buffer).strip()
    return fields


def load_states(domain: str) -> dict[tuple[str, int], dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for record in iter_chunks(domain):
        meta = record["metadata"]
        key = (meta["longmemeval_v2_trajectory_id"], int(meta["longmemeval_v2_state_index"]))
        parsed = split_chunk(record["content"])
        entry = grouped.setdefault(
            key,
            {
                "trajectory_id": key[0],
                "state_index": key[1],
                "url": parsed["url"],
                "preamble": parsed["preamble"],
                "action": "",
                "thought": "",
                "parts": {},
                "chunk_texts": {},
            },
        )
        fields = parse_header_fields(parsed["header"])
        if fields.get("action"):
            entry["action"] = fields["action"]
        if fields.get("thought"):
            entry["thought"] = fields["thought"]
        part_index = int(meta["longmemeval_v2_state_part_index"])
        entry["parts"][part_index] = parsed["body"]
        entry["chunk_texts"][part_index] = parsed["header"] + "\n" + parsed["body"]
    for entry in grouped.values():
        entry["tree"] = "".join(entry["parts"][i] for i in sorted(entry["parts"]))
    return grouped


def state_evidence_text(entry: dict[str, Any]) -> str:
    return "\n".join((entry["url"], entry["action"], entry["thought"], entry["tree"]))


def percentiles(values: list[int]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {}

    def at(fraction: float) -> int:
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    return {
        "count": len(ordered),
        "mean": round(statistics.mean(ordered), 1),
        "p50": at(0.50),
        "p90": at(0.90),
        "p99": at(0.99),
        "min": ordered[0],
        "max": ordered[-1],
    }


def build_slices(entry: dict[str, Any]) -> list[dict[str, Any]]:
    pieces, stats = slice_body(entry["tree"])
    total = len(pieces)
    built: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces, start=1):
        header = slice_header(entry["state_index"], entry["url"], index, total)
        built.append(
            {
                "header": header,
                "breadcrumb": piece.breadcrumb,
                "content": piece.content,
                "cut_depth": piece.cut_depth,
                "reason": piece.reason,
                "rendered": render_slice(header, piece.breadcrumb, piece.content),
                "slice_index": index,
                "slice_total": total,
            }
        )
    return built, stats


def analyse_domain(domain: str) -> dict[str, Any]:
    states = load_states(domain)
    with (EVAL_ROOT / domain / "runtime_inputs" / "questions.json").open() as handle:
        questions = json.load(handle)

    eligible: list[dict[str, Any]] = []
    for question in questions:
        phrases = answer_evidence_phrases(question)
        ok = bool(phrases) and all(is_exact_evidence_eligible(p) for p in phrases)
        if ok:
            eligible.append({"question": question, "phrases": phrases})

    content_chars: list[int] = []
    header_chars: list[int] = []
    crumb_chars: list[int] = []
    per_state_counts: list[int] = []
    cut_depths: Counter = Counter()
    reasons: Counter = Counter()
    descend_events = 0
    oversize_leaf = 0
    body_chars_total = 0
    slice_store: dict[tuple[str, int], list[dict[str, Any]]] = {}

    # Which states carry at least one gold phrase (over all eligible questions)?
    gold_states: dict[tuple[str, int], set[int]] = defaultdict(set)
    for key, entry in states.items():
        normalized = normalize_text(state_evidence_text(entry))
        for qi, item in enumerate(eligible):
            if any(p in normalized for p in item["phrases"]):
                gold_states[key].add(qi)

    for key, entry in states.items():
        built, stats = build_slices(entry)
        descend_events += stats["descend_events"]
        oversize_leaf += stats["oversize_leaf"]
        body_chars_total += len(entry["tree"])
        per_state_counts.append(len(built))
        for piece in built:
            content_chars.append(len(piece["content"]))
            header_chars.append(len(piece["header"]))
            crumb_chars.append(len(piece["breadcrumb"]))
            cut_depths[piece["cut_depth"]] += 1
            reasons[piece["reason"]] += 1
        if key in gold_states:
            slice_store[key] = built

    total_content = sum(content_chars)
    total_header = sum(header_chars)
    total_crumb = sum(crumb_chars)
    total_rendered = total_content + total_header + total_crumb + 2 * len(content_chars)

    # ---- oracle exposure -------------------------------------------------
    exposure: list[dict[str, Any]] = []
    for qi, item in enumerate(eligible):
        phrases = item["phrases"]
        question = item["question"]
        carrier_states = [k for k, v in gold_states.items() if qi in v]

        fat_state_hit = False
        fat_chunk_hit = False
        slice_single_hit = False
        slice_pair_hit = False
        best_single_cover = 0
        best_pair_cover = 0
        phrase_union_slices: set[int] = set()
        detail: list[dict[str, Any]] = []

        for key in carrier_states:
            entry = states[key]
            normalized_state = normalize_text(state_evidence_text(entry))
            covered_state = {i for i, p in enumerate(phrases) if p in normalized_state}
            if len(covered_state) == len(phrases):
                fat_state_hit = True
            for part_index in sorted(entry["chunk_texts"]):
                normalized_chunk = normalize_text(entry["chunk_texts"][part_index])
                if all(p in normalized_chunk for p in phrases):
                    fat_chunk_hit = True
            built = slice_store[key]
            normalized_slices = [normalize_text(s["rendered"]) for s in built]
            per_slice_cover = [
                {i for i, p in enumerate(phrases) if p in text} for text in normalized_slices
            ]
            for cover in per_slice_cover:
                phrase_union_slices |= cover
                best_single_cover = max(best_single_cover, len(cover))
                if len(cover) == len(phrases):
                    slice_single_hit = True
            for index in range(len(built) - 1):
                merged = normalize_text(
                    built[index]["rendered"] + "\n" + built[index + 1]["rendered"]
                )
                cover = {i for i, p in enumerate(phrases) if p in merged}
                best_pair_cover = max(best_pair_cover, len(cover))
                if len(cover) == len(phrases):
                    slice_pair_hit = True
            if len(covered_state) == len(phrases):
                detail.append(
                    {
                        "state": f"{key[0]}:{key[1]}",
                        "slices": len(built),
                        "per_slice_cover": [sorted(c) for c in per_slice_cover if c],
                        "slice_indices_with_any": [
                            i + 1 for i, c in enumerate(per_slice_cover) if c
                        ],
                    }
                )
        exposure.append(
            {
                "question_id": question["id"],
                "question_type": question.get("question_type"),
                "phrase_count": len(phrases),
                "phrases": list(phrases),
                "carrier_states": len(carrier_states),
                "fat_state_hit": fat_state_hit,
                "fat_chunk_hit": fat_chunk_hit,
                "slice_single_hit": slice_single_hit,
                "slice_pair_hit": slice_pair_hit,
                "best_single_cover": best_single_cover,
                "best_pair_cover": best_pair_cover,
                "phrase_union_over_slices": sorted(phrase_union_slices),
                "detail": detail[:4],
            }
        )

    return {
        "domain": domain,
        "states": len(states),
        "chunks_fat": sum(len(e["chunk_texts"]) for e in states.values()),
        "body_chars_total": body_chars_total,
        "slice_total": len(content_chars),
        "slice_content_chars": percentiles(content_chars),
        "slices_per_state": percentiles(per_state_counts),
        "header_chars": percentiles(header_chars),
        "breadcrumb_chars": percentiles(crumb_chars),
        "header_tax": {
            "total_content_chars": total_content,
            "total_header_chars": total_header,
            "total_breadcrumb_chars": total_crumb,
            "total_rendered_chars": total_rendered,
            "header_fraction": round(total_header / total_rendered, 4),
            "breadcrumb_fraction": round(total_crumb / total_rendered, 4),
            "combined_overhead_fraction": round((total_header + total_crumb) / total_rendered, 4),
        },
        "depth": {
            "cut_depth_distribution": dict(sorted(cut_depths.items())),
            "slice_reason": dict(reasons),
            "descend_events": descend_events,
            "oversize_leaf_fallbacks": oversize_leaf,
        },
        "eligible_questions": len(eligible),
        "total_questions": len(questions),
        "exposure": exposure,
    }


def main() -> None:
    report: dict[str, Any] = {}
    for domain in ("enterprise", "web"):
        report[domain] = analyse_domain(domain)
        print(f"== {domain} done", flush=True)
    (OUT / "stage0_report.json").write_text(json.dumps(report, indent=2) + "\n")

    for domain, data in report.items():
        exposure = data["exposure"]
        fat = [e for e in exposure if e["fat_state_hit"]]
        print(f"\n### {domain}")
        print(
            f"states={data['states']} fat_chunks={data['chunks_fat']} slices={data['slice_total']}"
        )
        print("slice chars", data["slice_content_chars"])
        print("slices/state", data["slices_per_state"])
        print("header tax", data["header_tax"])
        print("depth", data["depth"])
        print(
            f"eligible={data['eligible_questions']}/{data['total_questions']} "
            f"fat_state_exposable={len(fat)} "
            f"slice_single={sum(1 for e in fat if e['slice_single_hit'])} "
            f"slice_pair={sum(1 for e in fat if e['slice_pair_hit'])}"
        )


if __name__ == "__main__":
    main()
