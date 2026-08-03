"""STAGE 2 - the pack-composition knobs on a passage substrate.

Every Stage 0 number is an **oracle** number: does some slice, or some window
of slices, carry the gold at all. A gate run does not read an oracle. It reads
the pack a ranker filled under two caps that were both tuned when one retrieval
unit was one whole state, and this stage measures what those caps do once the
unit is a ~1,030-char passage.

**Which caps actually bind was not obvious, and the answer moved the
measurement.** Three code reads over the eight-PR A1 stack say:

  * `max_chunks_per_trajectory` reaches the server as `max_results_per_source`,
    but `_fuse_context_evidence` is the only consumer and it runs on the
    **accurate** path alone. A FAST gate run - the campaign's winning
    configuration - puts the value on the wire and nothing reads it. Its one
    live enforcement is the eval adapter's `_select_diverse_results`, which
    drops over-cap rows outright with no backfill, unlike the server's
    defer-then-backfill.
  * `seed_candidates_per_signal` bounds `build_context_retrieval_plan`, which
    serves the **context-pack facet sections**. The evidence lane does not go
    that way: `/context/pack` builds a `SearchRequest` and runs
    `execute_search_request` -> `tools.search.search` -> `hybrid_search`, where
    candidate depth is `limit * 3` and no `CandidateLimits` object exists.
    Passages appear in no `FACET_TYPES` entry, and `context_pack_to_search_results`
    admits only note / procedure / error_pattern / event, so no passage can
    reach the pack through the lane that constant governs.
  * What plays the seed-budget role for passages is the evidence lane's own
    `limit`, `min(max(search_limit, max_context_items), 50)`. `tools.search`
    returns `all_results[offset : offset + limit]`, so that number is a hard
    ceiling on how many passages exist to be capped, composed, or read.

So the sweep is `max_chunks_per_trajectory` x **evidence-lane depth**, and the
per-lane seed-budget result is a null one recorded against the code.

Two arms, because a cap and a ranker fail differently:

  * `cap_ceiling` is ranking-free. For a cap T it asks whether *any* selection
    holding at most T passages per trajectory covers the gold, by exact search
    over per-trajectory coverage masks. Whatever this arm cannot reach, no
    ranker can reach either, so it is the honest ceiling of the knob.
  * `bm25` applies the same caps to a real ranking. Its exposure is a floor,
    not an estimate: the shipped lane is `hybrid_search`, vector-weighted and
    graph-expanded, and Stage 1 already measured BM25 alone as the weakest
    ranker on a sliced substrate. A dense arm would move this arm's absolute
    numbers and cannot move `cap_ceiling`, which is why the knob recommendation
    is read off the ceiling and the depth recommendation is read off BM25 as a
    conservative bound.

The loss taxonomy separates the knob from everything else. Gold that is in no
passage was lost at ingest, gold the pool never reached was lost by the ranker
or the depth, and only gold that the pool held and the cap then dropped is
caused by the cap.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from corpus import OUT, eligible_questions, normalize_text, state_evidence_text
from stage0 import load_states
from stage0h import production_slices

# The staged gate geometry. Item count and character budget are held fixed so
# the grid moves only the two knobs under test.
GATE_MAX_CONTEXT_ITEMS = 28
GATE_CHAR_BUDGET = 48_000

# `max_chunks_per_trajectory`. 2 is the shipped default, 8 is the value the
# staged gate command guesses, None is uncapped.
TRAJECTORY_CAPS: tuple[int | None, ...] = (1, 2, 4, 8, 16, None)

# Evidence-lane depth: the `limit` the pack request carries, which is what
# the per-lane seed budget was believed to be. 28 is the staged geometry
# (`max_context_items` beats the default `search_limit` of 12); 50 is the
# schema maximum, reachable with `--search-limit 50`.
POOL_DEPTHS = (8, 16, 24, 28, 50)

# `TYPED_NOTE_RESERVATION_ITEMS`, pinned absolutely rather than proportionally.
# The note lane spends its characters before the passage lane sees any, so its
# per-note size is subtracted from the budget the passages compete for.
NOTE_RESERVATION_ITEMS = 3
NOTE_SIZE_PROBES = (0, 1_000, 2_000, 4_000)

# The staged 48,000 is not the largest legal budget. `validate_evidence_char_budget`
# rejects a budget above `max_context_total_chars`, which defaults to 60,000, and
# 60,000 is what the fat baseline's renderer already spends. A substrate arm
# budgeted below its comparator is handicapped before any knob is set, so the
# budget is probed here rather than assumed.
CHAR_BUDGET_PROBES = (24_000, 48_000, 60_000)

# Same scoring constants as `stage1.py`, so a BM25 number here is readable
# against the Stage 1 tables rather than being a second private convention.
BM25_K1 = 1.2
BM25_B = 0.75

COMMITTED_RECEIPT = Path(__file__).resolve().parent / "out" / "stage0h_report.json"
BASELINE_WINDOW = 3

# `out/stage1_summary.txt`, SLICE arm, bm25 ranker, recall@50. Stage 1 cut whole
# reassembled states rather than one evidence part at a time, so this is a
# near-match rather than an identity: a second independent check that this
# file's stdlib BM25 is the same scorer Stage 1 ran through scikit-learn.
STAGE1_SLICE_BM25_AT_50 = {"enterprise": 0.204, "web": 0.167}


def build_passages(
    states: dict[Any, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[int]]]:
    """Cut the corpus the way the projection does and index every passage.

    `production_slices` is `stage0h`'s arm verbatim - each evidence part cut on
    its own - so the substrate this stage composes packs from is the same one
    the part-confined oracle scored. That shared cut is what lets the baseline
    below reproduce a committed number rather than merely resemble it.
    """
    passages: list[dict[str, Any]] = []
    parts: list[list[int]] = []
    for key in sorted(states):
        entry = states[key]
        for part_index, group in enumerate(production_slices(entry)):
            indices: list[int] = []
            for passage_index, rendered in enumerate(group):
                indices.append(len(passages))
                passages.append(
                    {
                        "trajectory": entry["trajectory_id"],
                        "state": entry["state_index"],
                        "part": part_index,
                        "passage": passage_index,
                        "text": normalize_text(rendered),
                        "chars": len(rendered),
                    }
                )
            parts.append(indices)
    return passages, parts


def measurable_questions(
    domain: str,
    states: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The `stage0h` denominator: phrase-eligible and carried by some state."""
    norm_state = [normalize_text(state_evidence_text(entry)) for entry in states.values()]
    out: list[dict[str, Any]] = []
    for item in eligible_questions(domain):
        phrases = item["phrases"]
        if all(any(phrase in text for text in norm_state) for phrase in phrases):
            out.append(item)
    return out


