# Sibyl Northstar

- Status: living product and architecture spec
- Last validated: 2026-05-11

This document defines Sibyl's northstar: the product shape, architecture principles, and deletion
gates for the next form of the system.

The center is not "move storage to SurrealDB." The center is a second brain for anything: a small,
powerful, multi-user memory system that preserves ground truth, retrieves precise context, and lets
humans and agents collaborate without leaking private memory.

SurrealDB migration, Graphiti removal, FalkorDB/PostgreSQL deletion, and native retrieval are
implementation threads inside this larger product direction.

## Northstar

Sibyl becomes a domain-general, multi-user second brain for people and agents.

It should be useful for any domain with durable context. The core substrate stays domain-general:
sources, raw memories, entities, episodes, scopes, provenance, and context packs. Coding and
knowledge work are the first overlays and proving ground: remembering decisions, code references,
debugging sessions, architecture tradeoffs, project plans, review findings, generated artifacts, and
the working state that normally disappears between agent sessions.

The product interface is small:

1. `wake` retrieves bounded startup context for the current principal and workspace.
2. `recall` retrieves a precise context pack for the current intent.
3. The agent or human acts using that context.
4. `remember` captures source material, facts, decisions, ideas, plans, artifacts, and
   relationships.
5. `reflect` consolidates noisy traces into durable knowledge without destroying source truth.
6. `synthesize` produces large source-grounded outputs from graph-guided memory slices.

That loop must work for a solo developer, a team of humans, a swarm of agents, or an organization
with private and shared memory spaces. It must cover software projects, product strategy, home
automation, creative planning, research, relationships between people and organizations, or any
other modeled domain. The graph is not a "code graph"; it is a context graph. The context graph is
not the source of truth by itself; it is an index, explanation layer, and reasoning surface over
preserved sources.

Haven is a lighthouse integration for this shape. If Sibyl can become the memory brain for a
privacy-first home assistant, it proves the system can handle real personal context, home state,
preferences, routines, relationships, and ambient life memory without collapsing into a coding-only
tool.

## Product Goal

Sibyl should make agents faster and safer because it can answer:

- Who am I, who am I acting for, and what workspace am I in?
- What are we doing right now?
- What did we already decide?
- What plans, ideas, constraints, and artifacts are relevant?
- Who knows this, who owns it, and who is allowed to use it?
- What original source proves this memory?
- What entities are connected to this work, even if the prompt uses different words?
- What changed since the last session?
- What changed for this user, team, project, or organization?
- What have I known, written, received, decided, or cared about across my long-running personal
  corpus?
- What should be injected before the agent wastes time rediscovering context?
- What should be hidden, redacted, or only summarized for this caller?
- What documentation, report, roadmap, or briefing can be generated from this graph slice?

The output should be compact enough to fit into agent prompts, structured enough for tools, and rich
enough that the agent can build with the project's real history in hand.

The coding experience is the proving ground. Sibyl should know the active branch of thought around a
project: what changed, why it changed, which files and APIs matter, which tests proved it, which
risks remain, and what another agent should read before touching the same system.

## Product Direction

The product center is a hyper-capable memory system where many users, teams, organizations, and
agents can safely share one runtime without sharing the wrong context.

Sibyl must treat identity, membership, permissions, provenance, source preservation, and memory
scope as part of the retrieval contract. A context pack is only correct if it is useful, grounded,
and authorized.

The multi-user twist is not a bolt-on social layer. It is what lets Sibyl become a shared second
brain: private memory by default, deliberate promotion into project or organization memory, scoped
agent delegation, and auditable sharing when teams want one collective context without blending
everyone's personal history together.

The UX has to be unusually humane because Sibyl handles unusually personal context. Agents may call
it constantly, but humans need to feel oriented, respected, and in control when they add private
details, inspect memory, correct a wrong inference, promote a memory, or let an agent act on their
behalf.

## Design Laws

These laws keep the spec from becoming an enterprise permissions kingdom or a bespoke memory science
project.

### 1. Raw Memory Is Law

Sibyl preserves original source material whenever it can: conversations, tool traces, task notes,
documents, code references, email archives, imported exports, and generated artifacts. Extraction,
summarization, embedding, graph edges, and rollups are derived views. They may be rebuilt. The
source record stays.

The lesson from MemPalace is not to copy ChromaDB or the palace metaphor; it is to respect how far
verbatim retrieval goes before asking an LLM to decide what mattered. Its public README frames the
system around verbatim conversation storage, scoped semantic retrieval, and no summarization or
paraphrasing on the raw path. Sibyl should adopt that discipline while using SurrealDB as the native
data plane.

### 2. Context Is Layered

Context packs are not one giant search result. They are layered:

- **L0 Identity**: principal, delegated authority, organization, project, agent persona, and
  standing instructions that are always safe to load.
- **L1 Essential Story**: bounded, high-signal current state: active plans, recent decisions,
  important constraints, and top memories for the workspace.
- **L2 Scoped Recall**: facet and memory-space constrained retrieval when a topic appears.
- **L3 Deep Search**: full hybrid search across authorized memory when explicitly requested or when
  lower layers are insufficient.

The startup path should be cheap and bounded. Deep retrieval should be available without making
every session pay for it.

### 3. Authorization Is Retrieval

