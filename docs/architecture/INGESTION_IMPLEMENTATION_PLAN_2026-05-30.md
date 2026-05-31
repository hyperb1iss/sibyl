<!-- markdownlint-disable MD013 -->

# Sibyl Ingestion & Knowledge Implementation Specification — 2026-05-30

> **What this is.** One authoritative, dependency-ordered implementation specification for the
> knowledge-ingestion initiative, reconciled against the two plans it touches:
> `POST_GRAPHITI_PLAN_2026-05-19.md` (extraction restoration + SurrealDB 3.0 adoption) and
> `DATA_MODEL_IMPROVEMENT_PLAN_2026-05-30.md` (schema/tenant/perf hardening). Research basis:
> `KNOWLEDGE_INGESTION_RESEARCH_2026-05-30.md`.
>
> **Goal.** Take Sibyl from "can capture a corpus but can't digest it" to a working second-brain
> ingestion path: conversation logs, docs, and decades of email loaded behind one unified Source
> Adapter, extracted, promotable, and recallable — without duplicating or contradicting the
> extraction and data-model work already committed.
>
> **Release posture.** This is post-1.0 north-star work unless Bliss explicitly pulls M1/M2 into the
> RC window. It should not compete with the active evidence-freeze track.

---

## Reconciliation summary — what changed since the source plans

The two prior plans were written against an older reality. Verified state as of 2026-05-30:

| Assumption in a prior plan                                         | Reality now (verified)                                                                                                                                                                       | Consequence                                                                                                                       |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Server v3.0.5, SDK pinned `<3.0`, bump to 2.0.0 (POST_GRAPHITI C1) | SDK **is** `>=2.0.0,<3.0` (×4: api + core); server **v3.1.0** (`docker-compose*.yml`)                                                                                                        | **C1 landed.** SDK 2.0.0 on a 3.1 server already runs `CHANGEFEED`/`DEFINE EVENT`/`COMPUTED`/`DEFINE FUNCTION` via raw SurrealQL. |
| `search::rrf` hand-rolled in Python (POST_GRAPHITI C3)             | Native `search::rrf` live at `retrieval/search.py:1941` with Python fallback (`retrieval/fusion.py`)                                                                                         | **C3 landed.** Do not re-do.                                                                                                      |
| "Restore LLM entity extraction" is unbuilt (POST_GRAPHITI B1)      | `jobs/memory_extraction.py` exists (`extract_memory_entities`, `enqueue_memory_extraction_batches`) and **fires on entity creation** (`jobs/entities.py`, `routes/entities.py`, `worker.py`) | **B1 engine exists** for the entity/episode lane. Ingestion **extends** it to `raw_memory`; it does not build it.                 |
| Schema versioning is graph-only (DATA_MODEL W1.1)                  | `schema_version.py` now backs **both** `schema.py` and `content_schema.py`; content is at `CONTENT_SCHEMA_CURRENT_VERSION = 7` with named migrations                                         | Content-plane migration infra exists. Ingestion DDL must add named content migrations, not ad-hoc bootstrap statements.           |

**The "driver conflict" I flagged is largely resolved by reality.** The real Phase-0 risk is not the
SDK pin — it is the **test lane**: unit tests run SurrealDB 2.x `memory://` (embedded) and silently
rewrite `FULLTEXT`→`SEARCH`, so 3.x-only SurrealQL (`CHANGEFEED`, `DEFINE EVENT`,
`search::highlight`) passes units while being unverified against the live v3.1.0 server. See the
MEMORY note "SurrealDB 2.x-vs-3.x test gap."

**Decision resolved (SDK 3.0.0):** stay on `surrealdb 2.0.0`. POST_GRAPHITI C1 itself defers 3.0.0
("breaking change to `query()` return shape — every statement returns"). 2.0.0 already unlocks every
server feature this plan needs. Revisit 3.0.0 as its own sweep, not on the ingestion critical path.

---

## Status ledger — do not redo these

Verified landed (treat as done; build on them):

- SDK 2.0.0 + server v3.1.0 (POST_GRAPHITI C1).
- Native `search::rrf` hybrid fusion (POST_GRAPHITI C3).
- Extraction engine `memory_extraction.py`, wired to entity creation (`POST_GRAPHITI` B1, engine
  only).
- Generic `SourceAdapter` protocol + `import_source_batch` spine + resumable import jobs +
  `import_source_archive` registered as a worker job (`jobs/worker.py`).
- mbox/maildir mailbox adapter satisfying the protocol.
- Verbatim `raw_captures` + 7-level memory-scope + private-by-default SQL recall + reflection
  promote lane.
