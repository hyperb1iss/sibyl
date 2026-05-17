# Sibyl Post-v0.10 Release Remap Spec

- Status: active release planning spec
- Created: 2026-05-17
- Baseline release: v0.10.0, published 2026-05-17
- Baseline head at planning time: `v0.10.0-2-g5dc7a398`
- Tracking task: `12b1fee4-7bdd-45c8-8a6a-b13fd6eab308`
- Parent roadmap: [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)

## 1. Decision

Start the next large Sibyl release from the shipped v0.10.0 baseline, not from the old v0.11/v0.12
planning labels.

v0.10.0 absorbed more of the original 1.0 stack than expected:

- native LLM substrate foundations
- trust-control and auth-session gates
- Reflection OS foundations and the reflection-quality gate
- runtime telemetry and overview performance work
- CLI pending writes and idempotent replay
- single-host deployment scaffolding
- broad RBAC and scope-isolation hardening

The old roadmap labels are still useful as design provenance, but they are no longer a good release
schedule. The new schedule should be product-shaped:

1. v0.11: real corpus ingestion and source-grounded synthesis at scale
2. v0.12: Memory Workspace and context-quality product hardening
3. v0.13: Surreal-only runtime closure and Graphiti deletion
4. v1.0 RC: evidence freeze, docs/install rehearsal, and boring release gates

Keep v0.14 in reserve only if v0.13 discovers a release-sized distribution or migration gap.

## 2. Why Remap Now

The v0.10 release notes describe the largest release since the SurrealDB cutover. The codebase now
already contains gates and surfaces that the roadmap previously assigned to v0.11 and v0.12:

- `tools/trust/trust_control_gate.py`
- `tools/trust/auth_session_gate.py`
- `tools/trust/reflection_quality_gate.py`
- `tools/trust/overview_perf_gate.py`
- `tools/trust/synthesis_gate.py`
- `tools/trust/adapter_ingest_gate.py`
- MemorySpace, audit, inspect, synthesis, import, and reflection surfaces across API, CLI, MCP, and
  web

The next roadmap should avoid redoing shipped work. It should harden the behavior that v0.10 made
possible: ingesting private source corpora, synthesizing reliable artifacts, making automatic memory
decisions visible, and deleting the remaining Graphiti support only when native behavior has
receipts.

## 3. Release Principles

### 3.1 Ship Product Slices, Not Internal Layers

Every release after v0.10 should prove an end-to-end product promise. Internal work still matters,
but each release needs a user-visible loop:

- capture or import
- policy check
- retrieval or synthesis
- inspect or correction
- audit receipt
- regression gate

### 3.2 Treat Gates As Product Claims

Gate names should map to claims the release notes can make. A green gate is not just a test bundle;
it is a receipt that a public claim is currently supported.

### 3.3 Keep Humans On Exceptions

The product direction remains automatic-first memory. Humans should see sensitive material,
ambiguous contradictions, policy failures, destructive actions, high-impact sharing, and correction
flows. Humans should not approve routine safe memory writes one by one.

### 3.4 Make Graphiti Deletion Earned

Graphiti deletion is a 1.0 gate, not a vibes cleanup. Remove it only after native source ingest,
reflection quality, synthesis, context packs, archive restore, and benchmark receipts cover the
behavior that used to depend on Graphiti-shaped code.

### 3.5 Keep Releases Small Enough To Revert

Use atomic feature commits and release packets with clear verification. If a release starts eating
multiple product promises, split it.

## 4. Baseline: What v0.10 Already Owns

v0.10 is the new floor for planning. Treat these as shipped foundations:

- native model/provider substrate and runtime configuration surfaces
- reflection lifecycle records, dream-cycle maintenance jobs, and automatic promotion receipts
- trust-control gate coverage for memory spaces, API-key restrictions, MCP policy, job policy
  receipts, inspect/audit parity, and UI visibility
- CLI pending write buffer with idempotent replay
- source-grounded synthesis and source import foundations from v0.9
- Memory Workspace routes for captures, imports, source inspect, and synthesis
- runtime telemetry and overview performance receipts
- single-host Docker/Ansible deployment scaffolding

