# Chunk-geometry measurement harness (v1.2 Track A1, Stage 0/1)

This directory holds the offline measurement code behind
[`docs/architecture/SIBYL_1_2_IMPLEMENTATION_PLAN.md`](../../docs/architecture/SIBYL_1_2_IMPLEMENTATION_PLAN.md)
§4 A1, the section that resolved the design fork "are sliced spans first-class
graph entities" — they are, and they shipped as `EntityType.PASSAGE`. Every
number quoted in that section was produced here.

These are **manual research scripts**, not a CI suite. Nothing in this directory
is wired into a moon task that executes them, because they read two large
machine-local corpora that are never committed. They are lint-gated only, via
`root:inventory-lint`.

## Why this directory is called `chunk_geometry`

`benchmarks/longmemeval_v2_*_slice*.py` and `longmemeval_v2_diagnostic_slice.json`
already use **slice** to mean *a subset of the evaluation dataset*. This harness
uses **slice** in the completely different sense of *a bounded piece of text cut
out of one accessibility-tree state*. Naming the directory after the second sense
would have made every future grep, glob, and skim ambiguous at exactly the point
where the two meanings collide.

`chunk_geometry` names what is actually measured — the size, boundaries, header
overhead, and retrievability of the indexed unit — and shares no token with the
dataset-subset files. Inside this directory the word "slice" always means the
text chunk; that is the only place the meaning is fixed, and it is fixed here.

**Slice is the verb, passage is the noun.** These scripts *slice* a state into
spans, and the algorithm, its functions (`slice_body`, `slice_header`), and its
constants keep that name. The graph rows the spans become shipped as
`EntityType.PASSAGE`, not `SLICE`, precisely because "slice" was already
load-bearing for dataset subsets. So: slicing produces passages. When this
README quotes a measurement it says slice, because that is what the code
measured; when you wire results into the graph, they are passages.

## Machine-local inputs — read this before running anything

Both corpora are build artifacts living under `.moon/cache`. Neither is in git,
and **a git worktree does not inherit the primary checkout's `.moon/cache`**, so
in a worktree you must point at the primary checkout explicitly.

| Input | Default location | Override | Used by |
| --- | --- | --- | --- |
| Frozen era-3 chunk catalogs | `<repo>/.moon/cache/evals/lme-v2-two-typed-29526606329` | `SIBYL_A1_EVAL_ROOT` | every stage |
| LongMemEval-V2 full download | `<repo>/.moon/cache/benchmarks/longmemeval-v2-full` | `SIBYL_A1_DATA_ROOT` | `stage0d`–`stage0g`, `stage1`, `stage1b` |
| Report destination | `./out` | `SIBYL_A1_OUT` | every stage |

The catalog path is read as
`<SIBYL_A1_EVAL_ROOT>/<domain>/memory_state/chunk_catalog.jsonl.gz` for
`domain` in `enterprise`, `web`. `stage0` and `stage0b` additionally read
`<SIBYL_A1_EVAL_ROOT>/<domain>/runtime_inputs/questions.json`; the later stages
read the full 451-question set from `<SIBYL_A1_DATA_ROOT>/questions.jsonl`.

If the catalogs are gone, the corpus must be rebuilt before any of this runs —
there is no smaller fixture, and no stage degrades gracefully without one.

### Dependencies come in three tiers

None of the third-party ones are project dependencies, so the dense arms need a
separate environment. Nothing here is import-safe for a test collector to sweep
up, and nothing in CI imports it — `root:inventory-lint` only lints statically.

| Tier | Needs | Scripts |
| --- | --- | --- |
| Cheap | standard library only, plus the in-repo `benchmarks/longmemeval_v2_diagnostics.py` | `stage0`, `stage0b`, `stage0c`, `stage0d`, `stage0e`, `stage0f`, `stage0g`, `stage0h`, `stage2`, `summarize` |
| Middle | `numpy`, `scipy`, `scikit-learn` | `stage1b` |
| Heavy | the middle tier plus `sentence-transformers` and `torch` | `stage1` |