- `schema_version` infra imported by graph + content schemas.
- Content schema versioned migrations, content URL org/source scoping, DB-level content-table
  permissions, and `document_chunks.organization_id/source_id` are present.

Verified NOT done (this plan or the source plans own them):

- Extraction never fires from the **import path**; worker filters to `{episode, session}`
  (`memory_extraction.py:31,292`). → **M2.3 (extends B1)**
- No promotion bridge from imported `raw_memory` (`memory-promote` is reflection-only,
  `main.py:2263`). → **M2.5**
- `raw_captures` has BM25 on `title`/`raw_content` but **no embedding field/vector index**;
  `entity_id` is indexed (`10_tables.surql:133`) but not mapped on `RawMemory` and never written. →
  **M2.1, M2.2, M2.4, M2.5**
- No `(organization_id, dedupe_key)` index; dedup key bakes in `source_version`. → **M1.2**
- `/sources` routes live on the crawler router (`routes/crawler.py:125,455-588`); import awaited
  inline though the worker job exists. → **M1.1, M1.3**
- No conversation-log adapters; no docs API/CLI; no IMAP; no per-record sensitivity classifier. →
  **M3, M4, M5**
- `entity.updated_at` is still a string (DATA_MODEL W3.3); `raw_captures` is shared-namespace
  row-permission isolated, not graph namespace isolated (DATA_MODEL W6.4 honesty guard); A1 episode
  duality is still present (`EntityType.EPISODE`, `entities.py:20,192`).

---

## Implementation contracts

These invariants are part of the spec. If a milestone cannot preserve them, stop and adjust the
milestone rather than bending the contract.

### Pipeline contract

The canonical ingestion pipeline is:

```text
adapter discovery
  -> bounded SourceRecord batches
  -> verbatim raw_captures write
  -> idempotent dedupe/supersession
  -> raw promotion into document_chunks
  -> optional entity extraction
  -> promotion/share through the memory policy gate
  -> recall across raw, chunks, and graph candidates
```

- `raw_captures` is the source-of-truth tier. Chunks, extracted graph entities, and lineage edges
  are rebuildable indexes over it.
- Adapter normalization may add metadata, stable ids, and redacted display fields, but must not
  mutate `raw_content`.
- Every stage is idempotent and resumable. Re-running a job converges instead of duplicating rows or
  silently skipping changed content.
- Background enrichment may lag capture, but status must say what is pending. A capture with no
  chunks/entities is not "fully ingested."
- 3.x-only SurrealQL (`CHANGEFEED`, `DEFINE EVENT`, `search::highlight`) must be covered by the live
  v3.1 lane before becoming required runtime behavior.

### Source identity and dedup contract

Use two independent concepts:

- **Record identity:** `adapter_name + source_identity + adapter_record_id`. This is
  version-insensitive and survives re-export, IMAP resync, and local file moves when the adapter can
  prove the same logical source record.
- **Content identity:** normalized `content_hash` for the emitted body plus any semantics-bearing
  title/metadata the adapter declares part of the record.

`dedupe_key = hash(record_identity, content_hash)`. `source_version` is stored for provenance and
checkpoints but must not participate in dedupe. Exact same record/content returns the existing raw
id. Same record identity with a different content hash writes a new `raw_captures` row, marks the
older row `superseded`, and links the new row to the old one through metadata now and a
`SUPERSEDES`/`supersedes` edge once M6.1 lands.

### Scope and trust contract

- PRIVATE is the default for personal, private, sensitive, and conversation-log adapters.
  PROJECT/ORGANIZATION/PUBLIC require explicit scope selection and the existing promotion/share
  preview gates.
- Imported mail cannot run against real mailboxes until per-record sensitivity classification lands.
  The deterministic floor runs by default; LLM refine is optional BYOK and budget-gated.
- `raw_captures` lives in the shared content namespace with `organization_id` predicates and table
  permissions. Do not market it as graph namespace isolation until DATA_MODEL W6.4 changes that.
- Performance fixes scale the write path: pooled clients, batched writes, worker drain, and indexed
  queries. Do not solve import pressure by lowering product capacity unless it is a named temporary
  mitigation with a removal path.

### Adapter contract

Each adapter implements the existing `SourceAdapter` protocol:

- `descriptor`: stable adapter name/version/source type, default privacy class, transform behavior,
  capabilities, and metadata schema.
