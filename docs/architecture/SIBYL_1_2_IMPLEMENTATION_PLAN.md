# Sibyl v1.2 Implementation Plan — "Retrieve It"

- Status: active execution plan
- Created: 2026-07-24 (the day v1.1.0 → v1.1.2 shipped)
- Fills the v1.2 slot of [`SIBYL_POST_1_0_ROADMAP.md`](SIBYL_POST_1_0_ROADMAP.md) §5 and **re-scopes
  it from "Coalesce It" to "Retrieve It"**: live coalescence (roadmap v1.2 W1–W3) and TeamMemBench
  (W5) slide to v1.3; the projection layers (W6–W7) stay; retroactive re-extraction (W8) stays as
  stretch. Rationale in §7. The product truth remains [`SIBYL_NORTHSTAR.md`](SIBYL_NORTHSTAR.md).
- Decision provenance: leaderboard timing locked by Bliss 2026-07-24 (hold submission until the
  naive-RAG baseline is beaten). The re-scoping itself is recommended-and-planned here; §9 tracks
  locked vs open.

## 1. Thesis

v1.1 built the trust machinery: usage-aware forgetting, citation contract, team scope, OKF export,
mutation receipts, and five trust gates. v1.2 uses that machinery to fix the one structural defect
the eval loops exposed — and to make the flagship number honest-good, not just honestly reported.

Three facts drive the release shape:

1. **The headline number is below the paper's own naive baseline.** Sibyl's best full-451
   LongMemEval-V2 run is 31.26% @ 7.48s; the benchmark paper's reference frontier is 42.8% for naive
   query→slice RAG and 51.0% @ 0.2s for query→slice+notes. For a product whose moat is honest
   benchmarks, sitting under `naive RAG` is the single most expensive fact on the books. Everything
   else in this plan is sequenced around removing it.
2. **The defect is payload format and exposure, not query planning.** A month of query-side
   refinement churned inside reader noise while the format gap dominated (receipts in §2.3). The
   multi-step retrieval that shipped in v1.1 (`retrieval_mode=accurate`) is measured harmful at
   451-scale: −0.9pp combined and 2.5× latency vs fast mode. "Multi-step retrieval, locked in"
   therefore means **model-directed traversal over a slice-granular substrate** (the graph-backed
   agentic path, decision `0f4ab0c8a0cc`) — not more deterministic query fan-out.
3. **Our own memory loop carries trust debt — and it is worse than we thought.** Adjudicated
   2026-07-24 (§2.5): private memories leak to org co-members through unscoped graph entities; the
   offline write queue silently and unrecoverably deletes buffered writes on a 409; the content
   schema ladder has been deadlocked at v15 of 23 since 2026-07-11 while `/api/health` reported
   healthy; and ~15 CLI commands exit 0 on failure. A memory product that leaks private memories and
   loses offline writes cannot preach memory sovereignty. This warrants **1.1.3 before 1.2** (§2.6),
   not someday.