def passage_covers(passages: list[dict[str, Any]], phrases: tuple[str, ...]) -> dict[int, int]:
    """Map each carrying passage onto the bitmask of phrases it holds."""
    covers: dict[int, int] = {}
    for index, passage in enumerate(passages):
        text = passage["text"]
        mask = 0
        for position, phrase in enumerate(phrases):
            if phrase in text:
                mask |= 1 << position
        if mask:
            covers[index] = mask
    return covers


def _window_covers(indices: list[int], covers: dict[int, int], full: int, width: int) -> bool:
    for start in range(max(1, len(indices) - width + 1)):
        merged = 0
        for index in indices[start : start + width]:
            merged |= covers.get(index, 0)
        if merged == full:
            return True
    return False


def baseline(
    domain: str,
    passages: list[dict[str, Any]],
    parts: list[list[int]],
    questions: list[dict[str, Any]],
    covers_by_question: list[dict[int, int]],
) -> dict[str, Any]:
    """Reproduce committed `stage0h` numbers from this stage's own structures.

    A grid of new arms is only worth reading if the substrate underneath it is
    the substrate the campaign already measured. This recomputes the passage
    count, the question denominator, and the single-passage and 3-adjacent
    production exposure rates from the data structures this file builds, then
    compares them to `out/stage0h_report.json`. A mismatch means the corpus,
    the cutter, or the eligibility rule moved, and every number below is void.
    """
    committed = json.loads(COMMITTED_RECEIPT.read_text())[domain]
    single = 0
    windowed = 0
    for item, covers in zip(questions, covers_by_question, strict=True):
        full = (1 << len(item["phrases"])) - 1
        if any(mask == full for mask in covers.values()):
            single += 1
        if any(_window_covers(indices, covers, full, BASELINE_WINDOW) for indices in parts):
            windowed += 1
    measured = {
        "production_slices": len(passages),
        "measurable": len(questions),
        "production_w1": round(single / len(questions), 4),
        f"production_w{BASELINE_WINDOW}": round(windowed / len(questions), 4),
    }
    expected = {
        "production_slices": committed["production_slices"],
        "measurable": committed["measurable"],
        "production_w1": committed["rates"]["production_w1"],
        f"production_w{BASELINE_WINDOW}": committed["rates"][f"production_w{BASELINE_WINDOW}"],
    }
    return {
        "source": "out/stage0h_report.json",
        "measured": measured,
        "committed": expected,
        "reproduces": measured == expected,
    }