`stage1b.py` is the BM25-only falsification and needs no model, no GPU, and no
download — but it is **not** dependency-free: it imports `numpy` directly and
pulls `scipy` and `sklearn` transitively through `from stage1 import ...`, both
of which are module-scope imports in `stage1.py`. Only `torch` and
`sentence_transformers` are deferred, imported inside `stage1.main()`, which is
what keeps the whole Stage 0 tier runnable on a bare interpreter.

### The one in-repo import

Every stage reaches `benchmarks/longmemeval_v2_diagnostics.py` for
`answer_evidence_phrases`, `is_exact_evidence_eligible`, and `normalize_text`.
That resolves because each entry-point script puts its parent directory —
`benchmarks/` — on `sys.path` before the import, computed from `__file__` rather
than hardcoded, so it follows the checkout and works from any worktree. Moving
this directory deeper or shallower breaks that import; adjust the `sys.path`
line if you do.

## How to re-run

From the repository root, with an interpreter that has the deps a given stage
needs. Stages are ordered; `corpus.py` reuses `stage0.load_states`, so nothing
here is standalone.

```bash
export SIBYL_A1_EVAL_ROOT=/path/to/.moon/cache/evals/lme-v2-two-typed-29526606329
export SIBYL_A1_DATA_ROOT=/path/to/.moon/cache/benchmarks/longmemeval-v2-full

python benchmarks/longmemeval_v2_chunk_geometry/stage0c.py   # cheapest, stdlib only
python benchmarks/longmemeval_v2_chunk_geometry/stage0.py    # slowest Stage 0 pass
python benchmarks/longmemeval_v2_chunk_geometry/stage0h.py   # part-confined arms, ~25s
python benchmarks/longmemeval_v2_chunk_geometry/stage2.py    # pack-knob sweep, stdlib BM25
python benchmarks/longmemeval_v2_chunk_geometry/stage1.py    # needs the ML stack
python benchmarks/longmemeval_v2_chunk_geometry/summarize.py # renders the Stage 1 verdict table
```

Each stage rewrites its own `out/*_report.json`, so re-running in place produces
**no git diff** when the corpus is unchanged — verified for `stage0c`,
`stage0f`, `stage0e` and `stage0h`, which reproduce their committed receipts
byte for byte. Any diff you
do see is therefore signal: the corpus, the cutter, or a threshold moved. Set
`SIBYL_A1_OUT` to a scratch directory when you want to compare without touching
the receipts.

## What each stage measures

| Stage | Measures | Feeds this claim in §4 A1 |
| --- | --- | --- |
| `stage0.py` | Slice size / count / header-tax / cut-depth distributions, plus oracle exposure on the phrase-eligible questions of each domain's runtime input set. | "Slices per state ~25 / ~37", "slice chars mean ~950–1,030", "header tax 19% enterprise / 12% web". |
| `stage0b.py` | Straddle characterisation at scale: real gold triples (probe A), synthetic k-line spans (B), parent/child adjacency (C), and where gold phrases actually live (D). | Widens the tiny real-gold denominator; supports the zero-straddle finding before `stage0d` confirms it on the full set. |
| `stage0c.py` | Band adherence, hard-max overflow, and whether internal subtrees survive intact in one slice. | The boundary-rule evidence: 651 enterprise slices over the hard max, max 4,103 chars — the reason v2 bounds the ancestor prepend. |
| `stage0d.py` | Oracle exposure ceiling over the **full 451**: fat-state vs fat-chunk vs single slice vs slice+neighbour, plus phrase-level straddle. | "**Straddle rate is zero** — 31,244 of 31,244 triples" (24,083 enterprise + 7,161 web) and the 92 measurable questions (44 + 48). |
| `stage0e.py` | Exposure as a function of window width w ∈ {1,2,3,5}. | "A 3-adjacent-slice window reaches the fat-state ceiling exactly: 95.5% / 100%", single-slice "93.2% / 97.9%". |
| `stage0h.py` | Re-runs the `stage0e` widths under the confinement shipped retrieval actually imposes: passages cut per evidence part, windows unable to leave one. Three arms, plus a straddle probe that proves the arms can diverge. | Tests whether "a 3-adjacent window reaches the fat-state ceiling" survives the port to production. It does — zero delta at every width in both domains. |
| `stage0f.py` | Breadcrumb cost as a design knob: what capping to the last N ancestors saves. | "the breadcrumb is the expensive half (11.5%, mean 134 chars). Capping to the last two ancestors drops it to 3.9%." |
| `stage0g.py` | v1 vs v2 boundary rules side by side, re-checking band, count, and exposure. | "Boundary rules the data argues for, all verified to preserve exposure and the zero straddle rate." |
| `stage1.py` | Offline selection simulation. Arms FAT / SLICE / SLICE_GOAL against BM25, dense (local MiniLM), and RRF; recall at both item and character budgets. | The selection-dilution result, the goal-carry lift (RRF 0.727 / 0.812 vs fat 0.682 / 0.792), and the character-budget argument. |
| `stage1b.py` | Falsification: strip the trajectory preamble from the fat arm and see whether its BM25 lead collapses. | "the mechanism is BM25 document length, not semantics — stripping the goal preamble from fat chunks barely moves them." |
| `stage2.py` | The two pack-composition knobs on the production passage substrate: `max_chunks_per_trajectory` x evidence-lane depth, at the staged gate geometry. Ranking-free cap ceiling, a stdlib BM25 grid, and the loss cause behind every miss. | Sets both knobs for the paid slice-substrate gate run, and records which enforcement paths a fast-mode run actually has. |
| `summarize.py` | Renders the Stage 1 tables and evaluates the pre-registered rules. | Produces `out/stage1_summary.txt`. |