Permissions are not a UI feature and not a final output filter. `recall`, `remember`, `wake`, and
`reflect` all take a principal, optional delegated principal, organization, project, and
memory-space scope. Retrieval only searches spaces the caller can access, and rendering performs a
final policy check before prompt injection.

### 4. Simple Policy First

Sibyl should start with a boring internal primitive:

```
Principal + MemoryScope + can_read/can_write/can_share/can_reflect
```

Avoid a custom policy language until real users force it. Roles can be data. Policies can be
predicates. The implementation should stay concentrated at capture, retrieval, reflection, export,
and sharing boundaries.

### 5. Boost Weak Signals, Do Not Gate On Them

Weak signals should improve ranking, not hide direct evidence. Graph neighborhoods, facets, rooms,
source adapters, extracted entities, and classifier output can boost or annotate candidates. They
should not prevent raw source matches from appearing unless the filter is a hard authorization or
explicit caller scope.

### 6. One Runtime, Not Fifty Features

The product should have a few strong primitives instead of dozens of tools:

- `wake`
- `recall`
- `remember`
- `reflect`
- `synthesize`
- `inspect`
- `share`
- `admin`

Specialized CLI commands, MCP tools, web routes, and hooks should compose these primitives rather
than inventing parallel memory behavior.

### 7. Human Trust Is The Interface

Personal memory systems can become creepy if they feel opaque, hungry, or hard to correct. Sibyl's
human surfaces should feel friendly, calm, and legible even when the underlying graph is powerful.

The user should always be able to answer:

- what did Sibyl remember?
- why did it remember that?
- where did the memory come from?
- who or what can see it?
- what did an agent use it for?
- how do I fix, hide, delete, or promote it?

Power users should get depth, but nobody should need graph vocabulary to manage personal memory.
Every sensitive flow should prefer preview, undo, explanation, and safe defaults over surprise
automation.

## Non-Goals

- No bespoke enterprise policy engine in the first native slice.
- No 20-plus MCP memory tools when a small primitive set can cover the workflow.
- No lossy extraction path that discards raw sessions after summarization.
- No hard dependency on a single hosted LLM for core recall.
- No Graphiti deletion until native behavior is better, measured, and reversible.
- No indefinite dual-store product. FalkorDB and PostgreSQL are migration bridges, not permanent
  architecture.
- No Cloud-only architecture. Local/server mode remains first-class.

## Current State Already Landed

These pieces are part of the foundation and must not get lost while we push toward native SurrealDB:

- `v0.6.0` established SurrealDB as the default storage direction for graph, content, and auth.
- Legacy FalkorDB and PostgreSQL services are out of the default local, CI, and Helm paths. They
  remain migration/archive source surfaces only.
- Graph archives can be exported, imported, verified, and dry-run merged.
- Merge tooling can rewrite source org data into a target organization.
- Surreal auth supports username/password sign-in plus optional token authentication.
- Context packs exist across CLI, API, MCP, and prompt hooks.
- `remember` exists as an MCP tool and CLI command.
- `recall` exists as an intent-oriented CLI interface.
- Raw memory capture and scoped raw recall exist through the API and CLI, including private/project
  scope checks and agent diary metadata.
- `reflect` exists across CLI, API, and MCP as the consolidation review/persist surface.
- Context packs already include direct matches and one-hop related graph context.
- Wake, recall, and deep-search layers exist on context packs and session wake bundles.
- The Sibyl skill defines the agent memory contract: recall, act, remember, reflect.
- Graphiti-on-Surreal is transition scaffolding, not northstar proof. Treat the current insertion
  path as suspect until a native Surreal path proves raw memory, graph writes, and recall end to
  end.

## Target Architecture

### 1. One SurrealDB Runtime

Sibyl should run on one SurrealDB-backed data plane for graph memory, auth, content, tasks, raw
captures, context packs, and derived indexes.

Target properties:

- Remote server mode for any multi-process runtime.
- Embedded mode only for single-process dev or tests.
- Namespace-per-organization for graph memory.
- A dedicated auth namespace/database while users, agents, and memberships can span organizations.
- No default org fallback in graph operations.
- Explicit memory-space scope for personal, project, team, organization, and shared contexts.
- Authorization filters applied before context is rendered.
- Idempotent schema bootstrap.
- Archive-backed migration and rollback.
- No permanent mixed-mode destination after migration. Existing FalkorDB and PostgreSQL installs
  should be migrated, verified, cut over, and then removed from the default product surface.
- Runtime-neutral primitives shared by CLI, API, MCP, prompt hooks, and web UI.

SurrealDB Cloud remains attractive for managed multi-user deployments, but the official Cloud FAQ
still calls Cloud beta, AWS-only, and not configurable via custom CLI flags or environment
variables. Treat Cloud as a managed target, with local/server mode staying first-class.

### 2. Multi-User Memory Model

Sibyl needs a control plane that is as real as the memory graph. The graph can be brilliant and
still wrong if it leaks private context into a shared agent session.

Core control-plane records:

- `User`: human identity with auth credentials, profile, and preferences.
- `Agent`: non-human actor with owner, capabilities, credentials, and delegation constraints.
- `Organization`: tenant boundary for billing, administration, policy, and default memory.
- `Project`: work boundary inside an organization.
- `MemorySpace`: retrieval and write boundary for personal, project, team, organization, imported,
  or externally shared memory.