def _reachable_masks(patterns: set[int], cap: int | None, phrase_count: int) -> dict[int, int]:
    """Coverage masks one trajectory can reach, and the fewest passages each costs."""
    steps = phrase_count if cap is None else min(cap, phrase_count)
    reachable = {0: 0}
    for _ in range(steps):
        for mask, cost in list(reachable.items()):
            for pattern in patterns:
                merged = mask | pattern
                if reachable.get(merged, math.inf) > cost + 1:
                    reachable[merged] = cost + 1
    return reachable


def cap_feasibility(
    covers: dict[int, int],
    passages: list[dict[str, Any]],
    phrase_count: int,
    cap: int | None,
) -> int | None:
    """Fewest passages covering every phrase with at most `cap` per trajectory.

    Exact rather than greedy. Choosing each trajectory's largest coverage mask
    is not optimal - a trajectory that can reach either {A,B} or {C,D} must
    pick the one its neighbours do not already hold - so this carries the whole
    reachable set forward and lets the join decide. The state space is bounded
    by 2**phrase_count, at most 32 here.
    """
    full = (1 << phrase_count) - 1
    patterns_by_trajectory: dict[str, set[int]] = defaultdict(set)
    for index, mask in covers.items():
        patterns_by_trajectory[passages[index]["trajectory"]].add(mask)
    best = {0: 0}
    for patterns in patterns_by_trajectory.values():
        reachable = _reachable_masks(patterns, cap, phrase_count)
        merged_best: dict[int, int] = {}
        for state, spent in best.items():
            for mask, cost in reachable.items():
                key = state | mask
                if merged_best.get(key, math.inf) > spent + cost:
                    merged_best[key] = spent + cost
        best = merged_best
        if full in best:
            return best[full]
    return best.get(full)


def passages_of_one_trajectory_needed(
    covers: dict[int, int],
    passages: list[dict[str, Any]],
    phrase_count: int,
) -> int | None:
    """Fewest passages of a single trajectory that carry every phrase."""
    full = (1 << phrase_count) - 1
    patterns_by_trajectory: dict[str, set[int]] = defaultdict(set)
    for index, mask in covers.items():
        patterns_by_trajectory[passages[index]["trajectory"]].add(mask)
    cheapest: int | None = None
    for patterns in patterns_by_trajectory.values():
        cost = _reachable_masks(patterns, None, phrase_count).get(full)
        if cost is not None:
            cheapest = cost if cheapest is None else min(cheapest, cost)
    return cheapest


def mechanism(
    questions: list[dict[str, Any]],
    passages: list[dict[str, Any]],
    covers_by_question: list[dict[int, int]],
) -> dict[str, Any]:
    """Where the gold sits, independent of any ranking.

    The per-trajectory cap can only exclude gold that a question must draw from
    one trajectory more than `cap` times. This counts exactly that, plus how
    much room the question has to route around the cap: a question several
    trajectories can each answer alone is insensitive to the cap in a way that
    a question with a single carrier is not.
    """
    carriers: Counter = Counter()
    solo_covering: Counter = Counter()
    within_one: Counter = Counter()
    required_cap: Counter = Counter()
    min_items: Counter = Counter()
    for item, covers in zip(questions, covers_by_question, strict=True):
        phrase_count = len(item["phrases"])
        full = (1 << phrase_count) - 1
        patterns_by_trajectory: dict[str, set[int]] = defaultdict(set)
        for index, mask in covers.items():
            patterns_by_trajectory[passages[index]["trajectory"]].add(mask)
        carriers[len(patterns_by_trajectory)] += 1

        solo = 0
        cheapest: int | None = None
        for patterns in patterns_by_trajectory.values():
            reachable = _reachable_masks(patterns, None, phrase_count)
            cost = reachable.get(full)
            if cost is None:
                continue
            solo += 1
            cheapest = cost if cheapest is None else min(cheapest, cost)
        solo_covering[solo] += 1
        within_one[cheapest if cheapest is not None else "no_single_trajectory_covers"] += 1

        smallest: int | str = "unreachable"
        for cap in range(1, phrase_count + 1):
            spent = cap_feasibility(covers, passages, phrase_count, cap)
            if spent is not None:
                smallest = cap
                min_items[spent] += 1
                break
        required_cap[smallest] += 1
    return {
        "gold_carrying_trajectories": _histogram(carriers),
        "trajectories_covering_all_gold_alone": _histogram(solo_covering),
        "passages_of_one_trajectory_needed": _histogram(within_one),
        "smallest_trajectory_cap_that_reaches_gold": _histogram(required_cap),
        "passages_the_cheapest_covering_selection_uses": _histogram(min_items),
    }