- `prepare_manifest`: returns source identity, version, URI, target scope, and import options.
- `iter_records`: yields bounded `SourceRecordBatch` values with a checkpoint cursor, skipped-record
  receipts, and stable record ids.

Each `SourceRecord` must include: `adapter_record_id`, `source_id`, `content_hash`, `dedupe_key`,
`source_type`, `source_uri`, `source_version`, `title`, `body`, privacy/transform behavior, optional
`occurred_at`, `participants`, `labels`, and adapter-specific metadata. New adapters add fixtures
from real samples plus malformed input cases.

### State and observability contract

Do not overload "imported" to mean "digested." Track the stages separately:

- `source_imports.status`: transport/checkpoint state for adapter drain.
- `raw_captures.metadata.raw_promotion_state`: `pending`, `promoted`, `failed`,
  `skipped_superseded`, or `skipped_deleted`.
- `raw_captures.metadata.source_extraction_state`: `disabled`, `queued`, `extracted`, `failed`, or
  `not_projectable`.
- `raw_captures.metadata.sensitivity_state`: `classified`, `needs_review`, or `classifier_failed`.

Status responses should expose counters for imported, deduped, superseded, promoted,
promotion-failed, extraction-queued, extraction-failed, classified-sensitive, and classified-secret
records. This is the difference between "the file loaded" and "the brain can use it."

---

## Critical path

```text
M0 (live 3.x test lane + content migration receipt)
   └─> M1 (decouple /ingestion router + idempotent dedup)
          └─> M2 (close the loop: promote_raw_captures)
                                   │  (extends POST_GRAPHITI B1 engine)
                                   ├─> M3 (conversation-log adapter — dogfood)
                                   ├─> M4 (docs API + CLI)
                                   └─> M5 (email consolidation + sensitivity)
                                          │
                                          v
                          M6 (native lineage + CHANGEFEED extraction)  [needs M0 3.x lane]
                          M7 (correctness + honesty: isolation, bi-temporal, faceted UX)
```

**The gate that matters:** nothing in M3/M4/M5 produces knowledge until **M2** lands. Ship M2 before
or with the first adapter, or each feature just grows the inert verbatim pile.

---

## Milestone 0 — Foundations & 3.x truth (prerequisite, small)

### M0.1 — Live 3.x integration test lane

- **Goal:** stop trusting 2.x `memory://` unit-green as 3.x-correct; exercise 3.x-only SurrealQL
  (`FULLTEXT`/`HIGHLIGHTS`, `CHANGEFEED`, `DEFINE EVENT`, `search::` builtins) against a real v3.1.0
  (container or embedded surrealkv).
- **Files:** test harness/config under `apps/api/tests/`, `packages/python/sibyl-core/tests/`, moon
  task wiring.
- **Depends on:** none. **Ownership:** sharpens POST_GRAPHITI C1/C11; addresses MEMORY 2.x-vs-3.x
  gap.
- **Verify:** a test that asserts a `DEFINE EVENT` / `CHANGEFEED` statement parses and runs on the
  live lane but is correctly skipped/xfail on 2.x `memory://`.
- **Effort:** M. **Blocks:** M6 (anything CHANGEFEED/EVENT).

### M0.2 — Content migration receipt + ingestion migration template

- **Goal:** treat content-plane versioning as present and document exactly how ingestion DDL enters
  it. New fields/indexes (`raw_captures.embedding`, `idx_raw_captures_embedding`,
  `idx_raw_captures_org_dedupe`) must land as named content migrations after version 7.
- **Files:** `backends/surreal/content_schema.py`, `schemas/content/*.surql`, `schema_version.py`;
  schema tests under `apps/api/tests/test_surreal_content_persistence.py`.
- **Depends on:** none. **Ownership:** ingestion extends already-landed DATA_MODEL W1/W2.2.
- **Verify:** empty bootstrap + existing-DB bootstrap + second-bootstrap-no-op for the new ingestion
  migration; fixture DB at content schema v7 upgrades to v8 without rewriting old migrations.
- **Effort:** XS–S.

---

## Milestone 1 — Decouple ingestion + idempotent dedup (quick wins)

Low blast radius, immediately shippable, no crawler or deep-pipeline changes.

### M1.1 — Neutral `/ingestion` router

- **Goal:** move the four adapter-import routes off the crawler router into a first-class
  `/ingestion` surface; keep the `/memory/source-imports` read alias the CLI calls.
- **Files:** new `apps/api/src/sibyl/api/routes/ingestion.py`; `routes/crawler.py:455-588` (remove);
  `api/app.py` (register); `api/schemas/` (move import schemas); route tests.
