# Sibyl 1.0 Roadmap

- Status: shipped (v1.0.0 → v1.0.2); superseded for forward planning by
  [`SIBYL_POST_1_0_ROADMAP.md`](SIBYL_POST_1_0_ROADMAP.md)
- Created: 2026-05-15
- Current release floor: v1.0.2 (1.0 shipped)
- Current implementation focus: v1.0 shipped (v1.0.0 → v1.0.2); post-1.0 planning underway
- Active remap spec:
  [`SIBYL_POST_V010_RELEASE_REMAP_SPEC.md`](SIBYL_POST_V010_RELEASE_REMAP_SPEC.md)
- Tracking task: `12b1fee4-7bdd-45c8-8a6a-b13fd6eab308`

## 1. Thesis

Sibyl 1.0 is not a bigger review queue. It is the point where Sibyl becomes an automatic memory
operating system for agents and humans.

Routine memory work should run by default:

1. Capture source material.
2. Preserve provenance and policy context.
3. Consolidate noisy traces into durable memory.
4. Detect duplicates, contradictions, stale facts, and sensitive material.
5. Promote safe memory into the right space.
6. Keep recall, synthesis, and task context current.
7. Ask humans only for exceptions.

The product promise is simple: an agent can enter a workspace, get the right bounded context, do
real work, and leave the graph smarter without Bliss babysitting a manual inbox.

## 2. Active Source Docs

These living docs in `docs/architecture/` remain active planning inputs:

- [`SIBYL_NORTHSTAR.md`](SIBYL_NORTHSTAR.md): product and architecture truth.
- [`SIBYL_POST_V010_RELEASE_REMAP_SPEC.md`](SIBYL_POST_V010_RELEASE_REMAP_SPEC.md): current
  post-v0.10 release schedule and RC evidence checklist.
- [`SIBYL_1_0_RC_PLAN.md`](SIBYL_1_0_RC_PLAN.md): concrete remaining-work plan for the v1.0 RC cut.

These docs are release receipts or historical execution plans. They moved to `docs/_archive/` once
their work shipped. They can still hold useful receipts and design contracts, but they do not
override this roadmap or the Northstar when release status has changed:

- [`SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md`](../_archive/SIBYL_POST_V08_SYNTHESIS_AND_MEMORY_WORKSPACE_PLAN.md):
  source-grounded synthesis, inspect, correction, import, and workspace product contracts shipped in
  v0.9.
- [`SIBYL_LLM_SUBSTRATE_PLAN.md`](../_archive/SIBYL_LLM_SUBSTRATE_PLAN.md): native model provider
  substrate landed in v0.10.
- [`SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md`](../_archive/SURREALDB_GRAPHITI_EXIT_BENCHMARK_EVIDENCE.md):
  benchmark evidence and public-claim rules.
- [`SURREALDB_NATIVE_MEMORY_CORE_SPEC.md`](../_archive/SURREALDB_NATIVE_MEMORY_CORE_SPEC.md)
- [`SURREALDB_V07_GRAPHITI_EXIT_AND_PURE_SURREAL_PLAN.md`](../_archive/SURREALDB_V07_GRAPHITI_EXIT_AND_PURE_SURREAL_PLAN.md)
- [`SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md`](../_archive/SIBYL_V08_PURE_SURREAL_CLOSURE_AND_MEMORY_TRUST_PLAN.md)
- [`SIBYL_V08_PURE_SURREAL_CLOSURE_EXECUTION_PLAN.md`](../_archive/SIBYL_V08_PURE_SURREAL_CLOSURE_EXECUTION_PLAN.md)
- [`SURREALDB_PHASE1_BUGS.md`](../_archive/SURREALDB_PHASE1_BUGS.md)
- [`SURREALDB_PHASE2_AUTH_MIGRATION.md`](../_archive/SURREALDB_PHASE2_AUTH_MIGRATION.md)
- [`SURREALDB_PHASE2_LIVE_GATES.md`](../_archive/SURREALDB_PHASE2_LIVE_GATES.md)
- [`SURREALDB_PHASE3_BURNDOWN.md`](../_archive/SURREALDB_PHASE3_BURNDOWN.md)

## 3. 1.0 Definition

Sibyl 1.0 is ready when all of these are true:

- `wake`, `recall`, `remember`, `reflect`, `synthesize`, `inspect`, and `share preview` form one
  coherent product loop across API, CLI, MCP, prompt hooks, jobs, and web.
- Routine memory capture and consolidation are automatic-first. Humans only review policy
  exceptions, ambiguous contradictions, sensitive data, destructive actions, and high-impact
  sharing.
- Every derived memory has source IDs, policy receipts, confidence/freshness metadata, and rollback
  or correction history.
- Context packs explain inclusion, visibility, redaction, confidence, freshness, and hidden-but-
  relevant signals.