def _histogram(counter: Counter) -> dict[str, int]:
    return {
        str(key): value for key, value in sorted(counter.items(), key=lambda pair: str(pair[0]))
    }


def build_bm25(
    passages: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> tuple[dict[str, list[tuple[int, int]]], dict[str, float], list[int], float]:
    """Index only the terms some question asks for.

    The full inverted index over 85k passages is never needed: BM25 touches a
    document only through the query terms it contains, and every query is known
    before the first pass. Restricting the postings to that vocabulary is what
    keeps this arm inside the stdlib tier.
    """
    vocabulary = set()
    for item in questions:
        vocabulary.update(normalize_text(item["question"]["question"]).split())
    postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
    lengths: list[int] = []
    for index, passage in enumerate(passages):
        counts: Counter = Counter(passage["text"].split())
        lengths.append(sum(counts.values()))
        for term, frequency in counts.items():
            if term in vocabulary:
                postings[term].append((index, frequency))
    document_count = len(passages)
    idf = {
        term: math.log(
            1.0 + (document_count - len(entries) + 0.5) / (len(entries) + 0.5),
        )
        for term, entries in postings.items()
    }
    average_length = sum(lengths) / max(1, len(lengths))
    return postings, idf, lengths, average_length


def bm25_order(
    query: str,
    depth: int,
    postings: dict[str, list[tuple[int, int]]],
    idf: dict[str, float],
    lengths: list[int],
    average_length: float,
) -> list[int]:
    """Top-`depth` passages, ties broken by index as a stable argsort would."""
    scores: dict[int, float] = defaultdict(float)
    for term in set(query.split()):
        entries = postings.get(term)
        if not entries:
            continue
        weight = idf[term]
        for index, frequency in entries:
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * lengths[index] / average_length
            )
            scores[index] += weight * frequency * (BM25_K1 + 1) / denominator
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [index for index, _score in ranked[:depth]]


def select_diverse(
    pool: list[int],
    passages: list[dict[str, Any]],
    cap: int | None,
    limit: int,
) -> list[int]:
    """`sibyl_memory._select_diverse_results`, over passage rows.

    Two passes: the first admits at most one row per trajectory, the second
    fills up to `cap`. The second pass also refuses a row whose diversity key
    is already present, but a passage carries no `longmemeval_v2_state_index` -
    the projection stamps the experience's metadata block, which has the
    trajectory id and not the state - so its key falls back to the row id and
    that guard never fires. Over-cap rows are dropped, not deferred: the
    server's backfill lives in `_fuse_context_evidence`, which only the
    accurate path calls.

    That fallback is a key-name mismatch rather than missing information, and
    the difference is a live hazard. `_passage_projection` does stamp
    `observation_ordinal`, and the eval sets an observation's ordinal to the
    state index, so the state a passage came from is recoverable - which is
    exactly what `_source_support_state` already does for the source-support
    path. Teaching `_result_diversity_key` that trick would make the second
    pass admit **one passage per state**, a far tighter bound than any value of
    `max_chunks_per_trajectory`, and it would look like a metadata cleanup.
    """
    selected: list[int] = []
    seen: set[int] = set()
    counts: Counter = Counter()
    for trajectory_pass in (True, False):
        for index in pool:
            if index in seen:
                continue
            trajectory = passages[index]["trajectory"]
            if cap is not None and counts[trajectory] >= cap:
                continue
            if trajectory_pass and counts[trajectory] > 0:
                continue
            selected.append(index)
            seen.add(index)
            counts[trajectory] += 1
            if len(selected) >= limit:
                return selected
    return selected