Support modules: `slicer.py` (the v1 cutter and its constants), `slicer_v2.py`
(the three candidate boundary fixes), `chunkparse.py` (catalog record →
preamble / header / body), `corpus.py` (shared unit construction), `paths.py`
(the roots above).

## Does the whole-state oracle describe what ships? (`stage0h`)

Every Stage 0 exposure number is measured on **reassembled whole states**.
`stage0.load_states` joins each state's parts back together
(`entry["tree"] = "".join(...)`), the slicer cuts that joined body, and
`stage0e` slides its window across the whole state's slice list. Shipped
retrieval does neither. `_passage_projection` calls `slice_body` on one
`evidence.content` at a time, and `operational_sources._run_key` keys a passage
window on `(observation_ordinal, evidence_part_index)`, so a window stops dead
at a part boundary. Those are two different measurements, and the second is the
one the design rests on.

The split is not rare. **4,874 of 6,387 enterprise parts (76.3%) and 3,693 of
4,609 web parts (80.1%) belong to a state that split into several.** Per *state*
the rate is lower — 1,845/3,358 (54.9%) enterprise and 821/1,737 (47.3%) web —
so quote whichever denominator you mean; the 76/80 figures are parts, not
states.

`stage0h` re-runs the `stage0e` widths in three arms over the identical
question set and the identical `measurable` denominator (44 enterprise, 48 web):
`whole_state` recomputes stage0e's arm from scratch, `part_confined` keeps the
whole-state cut but forbids a window from spanning two parts, and `production`
cuts each part on its own and windows inside it.

**All three arms agree exactly, at every width, in both domains.** Enterprise
0.9318 / 0.9318 / 0.9545 / 0.9545 and web 0.9792 / 1.0 / 1.0 / 1.0 for
w ∈ {1,2,3,5}; the `whole_state` arm reproduces the committed `stage0e` receipt
digit for digit. The delta from confinement is 0.0 everywhere and
`confinement_losses` is empty. **The concern that motivated this stage was
unfounded: the shipped ceiling is the measured ceiling.**

The mechanism is in `parts_needed_to_cover_gold`: **42/42 enterprise and 48/48
questions whose gold lives in one state have that gold inside one evidence
part.** Not one measurable question needs two parts, so the boundary is never
between a window and its answer. The two enterprise questions that no arm
reaches fail as `no_single_state_carries_gold` — their gold was never in one
state to begin with, which no window width or window scope can fix.

