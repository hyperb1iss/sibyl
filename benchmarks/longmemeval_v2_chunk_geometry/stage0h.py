"""STAGE 0h - exposure when a window may not leave its evidence part.

`stage0e` measured the window-width oracle on **reassembled whole states**:
`stage0.load_states` joins every part of a state back into one `tree`, the
slicer cuts that tree, and the window slides across the whole state's slice
list. Shipped retrieval does neither of those things. Passages are minted one
evidence part at a time (`_passage_projection` calls `slice_body` on
`evidence.content`), and the window run key is `(observation_ordinal,
evidence_part_index)`, so a window can never span two parts of one state.

Most parts belong to a state that split, so the two measurements are different
questions and only the second describes what ships. This stage runs three arms
over the same questions and the same `measurable` denominator as `stage0e`:

  * `whole_state`   - stage0e's arm, recomputed here so the other two are read
                      against a baseline this file produced itself.
  * `part_confined` - the whole-state cut, windows restricted to slices lying
                      inside a single evidence part. Isolates the confinement
                      from the change in cut points.
  * `production`    - each part sliced on its own and windowed on its own,
                      which is what `nova/a1-passage-retrieval` ships.

The loss taxonomy separates confinement from everything else. A question no
window can reach because no single state carries all its gold, or because the
gold never lands in the passage substrate at all, was already lost before any
part boundary existed.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from itertools import combinations, pairwise
from typing import Any

from corpus import OUT, eligible_questions, normalize_text, state_evidence_text
from longmemeval_v2_diagnostics import is_exact_evidence_eligible
from slicer import render_slice, slice_body, slice_header
from stage0 import load_states

WINDOWS = (1, 2, 3, 5)
ARMS = ("whole_state", "part_confined", "production")

# The width the shipped window runs at (`PASSAGE_WINDOW_UNITS = 3`), used by
# the straddle probe below.
PROBE_WINDOW = 3

# `_passage_projection` drops a rendered passage this long rather than emit an
# over-cap entity. The report records the largest passage the corpus produces
# so the reader can see whether that drop is reachable at all.
MAX_TYPED_ENTITY_CONTENT_CHARS = 18_000


def part_line_spans(entry: dict[str, Any]) -> list[tuple[int, int]]:
    """Map each evidence part onto its half-open line range in the joined tree.

    `load_states` joins part bodies with no separator, so this is only exact
    while every non-final part ends on a newline. Both corpora satisfy that;
    the assertion keeps a corpus rebuild from silently invalidating the
    mapping, because a part ending mid-line would merge two lines across the
    boundary and no per-part cut could ever reproduce that text.
    """
    keys = sorted(entry["parts"])
    spans: list[tuple[int, int]] = []
    cursor = 0
    for position, key in enumerate(keys):
        body = entry["parts"][key]
        is_final = position == len(keys) - 1
        if not is_final:
            assert body.endswith("\n"), "non-final part does not end on a line boundary"
        consumed = len(body.split("\n")) - (0 if is_final else 1)
        spans.append((cursor, cursor + consumed))
        cursor += consumed
    assert cursor == len(entry["tree"].split("\n"))
    return spans


def whole_state_slices(entry: dict[str, Any]) -> tuple[list[str], list[frozenset[int]]]:
    """Render the whole-state cut and say which parts each slice's lines touch."""
    pieces, _ = slice_body(entry["tree"])
    spans = part_line_spans(entry)
    texts: list[str] = []
    touched: list[frozenset[int]] = []
    for order, piece in enumerate(pieces, start=1):
        header = slice_header(entry["state_index"], entry["url"], order, len(pieces))
        texts.append(render_slice(header, piece.breadcrumb, piece.content))
        touched.append(
            frozenset(
                index
                for index, (start, stop) in enumerate(spans)
                if any(start <= line < stop for line in piece.line_indices)
            )
        )
    return texts, touched


def production_slices(entry: dict[str, Any]) -> list[list[str]]:
    """Cut every evidence part on its own, the way the projection does."""
    per_part: list[list[str]] = []
    for key in sorted(entry["parts"]):
        pieces, _ = slice_body(entry["parts"][key])
        per_part.append(
            [
                render_slice(
                    slice_header(entry["state_index"], entry["url"], order, len(pieces)),
                    piece.breadcrumb,
                    piece.content,
                )
                for order, piece in enumerate(pieces, start=1)
            ]
        )
    return per_part