- `Membership`: user or agent participation in an organization, project, or memory space.
- `Role`: named permission bundle with built-in and custom grants.
- `Policy`: rule set for create, read, update, delete, export, recall, remember, reflect, and share.
- `Invitation`: auditable onboarding flow for new users and agents.
- `AuditEvent`: immutable record of access, writes, policy changes, exports, and delegated actions.

Memory visibility must be explicit:

- `private`: visible only to the owning user or agent.
- `delegated`: visible to an agent acting for a specific user or task.
- `project`: visible to project members with the right role.
- `team`: visible to a named group across projects.
- `organization`: visible across the tenant according to policy.
- `shared`: intentionally exposed across organizations or external integrations.
- `public`: intentionally published memory, docs, or artifacts.

Policy belongs on both reads and writes. `remember` must know where a memory lands, `recall` must
search only spaces the caller can access, and `reflect` must not promote private material into
shared knowledge without an allowed transition. Every context pack should be able to explain why
each item was included and which scope made it visible.

`MemorySpace` is primarily a control-plane scope, not just another graph node. The policy store is
the source of truth for who can read, write, share, reflect, export, or delegate against a space.
The graph may contain a `MemorySpace` projection for traversal and explanation, and memories may use
`VISIBLE_IN` edges for graph-native queries, but authorization must resolve against the control
plane before retrieval and again before rendering.

Agent identity is first-class. API keys and MCP clients should authenticate as an agent, optionally
delegated by a user, not as an unscoped server process. That gives Sibyl a clean answer to "who
knew, who acted, and under whose authority?"

### 3. Domain-General Graph Model

The native model must represent any domain without baking in software-only language. Keep the core
schema small, then add typed overlays for knowledge work, coding, smart home, creative planning, and
other domains.

Core substrate nodes:

- `Entity`: durable thing, person, org, system, project, location, concept, tool, or topic.
- `Source`: original input stream, file, conversation export, external service, imported archive, or
  generated trace.
- `RawMemory`: verbatim or declared-transform-preserved source slice.
- `Episode`: observed event or captured conversation slice.
- `MemorySpace`: graph projection of a policy-scoped control-plane memory space.
- `ContextPack`: rendered retrieval result with inputs, facets, source IDs, and injection target.
- `AgentDiary`: compact stream of observations, findings, and recurring patterns for one stable
  agent identity.

Knowledge-work overlay nodes:

- `Artifact`: file, doc, repo, design, message, image, recording, schema, dataset, or generated
  output.
- `Session`: bounded work period with prompts, actions, tool calls, outcomes, and reflections.
- `Decision`: chosen direction, rejected alternative, rationale, and status.
- `Plan`: intended work, milestones, blockers, acceptance criteria, and ownership.
- `Idea`: speculative concept before it becomes a decision or plan.
- `Claim`: assertion with confidence, source, and contradiction support.
- `Task`: actionable unit of work with status, project, and evidence.

Other domains should add overlays rather than bloating the core. Haven, for example, may introduce
home-specific nodes for rooms, devices, routines, preferences, presence, and safety events while
still using the same source, raw memory, scope, provenance, and retrieval contracts.

Core relationship families:

- `ABOUT`: a thing concerns another thing.
- `MENTIONS`: an episode or artifact references an entity.
- `PRODUCES`: a session, task, or plan creates an artifact.
- `TOUCHES`: work affects an entity or artifact.
- `DECIDES`: a decision resolves a question or plan.
- `SUPPORTS`: evidence strengthens a claim, decision, or plan.
- `CONTRADICTS`: evidence conflicts with a claim or prior edge.
- `DEPENDS_ON`: one task, plan, entity, or artifact requires another.
- `DERIVED_FROM`: a memory, artifact, or summary descends from source material.
- `CAPTURED_IN`: a fact appears in a session, episode, artifact, or raw capture.
- `SOURCED_FROM`: derived memory points to the raw source record that proves it.
- `VISIBLE_IN`: a memory is available inside a memory space.
- `SHARED_WITH`: a memory space, artifact, or context pack is intentionally exposed to another
  principal or space.

Every node and edge needs provenance. Context quality depends on knowing where facts came from, when
they were valid, which memory space they belong to, and whether they were inferred, imported,
user-stated, tool-observed, or generated.

### 4. Native Retrieval Engine

Sibyl's retrieval engine should combine:

- exact ID lookup
- lexical full-text search
- vector similarity search
- raw source search
- typed graph traversal
- time and validity filtering
- source/provenance filtering
- task/project/org scoping
- memory-space and principal scoping
- agent diary lookup
- relationship-aware reranking
- optional model reranking over top candidates

SurrealDB gives us HNSW vector indexes, full-text search, reciprocal rank fusion via `search::rrf`,
graph relations through `RELATE`, recursive arrow traversal, events, computed fields, and live
queries. Those primitives are enough to build a better context graph than Graphiti, but only if
Sibyl owns the retrieval contract directly.

Filtered vector search must be benchmarked before we rely on selective filters for recall quality.
Official docs show filters combined with KNN queries; they do not settle planner ordering or recall
behavior under realistic org/project filters. The native spike must measure this.

Authorization is part of retrieval, not a post-processing garnish. The engine should generate
candidate sets within allowed memory spaces whenever possible, enforce policy again before
rendering, and record redactions when relevant context exists but cannot be shown.

Ranking should prefer evidence that is direct, source-grounded, current, important, and connected to
active plans. Entity extraction and graph matches are signals. They cannot replace raw source
retrieval as the baseline.