These foundations may need hardening, but they should not be reintroduced as new milestones.

## 5. Target Schedule

| Release | Name                         | Product Promise                                                                                                         | Primary Workstreams                                  |
| ------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| v0.10.x | Patch Rail                   | Keep the shipped baseline healthy until v0.11 RC1 is tagged.                                                            | CI, release workflow, docs drift, urgent trust fixes |
| v0.11   | Corpus Runtime               | A real private corpus can be imported, searched, inspected, corrected, and synthesized without leaking scope.           | W5, W2, W7                                           |
| v0.12   | Memory Workspace OS          | Humans can see what Sibyl did automatically, correct it, undo it, and debug context inclusion from one product surface. | W2, W4, W7                                           |
| v0.13   | Surreal-Only Runtime Closure | The supported runtime no longer depends on Graphiti or Redis-by-default assumptions.                                    | W8, W9                                               |
| v1.0 RC | Evidence Freeze              | The claim surface is frozen, docs/install agree, and every release claim has a current receipt.                         | All workstreams                                      |

## 6. v0.11: Corpus Runtime

### Release Promise

v0.11 makes source-grounded memory useful on real corpora, not just curated fixtures.

A user should be able to import a large private source set, resume interrupted work, search before
expensive extraction finishes, inspect skipped/deduped records, correct visibility, synthesize an
artifact with explicit source IDs and gaps, remember that artifact with provenance, and prove no
private source leaked into project memory.

### In Scope

- source adapter contract hardening
- at least one import target beyond the current mailbox/MBOX path, or a deliberately broader
  mailbox-family adapter if that is the fastest real-corpus win
- persistent import-run observability: imported, skipped, deduped, errored, attachment, checkpoint,
  and extraction state
- large-corpus import rehearsal using a private-by-default dogfood fixture
- source search before extraction completion
- stronger section-pack materialization and source selection
- unsupported-claim, hidden-source, redaction, and freshness verification in synthesis
- artifact memory lifecycle: generated artifact -> raw source -> graph memory -> correction impact
- Memory Workspace visibility for import and synthesis receipts

### Out Of Scope

- full Graphiti dependency deletion
- full arbitrary public sharing
- broad team/org sharing UX
- complete redesign of synthesis generation
- replacing every import or synthesis metadata shape in one migration

### Packets

#### Packet A: Roadmap And Task Reconciliation

Goal: make planning state trustworthy before implementation starts.

Actions:

- close or retag stale v0.8 tasks that shipped through v0.10
- create the v0.11 Corpus Runtime epic
- create child tasks for the packets in this spec
- update active docs so v0.10 is the baseline and v0.11 is the current implementation focus
- keep historical v0.8-v0.12 docs as receipts, not live release promises

Verify:

- `sibyl context` shows one active v0.11 epic instead of stale v0.8 blockers
- `docs/architecture/SIBYL_1_0_ROADMAP.md` points at this remap spec
- `git status` only shows intended docs/task metadata changes

#### Packet B: Source Adapter Scale

Goal: make import adapters durable enough for more than one curated path.

Likely files:

- `packages/python/sibyl-core/src/sibyl_core/services/source_adapters.py`
- `packages/python/sibyl-core/src/sibyl_core/models/sources.py`
- `packages/python/sibyl-core/src/sibyl_core/services/mailbox_adapter.py`
- `apps/api/src/sibyl/jobs/source_imports.py`
- `apps/api/src/sibyl/api/routes/crawler.py`
- `apps/web/src/components/memory/source-import-progress.tsx`

Actions:

- clarify stable source identity, adapter version, source version, dedupe key, and transform version
- make skipped records first-class enough to inspect and count
- make attachment and metadata-only records visible without forcing extraction
- add one broader source path or broaden mailbox import to cover the next real corpus
- keep imported records private by default unless policy says otherwise

Verify:

- `moon run adapter-ingest-gate`
- adapter contract tests cover stable IDs, dedupe, private scope, checkpoint resume, and skipped
  records
- a fixture can pause/resume and preserve counts without duplicating raw memory

#### Packet C: Large-Corpus Import Rehearsal

Goal: prove the import loop works under realistic volume and partial failure.