Because a null result is only worth as much as the harness's ability to show a
non-null one, the stage carries a **positive control**. `boundary_probe` mints
synthetic gold pairs straddling a real part boundary — last eligible line
before it, first eligible line after it, kept only when each occurs exactly once
in the whole state — and scores them through the same three arms at w=3. The
`whole_state` arm covers **2,555/2,555 enterprise and 1,623/1,623 web** probes;
the two confined arms cover **0**. The apparatus registers confinement loss at
full strength when confinement loss exists. It reported zero on the real gold
because there is zero to report.

Two caveats worth carrying forward. First, the margin is thin: the whole w1→w3
gain is **one question per domain** (`0cf979c4` enterprise at w3, `c6124506`
web at w2), and both are bought inside a part, not across a boundary. The
equality of the 3-window and the fat state is real but rests on a single
question either side. Second, 30/44 enterprise and 35/48 web measurable
questions are single-phrase, where extra width can only help if the cutter
splits a phrase — and `stage0d` already measured that straddle rate at zero.

Incidentally the same run rules out the other shipped divergence in that code
path: `_passage_projection` drops a rendered passage over
`MAX_TYPED_ENTITY_CONTENT_CHARS = 18_000`, but the longest passage either
corpus produces is 4,384 chars (enterprise) and 2,186 (web), so that drop is
unreachable here.

`out/STAGE1_PREREGISTRATION.md` was written **before** `stage1.py` first ran and
fixes the decision rule, the falsifiers, and the underpowered-n caveat in
advance. Read it before reinterpreting any Stage 1 number.

## What the pack knobs are worth (`stage2`)

Every Stage 0 number is an oracle number. `stage2` is the first stage that
composes a **pack**: the ranked, capped, budgeted set of passages a reader
would actually receive, at the geometry the slice-substrate gate is staged to
run (`max_context_items 28`, `evidence_char_budget 48000`). It exists because
two caps were about to be set by guess, and both were tuned when one retrieval
unit was one whole state.

### Which caps a fast-mode run actually has

The first result is a code reading, not a measurement, and it moved what the
rest of the stage sweeps. Three of the four paths below are dead in the
configuration the gate runs (`retrieval_mode` `fast`, the campaign's winning
arm); the third is the one that actually binds.

- **`max_results_per_source` is inert in fast mode.** The adapter puts
  `max_chunks_per_trajectory` on the wire as `max_results_per_source`, but its
  only consumer is `_fuse_context_evidence` (`apps/api/src/sibyl/api/routes/context.py`),
  reached only from `_execute_accurate_context_evidence_search`. A fast-mode run
  sends the field and nothing reads it. The knob's one live enforcement is the
  eval adapter's `_select_diverse_results`, which **drops** over-cap rows where
  the server would have deferred and backfilled them.
- **The plan's per-lane seed budget cannot reach a passage.** It bounds
  `CandidateLimits` for `build_context_retrieval_plan` / `context_search`, whose
  only non-test caller is `sibyl_core/tools/context.py` serving context-pack
  *facet sections*. The evidence lane goes `execute_search_request` ->
  `tools.search.search` -> `hybrid_search`, which constructs no `CandidateLimits`
  and reads candidates at `limit * 3`. Sections cannot carry a passage either:
  `_types_for_facets` unions `FACET_TYPES` into a hard type filter and no entry
  there names a passage, and `context_pack_to_search_results` then admits only
  note / procedure / error_pattern / event. **Sweeping it at {8, 16, 24} would
  have bought nothing and widened every facet read for every caller.**
- **The seed budget the passage lane really has** is the evidence request's own
  `limit`, `min(max(search_limit, max_context_items), 50)`, applied in
  `tools/search.py` as `all_results[offset : offset + limit]`. That is the axis
  `stage2` sweeps in place of the signal cap, and `--search-limit` is the flag
  that moves it.
- **`PASSAGE_WINDOW_UNITS = 3` is accurate-mode only.** `select_operational_source_span`
  is reached only through `expand_operational_source_evidence`, whose single
  call site is inside the accurate path. A fast-mode pack holds individually
  ranked passages, so the 3-adjacent window that `stage0e`/`stage0h` measured is
  not what a fast gate run composes. The window's value has to come from the
  ranker pulling neighbours in on their own merits.

### Baseline first