### 5. Context Pack Contract

A context pack is the agent-facing answer to "what should I know before acting?"

Minimum structure:

- caller principal, delegated user, organization, project, and memory spaces
- layer: `wake`, `recall`, or `deep_search`
- request intent and query
- facets requested by the caller
- L0 identity and authority, when requested
- L1 essential story, when requested
- direct matches
- related entities and relationships
- raw source excerpts with IDs
- current tasks and plans
- relevant decisions and constraints
- relevant agent diary entries
- raw source IDs for audit
- visibility scope and inclusion rationale per item
- redaction metadata for hidden-but-relevant context
- confidence and freshness signals
- Markdown renderer for prompt injection
- JSON renderer for tools

The pack should be precise rather than huge. It should prefer high-signal decisions, plans,
constraints, active work, and directly connected artifacts over a wall of semantically similar text.
It should also be shareability-aware: a pack rendered for a private agent session is not
automatically safe to paste into a team channel or expose through an external integration.

`wake` is a specialized context pack. It should return L0 and L1 by default, with hard token budgets
and no surprise deep search. `recall` should return L2 scoped retrieval. `deep_search` should be an
explicit L3 operation.

### 6. Capture and Reflection

`remember` is the fast path: capture now with enough structure to retrieve later.

`reflect` is the maintenance path: consolidate later into durable memory.

The runtime needs both:

- raw capture for every meaningful planning, ideation, and implementation moment
- task-aware capture and reflection that link memories to active work when a project has one clear
  current task
- declared transformations for imported source material
- source adapter metadata for where memories came from
- explicit memory-space selection on capture
- private-by-default capture when scope is ambiguous
- promotion flows from private memory into shared memory
- structured extraction into nodes and relationships
- source-preserving summaries
- contradiction handling
- obsolete or superseded decision marking
- periodic session digests
- project and domain-level rollups
- prompt-hook feedback when an agent should remember something it just learned
- policy-aware reflection that preserves boundaries between personal, project, and organization
  memory

Raw memory should never be thrown away just because extraction improves. Durable entities can be
rebuilt; original source history is the safety net.

Source ingestion should use an adapter contract, not one-off importer branches. Each adapter should
declare source identity, version token, metadata schema, privacy class, transformation behavior, and
incremental-ingest support. This lets Sibyl ingest repos, chat transcripts, docs, calendar exports,
Slack, email, device logs, or domain-specific sources without inflating core.

Large personal corpora are first-class. Importing twenty years of email, chat, notes, documents, and
archives should be a staged pipeline: register the source, preserve raw records and attachments,
index metadata immediately, deduplicate repeated exports, classify privacy and sensitivity, then
extract entities and relationships asynchronously. Sibyl should never require a giant up-front
summarization pass before the archive becomes searchable.

### 7. Agent Injection Surfaces

Sibyl should be reusable anywhere agents need memory:

- MCP tools for `wake`, `recall`, `remember`, `reflect`, `synthesize`, and inspection
- CLI commands for humans and shell workflows
- Claude/Codex prompt hooks
- API routes for web and external clients
- future app-specific integrations

No integration should know tenant-specific constants. Callers authenticate as a user or agent and
provide organization, project, intent, query, facets, and optional source hints; Sibyl resolves the
allowed memory spaces and renders the pack.

Specialist agents should get stable identities and lightweight diaries before we build elaborate
agent registries. A reviewer, architect, ops agent, or researcher can remember its own recurring
findings without polluting project-wide memory. Useful diary entries can later be promoted through
normal reflection.

### 8. Live and Reactive UI

Once core storage is stable, live queries become a product feature:

- task and context graph updates without polling
- live capture feed
- memory consolidation progress
- graph changes as agents work
- context pack preview and audit trail
- shared memory activity by user, agent, project, and organization
- permission and redaction previews before sharing context
- admin views for memberships, roles, invitations, API keys, and audit events
- source and reflection audit views showing raw memory, derived facts, and promotion history

Live query enablement must wait for a patched SurrealDB version and explicit permission testing.

### 9. Human Experience and Trust

The web UI is not just an admin console. It is the place where people build trust with the second
brain.

Core human jobs:

- capture personal memory without making the user think about schemas
- browse recent remembers, imports, reflections, and agent reads
- search personal, project, and shared memory with clear scope controls
- preview what an agent or teammate can recall before access is granted
- inspect source, visibility, confidence, freshness, and derived facts
- correct bad memories and mark claims as wrong, stale, sensitive, or superseded
- promote private memory into shared spaces with review and reversible history
- hide, redact, export, or delete personal memory with clear consequences
- see import progress, skipped records, dedupe decisions, and extraction status
- explain why a context pack included or omitted something important

UX principles:

- Friendly by default, powerful on demand.
- Plain-language labels before graph terms.
- Scope and privacy visible before action, not after.
- Every automation has a trace.
- Every sensitive action has preview and undo when technically possible.
- Empty states should guide the next useful action, not market the product.
- Personal memory should feel like a library the user owns, not a surveillance feed.

The existing Sibyl UX is a strong foundation: dashboard, tasks, graph, search, sources, settings,
and SilkCircuit visual language already give the app a real product shape. The northstar is to turn
that into a warm, high-trust memory cockpit where humans can comfortably manage deeply personal
context while agents do the repetitive retrieval work.