- Memory spaces are persisted control-plane records with users, agents, delegated authority,
  memberships, API-key restrictions, and audit trails.
- Project-private, delegated, and personal memory cannot leak through REST, CLI, MCP, web, prompt
  hooks, background jobs, imports, synthesis, or overview routes.
- Source import is resumable, deduplicated, inspectable, private by default, and searchable before
  expensive extraction finishes.
- Synthesis produces Markdown and JSON artifacts from authorized graph slices with section-level
  source IDs, unsupported-claim reporting, redaction metadata, and artifact provenance.
- The web Memory Workspace is the command center and exception console, not a manual approval
  treadmill.
- CLI auth and API sessions are boring: long-lived local CLI work should not require surprise
  re-login during normal use.
- Overview, metrics, context, and memory routes have measured latency budgets and do not run
  unbounded repeated graph scans while idle.
- SurrealDB is the only required default data plane. Graphiti is gone entirely from the supported
  runtime: no Graphiti Core dependency, no Graphiti Core import module, no compatibility extra, and
  no Graphiti-shaped fallback path.
- Default install, local dev, CI, Docker, Helm, and docs agree.

## 4. Workstreams

### W1. Autonomy Core

Build the automatic memory decision engine:

- score memory candidates for confidence, scope, sensitivity, duplication, contradiction, freshness,
  and blast radius
- auto-promote safe candidates through the same policy-backed path humans can inspect
- route only exceptions to review
- record every decision as an audit receipt with source IDs, reasons, model/provider, thresholds,
  and rollback metadata
- support dry-run and evidence modes for rollout and tests without making dry-run the product path

Exit criteria:

- a normal coding session can produce durable task learnings and project memories without manual
  approval
- exception queues stay small and explain why each item needs attention
- rollback or correction changes future recall and synthesis

### W2. Context Runtime

Make `wake`, `recall`, and `deep_search` explicit bounded products:

- enforce token and latency budgets per layer
- make source, confidence, visibility, freshness, and redaction reasons visible in every pack
- keep weak signals as boosts unless they are hard policy filters
- add dogfood fixtures that measure whether agents avoid rediscovery and ask fewer repeated
  questions
- fix overview and metrics routes so idle pages do not create slow-query storms

Exit criteria:

- overview page load and idle refresh have a budgeted query plan
- context-pack regressions fail named gates
- pack output is explainable enough for a human or agent to debug why context appeared

### W3. Trust And Control Plane

Make identity, policy, delegation, and audit first-class:

- persisted `MemorySpace` records and membership APIs
- first-class `Agent` identity and delegated authority
- API-key project and memory-space restrictions enforced at request time
- MCP `add` and `manage` either route through memory policy or fail closed for memory-sensitive
  actions
- job payloads carry actor, project, scope, policy receipt, and source IDs
- session refresh and CLI token behavior have regression coverage

Exit criteria:

- a restricted API key, delegated agent, project viewer, project maintainer, owner, and outsider all
  have tested recall/write behavior
- auth/session failures are observable and recoverable without silent local state corruption
- audit and inspect tell the same story across REST, CLI, MCP, jobs, and web

### W4. Reflection Intelligence

Turn reflection into a real consolidation loop:

- extract claims, decisions, plans, procedures, artifacts, tasks, and relationships
- detect duplicates and near-duplicates
- detect contradictions and stale facts
- mark supersession and freshness
- keep private, project, team, and organization rollups separate unless policy allows merging
- promote diary and session learnings through policy-backed reflection instead of direct shared
  writes

Exit criteria:

- repeated sessions converge instead of accreting noise
- contradiction and staleness flows create actionable exceptions, not silent graph drift
- reflection output improves recall quality in dogfood fixtures

### W5. Source And Synthesis Scale

Make sources and generated artifacts trustworthy at scale:

- stabilize the source adapter contract
- broaden import targets beyond mailbox-style archives
- make import status, dedupe, skipped records, attachments, and extraction state visible
- harden section-pack materialization
- verify citations, hidden-source handling, unsupported claims, and freshness
- remember generated artifacts with provenance back to source memory

Exit criteria:

- a large private corpus can be imported, resumed, searched, inspected, corrected, and synthesized
  without leaking into project memory
- every synthesized section has source IDs or explicit gaps
- correction and redaction affect future synthesis

### W6. Native LLM Substrate

Centralize model access so extraction, reflection, synthesis, and hooks use one runtime:

- one `sibyl_core.llm` provider layer
- curated model registry
- per-surface defaults and overrides
- settings API and web configuration
- key validation through current provider models
- token, latency, retry, model, and provider observability

Exit criteria:

- no active extraction, reflection, synthesis, or hook call site instantiates provider SDK clients
  directly
- model changes are testable per surface
- extraction and synthesis smoke runs preserve current field coverage and quality

