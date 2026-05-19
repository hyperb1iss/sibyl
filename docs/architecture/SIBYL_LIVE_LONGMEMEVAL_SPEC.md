# Sibyl Live LongMemEval Harness Spec

- Status: active implementation spec (revision 2, post Codex review)
- Created: 2026-05-19
- Owner: v1.0 RC quality evals
- Parent epic: `epic_c22c73b78887` (v1.0 RC: Quality evals and receipts)
- Tracking task: `09ba2791-b8e3-41c6-9d7a-1737b4097dc1`
- Related: [`docs/testing/benchmark-methodology.md`](../testing/benchmark-methodology.md),
  `benchmarks/longmemeval_bench.py`, `benchmarks/live_runtime_eval.py`,
  `tools/bench/eval_gate.py`

## 1. Problem

Sibyl has three retrieval evaluation surfaces, and none measures the live runtime at
scale.

- `benchmarks/longmemeval_bench.py` runs LongMemEval-S (500 questions) but is an
  offline reimplementation: it imports `chromadb` and nothing from `sibyl_core`,
  scores against an ephemeral ChromaDB index with a local 384-dimension MiniLM
  embedder, and carries the honest `claim_boundary` "not live API runtime evidence."
- The context-pack live eval (`core:bench-context`) exercises the real runtime but
  over only 8 hand-built fixture cases.
- The nightly regression runs the context-pack eval deterministically with mock keys.

The gap: a benchmark both at scale and against the real runtime.

## 2. Goal

A harness and CI job that run LongMemEval-S against a real, ephemeral Sibyl stack and
produce honestly-labelled retrieval metrics. CI spins the stack up fresh; no
developer's personal instance is involved.

This revision is scoped by a hard rule from the first Codex review: **the spec must
not assert runtime behavior it has not verified.** Section 5 (Preflight) makes that
verification a required, gating phase before any harness code is written.

## 3. What LongMemEval Requires

LongMemEval-S is 500 questions. Each ships a haystack of prior chat sessions; a few
hold the answer evidence, the rest are distractors. The task is to retrieve the
correct session(s). Each question's haystack is isolated — question N must never
retrieve question M's sessions.

Verified against the committed artifact `longmemeval_sibyl_raw_20260513.json`: answer
sets are usually multi-session — 176 questions have 1 answer session, 250 have 2, 41
have 3, and the rest more. This fact drives the metric definitions in Section 8.

## 4. Design Overview

For each question: mint an isolated tenant, ingest its haystack through a verified
write path, wait for a verified readiness signal, query a verified retrieval surface,
map results back to LongMemEval session IDs, score. Aggregate into a gate-valid
`ai-memory` artifact.

## 5. Phase 0 — Preflight Contract Verification (gating)

No harness code is written until these four contracts are proven with a recorded
probe (a script plus its captured output, committed under `benchmarks/preflight/`).
Each open question below is a BLOCKER the first Codex review raised; Phase 0 is where
they get answered with evidence, not assumed.

1. **Ingestion + retrieval contract.** Determine the exact endpoint that makes a
   LongMemEval session both (a) retrievable by the chosen query surface and (b)
   returned with its `longmemeval_session_id` metadata intact. The first review
   recommends `POST /api/entities?sync=true` with `entity_type: session`,
   `skip_conflicts: true`, then `/api/search` with `types: ["session"]`,
   `include_documents: false`. Treat that as the hypothesis; the deliverable is a
   probe proving a `session` entity round-trips with metadata, or an alternative
   endpoint pairing that does.
2. **Retrieval-path semantics.** Confirm whether the chosen query surface ranks
   `session` records by query embedding (vector search) or by full-text/RRF/temporal
   signals. `/api/search`'s graph path is substantially lexical for some record
   types; if it does not use embeddings for `session` records, the harness must
   instead target the native retrieval path that does (`recall` / context-pack
   native search), and the artifact must record which signals actually ranked each
   result. The headline claim in Section 2 is only valid for whichever path is
   verified to use the native embedder.
3. **Readiness signal.** A namespace row count does not prove searchability. Define
   readiness as: after ingesting a haystack, poll the query surface until every
   expected `longmemeval_session_id` is returnable and carries the expected metadata
   and embedding dimensions. If ingestion can run synchronously (`sync=true`), prefer
   that and still verify retrievability before scoring. Fail loudly on timeout.