- **Depends on:** none. **Ownership:** net-new.
- **Verify:** route tests for delegation + the retained read alias; OpenAPI shows `/ingestion/*`.
- **Effort:** S.

### M1.2 — Idempotent two-axis dedup

- **Goal:** re-exported corpora must converge, edited resends must supersede (not silently skip).
- **Implementation:** keep `SourceRecord.source_id` as the stable record identity, but stop treating
  "same source id" as an automatic duplicate. Make `build_source_dedupe_key` version-insensitive
  (drop `source_version` from the key) and define
  `dedupe_key = hash(record_identity, content_hash)`. The duplicate checker first looks up
  `(organization_id, metadata.dedupe_key)` for exact duplicates; if the same `source_id` exists with
  a different content hash, write the new capture, mark the older capture
  `review_state = "superseded"`, add `metadata.superseded_by_raw_memory_id` on the old row, and add
  `metadata.supersedes_raw_memory_id` on the new row. M6.1 materializes this metadata as
  `supersedes`/`SUPERSEDES` edges.
- **Files:** `services/source_adapters.py:203-226`; `jobs/source_imports.py` (duplicate checker);
  `services/surreal_content.py` (`RawMemory` metadata helpers); `schemas/content/*.surql` (index)
  via the M0.2 runner; tests.
- **Depends on:** M0.2. **Ownership:** net-new (lineage edge aligns with M6.1).
- **Verify:** re-import same mbox twice → zero new captures; edit one message and re-import → one
  new capture, older capture hidden from recall as `superseded`, provenance links in both
  directions, original retained for audit.
- **Effort:** M.

### M1.3 — Dispatch imports to the worker job

- **Goal:** stop running import batches inside the HTTP request lifecycle.
- **Implementation:** `/ingestion/imports` creates a persisted `source_imports` run and enqueues a
  background drain. The drain may be a thin `drain_source_import(import_id)` wrapper over repeated
  `resume_source_import`, while `import_source_archive` remains the bounded-batch primitive already
  registered in `jobs/worker.py`. HTTP returns the run id immediately and never loops batches
  inline.
- **Files:** `routes/ingestion.py` (was `crawler.py:483,538`); `jobs/source_imports.py` (drain
  loop + status broadcasts); `jobs/worker.py` (register new wrapper if needed).
- **Depends on:** M1.1. **Ownership:** net-new wiring (primitive exists).
- **Verify:** a large import returns immediately with a run id; status endpoint shows progress;
  drain converges to `completed`; cancel blocks future drain; stale policy context is rechecked on
  resume.
- **Effort:** M.

---

## Milestone 2 — Close the loop: `promote_raw_captures` (highest leverage)

The single most important milestone. Until this lands, every adapter feeds inert text.

### M2.1 — Vector path on `raw_captures`

- **Goal:** give raw memory the semantic recall it lacks today (BM25-only).
- **Implementation:** add `raw_captures.embedding` + an HNSW index (match `document_chunks`
  dims/params: `DIST COSINE TYPE F32`, EFC/M per current config); add `embedding` to `RawMemory` and
  the record mapper. Capture-level embeddings are recall hints, not a replacement for chunk
  embeddings. Use a safe bounded text surface (`title + raw_content` truncated by the configured
  embedding limit) and record provider/model/dim metadata.
- **Files:** `schemas/content/10_tables.surql` + content migration via M0.2;
  `services/surreal_content.py` (model + mapper + embed-on-write helper); tests.
- **Depends on:** M0.2. **Ownership:** net-new.
- **Verify:** `EXPLAIN FULL` shows the HNSW index used on a KNN raw recall; embedding written for
  new captures.
- **Effort:** M.

### M2.2 — `jobs/raw_promotion.py promote_raw_captures`

- **Goal:** the job that turns verbatim captures into chunks + graph index.
- **Implementation:** read pending `raw_captures`, chunk `raw_content` (pluggable by `media_type` /
  `source_type`), embed chunks, write `document_chunks` with `document_id = raw_capture.uuid`,
  `organization_id = raw_capture.organization_id`, and `source_id = raw_capture.source_id`;
  graph-link; populate the indexed-but-unwritten `raw_captures.entity_id` and map it on `RawMemory`.
  Reuse the live crawler's chunk/embed/persistence components rather than wrapping
  `sibyl.crawler.pipeline._process_document` directly; the direct wrapper would inherit crawler
  extraction behavior and violate the import rule that LLM extraction stays off unless
  `settings.auto_extract_entities` is enabled. Register in `jobs/worker.py`; chunk/embed promotion
  itself should run whenever ingestion drain requests it. If chunk-level entity linking from
  `_process_document` is still desired, port it as a separate gated follow-up instead of smuggling
  it through the crawler wrapper.