def admit_within_budget(
    indices: list[int],
    passages: list[dict[str, Any]],
    budget: int,
) -> tuple[list[int], int]:
    """Longest rank prefix that fits, stopping at the first row that does not."""
    admitted: list[int] = []
    spent = 0
    for index in indices:
        chars = passages[index]["chars"]
        if spent + chars > budget:
            break
        admitted.append(index)
        spent += chars
    return admitted, spent


def compose_pack(
    order: list[int],
    passages: list[dict[str, Any]],
    cap: int | None,
    depth: int,
    budget: int,
) -> tuple[list[int], list[int], int]:
    """Search depth, then the trajectory cap, then the character budget."""
    pool = order[:depth]
    # `context_pack_item_ceiling` raises the item ceiling to the candidate count
    # whenever a character budget is in force, so the composer is the only
    # place the pack is bounded and this ceiling cannot bind.
    seed_limit = max(GATE_MAX_CONTEXT_ITEMS, len(pool))
    selected = select_diverse(pool, passages, cap, seed_limit)
    pack, spent = admit_within_budget(selected, passages, budget)
    return selected, pack, spent


def _merged_cover(indices: list[int], covers: dict[int, int]) -> int:
    merged = 0
    for index in indices:
        merged |= covers.get(index, 0)
    return merged


def _loss_reason(
    *,
    covered: bool,
    in_corpus: bool,
    in_pool: bool,
    survives_cap: bool,
) -> str:
    if covered:
        return "covered"
    if not in_corpus:
        return "gold_outside_passage_substrate"
    if not in_pool:
        return "search_depth_never_reached_gold"
    if not survives_cap:
        return "trajectory_cap_excluded_reachable_gold"
    return "char_budget_excluded_reachable_gold"


def run_grid(
    questions: list[dict[str, Any]],
    passages: list[dict[str, Any]],
    covers_by_question: list[dict[int, int]],
    orders: list[list[int]],
) -> dict[str, Any]:
    """Exposure over the cap x depth grid, with the loss cause for each miss."""
    grid: dict[str, Any] = {}
    corpus_masks = [_merged_cover(list(covers), covers) for covers in covers_by_question]
    for cap in TRAJECTORY_CAPS:
        for depth in POOL_DEPTHS:
            hits = 0
            reasons: Counter = Counter()
            pack_items = 0
            pack_chars = 0
            pack_trajectories = 0
            for position, item in enumerate(questions):
                covers = covers_by_question[position]
                order = orders[position]
                full = (1 << len(item["phrases"])) - 1
                selected, pack, spent = compose_pack(order, passages, cap, depth, GATE_CHAR_BUDGET)
                covered = _merged_cover(pack, covers) == full
                hits += covered
                pack_items += len(pack)
                pack_chars += spent
                pack_trajectories += len({passages[index]["trajectory"] for index in pack})
                reasons[
                    _loss_reason(
                        covered=covered,
                        in_corpus=corpus_masks[position] == full,
                        in_pool=_merged_cover(order[:depth], covers) == full,
                        survives_cap=_merged_cover(selected, covers) == full,
                    )
                ] += 1
            key = f"cap={'none' if cap is None else cap}|depth={depth}"
            grid[key] = {
                "exposure": round(hits / len(questions), 4),
                "mean_pack_items": round(pack_items / len(questions), 2),
                "mean_pack_trajectories": round(pack_trajectories / len(questions), 2),
                "mean_pack_chars": round(pack_chars / len(questions), 1),
                "budget_headroom_chars": round(
                    GATE_CHAR_BUDGET - pack_chars / len(questions),
                    1,
                ),
                "loss_reasons": dict(sorted(reasons.items())),
            }
    return grid