4. **Artifact contract.** Read `tools/bench/eval_gate.py` and enumerate every field
   the `ai-memory` profile requires. Confirmed so far: a non-empty `schema_version`
   and `suite`; `generated_at` or `timestamp`; a non-empty `command`; a `runtime`
   block carrying `retrieval_mode`; a `dataset` block with `name` and `corpus_hash`;
   a per-question summary under one of
   `per_type`/`per_slice`/`per_category`/`per_task`; and `mode` must equal
   `runtime.retrieval_mode` and be one of `raw`, `hybrid`, `native`, `compare`.
   `mode: live` is invalid and will fail the gate.

Phase 0 also resolves whether `longmemeval_s_cleaned.json` contains abstention
questions (Section 8).

## 6. Isolation Model

One throwaway tenant per question. Sibyl is namespace-per-org (`org_<uuid_hex>`), so
an isolated org is an isolated SurrealDB namespace — the native isolation primitive,
matching LongMemEval's per-question haystack exactly. Local signup creates a personal
org per user, so the harness signs up a throwaway user per question and ingests into
that org.

Rejected: a single org with per-question scope filters — that makes benchmark
integrity depend on filter correctness and risks cross-question leakage.

Concurrency: a bounded pool (`--concurrency`, default low — see Section 10), each
worker owning its own tenant and HTTP session. Teardown: CI instances are ephemeral
so per-question teardown is optional; local runs support `--cleanup`. Phase 1
measures tenant-creation overhead at scale; if it dominates, a pooled-tenant fallback
with a provably complete namespace wipe between questions is permitted.

## 7. Corpus Construction

Offline and live harnesses **must index byte-identical text per session**, or the
offline-versus-live delta measures corpus shape rather than runtime. Phase 1 extracts
one shared loader that emits canonical `(session_id, text, timestamp)` tuples. The
current offline `_build_corpus` joins user-role turns only, one document per session;
the shared loader preserves exactly that policy. The policy string (e.g.
`user-turns-only-v1`) is recorded in every artifact as `corpus_text_policy` so any
future change is visible in the receipts.

## 8. Metrics

The first Codex review established that the offline `recall_at_k` is
`float(any(correct in top_k))` — binary hit@k, not recall — and that most questions
have multiple answer sessions. The headline "recall@5 0.98" on existing artifacts is
therefore hit@5 and overstates quality. The shared scorer defines three explicitly
named metrics:

- `hit@k` — 1.0 if any answer session appears in the top k. This is the legacy
  offline metric, retained under its honest name for backward comparability.
- `recall@k` — true recall: `|answer_sessions ∩ top_k| / |answer_sessions|`.
- `ndcg@k` — standard nDCG where the ideal ranking is computed over all answer
  sessions for the question, not only the retrieved ones.

Headline reporting uses `recall@k` and `ndcg@k`. `hit@k` is reported alongside,
labelled legacy. The offline bench is repointed at the shared scorer; to preserve its
historical numbers it keeps emitting `hit@k` under a `legacy_` prefix in addition to
the new metrics. Both harnesses score through this one module — that is the
comparability guarantee.

## 9. Report Artifact

The artifact conforms to the `ai-memory` ledger schema and passes
`bench-gate --profile ai-memory` (the field list is finalised in Phase 0). Concretely:

- `schema_version` and `suite`: both non-empty, per the `ai-memory` ledger schema —
  the `bench-gate` validator rejects an artifact missing either.
- `mode`: `native` (a gate-allowed value), equal to `runtime.retrieval_mode`.
- `runtime.runtime_mode`: `live-api` — this is where liveness is expressed.
- `runtime`: also `graph_engine: surreal`, `store: surreal`, `embedding_provider`,
  `embedding_model`, `embedding_dimensions`, `tokenizer_estimate_method`.
- `generated_at`, `command`, `sibyl_commit`, `repeat_count`, `auth_manifest_id`.
- `dataset`: `name`, `corpus_hash`, `total_entries`, `evaluated_entries`, `limit`,
  plus `corpus_text_policy`.
- `overall`: `recall@5`, `recall@10`, `ndcg@5`, `ndcg@10`, `hit@5`, `hit@10`.
- `per_type`: metrics broken down by LongMemEval question type.
- `case_results`: per-question records including ranked result IDs and which
  retrieval signals ranked them (per finding 2 of Phase 0).
- `claim_boundary`: "Live API runtime evidence: real SurrealDB store and the verified
  retrieval path, with per-question isolation via throwaway org namespaces. Retrieval
  signals per result are recorded in case_results."

The `ai-memory` gate currently sets no numeric thresholds. This spec does not invent
them. Phase 4 sets them from the first full-run baseline, with margin.

## 10. Temporal Questions — Known Limitation