Actions:

- create a dogfood import fixture with private, project, duplicate, skipped, attachment, and error
  cases
- run import in bounded batches with resumable checkpoints
- expose run receipts in API, CLI, and web
- ensure records become searchable before expensive extraction finishes when metadata/raw content is
  available
- document the rehearsal command and artifact location
- register `moon run large-corpus-rehearsal` or make `adapter-ingest-gate` invoke the same focused
  rehearsal, with fixtures under `packages/python/sibyl-core/tests/fixtures/large_corpus/`

Verify:

- new large-corpus rehearsal task or script exits nonzero on count drift, duplicate drift, policy
  leak, or unrecoverable resume failure
- the rehearsal command and artifact path are stable enough to cite in release notes
- `moon run adapter-ingest-gate` includes or invokes the rehearsal in focused form
- Memory Workspace shows imported, skipped, deduped, errored, and pending-extraction states

#### Packet D: Synthesis Verification Depth

Goal: make generated artifacts trustworthy when source material is messy.

Likely files:

- `packages/python/sibyl-core/src/sibyl_core/services/synthesis.py`
- `packages/python/sibyl-core/src/sibyl_core/tools/synthesis.py`
- `apps/api/src/sibyl/api/routes/synthesis.py`
- `apps/cli/src/sibyl_cli/main.py`
- `apps/web/src/components/memory/synthesis-runner.tsx`
- `apps/web/src/components/memory/synthesis-verification-panel.tsx`

Actions:

- strengthen unsupported-claim detection and gap reporting
- preserve section-level source IDs and hidden-source/redaction reasons
- track freshness and supersession signals from reflection findings
- require artifact provenance when remembering synthesis output
- make correction/redaction affect future synthesis selection

Verify:

- `moon run synthesis-gate`
- hidden-source and unsupported-claim fixtures fail with stable reasons
- remembered synthesis artifacts carry source IDs, verification status, and correction impact

#### Packet E: Corpus-To-Artifact Product Loop

Goal: wire import, inspect, correction, synthesis, and remember into one usable product loop.

Actions:

- add workspace affordances for source import receipts and synthesis run receipts
- link synthesis sections back to source inspect views
- show correction impact on recall and synthesis
- make CLI vocabulary match the web surface for import status, synthesis verify, artifact remember,
  and memory inspect

Verify:

- a browser/workspace trust-flow check covers import -> inspect -> correct -> synthesize -> remember
- CLI commands can produce the same receipt IDs shown in web
- correction tests prove recall and synthesis behavior changes after hiding, redacting, or marking
  stale

### v0.11 Release Gate

Minimum release-candidate commands:

```bash
moon run adapter-ingest-gate
moon run large-corpus-rehearsal
moon run synthesis-gate
moon run autonomy-gate
moon run reflection-quality-gate
moon run trust-control-gate
moon run auth-session-gate
moon run memory-trust-gate
moon run overview-perf-gate
moon run docs:lint
moon run docs:build
moon run :check
```

Release notes must cite:

- large-corpus rehearsal artifact
- adapter ingest gate receipt
- synthesis gate receipt
- hidden-source and unsupported-claim fixture receipts
- workspace trust-flow check receipt

## 7. v0.12: Memory Workspace OS

### Release Promise

v0.12 makes the web workspace and CLI feel like one coherent memory operating system.

A human should be able to answer:

- what did Sibyl remember or import?
- why did it promote, hide, dedupe, mark stale, or route an exception?
- who or what can see it?
- which source proves it?
- how do I correct or undo it?
- why did it appear in this context pack?

### In Scope

- exception console for automatic memory decisions
- context-quality gate for `wake`, `recall`, and `deep_search`
- workspace-trust gate for browser-visible trust flows
- product-grade inspect, correction, restore, and promotion preview flows
- context-pack explanation: inclusion, exclusion, redaction, freshness, confidence, source IDs
- CLI simplification around the same nouns as the web UI
- realtime activity that is useful without creating idle query storms

### Out Of Scope

- public sharing as a social feature
- arbitrary policy language
- full Graphiti deletion unless v0.11 unexpectedly clears every dependency