### 10. Benchmark and Evaluation Harness

The native runtime needs an evaluation harness from the start. The harness should measure retrieval
recall, answer usefulness, latency, token budget, source grounding, and permission safety.

Minimum benchmark fixtures:

- raw-source retrieval baseline
- hybrid lexical/vector/graph retrieval
- optional reranking
- layered `wake` budget quality
- multi-session temporal questions
- contradictory or superseded facts
- private memory that must not leak into shared packs
- noisy prompt-contaminated queries
- source adapter incremental ingest
- human UX task completion for capture, correction, promotion, sharing preview, and deletion
- coding dogfood tasks that compare agent behavior with and without Sibyl context
- Haven-style home assistant tasks that test preference recall, routine recall, and private memory
  isolation

Benchmark numbers should be used as regression gates first, not marketing claims. The test that
matters most is whether agents build faster and humans trust the memory enough to use it for real
personal and project context.

### 11. Graph-Guided Synthesis and Export

`recall` is for bounded context. `synthesize` is for getting a lot of grounded information out at
once: documentation, architectural overviews, onboarding guides, ADRs, roadmap narratives, release
notes, incident reports, research briefs, and audit packets.

The shape should be agent-steered, not a blind export:

1. caller chooses principal, memory spaces, output intent, audience, and constraints
2. Sibyl proposes an outline from graph neighborhoods, active plans, decisions, artifacts, and raw
   sources
3. the steering agent accepts, edits, expands, or narrows the outline
4. Sibyl materializes source packs for each section with raw IDs, claims, decisions, and related
   entities
5. the agent drafts from those packs
6. Sibyl verifies citations, redactions, freshness, and missing-source gaps
7. the final artifact links back to source memory and can itself be remembered

This is a large-read mode, not a different memory system. It should reuse retrieval, policy,
provenance, and context-pack renderers. The only new surface is orchestration around outline,
section packs, draft, and verification.

Minimum input:

- principal and optional delegated principal
- organization, project, and memory spaces
- output type: documentation, report, briefing, roadmap, release notes, audit packet, or custom
- audience and depth
- seed query or entity IDs
- optional outline or required sections
- source freshness and visibility requirements

Minimum output:

- accepted or generated outline
- section-level source packs
- draft artifact in Markdown and JSON
- source IDs per section
- redactions and hidden-but-relevant signals
- unresolved claims or missing-source gaps
- optional remembered artifact ID when the caller chooses to store the result

## Build-Time Learning Contract

Sibyl should dogfood itself while the native runtime is being built. Every significant slice should
leave the graph smarter:

- create or start a Sibyl task before implementation
- capture non-obvious findings as task notes while working
- preserve raw diagnostic evidence when a behavior is surprising
- complete tasks with learnings, not just status changes
- add durable patterns for discoveries that will matter across sessions
- avoid treating unverified migration behavior as product truth

The Graphiti-on-Surreal insertion uncertainty belongs here: it is a captured constraint and a
native-path design pressure, not a compatibility project unless a small, obvious fix appears.

## Graphiti Deletion Position

Fully deleting Graphiti should leave us in a better place, but native Sibyl should not become a
line-by-line clone of Graphiti. Graphiti is a behavioral baseline and a source of useful patterns,
not the product we are rebuilding.

The current Graphiti-on-Surreal path is not the desired intermediate truth source. If it is not
writing properly, that is a reason to accelerate native Surreal paths, not to deepen compatibility
work.

Better:

- one data model instead of Graphiti abstractions wrapped around SurrealDB
- direct SurrealQL for graph, search, and traversal
- fewer dependency and security surfaces
- a domain-general model owned by Sibyl
- context packs tuned for agent speed instead of Graphiti's memory model
- easier migration away from FalkorDB concepts
- deliberate Sibyl-native replacements for temporal reasoning, summaries, and graph neighborhoods

Worse if we delete too early:

- lose mature episode/entity orchestration before replacement tests exist
- regress graph-neighborhood quality, summaries, or temporal reasoning without noticing
- lose embedder/search abstractions without clean successors
- rewrite many tests at once with no behavioral baseline

Deletion gate:

- native entity, episode, edge, and search paths satisfy behavioral baseline tests
- community detection is replaced, redesigned, or explicitly dropped based on context-pack quality
  evidence
- temporal edge invalidation is replaced by a Sibyl-native validity and supersession model
- context pack quality is better than the Graphiti-backed baseline
- raw source retrieval remains available and measured after native extraction lands
- layered `wake`/`recall`/`deep_search` packs fit their token budgets
- permission-aware retrieval and capture are proven before multi-user defaults
- migration and rollback are rehearsed
- legacy services are out of the default path
- Graphiti security posture is known until the dependency is gone

## Workstreams

Workstreams are not strictly sequential. The `v0.6.0` release landed the first Surreal-default
runtime slice plus the initial raw-memory, layered-context, reflection, and diary surfaces. The next
wave should harden W1, W2, W2.5, W5, W6, and W9 with measured behavior before heavier tenancy,
adapters, synthesis, or collaborative surfaces.

### W0. Northstar Tracking

Keep this northstar current as decisions harden. When implementation branches land, update the
"Current State Already Landed" section and remove stale gates.

### W1. Native Memory Primitive

Status: first slice landed and hardened. Default CLI and MCP `remember` preserve raw source material
before durable graph writes and return raw source identifiers. The raw memory local latency gate now
exercises real memory-backed Surreal writes/recalls, and keyed scopes require scope keys on both
write and recall.