### W7. Product Surface

Make the Memory Workspace feel like the real product:

- exception console for automatic memory decisions
- source inspect and correction from one place
- import progress and synthesis runs in one flow
- agent access preview and policy explanations
- fast overview panels with useful default density
- CLI commands that use the same vocabulary as the web UI

Exit criteria:

- a human can understand what Sibyl did automatically, correct it, undo it, and preview who can see
  it without learning graph internals
- the default screen is useful for repeated work, not a marketing surface or a manual queue

### W8. Runtime, Distribution, And Evidence

Make 1.0 shippable outside Bliss's terminal:

- Surreal-only default deployment
- Graphiti removed entirely from supported runtime, package metadata, optional extras, import
  allowlists, and compatibility tests
- legacy Graphiti-shaped archives imported through Sibyl-owned readers that do not import the
  Graphiti Core module
- backup and restore round-trips for auth, graph, content, raw memory, tasks, and settings
- Docker, Helm, Homebrew, and docs match the same install story
- local coordination works for single-machine installs; Redis remains opt-in for distributed runs
- benchmark and release claim ledgers stay artifact-first

Exit criteria:

- a fresh install can run with only SurrealDB as the required data service
- release notes cite gates and artifacts, not vibes
- public performance or quality claims only cite accepted benchmark rows

### W9. Graphiti Deletion

Delete Graphiti as a product and dependency concern, not merely as a default-loop concern:

- replace remaining Graphiti-shaped entity, episode, relationship, search, extraction, embedder,
  maintenance, and restore adapters with Sibyl-native services
- delete Graphiti Core imports and the generated allowlist entries that currently classify them
- delete the `sibyl-core[compatibility]` extra and any dev-only Graphiti dependency once replacement
  tests exist
- replace compatibility tests with native archive/import regression tests
- keep legacy data readability through explicit projection/import code owned by Sibyl
- keep benchmark baselines as archived artifacts, not live Graphiti runtime paths

Exit criteria:

- `rg "graphiti[_-]core"` returns only historical docs, archived benchmark notes, or explicit
  migration-format labels
- no Python package metadata, lockfile default set, optional extra, test dependency group, Docker,
  Helm, CI, or dev command installs Graphiti
- no application, CLI, MCP, worker, crawler, prompt hook, backup, restore, or test helper imports
  the Graphiti Core module
- `moon run inventory-check inventory-typecheck inventory-test` fails on any new Graphiti import or
  dependency
- no-Graphiti smoke becomes a deletion proof, not just a default-loop proof

## 5. Milestone Stack

### v0.10: Shipped Baseline

Goal: establish the new floor.

v0.10 shipped more of the original roadmap than expected: native LLM substrate foundations,
Reflection OS foundations, trust-control and auth-session gates, runtime telemetry, CLI pending
writes, single-host deployment scaffolding, and broad RBAC/scope hardening. Treat v0.10 as the
current baseline, not as the start of new implementation.

### v0.11: Corpus Runtime (absorbed into the v1.0 RC line)

Status: not cut as a standalone release. This packet's work landed across `v1.0.0-rc.1` through
`v1.0.0-rc.8`. Kept here as the corpus-runtime scope and gate list, not a forthcoming version.

Goal: make source-grounded memory work on real corpora.

- source adapter contract hardening
- broader or deeper import target support beyond the curated mailbox path
- large-corpus import rehearsal with resumability, dedupe, skipped/error state, attachments, and
  private-by-default policy
- source search before expensive extraction finishes
- synthesis verification expansion for hidden sources, unsupported claims, freshness, redaction, and
  artifact provenance
- correction and redaction propagation into future recall and synthesis

Required gates:

- `adapter-ingest-gate`
- `large-corpus-rehearsal`
- `synthesis-gate`
- `autonomy-gate`
- `reflection-quality-gate`
- `trust-control-gate`
- `auth-session-gate`
- `memory-trust-gate`
- `overview-perf-gate`
- hidden-source and unsupported-claim fixtures
- `moon run docs:lint`
- `moon run docs:build`
- `moon run :check`

### v0.12: Memory Workspace OS (absorbed into the v1.0 RC line)

Status: not cut as a standalone release. This packet's work landed across `v1.0.0-rc.1` through
`v1.0.0-rc.8`. Kept here as the workspace-OS scope and gate list, not a forthcoming version.

Goal: make Sibyl's automatic memory behavior understandable and correctable from one product
surface.

- exception console for automatic memory decisions
- context-pack explanation for inclusion, exclusion, redaction, freshness, confidence, and source
  IDs
- product-grade inspect, correction, restore, and promotion preview flows
- CLI vocabulary aligned with the web Memory Workspace
- realtime activity and overview behavior that stays within query budgets

Required gates:

- new `context-quality-gate`
- new `workspace-trust-gate`
- `memory-trust-gate`
- `autonomy-gate`
- `reflection-quality-gate`
- `trust-control-gate`
- `auth-session-gate`
- `synthesis-gate`
- `overview-perf-gate`
- `moon run :check`

### v0.13: Surreal-Only Runtime Closure (absorbed into the v1.0 RC line)

Status: not cut as a standalone release. This packet's work landed across `v1.0.0-rc.1` through
`v1.0.0-rc.8`. Kept here as the runtime-closure scope and gate list, not a forthcoming version.

Goal: remove remaining Graphiti and Redis-required default-runtime assumptions.

- Graphiti removed from supported runtime, package metadata, optional extras, dev dependencies, CI,
  and live compatibility tests
- legacy Graphiti-shaped archives imported through Sibyl-owned readers that do not import the
  Graphiti Core module
- no-Graphiti smoke strengthened from default-loop proof to supported-runtime proof
- Redis remains opt-in for distributed deployments while local single-machine installs work with
  SurrealDB as the only required data service
- `backup-restore-gate` round-trip for auth, graph, content, raw memory, tasks, settings, source
  imports, and synthesis artifact provenance
- Docker, Helm, Homebrew, quickstart, and docs alignment to remove Graphiti from default install
  paths and prove the Surreal-only runtime

Required gates:

- `moon run inventory-check inventory-typecheck inventory-test`
- strengthened no-Graphiti supported-runtime proof
- `backup-restore-gate`
- `memory-trust-gate`
- `trust-control-gate`
- `reflection-quality-gate`
- `auth-session-gate`
- `autonomy-gate`
- `overview-perf-gate`
- `adapter-ingest-gate`
- `synthesis-gate`
- `backup-restore-gate`
- `moon run :check`

### v1.0 RC — Shipped (v1.0.0 → v1.0.2)

Status: Shipped (v1.0.0 → v1.0.2). The release-candidate line (`v1.0.0-rc.1` through `v1.0.0-rc.8`)
shipped and was followed by `v1.0.0`, `v1.0.1`, and `v1.0.2`, absorbing the v0.11 through v0.13
packets above.

Goal: freeze the claim surface and cut only when receipts are boring.

- no stale release docs
- all active architecture docs agree
- Graphiti gone from supported runtime and package dependency graph
- Surreal-only default deployment
- benchmark ledger current
- auth, retrieval, memory trust, synthesis, source ingest, overview performance, and product flows
  have current receipts

Release decision format:

```text
Ship v1.0:
  yes/no
Blocking packet:
  smallest remaining blocker, if no
Proof command:
  command or external receipt that will prove the blocker closed
Residual risk:
  accepted risks with owners
```

## 6. New Gates To Add

Current gates remain useful, but 1.0 needs named gates that map to product claims:

- `autonomy-gate`: auto-promotion decisions, exception routing, rollback metadata, and dry-run
  parity
- `auth-session-gate`: CLI refresh behavior, revoked sessions, API keys, MCP tokens, and delegated
  agents
- `context-quality-gate`: wake/recall/deep_search quality, token budgets, latency budgets, and
  source metadata
- `reflection-quality-gate`: duplicate, contradiction, stale, supersession, and correction behavior
- `overview-perf-gate`: overview page and metrics API query count and latency budgets
- `workspace-trust-gate`: browser-visible inspect, correction, promotion preview, import progress,
  synthesis verification, and exception-console flows
- backup/restore round-trip gate: auth, graph, content, raw memory, tasks, settings, and source
  imports survive export and restore

`context-quality-gate`, `workspace-trust-gate`, and the backup/restore round-trip gate now exist.
The RC work is receipt refresh and release rehearsal, not adding new gate names.

## 7. Strategic Boundary

Adjacent Rust runtime work can share design DNA, primitives, and evaluation fixtures with Sibyl, but
it should not derail Python Sibyl's 1.0. Sibyl remains the polished personal/team memory product.
High-throughput swarm architecture can evolve separately.

Shared:

- `wake`, `recall`, `remember`, `reflect`, `synthesize`, `inspect`, `share`, and `admin` primitives
- eval fixtures
- policy and provenance ideas
- source-grounded synthesis patterns

Separate:

- runtime implementation
- release cadence
- deployment assumptions
- product surface

## 8. Recommendation

v1.0 shipped as the Evidence Freeze release (v1.0.0 → v1.0.2). Forward planning continues in
[`SIBYL_POST_1_0_ROADMAP.md`](SIBYL_POST_1_0_ROADMAP.md).

v0.10 already made Sibyl less needy, safer, and more model/runtime-aware. The v0.11 through v0.13
packets have produced the needed gate surfaces. The final job is to refresh receipts, align active
docs and install surfaces, and make the ship/no-ship decision boring.