### Required New Gates

- `context-quality-gate`
- `workspace-trust-gate`

The gates should become Moon tasks before v0.12 implementation is considered feature-complete.

### Release Gate

```bash
moon run context-quality-gate
moon run workspace-trust-gate
moon run memory-trust-gate
moon run autonomy-gate
moon run reflection-quality-gate
moon run trust-control-gate
moon run auth-session-gate
moon run synthesis-gate
moon run overview-perf-gate
moon run :check
```

## 8. v0.13: Surreal-Only Runtime Closure

### Release Promise

v0.13 removes the remaining supported runtime dependence on Graphiti-shaped code and Redis-required
single-machine assumptions.

A fresh install should run with SurrealDB as the only required data service. Legacy Graphiti-shaped
archives should remain readable through Sibyl-owned import/projection code that does not import
`graphiti_core`.

### In Scope

- delete `graphiti-core` from package metadata, default dependency groups, optional extras, and CI
  paths
- replace remaining Graphiti-shaped entity, relationship, search, embedder, extraction, and restore
  adapters with Sibyl-native services or explicit migration readers
- replace Graphiti compatibility tests with native archive/import regression tests
- turn no-Graphiti smoke from default-loop proof into supported-runtime proof
- complete Redis-optional coordination boundary for local installs
- backup/restore round-trip for auth, graph, content, raw memory, tasks, settings, and source
  imports
- Docker, Helm, Homebrew, quickstart, and docs alignment to remove Graphiti from default install
  paths and prove the Surreal-only runtime

### Out Of Scope

- high-throughput distributed swarm runtime
- Gradial Rust runtime decisions
- cloud-only deployment assumptions

### Required Packets

#### Packet A: Graphiti Deletion Inventory

Goal: make the deletion surface explicit before removing compatibility code.

Verify:

- `moon run inventory-check`
- `moon run inventory-typecheck`
- `moon run inventory-test`
- `rg "graphiti_core|graphiti-core"` only returns accepted historical or migration-format labels

#### Packet B: Backup/Restore Release Gate

Goal: turn the backup/restore round-trip into an executable release gate before runtime deletion
work lands.

Actions:

- add `moon run backup-restore-gate`
- cover auth, graph, content, raw memory, tasks, settings, source import runs, and synthesis
  artifacts
- write the round-trip receipt to a release-citable artifact

Verify:

- `moon run backup-restore-gate`
- restored data preserves policy scope, source IDs, task links, and synthesis provenance

#### Packet C: Redis-Optional Local Runtime Closure

Goal: finish the local coordination boundary for single-machine installs.

Verify:

- local mode can enqueue crawl, entity creation, task update, backup, and reflection maintenance
  work without Redis
- Redis mode remains opt-in for distributed deployments

### Release Gate

```bash
moon run inventory-check
moon run inventory-typecheck
moon run inventory-test
moon run memory-trust-gate
moon run trust-control-gate
moon run reflection-quality-gate
moon run auth-session-gate
moon run autonomy-gate
moon run overview-perf-gate
moon run adapter-ingest-gate
moon run synthesis-gate
moon run backup-restore-gate
moon run :check
```

Additional deletion receipts:

- `rg "graphiti_core|graphiti-core"` returns only historical docs, archived benchmark notes, or
  explicit migration-format labels
- no default install, local dev, Docker, Helm, CI, or test task installs Graphiti
- backup/restore round-trip artifact is attached to release notes
- fresh quickstart works with only SurrealDB as required infrastructure

## 9. v1.0 RC: Evidence Freeze

### Release Promise

v1.0 ships only after the boring receipts are current.

No new large product promises land in the RC. The work is claim freeze, docs/install rehearsal,
evidence refresh, and fixing the smallest remaining blockers.

### In Scope

- active architecture docs agree
- stale release docs are archived or labeled historical
- benchmark ledger current and claim-safe
- install, local dev, Docker, Helm, Homebrew, and docs tell one story
- auth, retrieval, memory trust, source ingest, synthesis, overview performance, product flows, and
  Graphiti deletion all have current receipts
- version, changelog, release notes, and package metadata are aligned

### Release Decision Format

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