def char_budget_probe(
    questions: list[dict[str, Any]],
    passages: list[dict[str, Any]],
    covers_by_question: list[dict[int, int]],
    orders: list[list[int]],
    cap: int | None,
    depth: int,
) -> dict[str, Any]:
    """Exposure and payload against the budget, at the widest knob setting."""
    out: dict[str, Any] = {}
    for budget in CHAR_BUDGET_PROBES:
        hits = 0
        items = 0
        chars = 0
        for item, covers, order in zip(questions, covers_by_question, orders, strict=True):
            full = (1 << len(item["phrases"])) - 1
            _selected, pack, spent = compose_pack(order, passages, cap, depth, budget)
            hits += _merged_cover(pack, covers) == full
            items += len(pack)
            chars += spent
        out[str(budget)] = {
            "exposure": round(hits / len(questions), 4),
            "mean_pack_items": round(items / len(questions), 2),
            "mean_pack_chars": round(chars / len(questions), 1),
        }
    return out


def note_lane_interaction(
    questions: list[dict[str, Any]],
    passages: list[dict[str, Any]],
    covers_by_question: list[dict[int, int]],
    orders: list[list[int]],
    cap: int | None,
    depth: int,
) -> dict[str, Any]:
    """What the pinned note lane costs the passage lane at one grid point.

    The note reservation is an absolute count of items, so its cost to the
    passage lane is a number of characters nobody has measured on this corpus -
    the distilled notes are not in the frozen catalogs. This sweeps plausible
    per-note sizes instead, which is enough to say whether the coupling can
    reach the passage lane at all at this budget.
    """
    out: dict[str, Any] = {}
    for note_chars in NOTE_SIZE_PROBES:
        budget = GATE_CHAR_BUDGET - NOTE_RESERVATION_ITEMS * note_chars
        hits = 0
        for item, covers, order in zip(questions, covers_by_question, orders, strict=True):
            full = (1 << len(item["phrases"])) - 1
            _selected, pack, _spent = compose_pack(order, passages, cap, depth, budget)
            hits += _merged_cover(pack, covers) == full
        out[str(note_chars)] = {
            "passage_budget_chars": budget,
            "exposure": round(hits / len(questions), 4),
        }
    return out


def analyse(domain: str) -> dict[str, Any]:
    states = load_states(domain)
    passages, parts = build_passages(states)
    questions = measurable_questions(domain, states)
    covers_by_question = [passage_covers(passages, item["phrases"]) for item in questions]

    solo_cost = [
        passages_of_one_trajectory_needed(covers, passages, len(item["phrases"]))
        for item, covers in zip(questions, covers_by_question, strict=True)
    ]
    ceiling: dict[str, Any] = {}
    for cap in TRAJECTORY_CAPS:
        reached = 0
        spends: list[int] = []
        for item, covers in zip(questions, covers_by_question, strict=True):
            spent = cap_feasibility(covers, passages, len(item["phrases"]), cap)
            if spent is not None:
                reached += 1
                spends.append(spent)
        # The single-source arm is the one that speaks to answerability: it
        # requires every phrase to come from one trajectory, the way `stage0d`
        # and `stage0h` required one carrier state. The any-source arm above
        # lets phrases be assembled from unrelated trajectories, which is a
        # true statement about the pack's text and a weak one about whether a
        # reader could have answered from it.
        solo_reached = sum(
            1 for cost in solo_cost if cost is not None and (cap is None or cost <= cap)
        )
        ceiling[f"cap={'none' if cap is None else cap}"] = {
            "exposure_any_source": round(reached / len(questions), 4),
            "exposure_single_source": round(solo_reached / len(questions), 4),
            "max_passages_a_covering_selection_needs": max(spends, default=0),
        }

    postings, idf, lengths, average_length = build_bm25(passages, questions)
    orders = [
        bm25_order(
            normalize_text(item["question"]["question"]),
            max(POOL_DEPTHS),
            postings,
            idf,
            lengths,
            average_length,
        )
        for item in questions
    ]

    grid = run_grid(questions, passages, covers_by_question, orders)
    checks = baseline(domain, passages, parts, questions, covers_by_question)
    checks["stage1_bm25_slice_at_50"] = {
        "source": "out/stage1_summary.txt",
        "measured": grid[f"cap=none|depth={max(POOL_DEPTHS)}"]["exposure"],
        "committed": STAGE1_SLICE_BM25_AT_50[domain],
    }

    chars = [passage["chars"] for passage in passages]
    return {
        "domain": domain,
        "baseline": checks,
        "shipped_enforcement": {
            "max_chunks_per_trajectory": (
                "wire value reaches `max_results_per_source`, whose only consumer "
                "is `_fuse_context_evidence` on the accurate path; a fast-mode run "
                "enforces it solely in the adapter's `_select_diverse_results`"
            ),
            "max_candidates_per_signal": (
                "bounds `build_context_retrieval_plan`, which serves context-pack "
                "facet sections; the evidence lane runs `execute_search_request` -> "
                "`tools.search.search` -> `hybrid_search` and never constructs a "
                "`CandidateLimits`, and no `FACET_TYPES` entry names a passage"
            ),
            "evidence_lane_depth": (
                "`min(max(search_limit, max_context_items), 50)`, applied as "
                "`all_results[offset : offset + limit]`; this is the seed budget "
                "the passage lane actually has"
            ),
            "passage_window_units": (
                "`select_operational_source_span` is reached only through "
                "`expand_operational_source_evidence`, whose single call site is "
                "the accurate path, so a fast-mode pack holds individually ranked "
                "passages rather than 3-adjacent windows"
            ),
        },
        "substrate": {
            "passages": len(passages),
            "trajectories": len({passage["trajectory"] for passage in passages}),
            "evidence_parts": len(parts),
            "mean_passage_chars": round(sum(chars) / len(chars), 1),
            "max_passage_chars": max(chars),
            "measurable": len(questions),
        },
        "gate_geometry": {
            "max_context_items": GATE_MAX_CONTEXT_ITEMS,
            "char_budget": GATE_CHAR_BUDGET,
            "note_reservation_items": NOTE_RESERVATION_ITEMS,
        },
        "mechanism": mechanism(questions, passages, covers_by_question),
        "cap_ceiling": ceiling,
        "bm25_grid": grid,
        "char_budget_probe": char_budget_probe(
            questions,
            passages,
            covers_by_question,
            orders,
            cap=None,
            depth=max(POOL_DEPTHS),
        ),
        "note_lane_interaction": note_lane_interaction(
            questions,
            passages,
            covers_by_question,
            orders,
            cap=8,
            depth=28,
        ),
    }