def _covers(texts: list[str], phrases: tuple[str, ...]) -> list[set[int]]:
    return [{index for index, phrase in enumerate(phrases) if phrase in text} for text in texts]


def _window_hit(covers: list[set[int]], width: int, total: int) -> bool:
    for start in range(max(1, len(covers) - width + 1)):
        merged: set[int] = set()
        for cover in covers[start : start + width]:
            merged |= cover
        if len(merged) == total:
            return True
    return False


def _runs_within_one_part(touched: list[frozenset[int]]) -> list[list[int]]:
    """Split the whole-state slice list into maximal runs sharing one part.

    A slice whose lines straddle a boundary touches two parts and joins no run:
    the per-part cut cannot produce it, so no confined window may use it.
    """
    runs: list[list[int]] = []
    current: list[int] = []
    current_part: int | None = None
    for index, parts in enumerate(touched):
        part = next(iter(parts)) if len(parts) == 1 else None
        if part is None or part != current_part:
            if current:
                runs.append(current)
            current = []
            current_part = part
        if part is not None:
            current.append(index)
    if current:
        runs.append(current)
    return runs


def _min_parts_to_cover(part_covers: list[set[int]], total: int) -> int | None:
    """Fewest evidence parts whose passages together carry every gold phrase."""
    carrying = [cover for cover in part_covers if cover]
    for size in range(1, len(carrying) + 1):
        for combo in combinations(carrying, size):
            merged: set[int] = set()
            for cover in combo:
                merged |= cover
            if len(merged) == total:
                return size
    return None


def _loss_reason(hits: dict[str, bool], arm: str, width: int) -> str:
    """Name what stopped this arm, distinguishing confinement from prior loss."""
    if hits[f"{arm}_w{width}"]:
        return "covered"
    if not hits["fat_state"]:
        return "no_single_state_carries_gold"
    if not hits["whole_state_all_slices"]:
        return "gold_outside_passage_substrate"
    if not hits["one_part_carries_gold"]:
        return "gold_spans_evidence_parts"
    return "window_too_narrow_within_part"


def _evaluate(
    phrases: tuple[str, ...],
    carriers: list[tuple[str, int]],
    norm_state: dict[tuple[str, int], str],
    whole_cut: dict[tuple[str, int], tuple[list[str], list[frozenset[int]]]],
    part_cut: dict[tuple[str, int], list[list[str]]],
) -> tuple[dict[str, bool], int | None]:
    """Score one question across all arms, and how many parts its gold needs."""
    total = len(phrases)
    hits = {f"{arm}_w{width}": False for arm in ARMS for width in WINDOWS}
    hits["fat_state"] = False
    hits["whole_state_all_slices"] = False
    hits["one_part_carries_gold"] = False
    min_parts: int | None = None

    for key in carriers:
        whole_complete = all(phrase in norm_state[key] for phrase in phrases)
        if whole_complete:
            hits["fat_state"] = True

        whole_texts, touched = whole_cut[key]
        whole_covers = _covers(whole_texts, phrases)
        union: set[int] = set()
        for cover in whole_covers:
            union |= cover
        if len(union) == total:
            hits["whole_state_all_slices"] = True
        for width in WINDOWS:
            if _window_hit(whole_covers, width, total):
                hits[f"whole_state_w{width}"] = True
        for run in _runs_within_one_part(touched):
            run_covers = [whole_covers[index] for index in run]
            for width in WINDOWS:
                if _window_hit(run_covers, width, total):
                    hits[f"part_confined_w{width}"] = True

        per_part_union: list[set[int]] = []
        for group in part_cut[key]:
            part_covers = _covers(group, phrases)
            part_union: set[int] = set()
            for cover in part_covers:
                part_union |= cover
            per_part_union.append(part_union)
            if len(part_union) == total:
                hits["one_part_carries_gold"] = True
            for width in WINDOWS:
                if _window_hit(part_covers, width, total):
                    hits[f"production_w{width}"] = True
        if whole_complete:
            needed = _min_parts_to_cover(per_part_union, total)
            if needed is not None:
                min_parts = needed if min_parts is None else min(min_parts, needed)

    return hits, min_parts