Build the smallest shared primitive that every surface can call:

- `Principal`
- `MemoryScope`
- `Source`
- `RawMemory`
- `ContextPack`
- `can_read`, `can_write`, `can_share`, `can_reflect`
- source ID and provenance metadata on every derived result

This is the first code slice. It should prove raw capture, scoped recall, and policy checks without
building custom roles, full admin UI, or cross-org sharing.

Implement this as a parallel native path behind the existing surfaces, not by deepening the
Graphiti-backed path. The point is to compare behavior safely while Graphiti-on-Surreal remains
suspect.

First-slice gates:

- `remember` writes one `RawMemory` record with source ID, principal ID, memory scope, capture time,
  and provenance
- `recall` runs scoped full-text search over `RawMemory`
- context-pack rendering returns source IDs and inclusion rationale
- privacy fixture proves a caller cannot retrieve another scope's memory
- local dev target: raw `remember` p95 under 300ms and scoped full-text `recall` p95 under 1s
- no embeddings, extraction, graph traversal, reflection, custom roles, or admin UI are required

Evidence:

- `packages/python/sibyl-core/tests/test_surreal_content_latency.py` samples 24 raw writes and 12
  scoped recalls against memory-backed Surreal content storage, then gates p95 under the first-slice
  latency targets.
- `packages/python/sibyl-core/tests/test_services_surreal_content.py` rejects delegated, project,
  team, and shared raw-memory writes without a `scope_key`, matching the recall-side guard.

### W2. Layered Context Packs

Status: first slice landed; next work is stricter token budgets, clearer wake/recall naming, and
quality gates around source grounding and authorization.

Ship the context contract as product behavior:

- `wake` returns L0 identity plus L1 essential story under a strict budget
- `recall` returns L2 scoped context for an intent
- `deep_search` runs L3 hybrid search over authorized spaces
- packs explain inclusion, source, visibility, confidence, and freshness
- prompt hooks use `wake` by default and call `recall` only when needed

Initial targets:

- `wake` renders L0 + L1 under 1,200 tokens
- `recall` renders L2 under 2,000 tokens unless the caller requests more
- `remember` acknowledges raw capture before extraction, embedding, or reflection work
- first-slice `recall` works without vector search so HNSW behavior cannot block the baseline

### W2.5. Context Quality and Dogfood Evaluation Harness

Make "agents build faster" measurable before the graph gets fancy:

- define seeded fixtures for raw recall, layered `wake`, scoped `recall`, and `deep_search`
- measure source grounding, permission safety, latency, token budget, and answer usefulness
- include coding dogfood tasks that require decisions, file references, test evidence, and handoff
  context
- include Haven-style personal-context fixtures for preferences, home state, routines, and privacy
  boundaries
- compare raw lexical, hybrid, graph-expanded, and reranked packs against the same tasks
- record whether agents avoid rediscovery, ask fewer repeated questions, and make fewer context
  mistakes

First evaluation gate:

- a seeded project memory space lets an agent answer a coding handoff question with source IDs,
  current decisions, changed files, remaining risks, and relevant tests
- a seeded Haven memory space lets an agent answer a home preference/routine question without
  leaking private unrelated memories
- failing fixtures block retrieval changes, not just storage changes

### W3. Multi-User Tenancy and Policy Model

Evolve the product around many users, many agents, and many scoped memory spaces:

- define users, agents, organizations, projects, memory spaces, memberships, roles, and policies
- enforce read policy in `recall`, `context`, MCP tools, API routes, and prompt hooks
- enforce write policy in `remember`, imports, task updates, and reflection jobs
- make delegated agent identity explicit in API keys and MCP sessions
- add audit events for recalls, remembers, reflection promotion, exports, and policy changes
- support private, delegated, project, team, organization, shared, and public visibility scopes
- build fixtures that prove private memory cannot leak into shared packs

Archive imports remain useful, but they are an admin ingestion path into a chosen memory space, not
the product narrative.

### W4. Source Adapter and Raw Ingest Pipeline

Make ingestion boring, extensible, and source-preserving:

- define source adapter contract
- record source identity, version, privacy class, metadata schema, and transformations
- support incremental ingest without sidecars
- import conversations, repo files, docs, task notes, email, chat, calendar, and archives through
  the same path
- attach raw source IDs to all derived graph nodes and summaries
- handle very large backfills through resumable batches, deduplication, checkpoints, and delayed
  extraction
- classify sensitive source classes before promotion into shared memory spaces

First large-corpus gate:

- import a mailbox-style archive into private raw memory
- preserve message IDs, thread IDs, timestamps, participants, subject, body, attachments, and source
  path
- support resumable import after interruption
- make metadata and lexical body search available before embeddings finish
- prove private imported email cannot appear in project or organization packs unless explicitly
  promoted

### W5. SurrealDB SDK and Runtime Hardening

Status: SDK 2.0 compatibility slice landed. The lockfile now resolves `surrealdb` 2.0.0, the default
local/CI server image is pinned to `surrealdb/surrealdb:v3.0.5`, and the core/API/CLI Surreal
contract tests pass against it.

The official Python SDK's latest stable PyPI release is `surrealdb` 2.0.0, released 2026-04-23.
Sibyl currently needs a compatibility spike before building new Cloud or live-query work on old SDK
assumptions.