- **Files:** new `apps/api/src/sibyl/jobs/raw_promotion.py`; `jobs/worker.py`; reuse crawler
  chunker/embedder/persistence components; `services/surreal_content.py` (`entity_id` mapping);
  tests.
- **Depends on:** **M2.1**. Content org/source scope fields already exist; M2.2 must populate them.
- **Ownership:** net-new (the orchestration); reuses the crawler pipeline.
- **Verify:** a captured email/transcript yields embedded `document_chunks`, scoped chunk fields,
  and a populated `entity_id`; re-running the job does not duplicate chunks or graph nodes;
  deleted/superseded captures are skipped.
- **Effort:** L.

### M2.3 — Extend extraction to `raw_memory` (extends POST_GRAPHITI B1)

- **Goal:** the extraction engine exists but skips imports. Widen it.
- **Implementation:** enqueue extraction from `promote_raw_captures` using normalized source
  payloads, or relax `_PROJECTABLE_MEMORY_TYPES` (`memory_extraction.py:31,292`) behind an explicit
  import-lane path. **Coordinate with POST_GRAPHITI B1/B2** — reuse the existing memory batch
  extractor and projection path; do not fork a second extractor. Extraction remains off unless
  `settings.auto_extract_entities` is enabled.
- **Files:** `jobs/memory_extraction.py`; `jobs/raw_promotion.py`; tests.
- **Depends on:** M2.2; the B1 engine (present). **Ownership:** extends POST_GRAPHITI B1.
- **Verify:** an imported capture produces extracted entities + edges, off by default, on behind
  `auto_extract_entities`; LongMemEval-style ablation if feasible.
- **Effort:** M (because the engine exists).

### M2.4 — Metadata-aware + vector raw recall

- **Goal:** "emails from a person in 2014 about a topic" must work.
- **Implementation:** extend `recall_raw_memory` (`surreal_content.py:983-990`) to filter on
  `participants`/`occurred_at`/`labels`/`thread_id` and add a vector path over
  `raw_captures.embedding`, fused with BM25 via the existing `search::rrf`.
- **Files:** `services/surreal_content.py`; recall API/CLI; tests.
- **Depends on:** M2.1. **Ownership:** net-new (reuses `search::rrf`).
- **Verify:** metadata-filtered + semantic recall returns the right capture; parity test vs
  BM25-only baseline.
- **Effort:** M.

### M2.5 — Generalize `memory-promote` to imported captures

- **Goal:** one promotion path for all source families, not just reflection candidates.
- **Implementation:** current reflection promotion rejects non-`reflection_candidate` rows via
  `_is_reflection_candidate`; keep that behavior for reflection commands and add a general
  raw-source promotion service/route. `memory-promote` (`main.py:2263`) should accept either a
  reflection candidate or an imported `raw_memory` id, route to the right service, and move
  PRIVATE→PROJECT/SHARED through the existing promotion/share preview gate. Dream/reflection source
  collection can already read non-reflection raw captures, but it does not replace imported-capture
  promotion.
- **Files:** `apps/cli/src/sibyl_cli/main.py`; promotion service; API; tests.
- **Depends on:** M2.2. **Ownership:** net-new.
- **Verify:** an imported private capture promotes to PROJECT via the preview gate; scope enforced
  in SQL recall.
- **Effort:** M.

---

## Milestone 3 — Conversation-log adapter (dogfood the loop)

Build on the proving-ground corpus Bliss generates daily. Pure adapter work — the spine and
`promote_raw_captures` are reused verbatim.

### M3.1 — `transcript_adapters.py`

- **Goal:** `ClaudeCodeJsonlAdapter` (`source_type=agent_transcript`) + `CodexJsonlAdapter`,
  registered like the mailbox adapter; default scope per decision below; RAW transform.