def _probe_phrase(lines: list[str]) -> str | None:
    """First line in this order that would pass the real gold-eligibility rule."""
    for line in lines:
        candidate = normalize_text(line)
        if is_exact_evidence_eligible(candidate):
            return candidate
    return None


def boundary_probe(
    states: dict[tuple[str, int], dict[str, Any]],
    norm_state: dict[tuple[str, int], str],
    whole_cut: dict[tuple[str, int], tuple[list[str], list[frozenset[int]]]],
    part_cut: dict[tuple[str, int], list[list[str]]],
) -> dict[str, Any]:
    """Positive control: synthetic gold placed deliberately across a boundary.

    A zero delta on the real questions only means something if this harness can
    register a delta at all. Each usable probe takes the last eligible line
    before a part boundary and the first eligible line after it, keeps the pair
    only when each occurs exactly once in the whole state, and scores it through
    the same three arms. A confined window cannot reach both by construction, so
    a working harness must show `whole_state` high and the confined arms at
    zero. If all three agreed here, the zero measured above would be an artifact.
    """
    attempted = 0
    usable = 0
    hits: Counter = Counter()
    for key, entry in states.items():
        part_keys = sorted(entry["parts"])
        for position in range(len(part_keys) - 1):
            attempted += 1
            before = entry["parts"][part_keys[position]].split("\n")
            after = entry["parts"][part_keys[position + 1]].split("\n")
            phrase_a = _probe_phrase(before[::-1])
            phrase_b = _probe_phrase(after)
            if phrase_a is None or phrase_b is None or phrase_a == phrase_b:
                continue
            state_text = norm_state[key]
            if state_text.count(phrase_a) != 1 or state_text.count(phrase_b) != 1:
                continue
            usable += 1
            phrases = (phrase_a, phrase_b)
            whole_texts, touched = whole_cut[key]
            whole_covers = _covers(whole_texts, phrases)
            if _window_hit(whole_covers, PROBE_WINDOW, 2):
                hits["whole_state"] += 1
            if any(
                _window_hit([whole_covers[index] for index in run], PROBE_WINDOW, 2)
                for run in _runs_within_one_part(touched)
            ):
                hits["part_confined"] += 1
            if any(
                _window_hit(_covers(group, phrases), PROBE_WINDOW, 2) for group in part_cut[key]
            ):
                hits["production"] += 1
    return {
        "boundaries": attempted,
        "usable_probes": usable,
        "window": PROBE_WINDOW,
        "hits": {arm: hits[arm] for arm in ARMS},
        "rates": {arm: round(hits[arm] / usable, 4) if usable else None for arm in ARMS},
    }