The stage rebuilds the production substrate from `stage0h.production_slices` and
reproduces four committed numbers from its own structures before any new arm is
read: **84,968 / 65,573 passages, 44 / 48 measurable questions, single-passage
exposure 0.9318 / 0.9792 and 3-adjacent exposure 0.9545 / 1.0**, all identical
to `out/stage0h_report.json`. The stdlib BM25 is checked separately against
Stage 1's scikit-learn scorer: **0.2045 / 0.1667 at depth 50 against Stage 1's
SLICE bm25 recall@50 of 0.204 / 0.167**, on a slightly different cut.

### `max_chunks_per_trajectory`: the exposure curve flattens at 2

The cap ceiling arm is ranking-free. For a cap it asks whether *any* selection
holding at most that many passages per trajectory covers the gold, by exact
search over per-trajectory coverage masks, so whatever it cannot reach no ranker
can reach either.

| cap | 1 | 2 | 4 | 8 | 16 | none |
| --- | --- | --- | --- | --- | --- | --- |
| enterprise | 0.9318 | 0.9545 | 0.9545 | 0.9545 | 0.9545 | 0.9545 |
| web | 0.9792 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |

**Flat from 2 onward, and 2 is exactly the whole-state ceiling** — the same
0.9545 / 1.0 `stage0h` measured for a 3-adjacent window. The mechanism is that
gold is not spread out: **41 of 44 enterprise and 47 of 48 web questions need
exactly one passage of one trajectory**, one question each needs two, and two
enterprise questions have gold in no single trajectory at any cap — the same
count `stage0h` records as `no_single_state_carries_gold`, though this stage
does not check that they are the same two, because a whole state's evidence text
includes action and thought lines that no passage carries. Under BM25 the cap
excludes reachable gold in **one question, only at cap 1 or 2**, and never at cap
4 or above in either domain.

So the guessed 8 is not wrong on exposure. Nothing above 2 is. **The cap's real
cost is payload**, and at the staged geometry it is severe (depth 28, mean chars
of the composed pack against a 48,000 budget):

| cap | 2 | 4 | 8 | 16 | none |
| --- | --- | --- | --- | --- | --- |
| enterprise | 7,487 | 14,456 | 27,683 | 40,676 | 45,630 |
| web | 18,485 | 26,365 | 33,391 | 39,308 | 39,976 |

At the shipped default of 2 an enterprise pack spends **16% of the budget the
run was configured for**; at the guessed 8 it spends 58%. The cause is that BM25
top-k over passages concentrates: the top 28 span **1.73 enterprise trajectories
on average**.

The source-diversity argument for keeping the cap low does not survive contact
with `_select_diverse_results`. Its first pass admits at most one row per
trajectory *before* the cap can bind, so every distinct trajectory in the pool is
represented at any cap. Measured: **the mean number of distinct trajectories in
the pack is identical at every cap** — 1.73 enterprise, 7.79 web at depth 28,
unchanged from cap 1 to uncapped. The cap adds no diversity the first pass has
not already delivered; it only truncates the second pass.

**Recommendation: `--max-chunks-per-trajectory 50`.** Fifty is the
`max_results_per_source` schema ceiling and is effectively uncapped at any legal
depth, which makes the character budget the one thing bounding the pack — which
is what the budget was added for. 16 is the conservative alternative and still
leaves 5,000 enterprise characters unspent. Anything at or below 8 hands the
substrate arm a payload handicap against a fat baseline that renders up to
`max_context_total_chars`, and a paid run cannot tell that apart from slicing
not working.

### Evidence-lane depth: still climbing at the schema maximum

Exposure over the depth axis, at cap 8 (identical at every cap above 4):

| depth | 8 | 16 | 24 | 28 | 50 |
| --- | --- | --- | --- | --- | --- |
| enterprise | 0.1364 | 0.1591 | 0.1818 | 0.1818 | 0.2045 |
| web | 0.0833 | 0.1042 | 0.1250 | 0.1458 | 0.1667 |

Monotone, and **not flattening at 50** in either domain. Depth costs no money —
it is a wider read on a corpus already on disk — so **recommendation:
`--search-limit 50`**, which raises the evidence request's `limit` to the schema
maximum independently of `max_context_items`.