- **Formats:** Claude `~/.claude/projects/<slug>/<uuid>.jsonl`; Codex
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` + `~/.codex/history.jsonl`.
- **Implementation:** lift mempalace normalize hardening (`convo_miner.py`: per-line
  `json.loads(errors=replace)`, line-anchored noise stripping, project from record `cwd`);
  **Sibyl-native dedup ids** (Claude turn `uuid`; Codex `session-id+line-index` for messages,
  `call-id` for tool pairs) — not mempalace's file+chunk scheme; one `SourceRecord` per turn; fold
  `tool_result`-only user records into the prior assistant turn; tag `isCompactSummary` as
  `metadata.kind`.
- **Files:** new `packages/python/sibyl-core/src/sibyl_core/services/transcript_adapters.py`;
  `ensure_*_registered()`; tests + fixtures from real samples.
- **Depends on:** M2. **Ownership:** net-new.
- **Verify:** import a real session dir; turns become captures; re-import is idempotent on turn
  uuid.
- **Effort:** L.

### M3.2 — Thread/fork edges

- **Goal:** capture `parentUuid`/`forked_from`/subagent nesting as metadata the graph stage turns
  into `REPLIES_TO`/`FORKED_FROM`/`SPAWNED_SUBAGENT` edges.
- **Files:** adapter metadata; `promote_raw_captures` edge mapping; tests.
- **Depends on:** M3.1, M6.1 (lineage edges) ideal but can start with `relates_to`. **Effort:** M.

### M3.3 — CLI

- **Goal:** `sibyl ingest claude-code <dir> --scope private --drain`;
  `sibyl ingest codex <dir> --drain`.
- **Files:** new `apps/cli/src/sibyl_cli/ingest.py`; register in `main.py`. **Depends on:** M1.1,
  M3.1. **Effort:** S.

> **Scope decision.** Default to PRIVATE. When a transcript directory maps to a linked Sibyl
> project, the CLI may suggest `--scope project`, but it must not widen scope without an explicit
> flag.

---

## Milestone 4 — Docs API + CLI

### M4.1 — `document_adapters.py`

- **Goal:** `File` / `Folder` / `Url` (single-fetch, **no** discovery BFS) / `Text` adapters;
  `source_type=document`; default PROJECT, NORMALIZED. Folder reuses `LocalFileCrawler` globbing;
  `Url` reuses `CrawlerService.crawl_page` without the BFS. Dedup by abs-path / canonical-URL /
  `sha256(text)`.
- **Files:** new `services/document_adapters.py`; reuse `crawler/local.py`, `crawler/service.py`;
  tests.
- **Depends on:** M2. Content URL scope + chunk org/source scope are already present; document
  adapters must use those fields. **Ownership:** net-new.
- **Effort:** M.

### M4.2 — Docs API

- **Goal:** `POST /ingestion/documents` (discriminated `kind`: file/folder/url/text);
  `GET /ingestion/collections`.
- **Files:** `routes/ingestion.py`; schemas; tests. **Depends on:** M1.1, M4.1. **Effort:** S.

### M4.3 — Docs CLI

- **Goal:** `sibyl docs add <dir> --recursive --collection`; `sibyl docs add <url>`;
  `sibyl docs paste`; `sibyl docs list`.
- **Files:** `apps/cli/src/sibyl_cli/document.py` (exists — wire it up; it is not currently a
  registered typer); `main.py`. **Depends on:** M4.2. **Effort:** S.

> **Boundary justification (critic's flag).** Concrete delta over today's crawler: (1) documents
> gain the verbatim/provenance/promotion lifecycle by routing through `raw_captures`; (2)
> single-URL/paste/folder ingest **without** producing crawler graph-Entities with `CRAWLED_FROM`.
> If, on build, the only delta is "routes through `raw_captures`," fold M4 into M2 rather than
> shipping it as a separate feature.

---

## Milestone 5 — Email consolidation + sensitivity (the 20-year payoff)

Highest-value corpus, highest risk. The mailbox adapter already satisfies the protocol, so this is
register-plus-route plus the trust gate and a scaled write path.

### M5.1 — Fold mbox/maildir into the unified interface

- **Goal:** route the existing adapters through `promote_raw_captures`. **Files:**
  `services/mailbox_adapter.py`; registration. **Depends on:** M2. **Effort:** S.

### M5.2 — Read-only `ImapSourceAdapter`

- **Goal:** SELECT only (never STORE/EXPUNGE); `source_version = UIDVALIDITY`; UID-range cursor so a
  UIDVALIDITY change re-syncs; `Message-ID` + version-insensitive hash so mbox export and live IMAP
  converge to one capture. Treat Gmail Takeout `X-Gmail-Labels` as faceting metadata.
- **Files:** new adapter; tests with a fake IMAP server. **Depends on:** M1.2 (dedup), M2.
  **Effort:** L.

### M5.3 — Per-record sensitivity classifier

- **Goal:** the trust gate before real personal mail. `services/sensitivity.py classify_record`:
  deterministic floor (Luhn / SSN / API-key / high-entropy / 2FA patterns) emitting
  `contains_pii`/`contains_secret`/`sensitivity_flags` the **autonomy engine already consumes**
  (`memory_autonomy.py:248-271`); optional BYOK LLM refine; `contains_secret` escalates that record
  to SENSITIVE.
- **Implementation:** deterministic floor always runs before write. Optional LLM refine only runs
  when a provider is configured and budget allows it; refine may add flags or lower false positives,
  but it must not downgrade a deterministic `contains_secret` result.
- **Files:** new `services/sensitivity.py`; wire into `import_source_batch` write step before
  `remember_raw_memory`; tests with synthetic secrets.
- **Depends on:** none (can build in parallel); must land **before** M5.1/M5.2 ship against real
  mail. **Ownership:** net-new.
- **Effort:** M.

### M5.4 — Scale the write path (not a nerf)

- **Goal:** a 20-year mailbox needs throughput, not throttling. Shared pooled, batched
  `RawMemoryRememberer` replacing per-record connection churn; autonomous background drain with
  cross-worker coordination.
- **Files:** `services/source_adapters.py` (`RawMemoryRememberer`); `jobs/source_imports.py`
  (drain); pool config.
- **Depends on:** M1.3. **Ownership:** net-new. **Guardrail:** bounded batches are fine for
  checkpoints and memory safety, but throughput regressions must be fixed with pooling, bulk writes,
  and worker coordination, not by lowering batch size or rate-limiting as the durable answer.
- **Effort:** M.

---

## Milestone 6 — Native lineage + reactive extraction (DB-native depth)

Gated on M0.1 (live 3.x lane). Aligns with POST_GRAPHITI C-series and DATA_MODEL W6.

### M6.1 — Lineage as `RELATE` edges

- **Goal:** replace JSON-blob/string-FK provenance with `derived_from` (raw→source), `chunk_of`
  (chunk→document), `extracted_into` (entity→chunk), `supersedes`; backfill from existing
  `source_id`/`document_id`/`entity_ids`. Makes "why do we believe this fact?" one graph walk.
- **Files:** `schemas/content/*.surql`; `services/surreal_content.py`; backfill migration; tests.
- **Depends on:** M2; aligns with **POST_GRAPHITI C8** (REFERENCE) + **DATA_MODEL W6.2** (record
  references) — coordinate so we don't build two link models.
- **Effort:** L. **Risk:** string-FK→edge migration must be a backfill, not in-place ALTER (collides
  with namespace routing).

### M6.2 — CHANGEFEED-driven incremental extraction

- **Goal:** decouple capture from enrichment. `CHANGEFEED` on `raw_captures`; a background consumer
  reads `SHOW CHANGES SINCE` its durable cursor and does embed+extract; a crashed worker resumes
  without loss. Replaces inline execution.
- **Files:** schema (`CHANGEFEED`); new consumer in `jobs/`; cursor persistence; tests on the 3.x
  lane.
- **Depends on:** **M0.1**, M2.2, M2.3. **Ownership:** aligns POST_GRAPHITI B4 infra. **Effort:** L.

### M6.3 — `search::highlight` snippets + code-aware analyzer

- **Goal:** server-rendered recall snippets; a code-aware analyzer for the doc/transcript corpus.
- **Files:** schema analyzers; recall response; web/CLI render. **Depends on:** M0.1. **Effort:**
  S–M.

---

## Milestone 7 — Correctness & honesty (shared with the other plans)

These are differentiators and honesty guards, sequenced last; each largely **owned by the other
plans** — listed here so the ingestion story declares its dependencies.

- **M7.1 — Namespace-isolation honesty for `raw_captures`** (row predicates/table permissions exist;
  graph-style namespace-per-org isolation does not). Document the asymmetry precisely before
  marketing per-org isolation, or move content captures into per-org namespaces. → **DATA_MODEL
  W6.4.**
- **M7.2 — Bi-temporal as-of recall** (validity windows + contradiction invalidation). →
  **POST_GRAPHITI B4.**
- **M7.3 — Write-time entity resolution** via the existing HNSW KNN primitive (replace batch-only
  dedup). → **DATA_MODEL W4.2.**
- **M7.4 — Faceted browse UX** (source/person/date/label/scope) + one unified cross-source search;
  pin one authoritative LongMemEval run and reconcile the three in-repo number pairs.
  - **Doc pin:** canonical LongMemEval-S claim is run `26304777971`, commit `36032a25`, `96.96%`
    strict R@5 and `98.90%` R@10. Older `96.67%`/`98.68%` values remain only as historical
    score-progression evidence in `docs/testing/longmemeval.md`.

---

## Decisions and assumptions

1. **SDK 3.0.0?** — RESOLVED: stay on `surrealdb 2.0.0`; defer SDK 3.0.0 to its own sweep.
2. **Conversation-log default scope?** — RESOLVED: PRIVATE by default; suggest PROJECT only when a
   linked project is detected and the user passes `--scope project`.
3. **Reflection/dream overlap?** — RESOLVED for this spec: dream source collection can read
   non-reflection raw captures, but `memory-promote` still rejects imported captures via the
   reflection-candidate gate. M2.5 remains necessary.
4. **M2.2 vs DATA_MODEL W2.2 ordering?** — RESOLVED: content scope fields and migrations are
   present. `promote_raw_captures` must populate them.
5. **Sensitivity LLM posture?** — RESOLVED: deterministic floor by default; optional
   BYOK/provider-budget refine; never downgrade deterministic secret flags.
6. **Release posture?** — ASSUMED: post-1.0 north-star. M1/M2 can be pulled forward only if they do
   not disturb the RC evidence freeze.

---

## Definition of done

The implementation is not complete when adapters can write rows. It is complete when the loop is
observable end to end.

Minimum acceptance for the first dogfood cut:

- `/ingestion/imports` returns a run id immediately and a background drain completes the run.
- Re-importing the same fixture is idempotent; changing one fixture record creates one superseding
  capture and hides the older capture from recall.
- Every imported capture promoted by M2 yields scoped `document_chunks` with embeddings and a
  populated raw-capture `entity_id`.
- Raw recall supports BM25, vector, and metadata filters while preserving the existing memory-scope
  SQL policy.
- With `auto_extract_entities=false`, imports still capture, chunk, embed, and recall. With it
  enabled, imported captures enqueue extraction through the existing B1 engine.
- `memory-promote` can preview/promote an imported private capture to PROJECT and the widened scope
  is enforced by SQL recall and audit rows.
- A real Codex or Claude transcript fixture imports through the same path without bespoke pipeline
  code.

Verification matrix:

| Gate               | Command shape                                                         | Required receipt                                                              |
| ------------------ | --------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Adapter/unit tests | `moon run core:test -- test_source_adapters test_mailbox_adapter ...` | Stable ids, malformed input, dedupe, checkpoint fixtures pass                 |
| API/job tests      | `moon run api:test -- test_jobs_source_imports test_routes_* ...`     | Background drain, cancel/resume, policy recheck, raw promotion pass           |
| Content schema     | focused `test_surreal_content_persistence` slice                      | v7→v8 migration, empty bootstrap, repeat bootstrap pass                       |
| Live 3.x lane      | new moon task from M0.1                                               | `CHANGEFEED`/`DEFINE EVENT`/HNSW/`search::` parse and execute on v3.1         |
| End-to-end dogfood | staged transcript import fixture                                      | run completes, chunks exist, recall finds known turn, promotion preview works |

---

## Effort & sequencing snapshot

| Milestone               | Net new vs reused                                  | Rough effort | Gates                                          |
| ----------------------- | -------------------------------------------------- | ------------ | ---------------------------------------------- |
| M0 Foundations          | mostly confirm/extend                              | S–M          | blocks M6                                      |
| M1 Decouple + dedup     | net-new, low blast radius                          | M            | independent, ship first                        |
| **M2 Close the loop**   | net-new orchestration, reuses pipeline + B1 engine | **L**        | **blocks M3/M4/M5**                            |
| M3 Conversation logs    | net-new adapter                                    | L            | dogfood after M2                               |
| M4 Docs API+CLI         | net-new adapter                                    | M            | after M2; content scope fields already present |
| M5 Email + sensitivity  | net-new adapter + classifier                       | L            | sensitivity before real mail                   |
| M6 Lineage + CHANGEFEED | aligns C8/B4/W6.2                                  | L            | needs M0.1                                     |
| M7 Correctness + UX     | owned by other plans                               | —            | last                                           |

**Recommended first cut (shippable in order):** M0.2 → M1.1 → M1.2 → M1.3 → M0.1 → M2.1 → M2.2 →
M2.3 → M2.4 → M2.5 → M3 (dogfood). That sequence uses the existing content migration runner,
decouples ingestion, makes it idempotent, moves work out of HTTP, stands up the 3.x lane, closes the
loop, and proves it on real Claude/Codex transcripts before touching email.