def analyse(domain: str) -> dict[str, Any]:
    states = load_states(domain)
    norm_state = {key: normalize_text(state_evidence_text(entry)) for key, entry in states.items()}
    whole_cut: dict[tuple[str, int], tuple[list[str], list[frozenset[int]]]] = {}
    part_cut: dict[tuple[str, int], list[list[str]]] = {}
    straddling_slices = 0
    total_slices = 0
    production_slice_total = 0
    longest_production_slice = 0
    for key, entry in states.items():
        texts, touched = whole_state_slices(entry)
        whole_cut[key] = ([normalize_text(text) for text in texts], touched)
        straddling_slices += sum(1 for parts in touched if len(parts) > 1)
        total_slices += len(texts)
        groups = production_slices(entry)
        production_slice_total += sum(len(group) for group in groups)
        longest_production_slice = max(
            longest_production_slice,
            max((len(text) for group in groups for text in group), default=0),
        )
        part_cut[key] = [[normalize_text(text) for text in group] for group in groups]

    counts: dict[str, int] = defaultdict(int)
    reasons: dict[str, Counter] = {f"{arm}_w{w}": Counter() for arm in ARMS for w in WINDOWS}
    min_parts_hist: Counter = Counter()
    confinement_losses: list[dict[str, Any]] = []
    width_gainers: dict[str, list[str]] = {f"{arm}_w{w}": [] for arm in ARMS for w in WINDOWS[1:]}
    phrase_counts: Counter = Counter()
    measurable = 0

    for item in eligible_questions(domain):
        phrases = item["phrases"]
        if not all(any(phrase in text for text in norm_state.values()) for phrase in phrases):
            continue
        measurable += 1
        phrase_counts[len(phrases)] += 1
        carriers = [key for key in states if any(p in norm_state[key] for p in phrases)]
        hits, min_parts = _evaluate(phrases, carriers, norm_state, whole_cut, part_cut)

        for arm in ARMS:
            for narrow, wide in pairwise(WINDOWS):
                if hits[f"{arm}_w{wide}"] and not hits[f"{arm}_w{narrow}"]:
                    width_gainers[f"{arm}_w{wide}"].append(item["question"]["id"])
        for name, value in hits.items():
            if value:
                counts[name] += 1
        if hits["fat_state"]:
            min_parts_hist[min_parts if min_parts is not None else "unreachable"] += 1
        for arm in ARMS:
            for width in WINDOWS:
                reasons[f"{arm}_w{width}"][_loss_reason(hits, arm, width)] += 1
        for width in WINDOWS:
            if hits[f"whole_state_w{width}"] and not hits[f"production_w{width}"]:
                confinement_losses.append(
                    {
                        "question_id": item["question"]["id"],
                        "window": width,
                        "phrase_count": len(phrases),
                        "reason": _loss_reason(hits, "production", width),
                        "min_parts_to_cover": min_parts,
                    }
                )

    rates = {name: round(value / measurable, 4) for name, value in sorted(counts.items())}
    delta = {
        f"w{width}": round(
            (counts[f"production_w{width}"] - counts[f"whole_state_w{width}"]) / measurable, 4
        )
        for width in WINDOWS
    }
    return {
        "domain": domain,
        "measurable": measurable,
        "states": len(states),
        "multi_part_states": sum(1 for e in states.values() if len(e["parts"]) > 1),
        "parts_total": sum(len(e["parts"]) for e in states.values()),
        "parts_in_multi_part_states": sum(
            len(e["parts"]) for e in states.values() if len(e["parts"]) > 1
        ),
        "whole_state_slices": total_slices,
        "production_slices": production_slice_total,
        "boundary_straddling_slices": straddling_slices,
        "longest_production_slice_chars": longest_production_slice,
        "typed_entity_content_cap_chars": MAX_TYPED_ENTITY_CONTENT_CHARS,
        "boundary_probe": boundary_probe(states, norm_state, whole_cut, part_cut),
        "counts": dict(sorted(counts.items())),
        "rates": rates,
        "delta_production_minus_whole_state": delta,
        "phrase_count_distribution": {str(k): v for k, v in sorted(phrase_counts.items())},
        "width_gainers": {name: sorted(ids) for name, ids in sorted(width_gainers.items())},
        "loss_reasons": {name: dict(counter) for name, counter in sorted(reasons.items())},
        "parts_needed_to_cover_gold": {
            str(k): v for k, v in sorted(min_parts_hist.items(), key=str)
        },
        "confinement_losses": confinement_losses,
    }


def main() -> None:
    report: dict[str, Any] = {}
    for domain in ("enterprise", "web"):
        data = analyse(domain)
        report[domain] = data
        print(f"\n### {domain}  measurable={data['measurable']}")
        print(
            f"states={data['states']} multi_part={data['multi_part_states']} "
            f"parts={data['parts_total']} "
            f"parts_in_multi_part_states={data['parts_in_multi_part_states']}"
        )
        print(
            f"slices whole={data['whole_state_slices']} "
            f"production={data['production_slices']} "
            f"boundary_straddling={data['boundary_straddling_slices']}"
        )
        for arm in ARMS:
            row = " ".join(f"w{w}={data['rates'].get(f'{arm}_w{w}', 0.0):.4f}" for w in WINDOWS)
            print(f"  {arm:<14} {row}")
        print(f"  delta production-whole {data['delta_production_minus_whole_state']}")
        print(f"  phrases/question {data['phrase_count_distribution']}")
        gainers = {k: v for k, v in data["width_gainers"].items() if v}
        print(f"  questions bought by extra width {gainers}")
        print(f"  parts needed to cover gold {data['parts_needed_to_cover_gold']}")
        print(f"  loss w3 production {data['loss_reasons']['production_w3']}")
        probe = data["boundary_probe"]
        print(
            f"  straddle probe w{probe['window']} "
            f"usable={probe['usable_probes']}/{probe['boundaries']} {probe['rates']}"
        )
        print(
            f"  longest production passage {data['longest_production_slice_chars']} chars "
            f"vs cap {data['typed_entity_content_cap_chars']}"
        )
    (OUT / "stage0h_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