The first review found that preserving session timestamps into `valid_at` does **not**
make retrieval temporally aware: `SearchRequest` exposes no `as_of` / reference-time
parameter, and temporal boosting is computed relative to now. So the harness ingests
real timestamps but does not claim temporal-aware retrieval for temporal-reasoning
questions. Those questions are still scored, and the `per_type` breakdown will expose
how the runtime does without temporal grounding — itself useful evidence. An
eval-time `as_of` path is explicitly out of scope here and noted as future work.

## 11. Abstention

Whether LongMemEval-S-cleaned contains abstention questions (correct answer: "not in
history") is resolved in Phase 0. If present, the shared scorer gets an explicit
abstention branch with fixtures, and the harness needs a no-result/threshold policy
since `/api/search` otherwise always returns top-k. If absent, the abstention claim
is dropped entirely. No abstention handling is asserted until Phase 0 settles this.

## 12. CLI: `benchmarks/longmemeval_live.py`

Arguments: positional `dataset`; `--api-url` (default `http://localhost:3334/api`);
`--limit N`; `--concurrency N`; `--output PATH`; `--label`; `--metadata key=value`;
`--cleanup`; `--embedding-rps` (client-side rate cap). It fails fast with a clear
message if the stack is unreachable or a required key is absent, and reuses the
shared corpus loader and scorer.

## 13. CI Jobs

Two jobs, not one, added to `.github/workflows/eval.yml`:

- `longmemeval-live-smoke` — small `--limit` (e.g. 25), low concurrency, runs on
  every `workflow_dispatch`; bounded and reliable.
- `longmemeval-live-full` — the 500-question run, separate `workflow_dispatch` path
  with a raised `timeout-minutes`, gated and artifact-uploaded.

Both reuse the nightly environment recipe (SurrealDB, moon toolchain, backend,
worker) and the real `OPENAI_API_KEY` secret. The dataset is fetched from Hugging
Face (`xiaowu0162/longmemeval-cleaned`, `longmemeval_s_cleaned.json`), SHA-256
verified against `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`,
and cached by hash. Embedding calls use explicit client-side rate limiting and retry
with backoff; the artifact records token, request, and retry counts. Cost is small —
embeddings only, plausibly one to two dollars for a full run — the real budget is
wall-clock, controlled by `--limit` and `--concurrency`.

## 14. Phasing

- **Phase 0 — Preflight (gating).** Section 5. Recorded probes for the ingestion,
  retrieval-path, readiness, and artifact contracts, plus the abstention question.
  No harness code until these are proven.
- **Phase 1 — Shared scoring and corpus.** Extract the shared `(session_id, text,
  timestamp)` loader and the three-metric scorer (`hit@k`, `recall@k`, `ndcg@k`).
  Repoint `longmemeval_bench.py` at both. Verify: offline `hit@k` is unchanged versus
  a pre-extraction run on the same dataset.
- **Phase 2 — Live harness.** Build `longmemeval_live.py` against the Phase 0
  contracts. Runs locally against `moon run dev`. Verify with `--limit 10`: a
  schema-valid artifact, and per-question isolation proven (a question's results
  never contain another question's sessions). Record tenant-creation overhead.
- **Phase 3 — CI jobs.** Add the smoke and full jobs. Verify: a smoke
  `workflow_dispatch` run is green and uploads a gate-valid artifact.
- **Phase 4 — Ledger integration.** Run the full 500-question evaluation, commit the
  artifact as a citable row in `benchmarks/results/ai-memory/manifest.json`, set
  `ai-memory` thresholds from the observed baseline with margin, and document the
  suite in `benchmark-methodology.md`.

## 15. Definition of Done

- Phase 0 probes are committed and the four contracts are proven, not assumed.
- A shared corpus loader and three-metric scorer back both harnesses; offline `hit@k`
  is unchanged.
- `longmemeval_live.py` runs locally and emits a gate-valid `ai-memory` artifact with
  verified per-question isolation.
- The smoke and full CI jobs run on `workflow_dispatch`, are green, and gate.
- A full 500-question artifact is a citable manifest row whose `claim_boundary` is
  live runtime evidence and whose metrics are named honestly.
- `benchmark-methodology.md` documents the suite.

## 16. Recommendation

Build it, Phase 0 first. The offline bench stays as an algorithm baseline; the
offline-versus-live delta is informative once the shared corpus and scorer make it a
fair comparison. The live harness is the only artifact that can honestly say "this is
how well Sibyl retrieves" — but only if the metrics are named for what they measure
and the retrieval path is the one actually verified to use the native embedder. The
first Codex review's core warning stands as this spec's first principle: a benchmark
that passes its gate with numbers that do not mean what their labels claim is worse
than no benchmark.