def main() -> None:
    report: dict[str, Any] = {}
    for domain in ("enterprise", "web"):
        data = analyse(domain)
        report[domain] = data
        print(f"\n### {domain}  measurable={data['substrate']['measurable']}")
        print(f"baseline reproduces stage0h: {data['baseline']['reproduces']}")
        print(f"  measured  {data['baseline']['measured']}")
        print(f"  committed {data['baseline']['committed']}")
        print(f"  stage1 bm25 slice@50 {data['baseline']['stage1_bm25_slice_at_50']}")
        print(
            f"substrate passages={data['substrate']['passages']} "
            f"trajectories={data['substrate']['trajectories']} "
            f"mean_chars={data['substrate']['mean_passage_chars']}"
        )
        print("  cap ceiling (ranking-free):")
        for name, block in data["cap_ceiling"].items():
            print(
                f"    {name:<10} single_source={block['exposure_single_source']:.4f} "
                f"any_source={block['exposure_any_source']:.4f}"
            )
        for metric, label in (
            ("exposure", "exposure"),
            ("mean_pack_items", "items"),
            ("mean_pack_chars", "chars"),
        ):
            print(f"  bm25 grid {label}:")
            print("    cap".ljust(12) + "".join(f"d={d}".rjust(10) for d in POOL_DEPTHS))
            for cap in TRAJECTORY_CAPS:
                key = "none" if cap is None else str(cap)
                row = "".join(
                    f"{data['bm25_grid'][f'cap={key}|depth={d}'][metric]:.2f}".rjust(10)
                    for d in POOL_DEPTHS
                )
                print(f"    {key:<8}{row}")
        cap_losses = {
            name: block["loss_reasons"]["trajectory_cap_excluded_reachable_gold"]
            for name, block in data["bm25_grid"].items()
            if "trajectory_cap_excluded_reachable_gold" in block["loss_reasons"]
        }
        print(f"  grid points where the cap excluded reachable gold: {cap_losses}")
        print(f"  gold in one trajectory: {data['mechanism']['passages_of_one_trajectory_needed']}")
        print(f"  char budget at cap=none depth={max(POOL_DEPTHS)}: {data['char_budget_probe']}")
        print(f"  note lane at cap=8 depth=28: {data['note_lane_interaction']}")
    (OUT / "stage2_report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
