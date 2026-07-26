"""STAGE 0d - oracle exposure ceiling over the full 451-question set.

Denominator: every phrase-eligible question whose gold phrases all occur
somewhere in the domain corpus (source-complete). For each, ask:
  * FAT-STATE   does any single whole state carry all gold phrases?
  * FAT-CHUNK   does any single indexed chunk (state part) carry all of them?
  * SLICE-1     does any single A1 slice carry all of them?
  * SLICE-2     does any slice + one adjacent neighbour carry all of them?
Plus the per-(question, phrase, carrier state) straddle rate.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from corpus import OUT, build_units, eligible_questions, normalize_text, state_evidence_text


def analyse(domain: str) -> dict[str, Any]:
    units = build_units(domain)
    states = units["states"]
    slices_by_state: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in units["slices"]:
        slices_by_state[row["key"]].append(row)

    norm_state = {k: normalize_text(state_evidence_text(e)) for k, e in states.items()}
    norm_tree = {k: normalize_text(e["tree"]) for k, e in states.items()}
    norm_chunks = {
        k: [normalize_text(e["chunk_texts"][i]) for i in sorted(e["chunk_texts"])]
        for k, e in states.items()
    }
    norm_slices = {
        key: [normalize_text(row["index_text"]) for row in rows]
        for key, rows in slices_by_state.items()
    }
    norm_slice_pairs = {
        key: [
            normalize_text(rows[i]["index_text"] + "\n" + rows[i + 1]["index_text"])
            for i in range(len(rows) - 1)
        ]
        for key, rows in slices_by_state.items()
    }

    rows: list[dict[str, Any]] = []
    straddle = {"triples": 0, "single": 0, "pair": 0, "misses": []}

    for item in eligible_questions(domain):
        question = item["question"]
        phrases = item["phrases"]
        present = [any(p in text for text in norm_state.values()) for p in phrases]
        if not all(present):
            continue
        carriers = [k for k in states if any(p in norm_state[k] for p in phrases)]

        fat_state = any(all(p in norm_state[k] for p in phrases) for k in carriers)
        fat_chunk = any(
            all(p in text for p in phrases) for k in carriers for text in norm_chunks[k]
        )
        slice_single = False
        slice_pair = False
        best_single = 0
        best_pair = 0
        for k in carriers:
            for text in norm_slices.get(k, ()):
                cover = sum(1 for p in phrases if p in text)
                best_single = max(best_single, cover)
                if cover == len(phrases):
                    slice_single = True
            for text in norm_slice_pairs.get(k, ()):
                cover = sum(1 for p in phrases if p in text)
                best_pair = max(best_pair, cover)
                if cover == len(phrases):
                    slice_pair = True
            # straddle probe: every phrase that lives in this state's tree
            for phrase in phrases:
                if phrase not in norm_tree[k]:
                    continue
                straddle["triples"] += 1
                if any(phrase in text for text in norm_slices.get(k, ())):
                    straddle["single"] += 1
                    straddle["pair"] += 1
                elif any(phrase in text for text in norm_slice_pairs.get(k, ())):
                    straddle["pair"] += 1
                    straddle["misses"].append(
                        {
                            "question_id": question["id"],
                            "state": f"{k[0]}:{k[1]}",
                            "phrase": phrase[:140],
                            "recovered_by": "neighbour",
                        }
                    )
                else:
                    straddle["misses"].append(
                        {
                            "question_id": question["id"],
                            "state": f"{k[0]}:{k[1]}",
                            "phrase": phrase[:140],
                            "recovered_by": "none",
                        }
                    )
        rows.append(
            {
                "question_id": question["id"],
                "question_type": question.get("question_type"),
                "phrase_count": len(phrases),
                "carrier_states": len(carriers),
                "fat_state_hit": fat_state,
                "fat_chunk_hit": fat_chunk,
                "slice_single_hit": slice_single,
                "slice_pair_hit": slice_pair,
                "best_single_cover": best_single,
                "best_pair_cover": best_pair,
            }
        )

    measurable = len(rows)
    fat_pool = [r for r in rows if r["fat_state_hit"]]
    return {
        "domain": domain,
        "measurable_questions": measurable,
        "fat_state_exposable": len(fat_pool),
        "fat_chunk_exposable": sum(1 for r in rows if r["fat_chunk_hit"]),
        "slice_single_exposable": sum(1 for r in rows if r["slice_single_hit"]),
        "slice_pair_exposable": sum(1 for r in rows if r["slice_pair_hit"]),
        "oracle_ceiling_over_measurable": {
            "fat_state": round(len(fat_pool) / measurable, 4),
            "fat_chunk": round(sum(1 for r in rows if r["fat_chunk_hit"]) / measurable, 4),
            "slice_single": round(sum(1 for r in rows if r["slice_single_hit"]) / measurable, 4),
            "slice_pair": round(sum(1 for r in rows if r["slice_pair_hit"]) / measurable, 4),
        },
        "conditional_on_fat_state_exposable": {
            "n": len(fat_pool),
            "slice_single": round(
                sum(1 for r in fat_pool if r["slice_single_hit"]) / len(fat_pool), 4
            )
            if fat_pool
            else None,
            "slice_pair": round(sum(1 for r in fat_pool if r["slice_pair_hit"]) / len(fat_pool), 4)
            if fat_pool
            else None,
            "question_level_straddle_rate_single": round(
                1 - sum(1 for r in fat_pool if r["slice_single_hit"]) / len(fat_pool), 4
            )
            if fat_pool
            else None,
        },
        "phrase_level_straddle": {
            "question_phrase_state_triples": straddle["triples"],
            "in_one_slice": straddle["single"],
            "in_slice_plus_neighbour": straddle["pair"],
            "straddle_rate_single": round(1 - straddle["single"] / straddle["triples"], 6)
            if straddle["triples"]
            else None,
            "straddle_rate_pair": round(1 - straddle["pair"] / straddle["triples"], 6)
            if straddle["triples"]
            else None,
            "miss_total": len(straddle["misses"]),
            "miss_sample": straddle["misses"][:25],
        },
        "rows": rows,
    }


def main() -> None:
    report = {}
    for domain in ("enterprise", "web"):
        report[domain] = analyse(domain)
        data = report[domain]
        print(
            f"{domain}: measurable={data['measurable_questions']} "
            f"fat_state={data['fat_state_exposable']} fat_chunk={data['fat_chunk_exposable']} "
            f"slice1={data['slice_single_exposable']} slice2={data['slice_pair_exposable']} "
            f"phrase_triples={data['phrase_level_straddle']['question_phrase_state_triples']} "
            f"straddle_single={data['phrase_level_straddle']['straddle_rate_single']}",
            flush=True,
        )
    (OUT / "stage0d_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
