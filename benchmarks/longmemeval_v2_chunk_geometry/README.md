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
| Cheap | standard library only, plus the in-repo `benchmarks/longmemeval_v2_diagnostics.py` | `stage0`, `stage0b`, `stage0c`, `stage0d`, `stage0e`, `stage0f`, `stage0g`, `summarize` |
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
python benchmarks/longmemeval_v2_chunk_geometry/stage1.py    # needs the ML stack
python benchmarks/longmemeval_v2_chunk_geometry/summarize.py # renders the Stage 1 verdict table
```

Each stage rewrites its own `out/*_report.json`, so re-running in place produces
**no git diff** when the corpus is unchanged — verified for `stage0c` and
`stage0f`, which reproduce their committed receipts byte for byte. Any diff you
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
| `stage0f.py` | Breadcrumb cost as a design knob: what capping to the last N ancestors saves. | "the breadcrumb is the expensive half (11.5%, mean 134 chars). Capping to the last two ancestors drops it to 3.9%." |
| `stage0g.py` | v1 vs v2 boundary rules side by side, re-checking band, count, and exposure. | "Boundary rules the data argues for, all verified to preserve exposure and the zero straddle rate." |
| `stage1.py` | Offline selection simulation. Arms FAT / SLICE / SLICE_GOAL against BM25, dense (local MiniLM), and RRF; recall at both item and character budgets. | The selection-dilution result, the goal-carry lift (RRF 0.727 / 0.812 vs fat 0.682 / 0.792), and the character-budget argument. |
| `stage1b.py` | Falsification: strip the trajectory preamble from the fat arm and see whether its BM25 lead collapses. | "the mechanism is BM25 document length, not semantics — stripping the goal preamble from fat chunks barely moves them." |
| `summarize.py` | Renders the Stage 1 tables and evaluates the pre-registered rules. | Produces `out/stage1_summary.txt`. |

Support modules: `slicer.py` (the v1 cutter and its constants), `slicer_v2.py`
(the three candidate boundary fixes), `chunkparse.py` (catalog record →
preamble / header / body), `corpus.py` (shared unit construction), `paths.py`
(the roots above).

`out/STAGE1_PREREGISTRATION.md` was written **before** `stage1.py` first ran and
fixes the decision rule, the falsifiers, and the underpowered-n caveat in
advance. Read it before reinterpreting any Stage 1 number.

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
  argues for," alongside genuinely v2-only changes (400-char prepend bound,
  two-ancestor breadcrumb cap, tail-merge). But `uri_path()` already does host +
  path extraction with 48-char truncation, and `slice_header` already hard-caps
  at 120 — both active in the v1 run that produced these numbers. It cannot also
  be an improvement still to be made.

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