Positioning consequence: coalescence — the big differentiator bet — moves to v1.3. The roadmap's own
sequencing logic ("team coalescence is isolation-correctness-under-merge and cannot ship credibly
without the harness") extends one step: it also cannot ship credibly while the flagship retrieval
number loses to `cat *.txt`-adjacent baselines.

## 2. Verified starting point (2026-07-24)

### 2.1 Release state

v1.1.0/1.1.1/1.1.2 shipped 2026-07-24; v1.1.2 is the signed, CVE-clean image (GHCR/Docker Hub digest
parity verified). The v1.1 "Prove It" slate landed: usage loop (exposure/citation/misled signals,
retention multiplier 0.1×–4.0×), team control plane, OKF export, `correct`/`blame` + mutation
receipts, trust gates (`write_path_integrity`, `forgetting`, `usage_loop`, `okf_export`,
`doc_claim`), SurrealDB pinned 3.2.3 with conjunctive-safe fulltext arms (`1f086b22`). The one
benchmark-proven retrieval lever — distilled notes + reserved typed lane — is ported to production
(task `1e1caf57`, done: async distillation job, reserved note lane in evidence composition, digest
v2 field shapes).

### 2.2 LongMemEval-V2 campaign state

Full-451 three-way, 2026-07-22 (receipts `.moon/cache/evals/nova-full-451-fast/`, decision
`3da12cba3ccd`):

| Config           | Combined             | Latency (avg / p95) | Enterprise | Web    |
| ---------------- | -------------------- | ------------------- | ---------- | ------ |
| FAST+notes       | **141/451 = 31.26%** | **7.48s / 10.35s**  | **35.07%** | 27.92% |
| ACCURATE+notes   | 137/451 = 30.38%     | 18.73s / 27.46s     | 31.28%     | 29.58% |
| July-11 baseline | 129/451 = 28.60%     | 8.25s / —           | 33.18%     | 24.58% |

FAST+notes is a strict Pareto win over the July-11 baseline (+2.66pp AND faster) and the best
pipeline to date; enterprise 35.07% is best-ever. Accurate mode's 3-query planner cost ~4pp accuracy
and 2.5× latency on enterprise — the v1.1-era "enterprise regression" was an accurate-mode artifact.
Reference frontier: paper query→slice 42.8%, query→slice+notes 51.0% @ 0.2s; the LAFS latency knee
~27s holds ~62% of scoring mass. At 31.26%, LAFS gain vs the frontier ≈ 0.

The surviving lever: LLM note distillation at ingest + a **reserved** typed-note lane =
**+3.7pp/domain, stable across four independent 3-pass gates** (web v2 and enterprise v2 both
+3.70pp; the v1 enterprise +6.67pp was inflated by a depressed fat baseline). Interaction vs the
no-notes control is +8.15pp — matching the paper's ARB-R note-pool ablation (+8pp) almost exactly.
Notes without a reserved lane measure **negative**: never let notes compete with raw evidence for
slots. The ceil(3n/8) = 3-of-8 reservation is near-optimal (widening to 5 lost the entire gain).

### 2.3 Gap anatomy — where the missing ~11.5pp live

- **Selection is nearly solved; rendering is not.** Every deep-search arm selects to the same 82.6%
  phrase-presence ceiling; rendering exposes at most 65.2% (decision `7b7b46f1d169`).
- **Granularity is the defect.** trajectory_recall@10 = 94% but state_recall@10 = 68%; gold-literal
  exposure is only 21% (web) / 32% (enterprise). The reader converts 50–54% when the literal is
  exposed. Payload is 8 giant items × 12K chars (~34K tokens) against readers that degrade past ~3K
  retrieved tokens (LME-v1 finding).
- **Oracle format ablation: slices+notes 82.5% vs raw trajectories 59.6%** (qwen3.5-9b) — format
  alone is worth ~23pp (decision `0e0677006a04`).
- **Known residual pools:** query-anchored region ranking misses gold that shares no question
  vocabulary (3 of 23 phrases on the enterprise-45 slice); counting questions are unwinnable by
  exposure (2 of 23); the errors-gotchas pool is 67–71% confidently wrong (sycophancy — premise
  never checked); abstention is reader-prompt-bound (inventory annotations rendered in all 45
  contexts moved abstention 1/13).

### 2.4 Killed-lever ledger — do not re-run

All killed with 3-pass receipts; re-litigation requires new mechanism evidence, not a re-roll:
client-side geometry reshaping (fat 24.4% @ 10.52s vs windowed-compact 20.0% @ 36.15s — 3.4× slower,
past the knee), deep-search compact arms, inventory-annotation abstention chain, typed-stream lane
without notes (−1.48pp), reservation widening 3→5 (−3.70pp), deterministic multi-query planning at
scale (accurate mode, §2.2). Noise floor: 5 replays of one config span 23–27/90; identical contexts
rescored 22–29 across reader passes; per-question churn ~6.7/45 between passes. **Aggregate flatness
is not per-question stability; sub-noise deltas are NO-GO by default.**

### 2.5 Dogfood trust debt — adjudicated 2026-07-24

Five parallel investigations verified every claim below against shipped 1.1.2 and the live store.
Two items were **overstated**, two are **worse than filed**, and two defects were **newly
discovered**. The corrected picture drives the 1.1.3 recommendation in §2.6.

**Release-blocking (verified, source-traced):**

- **Private-memory leak on the default capture path** (`4736bf2d`, `error_pattern_8e04647be38f`).
  Worse than filed: the promotion path is fine, but `MemoryCaptureService.capture()` never copies
  `memory_scope` (which defaults to `private`) or `scope_key` into `graph_metadata`; `Entity` has no
  scope field in either the model or the graph schema; and `_candidate_scope_allowed` **fails open**
  on missing scope. A project co-member can therefore read another user's private memory title and
  full content. Live: 1,292 capture-path entities, 100% unscoped, 100% carrying content. Org
  namespace isolation **holds** — same-org only. Latent while single-user. Team scope has the
  inverse bug and fails **closed** (`accessible_teams` never threaded), so team memory is currently
  write-only and invisible even to its own members.
- **Silent unrecoverable loss of offline writes** (`df317be0`). `_should_keep_pending_write` omits
  409, so the queue file is deleted before the exception raises — and 409 is precisely the
  stranded-reservation case the client cannot prove was not applied. The payload is never persisted
  server-side. 74 stranded `102` rows observed; flushing mints more. The alarming `discarded: 1180`
  counter was a **red herring**, proven by controlled experiment: it counts only explicit
  `pending-writes discard` invocations, so the client-side deletes recorded nothing at all.
- **Content schema ladder deadlocked at v15 of 23 since 2026-07-11** (`4fd323a5` root cause,
  `error_pattern_c01dc4698419`). `DEFINE TABLE IF NOT EXISTS x SCHEMAFULL` silently no-ops against a
  table SurrealDB auto-created SCHEMALESS, so v16's `FLEXIBLE` field fails forever and the version
  is never recorded. `/api/health` reports healthy while `/api/health/ready` reports the failure,
  and the shipped compose healthcheck probes the former. Fresh installs are unaffected;
  **upgraders** whose store predates 2026-07-03 are the population.

**Confirmed, high:**

- **Lying exit codes** (`659b4ada`). `task archive` exits 0 on failure on both paths; ~15 commands
  swallow `SibylClientError`, incl. a live-reproduced `sibyl auth status`. Tests actively locked the
  bug in by asserting `exit_code == 0` on failed runs. This breaks the contract the shipped skill
  pack makes to agents.

**Overstated — corrected:**

- `0ea2177e` fulltext lanes are **not** a 3.2.x regression (`claim_72dd1444fa8e`). A controlled A/B
  on ephemeral 3.1.0 and 3.2.3 containers with a validated positive control showed the remaining
  lanes return zero on **both** engines. Pre-existing recall gap; no user is worse off on 1.1.2 than
  1.1.0. Rescoped to recall quality, priority lowered.
- `7a52bf39` raw-memory CREATE is **not** currently failing. The acute 500 is gone — but not because
  the intended repair ran: content v23 (`content_raw_capture_required_field_repair`) is stuck behind
  the deadlock and has never executed. The write path works by accident of where v16 died.
- **Epic linking is not broken.** Working IDs come from `sibyl epic list` (`epic_<12hex>`); the
  original report used a task UUID and invented prefixes. The real defect is that `errors.py` treats
  bare UUIDs as secrets and replaces the _entire_ message across 44 sites, so Sibyl redacts its own
  identifiers and makes 400/404s undiagnosable.

**Newly discovered:**

- Document search returns **empty rather than degraded** when the embedder soft-fails: the scan
  fallback only fires on `RuntimeError`, so a 2s embedding timeout plus a zero-recall lexical lane
  yields nothing (`411a5611`).
- `sibyl debug query` appends its org predicate with no leading space (any plain `SELECT` without
  `GROUP BY` is a parse error) and strips `id` from projections (`3f20f0e2`).
- Hierarchy drift: `-e` writes only `epic_id` while `parent_task_id` is the field
  `get_epic_progress` reads, so re-parenting silently miscounts forever.

**Gate false assurance (flag loudly):** `benchmarks/results/ai-memory/team-scope-trust-receipt.json`
asserts `leak_count: 0` under a declared surface of "private source isolation". It is a
hand-maintained static file with no generator anywhere in the repo — honest about being static in
its own `claim_boundary`, but a blocking gate reports zero leaks while the leak lives outside its
fixtures. Relatedly, the only unit test of `_candidate_scope_allowed` hand-injects the very
`memory_scope` field whose absence is the bug, proving the filter works while hiding that it never
fires. **Any gate whose receipt is hand-authored must be regenerated or deleted in v1.2.**

### 2.6 The 1.1.3 recommendation

Cut a patch release before 1.2 feature work. Drivers, in order: offline write loss (`df317be0`), the
private-memory leak (`4736bf2d`), the schema deadlock, and the exit-code cluster. Not drivers: the
fulltext lanes and raw-memory CREATE, both of which were overstated. Root `moon run :check` was
verified green (55 tasks) on the 1.1.2 cut, so the release gate is clear.

Deferred out of the patch by design: the `_candidate_scope_allowed` fail-open flip (would hide
legitimately unscoped tasks/epics — needs a first-class `Entity.memory_scope` column), the scope
backfill migration for existing unscoped rows, the pending-writes park/dead-letter directory and
attempt cap, the server-side `102`-row reaper, and the `epic_id`/`parent_task_id` unification.

**Live-store repair carries an ordering constraint** (`procedure_4e83bec5631f`): the store holds 179
duplicate usage-event uuids (181 excess rows). Since the uuid is a SHA-256 of the dedupe tuple, the
UNIQUE index builds will fail, be silently swallowed, and never retry — leaving dedupe permanently
unenforced. Collapse duplicates **first**, then run schema repair, then verify both indexes exist.
Usage counts from 2026-07-11 onward carry a ~1.4% upward bias from unenforced dedupe, so retention
multipliers derived from that window are slightly inflated.

## 3. Protocol law (binds every Track A experiment)

1. **Replay before paid.** Exposure-first replay harness screens every idea; scored runs only for
   arms that move exposure/selection in replay.
2. **3-pass paired replication on frozen prompts** is the only accepted scoring protocol. GO
   requires mean paired delta ≥ +3pp with consistent sign (gainers vs losers); pre-register GO/NO-GO
   numbers before the run.
3. **Latency receipts are 1-way.** Parallel prompt-build pollutes per-query latency (contention);
   never claim latency from a parallel arm.
4. **Budgets:** corpus rebuild ~$0.62–0.75/domain, notes ~$0.15/corpus, domain regate
   ~$5,
   full-451 ~$10. Corpora build from committed HEAD only (attach verifier refuses dirty-tree
   chunking; trajectory-mode fresh ingest is forbidden at HEAD — state mode is the path).
5. **Integrity boundaries:** the adapter stays score-blind and question-text-only; no benchmark
   labels near retrieval; no reader answer-shaping for abstention questions (integrity-gray,
   rejected — it cuts against the honest-benchmark moat). LAFS knee 27s is the hard latency ceiling;
   10s avg is the working budget.

## 4. Track A — beat the naive-RAG baseline (headline)

Target: combined full-451 **≥ 42.8%** (193/451) at ≤ 10s avg. Stretch: approach the 51.0%
slice+notes frontier. Phases are ordered by evidence strength; each gates before the next spends.

### A1. Slice-granular substrate + compact rendering

Replace the 8 × 12K fat-item payload with state-boundary slices as first-class retrieval units and a
compact renderer. This is where the paper's 42.8% lives (query→slice at 0.2s), and it is the
surviving re-architecture direction: the client-side kill (geometry reshaping) reshaped fat
retrievals at read time; this changes what is retrieved. Design seeds from decision `0e0677006a04`:
state-boundary slice chunking, typed note/event pools (already live at ingest), multi-stream
retrieval, neighbor-stitch that raises exposure while **shrinking** context. Open design choice
(§9): slices as graph entities vs a retrieval-layer view over states.

- Gate `slice-substrate-gate`: on the enterprise-45/web-45 replay slices, gold-literal exposure ≥
  50% (from 32%/21%) AND state-level recall@10 ≥ 85% (from 68%) within ≤ 60K chars (target ≤ 48K);
  then a scored 3-pass paired run vs frozen FAST+notes, GO per protocol law.

### A2. Ranking refinements

Semantic/embedding region scoring where query-anchored ranking misses no-vocabulary-overlap gold;
entity-overlap ranking for typed notes (the known `fa504f5e` refinement); gotcha/premise notes aimed
at the errors-gotchas pool (67–71% confidently wrong — the one pool where notes can check a premise
instead of answering it).

- Gate `ranking-gate`: recovers ≥ 2 of the 3 known no-vocab misses on the 23-phrase slice without
  net exposure regression; scored effects per protocol law.

### A3. Agentic graph retrieval — multi-step done right

Bounded model-in-the-loop traversal over the A1 substrate: search → expand-neighbors → fetch-slice
verbs, ≤ 3 rounds, deterministic final composition (the evidence composer and reserved note lane
stay; the model chooses what to gather, never how it renders). This is the graph-backed path of
decision `0f4ab0c8a0cc`, attempted only after A1 — traversal over fat states would re-buy the format
defect at higher latency.

- Gate `agentic-retrieval-gate`: score-blind, question-text-only; p95 memory latency ≤ 15s (hard
  ceiling: the 27s knee); GO = ≥ +3pp mean paired over the then-best config, numbers pre-registered
  before the first paid run.

### A4. Full-451 + leaderboard submission

- Gate `baseline-beat-gate`: combined full-451 ≥ 42.8% at ≤ 10s avg with committed receipts.
  **Submission is held until this gate passes** (Bliss, 2026-07-24): a dominated point on an empty
  board invites the wrong comparison. When it passes, submit promptly — first credible entry on an
  empty leaderboard is the payoff the v1.1 W5 harness was built for.

### A5. Accurate-mode fate

Deprecate the `accurate` retrieval mode (measured −4pp enterprise, 2.5× latency at scale). Fold
sidequest `6338cc8e` (audit orphaned `retrieval/query_planning.py`) into the removal: keep the
score-blind planner code only if A3 reuses it as a traversal seed, otherwise delete. The web-only
per-domain fork (+1.7pp) is rejected — mode-forking the product for 1.7pp on one benchmark split is
surface without a mechanism, and A1–A3 subsume the effect.

## 5. Track B — projection layers (roadmap W6–W7, kept)

Both consume machinery v1.1/1.2 already built: the production note-distillation pipeline, the usage
signal, and the OKF projection code.

### B1. Distilled per-project handbook

Dream-cycle stage maintaining a navigable per-project summary artifact (handbook + wake summary),
regenerated on sufficient graph change, built with `synthesis_plan/draft/verify`. Usage signal
orders what earns prominence; wake bundles and the SessionStart hook serve the distilled artifact
instead of raw top-k.

- Gate `handbook-integrity-gate`: the v1.1 write-path-integrity gate applied to distiller output —
  zero hallucinated/self-referential writes on the seeded fixture.

### B2. Materialized memory-as-files (`.sibyl/memory/`)

`sibyl export --project <id>` + session hook materializing the handbook and recent context pack as
read-only files. Filesystem-native agents grep at zero marginal latency and survive server outages;
the citation contract still routes usage back over the API. Structured substrate, curated
projection.

- Gate `files-projection-gate`: deterministic re-materialization; grep-usable with the server down;
  citations still recorded when it returns.

### B3. Retroactive re-extraction loop (stretch)

`sibyl admin re-extract --since-extractor-version <v>` over raw captures, delta scored by the
regression harness before promotion, supersession edges for rollback. Ships only if Tracks A and C
are green first.

## 6. Track C — dogfood trust debt (must-fix)

Fix the §2.5 list as a named campaign, not scattered chores. **Most of the acute work moves into
1.1.3** (§2.6); what remains in Track C is the design work deliberately excluded from the patch,
plus the residue.

Ships in 1.1.3: `df317be0` (409 retention + delete metrics), `659b4ada` (exit-code cluster), the
schema-deadlock pairing + repair sweep + `sibyld db init`, and the `4736bf2d` capture-time scope
stamp.

Stays in Track C for 1.2, in priority order:

1. **Scope model, properly.** A first-class `Entity.memory_scope` column with a sane default, the
   `_candidate_scope_allowed` fail-open decision, and the backfill joining
   `entity.attributes.raw_memory_id` back to `raw_captures.memory_scope` across the namespace split.
   Plus threading `accessible_teams` into the three team-read call sites so team memory stops
   failing closed.
2. **Regenerate or delete every hand-authored gate receipt**, starting with
   `team-scope-trust-receipt.json`. A blocking gate whose zero is typed by a human is worse than no
   gate. Pair each with a test that fails when the defect is reintroduced — the current scope test
   injects the field whose absence is the bug.
3. **Pending-writes durability design:** park/dead-letter directory, attempt cap (`attempts` is
   incremented and nothing branches on it), server-side `102`-row reaper, and releasing the
   reservation on route failure. Retaining 404 makes a poisoned queue drainable only one ID at a
   time, so this is now load-bearing rather than optional.
4. `85c4dc53` embedding backfill false-negatives; `0ea2177e` rescoped to doc-chunk lexical recall
   (two duplicated call sites) **plus the engine-level test coverage that does not exist today** —
   current tests assert on generated query strings via fake clients and pass regardless of engine
   semantics; `411a5611` empty-vs-degraded document search; `3f20f0e2` debug-query defects;
   `errors.py` identifier redaction across 44 sites; `epic_id`/`parent_task_id` unification.

Opportunistic seconds (CLI/API hygiene, medium): `7c4cb25a`, `e2206767`, `41606e9d`, `6fe13e16`.

- Gate `dogfood-trust-gate`: pending-writes queue drains to zero against a healthy server with
  per-file receipts; a regression fixture exists for each bug class; **every gate receipt in
  `benchmarks/results/` is machine-generated by a committed generator**; a two-principal leak test
  proves private capture is unreadable by an org co-member; fulltext-lane conversion passes baseline
  parity on both 3.2.3 and 3.1.0.

## 7. What slides to v1.3, and why

Live coalescence engine (roadmap v1.2 W1–W3), the scale-load/team-isolation gates (W4), and
TeamMemBench (W5) move to v1.3. The coalescence **data model** (W1) may proceed as a design doc in
v1.2 if bandwidth allows — design-only, no engine. The roadmap's differentiation argument is
untouched; only its timing moves: the credibility spend has to go to retrieval first, and the v1.1
team substrate plus usage loop remain in place, un-rotted, for a v1.3 engine. Nothing in Track A/B
forecloses any coalescence decision.

## 8. Task board: triage and grooming

### 8.1 Sidequests pulled into v1.2

`85c4dc53`, `7a52bf39` (Track C); `6338cc8e` (A5); eval-harness hardening taken opportunistically
during Track A: `2dbaf3c7` (force fresh artifact comparisons), `5b29a0ee` (mechanical treatment
gates), `48395009` (attach audits beyond session chunks).

### 8.2 Parked (explicitly not v1.2)

`6e69bc0c` bulk personal-archive ingestion (strong v1.3 candidate beside the OKF importer),
`41aba0d4` namespace-cleanup retries, `e5b328c3` GitOps-stable Surreal secret.

### 8.3 Stale-task dispositions (grooming pass to execute)

- `04e5f940` refresh same-SHA RC evidence (critical) → archive; superseded by 1.1 release receipts.
- `801e1f67` align 1.0 RC version metadata → archive.
- `b4061098` "Sibyl 1.0: autonomous memory workspace" epic (doing/critical) → close; 1.0 shipped.
- `5f6b964a` diagnose official accuracy collapse → complete with learnings pointer to decision
  `0e0677006a04` (diagnosis done: format defect).
- `71b2ef7f` enterprise readiness (review) → verify PR 257 shipped in 1.1, then complete.
- `7e8915a7` update project dependencies (doing) → complete; CVE chain done through 1.1.2.
- `8ac84142` clean local docs before RC gate rerun → archive.
- `c94d0556` improve LongMemEval live retrieval (doing) → superseded by the v1.2 Track A epic; close
  with a pointer here.

## 9. Decisions locked vs open

Locked:

- **Leaderboard: hold until `baseline-beat-gate` passes** (Bliss, 2026-07-24).
- **Protocol law (§3)** — standing, inherited from the campaign.
- **No reader answer-shaping for abstention** — integrity boundary.

Planned per this doc (flag before reversing):

- Coalescence W1–W3 + TeamMemBench slide to v1.3; W1 design-doc optional in v1.2.
- Accurate mode deprecated; no per-domain retrieval-mode fork.

Open (decide during execution):

- A1: slices as first-class graph entities vs retrieval-layer views over states.
- A3: server-side traversal loop vs client-exposed verbs (MCP surface implications).
- B1: handbook regeneration trigger (graph-delta threshold vs schedule).
- Whether B3 (re-extraction) ships in v1.2 or moves whole to v1.3.

## 10. Exit criteria

- Combined full-451 ≥ 42.8% at ≤ 10s avg with committed receipts; leaderboard submission made.
- Gold-literal exposure ≥ 50% and state recall@10 ≥ 85% on the replay slices.
- Accurate-mode fate executed; `query_planning.py` audit closed.
- Handbook + `.sibyl/memory/` projection shipped and serving wake bundles, both gates green.
- `dogfood-trust-gate` green: zero stranded pending writes, release-saga replay landed, fixtures for
  every §2.5 bug class.
- Task board groomed per §8.3.

## 11. Sequencing rationale

Substrate before traversal: agentic retrieval over fat states re-buys the format defect at higher
latency, so A1 gates A3. Ranking (A2) rides the A1 replay slices, costing near-zero marginal setup.
Track C runs parallel to Track A — different surface, different reviewers, and the eval work keeps
colliding with exactly these bugs (backfill false-negatives cost two corpus rebuilds). Track B
follows the production notes machinery it reuses and can land any time; it is deliberately not gated
on Track A. Coalescence waits because credibility compounds: the v1.3 coalescence story lands on top
of a retrieval number that beats the naive baseline, a leaderboard presence, and a dogfood loop with
zero stranded writes.

## 12. Receipts index

Sibyl decisions: `3da12cba3ccd` (full-451 Pareto win + accurate-mode kill), `0e0677006a04` (format
defect synthesis + oracle ablation), `7b7b46f1d169` (exposure sweep, selection ceiling),
`0d07482dd334` (reader A/B, geometry kill), `024f09dbeb0f` (notes win, era-3), `afe1a65abcdc` (web
v2 GO), `01e6ed6e4006` (enterprise v2 GO), `7a6a1d49b32c` (reservation tuning kill), `9f3c52581c68`
(typed-stream-alone kill), `109702d198fe` (abstention lever closed), `0f4ab0c8a0cc` (graph-backed
agentic path). Plans: `17188e6e7820`. Runs: `.moon/cache/evals/nova-full-451-fast/`. Commits:
`12ac2243` (note distillation), `430dea04` (content-aware digest v2), `1f086b22` (conjunctive-safe
fulltext), `83c8dd7b` (defaults reverted after tuning kill). Production port: task `1e1caf57`
(done).