The caveat belongs to the ranker, not the knob. BM25 alone is the weakest arm
Stage 1 measured on a sliced substrate (median first-gold rank 282 / 302 against
19 / 40 dense), so these absolute numbers are a floor, and a hybrid lane would
flatten this curve earlier than 50. That is why the cap recommendation is read
off the ranking-free ceiling and only the depth recommendation is read off BM25,
where being conservative means recommending *more* depth than needed rather than
less.

### Where the knobs collide with everything else

- **Depth and the character budget trade.** Once the cap stops binding, depth 50
  fills the budget: enterprise 46,498 of 48,000, web 46,956. At that point web
  loses one question to `char_budget_excluded_reachable_gold` — the same question
  cap 2 loses to the cap, so exposure is unchanged, but the crossover is real and
  a larger budget would move it.
- **48,000 is not the largest legal budget, and it is not payload-matched to the
  comparator.** `validate_evidence_char_budget` rejects only a budget above
  `max_context_total_chars`, which defaults to 60,000 — and 60,000 is what the
  fat baseline's renderer already spends, since eight whole states compact down
  to that cap. At 60,000 the passage pack reaches **58,596 enterprise / 57,257
  web characters** against 46,498 / 46,956 at 48,000. Exposure does not move
  under BM25 (24,000, 48,000 and 60,000 all score 0.2045 / 0.1667, the arm being
  ranking-limited), but the gate is a paid comparison of reader payloads, and
  budgeting the substrate arm 20% under its comparator is a handicap chosen by
  default rather than by measurement.
- **The pinned note lane spends the budget first.** `TYPED_NOTE_RESERVATION_ITEMS`
  is three items, and those characters come off the top. Three notes of 4,000
  characters leave the passage lane 36,000 of 48,000. Exposure does not move at
  any probe size here because the BM25 arm is ranking-limited well before the
  budget binds, so this is arithmetic to carry forward rather than a measured
  loss: the distilled notes are not in the frozen catalogs and their size on this
  corpus is unmeasured.
- **Neighbour stitch is inert for passages.** `_neighbor_results` requires an
  integer `longmemeval_v2_chunk_index`, which a passage does not carry, so
  `--neighbor-stitch-items 2` silently does nothing on a passage arm.
- **A metadata cleanup could tighten the cap to one passage per state.** The
  second diversity pass refuses a row whose `(trajectory, state)` key is already
  present, and it never fires today only because `_result_diversity_key` looks
  for `longmemeval_v2_state_index`, which passages do not carry. They do carry
  `observation_ordinal`, which the eval sets to the state index, and
  `_source_support_state` already recovers a state index that way. Teaching the
  diversity key the same trick would cap the pack at **one passage per state**,
  far below any `max_chunks_per_trajectory`, while reading like tidying.
- **The one server-side per-source cap in fast mode is not this knob.**
  `reserve_distilled_notes` defaults true and the adapter never overrides it, so
  a fast run also composes the note lane, and `compose_operational_evidence`
  holds the typed lane to one result per `operational_source_id`. That cap is
  hardwired, applies only to notes, and moves nothing measured here — but "fast
  mode does no server-side source capping at all" is the wrong summary.

## What is committed, and what is not

`out/` carries every report except one. **`stage1_report.json` is deliberately
not committed** — at 827 KB it is mostly per-question, per-arm, per-ranker recall
curves. Its headline numbers survive in `out/stage1_summary.txt` (generated by
`summarize.py`) and `out/stage1.log`. Regenerate the full report by re-running
`stage1.py`.

## Where the code and the prose disagree

The plan's §4 A1 numbers all reproduce. Its *descriptions* of the cutter do not
all match `slicer.py`, and neither does `slicer.py`'s own docstring. None of
these invalidate a measurement; all of them will mislead someone porting the
rules into production.

- **`TARGET_LO = 600` is dead in v1.** It is defined and exported but never read
  by `_emit`. The only band check is `buffer_chars + size > TARGET_HI`, a greedy
  ceiling flush — there is no floor. So the docstring's "600-1200 char band" is
  half-enforced, and the receipt shows it: **29.47% of enterprise slices land
  under 600 chars** (`out/stage0c_report.json`). This is precisely the stranding
  that `slicer_v2.py` FIX 1 exists to correct.