## 10. Task Graph Reconciliation

The project task graph still contains stale v0.8-era tasks after v0.10. Before v0.11 work starts,
reconcile the graph so recall does not inject obsolete blockers.

Actions:

- mark shipped v0.8/v0.10 tasks done with release receipts
- archive obsolete v0.8 epics if their remaining tasks are historical
- keep truly unfinished Graphiti-deletion tasks, but retag them under v0.13
- create the v0.11 Corpus Runtime epic and child tasks from this spec
- create placeholder v0.12 and v0.13 epics only if they help planning; avoid detailed future tasks
  until the prior release lands

Verification:

```bash
sibyl context
sibyl task list --status doing
sibyl task list --status todo
```

The output should show current work, not stale release ghosts.

## 11. Gate Matrix

| Gate                               | Current Status      | Release Role                                                               |
| ---------------------------------- | ------------------- | -------------------------------------------------------------------------- |
| `memory-trust-gate`                | exists              | Always-on regression gate for memory policy and leak prevention            |
| `trust-control-gate`               | exists              | Always-on regression gate for memory spaces, MCP, jobs, CLI, and web trust |
| `auth-session-gate`                | exists              | Required for v0.12+ and any auth/session change                            |
| `autonomy-gate`                    | exists              | Required when automatic promotion or exception routing changes             |
| `reflection-quality-gate`          | exists              | Required for v0.11+ because synthesis depends on lifecycle signals         |
| `overview-perf-gate`               | exists              | Required for product releases touching overview, activity, or realtime     |
| `adapter-ingest-gate`              | exists              | Primary v0.11 gate                                                         |
| `synthesis-gate`                   | exists              | Primary v0.11 gate                                                         |
| `large-corpus-rehearsal`           | missing             | Add in v0.11 or invoke from `adapter-ingest-gate`                          |
| `context-quality-gate`             | missing             | Add in v0.12                                                               |
| `workspace-trust-gate`             | missing             | Add in v0.12                                                               |
| no-Graphiti supported-runtime gate | partial             | Strengthen in v0.13                                                        |
| `backup-restore-gate`              | missing or informal | Add by v0.13                                                               |

## 12. Risk Register

| Risk                                                 | Impact                                | Mitigation                                                          |
| ---------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------------- |
| v0.11 becomes another mega-release                   | Hard to verify and release            | Keep Graphiti deletion and full workspace OS out of scope           |
| Large-corpus rehearsal exposes import model gaps     | Release slips                         | Treat rehearsal as Packet C and fix the smallest contract gap first |
| Synthesis quality depends on weak reflection signals | Unsupported claims or stale artifacts | Use reflection-quality gate as a v0.11 regression gate              |
| Workspace UI becomes a manual queue                  | Product regresses to review treadmill | Optimize for exception routing, receipts, and correction flows      |
| Graphiti deletion starts too early                   | Native behavior loses quality         | Do deletion after v0.11/v0.12 receipts, not during source scale     |
| Task graph remains stale                             | Agents plan from old blockers         | Reconcile tasks before implementation and after each release        |

## 13. Open Questions

These are decisions for v0.11 kickoff, not blockers for accepting this remap:

1. Which real corpus should drive the large-corpus rehearsal first: Gmail Takeout, Maildir, Apple
   Mail export, agent transcripts, or a mixed local document archive?
2. Should v0.11 add a second adapter, or deepen mailbox import until it covers the highest-value
   private corpus?
3. Which synthesis artifact should be the dogfood hero: release notes, architecture docs, onboarding
   guide, decision log, or audit packet?
4. Should context-quality gate start in v0.11 as a fixture-only spike, then become a release gate in
   v0.12?
5. Which v0.13 packet should land first: Graphiti deletion inventory, backup/restore gate, or
   Redis-optional local runtime closure?

## 14. Recommendation

Make v0.11 **Corpus Runtime**.

This is the cleanest next release because it builds directly on what v0.10 shipped without reopening
settled trust and reflection work. It proves Sibyl can handle the kind of messy, source-heavy memory
that makes a second brain valuable, while generating the evidence needed for workspace polish and
eventual Graphiti deletion.