Runtime gates:

- test SDK 2.0 against current driver behavior
- validate `signin`, `authenticate`, result shapes, and reconnect behavior
- keep WebSocket concurrency protected until a focused regression test proves safe relaxation
- keep embedded mode out of multi-process dev and production

### W6. Parallel Native Graph Schema Spike

Status: first direct SurrealQL spike landed as a tested path. It creates raw memory, direct graph
entities, an episode, a relationship edge, lexical/vector/graph searches, and a rendered context
pack with raw source IDs, then compares the native records against the current graph operation
loaders.

Replace one end-to-end path with direct SurrealQL before estimating the full Graphiti removal:

- create raw memory
- create entity
- create episode/session memory
- relate entities
- search by lexical, vector, and graph signals
- render a context pack
- compare results against the current path

The spike should produce a real estimate, not vibes.

Initial estimate from the spike:

- Native raw capture plus context-pack rendering is release-ready for the current CLI/MCP surface.
- Replacing the current Graphiti write path needs 3 focused slices: entity/episode/edge write
  adapters, hybrid retrieval fusion over lexical/vector/graph signals, and temporal/supersession
  semantics.
- Full Graphiti removal remains larger because extraction, duplicate detection, summaries, community
  clustering, and temporal reasoning still live in Graphiti-shaped contracts.
- `packages/python/sibyl-core/tests/graph/surreal/test_native_memory_spike.py` is now the minimum
  executable contract for future native-path work.

### W7. Native Retrieval and Context Quality

Build the retrieval engine around context pack quality and authorization:

- facet-aware search plans
- raw source baseline retrieval
- hybrid search using lexical plus vector fusion
- graph neighborhood expansion
- temporal filtering
- source/provenance filters
- principal, organization, project, and memory-space filters
- boost-not-filter weak signals
- reranking based on active plans, decisions, and recency
- optional model reranking
- redaction-aware rendering
- benchmarked precision/recall fixtures

The target is not "match Graphiti." The target is "give agents exactly the context that makes them
faster without crossing memory boundaries."

### W8. Reflection Engine

Build consolidation as a first-class worker flow:

- collect raw session captures
- extract entities, claims, ideas, plans, decisions, artifacts, and relationships
- mark superseded decisions
- create summaries with source links
- preserve original raw captures
- emit context-pack-ready graph updates
- propose private-to-shared promotions with provenance and review state
- keep personal, project, team, and organization rollups separate unless policy allows merging

Reflection is where Sibyl becomes a brain instead of a notebook.

### W9. Agent Diaries

Give stable agents lightweight continuity:

- write and read diary entries for named agents
- keep diaries scoped to principal, project, and memory space
- include diary hits in context packs only when authorized and relevant
- promote diary learnings through reflection instead of writing directly into shared memory

Early dogfood gate:

- a Codex/Nova-style coding agent can write private diary notes for recurring project gotchas and
  retrieve them in `wake`
- a Haven agent can write private home-observation diary notes without promoting them into shared
  household memory automatically
- diary entries show up in context packs only when identity, delegation, project, and memory-space
  scope all allow it

### W10. Graph-Guided Synthesis

Build large-read output on top of existing primitives after reflection has run enough real cycles to
provide trustworthy derived context:

- generate an outline from graph neighborhoods and source packs
- let an agent steer scope, audience, section order, and depth
- materialize section-level context packs with source IDs
- draft documentation, reports, and briefings from those packs
- verify source coverage, redactions, freshness, and unresolved claims
- remember generated artifacts with provenance back to source memory

First synthesis gate:

- reflection has produced source-linked summaries and supersession signals for the target memory
  space
- given a seeded memory space with raw notes, decisions, plans, and artifacts, `synthesize` produces
  a short architecture overview
- every section includes source IDs
- forbidden-scope memories are absent or redacted
- unresolved claims are listed instead of invented
- the output renders as Markdown and JSON

### W11. Human Memory Experience

Make personal memory feel usable and safe:

- build a memory home that shows recent captures, imports, reflections, recalls, and agent access
- make scope switching obvious across private, delegated, project, team, organization, shared, and
  public memory
- add source-centered memory inspection: raw source, derived facts, linked claims, visibility, and
  history
- support correction flows for wrong, stale, sensitive, duplicated, or superseded memories
- add private-to-shared promotion review with preview, rationale, and rollback
- make import progress, skipped records, dedupe, and extraction status visible
- show "what would this agent see?" previews before granting access
- design deletion, redaction, and export flows that explain consequences without fearmongering

First UX gate:

- a human can import or create private memory, find it again, understand why it appeared in a
  recall, correct it, promote it to a project memory space, preview agent access, and undo the
  promotion

### W12. Collaborative Product Surfaces

Expose the multi-user model in the web app, CLI, and MCP surfaces:

- invite users and agents into organizations and projects
- manage memory spaces, roles, memberships, and API keys
- preview what a user or agent would recall before granting access
- inspect why context was included, hidden, redacted, or promoted
- show shared memory activity and reflection progress in real time
- export audit trails for security and debugging

### W13. Legacy Removal

Remove legacy guts when migration gates are green. The destination is not "support both forever"; it
is migrate existing users, verify the cutover, and let the old stack disappear.

- FalkorDB removed from default dev/prod path, charts, docs, and runtime assumptions
- Graphiti removed after native behavior and context quality pass
- PostgreSQL removed only after auth, content, raw captures, RAG, settings, and jobs are SurrealDB
  native