- **There is no "shallowest depth" search.** The docstring says the cutter picks
  "the shallowest indent depth whose subtrees land in a … band." It does not.
  `_emit` greedily packs siblings at whatever level it is already on and descends
  only when one subtree exceeds `HARD_MAX`. Cut depth is an outcome, not a
  search target.
- **The documented `reason` values are stale.** `Slice.reason` is annotated
  `"pack" | "descend-solo" | "oversize-leaf" | "line-split"`, but the reachable
  values are `"multi-subtree"`, `"single-subtree"`, and `"oversize-leaf"`.
  `"descend-solo"` and `"line-split"` are never emitted, and `"pack"` only on a
  branch marked unreachable. Filtering on the documented names returns nothing.
- **The plan's "line-count fallback" is a character-threshold fallback.** The
  slicer has no line-count rule anywhere; every threshold is in characters. The
  counter is `oversize_leaf`, incremented when a leaf exceeds `HARD_MAX` and has
  no children to descend into — one line too long to cut, and cutting mid-line is
  forbidden. The rate the plan quotes (0.14% enterprise, 0% web) is right; the
  name is not.
- **URL path-extraction is already shipped, not a prospective fix.** The plan
  lists "path-extract and truncate header URLs" among "boundary rules the data
  argues for." But `uri_path()` already does host + path extraction with 48-char
  truncation, and `slice_header` already hard-caps at 120 — both active in the v1
  run that produced these numbers. It cannot also be an improvement still to be
  made.
- **Of the rules the plan lists beside it, only tail-merge is genuinely v2.**
  The other two named there are not implemented at all. `MAX_PREPEND_CHARS = 400`
  is declared at `slicer_v2.py:30` and never referenced by any code path; the
  real bound is the combined-size check against `HARD_MAX` at `slicer_v2.py:72`,
  which is what moved max slice 4,103 → 2,064. And `breadcrumb_for` joins every
  ancestor uncapped in **both** versions, so the two-ancestor cap was never
  implemented and never oracled — the 3.9% figure is `stage0f` arithmetic, not a
  re-run of the exposure oracle. Treating either as a shipped v2 rule inverts
  what the source says.
- **Band-aware packing is absent from the plan's rule list**, though it is the
  largest change to the size distribution. `slicer_v2.py:94-97` flushes once the
  buffer is above `TARGET_LO` and the next sibling would cross `TARGET_HI`,
  moving under-600-char slices from 29.5% to 23.8% and slices/state from 24.97
  to 23.13. Anyone implementing the plan's list literally would omit it.

Two more reading traps, in the measurements rather than the cutter:

- **`any_two_slices_same_state` in `stage0e` is not a ceiling.** It counts
  questions covered by a union of *exactly two distinct* carrying slices; a
  question already satisfied by one slice contributes nothing, because
  `combinations` of a one-element list is empty. It therefore reads *lower* than
  `window_1` (0.70 vs 0.93 enterprise) without contradicting it.
- **`url_over_120_char_header_budget_pct` in `stage0f` measures the raw URL, not
  the emitted header.** It formats `entry["url"]` directly, while `slice_header`
  formats `uri_path(url)` and then truncates. So it quantifies the motivation for
  path-extraction, not any overflow in shipped headers — those cannot exceed 120
  by construction.
- **Dense-beats-fat at equal payload is enterprise-only.** The plan scopes it
  correctly ("on enterprise"), but the web numbers run the other way at the same
  budget (dense slice 0.646 vs dense fat 0.708), so the sentence should not be
  generalised across domains.

## The next thing this harness is for

§4 A1 adopts a **contextualized-embedder offline arm** that must run *before* a
paid corpus rebuild locks an embedder. That arm is a variant of `stage1.py` —
swap the dense encoder and re-score the same units — not a rewrite. Keeping the
SLICE / SLICE_GOAL arms and the character-budget grid intact is what makes the
comparison meaningful.