- Redis removed only when Taskiq/job orchestration no longer needs it or a Surreal-backed queue is
  accepted

## Acceptance Criteria

The northstar is reached when:

- multiple organizations, projects, users, and agents can share one Sibyl runtime with strict
  isolation
- raw source memory is preserved, searchable, and linked to every derived graph claim
- large personal archives such as email, chats, notes, documents, and calendar exports can be
  imported into private memory through resumable, source-preserving adapters
- Haven can use Sibyl as its memory brain for private home preferences, routines, device context,
  and household knowledge without leaking unrelated personal memory
- `wake` returns bounded identity and essential story context without deep-search surprise
- users can keep private memory while contributing selected memory to project, team, organization,
  shared, or public spaces
- delegated agents can recall and remember only within their authorized scope
- `recall` returns precise project, planning, ideation, and domain context
- `remember` captures every important session detail with provenance
- `reflect` consolidates raw captures into durable graph memory
- `synthesize` can produce source-backed documentation or reports from an authorized graph slice
- context packs explain source, visibility, confidence, freshness, and redaction decisions
- context packs improve agent build speed in real work
- human users can inspect, correct, hide, promote, export, and delete personal memory from friendly
  product surfaces without understanding graph internals
- users can preview what another user, teammate, or delegated agent would be able to recall before
  sharing access
- coding workflows preserve decisions, code references, debugging evidence, review findings, test
  results, and handoff context across human and agent sessions
- benchmark fixtures catch retrieval regressions, source-grounding loss, and permission leaks
- SurrealDB is the only required data plane for default deployments
- Graphiti is gone from core memory paths
- FalkorDB and PostgreSQL are gone from supported northstar deployments after the migration window

## Open Questions

- What role model is enough for teams without becoming enterprise sludge?
- Should memory spaces be hierarchical, tag-based, or both?
- Should home-assistant memory spaces model a household, a person, a room, a device graph, or a
  combination of those scopes?
- What exact token budgets should L0/L1/L2 use for Codex, Claude Code, and MCP clients?
- How much raw source should `wake` include before it becomes noisy?
- How should agents represent delegated authority across MCP, CLI, API, and prompt hooks?
- What is the right review flow for promoting private memory into shared memory?
- What are the core human UX flows for memory confidence: capture, inspect, correct, promote,
  preview, redact, delete, export?
- What should the first memory home show so personal context feels useful instead of overwhelming?
- Which cross-organization sharing mode is actually useful: shared packs, shared spaces, published
  artifacts, or all three?
- Which source adapter shape covers repos, chat transcripts, and docs without overabstracting?
- Which mailbox formats should be first-class first: MBOX, Maildir, Gmail Takeout, Apple Mail
  export, IMAP, or all via adapters?
- What embedding model and async embedding pipeline should the native runtime use?
- Which callers still rely on default org fallback, and how do they migrate?
- What synthesis outputs matter first after reflection proves itself: architecture docs, onboarding,
  release notes, research briefs, household briefings, or audit packets?
- When is SurrealDB Cloud ready enough for managed multi-user deployments?
- Does SDK 2.0 expose any breaking result-shape changes that affect the driver?
- What is the measured recall/latency profile for filtered HNSW queries under Sibyl-sized data?
- Should graph neighborhoods use communities, clusters, typed traversals, recency windows, or a new
  Sibyl-specific signal?
- What graph neighborhood and temporal-validity signals should replace Graphiti concepts rather than
  porting them?
- Which context pack fixtures prove "agents build faster" instead of merely "search returned text"?
- How aggressive should prompt hooks be about nudging agents to call `remember`?

## Source Checkpoints

Validated against current primary sources on 2026-04-26:

- SurrealDB release notes: <https://surrealdb.com/releases>
- SurrealDB 3.0 product page: <https://surrealdb.com/3.0>
- SurrealDB vector search guide:
  <https://surrealdb.com/docs/surrealdb/reference-guide/vector-search>
- SurrealDB search functions: <https://surrealdb.com/docs/surrealql/functions/database/search>
- SurrealDB graph relations: <https://surrealdb.com/docs/surrealdb/reference-guide/graph-relations>
- SurrealDB authentication: <https://surrealdb.com/docs/surrealdb/security/authentication>
- SurrealDB `DEFINE ACCESS`: <https://surrealdb.com/docs/surrealql/statements/define/access>
- SurrealDB table permissions: <https://surrealdb.com/docs/surrealql/statements/define/table>
- SurrealDB Python SDK docs: <https://surrealdb.com/docs/sdk/python>
- PyPI `surrealdb` package: <https://pypi.org/project/surrealdb/>
- SurrealDB Cloud FAQ: <https://surrealdb.com/docs/cloud/faqs>
- MemPalace official repository: <https://github.com/MemPalace/mempalace>
- MemPalace memory stack docs: <https://mempalaceofficial.com/concepts/memory-stack>
- MemPalace knowledge graph docs: <https://mempalaceofficial.com/concepts/knowledge-graph.html>
- MemPalace agent diary docs: <https://mempalaceofficial.com/concepts/agents.html>

Local planning source:

- `/tmp/sibyl-surreal-research/00-synthesis.md`
- `/Users/bliss/dev/mempalace`
- `/Users/bliss/dev/haven`
