# SurrealDB-Native Sibyl Goal State

- Status: living architecture target
- Last validated: 2026-04-26

This document defines the goal state for making Sibyl SurrealDB-native while preserving every
load-bearing SurrealDB migration thread already in flight. It is the map for the next build phase:
consolidate memory into one reusable graph, make agent context generation precise, and remove legacy
guts only after native replacements are proven.

## North Star

Sibyl becomes a domain-general memory runtime for agents.

The perfect interface is small:

1. `recall` retrieves a precise context pack for the current intent.
2. The agent acts using that context.
3. `remember` captures facts, decisions, ideas, plans, artifacts, and relationships.
4. `reflect` consolidates noisy session traces into durable knowledge.

That loop must work for software projects, product strategy, home automation, creative planning,
research, relationships between people and organizations, or any other modeled domain. The graph is
not a "code graph"; it is a context graph.

## Product Goal

Agents should build faster because Sibyl can answer:

- What are we doing right now?
- What did we already decide?
- What plans, ideas, constraints, and artifacts are relevant?
- What entities are connected to this work, even if the prompt uses different words?
- What changed since the last session?
- What should be injected before the agent wastes time rediscovering context?

The output should be compact enough to fit into agent prompts, structured enough for tools, and rich
enough that the agent can build with the project's real history in hand.

## Current Decision

We are moving toward one canonical Hyperbliss Technologies organization for Bliss's Sibyl data.

That is a deployment and migration decision, not a hardcoded product assumption. Tooling must accept
any canonical organization, any source archives, and any target SurrealDB connection. Hyperbliss
Technologies is the first real consolidation target, not a magic constant.

## Current State Already Landed

These pieces are part of the foundation and must not get lost while we push toward native SurrealDB:

- SurrealDB is the default storage direction, with legacy FalkorDB and PostgreSQL still supported.
- Graph archives can be exported, imported, verified, and dry-run merged.
- Merge tooling can rewrite source org data into a canonical org.
- Surreal auth supports username/password sign-in plus optional token authentication.
- Context packs exist across CLI, API, MCP, and prompt hooks.
- `remember` exists as an MCP tool and CLI command.
- `recall` exists as an intent-oriented CLI interface.
- Context packs already include direct matches and one-hop related graph context.
- The Sibyl skill defines the agent memory contract: recall, act, remember, reflect.

## Goal-State Architecture

### 1. One SurrealDB Runtime

Sibyl should run on one SurrealDB-backed data plane for graph memory, auth, content, tasks, raw
captures, context packs, and derived indexes.

Target properties:

- Remote server mode for any multi-process runtime.
- Embedded mode only for single-process dev or tests.
- Namespace-per-organization for graph memory.
- A dedicated auth namespace/database while auth remains global across organizations.
- No default org fallback in graph operations.
- Idempotent schema bootstrap.
- Archive-backed migration and rollback.

SurrealDB Cloud remains attractive for Bliss's multi-machine consolidation, but the official Cloud
FAQ still calls Cloud beta, AWS-only, and not configurable via custom CLI flags or environment
variables. Treat Cloud as the managed target, with local/server mode staying first-class.

### 2. Domain-General Graph Model

The native model must represent any domain without baking in software-only language.

Core node families:

- `Entity`: durable thing, person, org, system, project, location, concept, tool, or topic.
- `Artifact`: file, doc, repo, design, message, image, recording, schema, dataset, or generated
  output.
- `Episode`: observed event or captured conversation slice.
- `Session`: bounded work period with prompts, actions, tool calls, outcomes, and reflections.
- `Decision`: chosen direction, rejected alternative, rationale, and status.
- `Plan`: intended work, milestones, blockers, acceptance criteria, and ownership.
- `Idea`: speculative concept before it becomes a decision or plan.
- `Claim`: assertion with confidence, source, and contradiction support.
- `Task`: actionable unit of work with status, project, and evidence.
- `ContextPack`: rendered retrieval result with inputs, facets, source IDs, and injection target.

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

Every node and edge needs provenance. Context quality depends on knowing where facts came from, when
they were valid, and whether they were inferred, imported, user-stated, tool-observed, or generated.

### 3. Native Retrieval Engine

Sibyl's retrieval engine should combine:

- exact ID lookup
- lexical full-text search
- vector similarity search
- typed graph traversal
- time and validity filtering
- source/provenance filtering
- task/project/org scoping
- relationship-aware reranking

SurrealDB gives us HNSW vector indexes, full-text search, reciprocal rank fusion via `search::rrf`,
graph relations through `RELATE`, recursive arrow traversal, events, computed fields, and live
queries. Those primitives are enough to build a better context graph than Graphiti, but only if
Sibyl owns the retrieval contract directly.

Filtered vector search must be benchmarked before we rely on selective filters for recall quality.
Official docs show filters combined with KNN queries; they do not settle planner ordering or recall
behavior under realistic org/project filters. The native spike must measure this.

### 4. Context Pack Contract

A context pack is the agent-facing answer to "what should I know before acting?"

Minimum structure:

- request intent and query
- facets requested by the caller
- direct matches
- related entities and relationships
- current tasks and plans
- relevant decisions and constraints
- raw source IDs for audit
- confidence and freshness signals
- Markdown renderer for prompt injection
- JSON renderer for tools

The pack should be precise rather than huge. It should prefer high-signal decisions, plans,
constraints, active work, and directly connected artifacts over a wall of semantically similar text.

### 5. Capture and Reflection

`remember` is the fast path: capture now with enough structure to retrieve later.

`reflect` is the maintenance path: consolidate later into durable memory.

The goal state needs both:

- raw capture for every meaningful planning, ideation, and implementation moment
- structured extraction into nodes and relationships
- source-preserving summaries
- contradiction handling
- obsolete or superseded decision marking
- periodic session digests
- project and domain-level rollups
- prompt-hook feedback when an agent should remember something it just learned

Raw memory should never be thrown away just because extraction improves. Durable entities can be
rebuilt; original source history is the safety net.

### 6. Agent Injection Surfaces

Sibyl should be reusable anywhere agents need memory:

- MCP tools for `context`, `recall`, `remember`, and eventually `reflect`
- CLI commands for humans and shell workflows
- Claude/Codex prompt hooks
- API routes for web and external clients
- future app-specific integrations

No integration should know Hyperbliss-specific IDs. Callers provide org, project, intent, query,
facets, and optional source hints; Sibyl resolves the rest.

### 7. Live and Reactive UI

Once core storage is stable, live queries become a product feature:

- task and context graph updates without polling
- live capture feed
- memory consolidation progress
- graph changes as agents work
- context pack preview and audit trail

Live query enablement must wait for a patched SurrealDB version and explicit permission testing.

## Graphiti Deletion Position

Fully deleting Graphiti should leave us in a better place, but only after native parity is real.

Better:

- one data model instead of Graphiti abstractions wrapped around SurrealDB
- direct SurrealQL for graph, search, and traversal
- fewer dependency and security surfaces
- a domain-general model owned by Sibyl
- context packs tuned for agent speed instead of Graphiti's memory model
- easier migration away from FalkorDB concepts

Worse if we delete too early:

- lose mature episode/entity orchestration before replacement tests exist
- regress community detection and summaries
- regress temporal edge invalidation
- lose embedder/search abstractions without clean successors
- rewrite many tests at once with no behavioral baseline

Deletion gate:

- native entity, episode, edge, and search paths pass parity tests
- community detection is ported or intentionally redesigned
- context pack quality is better than the Graphiti-backed baseline
- migration and rollback are rehearsed
- legacy services are out of the default path
- Graphiti security posture is known until the dependency is gone

## Workstreams

### W0. Goal-State Tracking

Keep this document current as decisions harden. When a SurrealDB migration branch lands, update the
"Current State Already Landed" section and remove stale gates.

### W1. Cloud and Canonical Org Consolidation

Finish archive merge validation for one canonical organization:

- export archives from source machines
- dry-run merge into a canonical org
- verify entity, relationship, embedding, auth, and content counts
- import into a disposable target
- sample recall/context queries
- cut over clients only after acceptance

Use Sibyl archives as the source of truth. Raw `surreal export` can be useful for backing up a
target instance, but it cannot merge scattered local org graphs by itself.

### W2. SurrealDB SDK and Runtime Hardening

The official Python SDK's latest stable PyPI release is `surrealdb` 2.0.0, released 2026-04-23.
Sibyl currently needs a compatibility spike before building new Cloud or live-query work on old SDK
assumptions.

Runtime gates:

- test SDK 2.0 against current driver behavior
- validate `signin`, `authenticate`, result shapes, and reconnect behavior
- keep WebSocket concurrency protected until a focused regression test proves safe relaxation
- keep embedded mode out of multi-process dev and production

### W3. Native Graph Schema Spike

Replace one end-to-end path with direct SurrealQL before estimating the full Graphiti removal:

- create entity
- create episode/session memory
- relate entities
- search by lexical, vector, and graph signals
- render a context pack
- compare results against the current path

The spike should produce a real estimate, not vibes.

### W4. Native Retrieval and Context Quality

Build the retrieval engine around context pack quality:

- facet-aware search plans
- hybrid search using lexical plus vector fusion
- graph neighborhood expansion
- temporal filtering
- source/provenance filters
- reranking based on active plans, decisions, and recency
- benchmarked precision/recall fixtures

The target is not "match Graphiti." The target is "give agents exactly the context that makes them
faster."

### W5. Reflection Engine

Build consolidation as a first-class worker flow:

- collect raw session captures
- extract entities, claims, ideas, plans, decisions, artifacts, and relationships
- mark superseded decisions
- create summaries with source links
- preserve original raw captures
- emit context-pack-ready graph updates

Reflection is where Sibyl becomes a brain instead of a notebook.

### W6. Legacy Removal

Remove legacy guts only when gates are green:

- FalkorDB removed from default dev/prod path
- Graphiti removed after native parity and context quality pass
- PostgreSQL removed only after auth, content, raw captures, RAG, settings, and jobs are SurrealDB
  native
- Redis removed only when Taskiq/job orchestration no longer needs it or a Surreal-backed queue is
  accepted

## Acceptance Criteria

The goal state is reached when:

- one canonical org can be used across Bliss's machines without hardcoded assumptions
- `recall` returns precise project, planning, ideation, and domain context
- `remember` captures every important session detail with provenance
- `reflect` consolidates raw captures into durable graph memory
- context packs improve agent build speed in real work
- SurrealDB is the only required data plane for default deployments
- Graphiti is gone from core memory paths
- legacy stack remains available only as migration or compatibility mode

## Open Questions

- When do we cut from local/server mode to SurrealDB Cloud for Bliss's canonical org?
- Does SDK 2.0 expose any breaking result-shape changes that affect the driver?
- What is the measured recall/latency profile for filtered HNSW queries under Sibyl-sized data?
- Should community detection preserve Graphiti semantics or become a new Sibyl-specific signal?
- Which context pack fixtures prove "agents build faster" instead of merely "search returned text"?
- How aggressive should prompt hooks be about nudging agents to call `remember`?

## Source Checkpoints

Validated against current primary sources on 2026-04-26:

- SurrealDB release notes: <https://surrealdb.com/releases>
- SurrealDB 3.0 product page: <https://surrealdb.com/3.0>
- SurrealDB vector search guide:
  <https://surrealdb.com/docs/surrealdb/reference-guide/vector-search>
- SurrealDB search functions: <https://surrealdb.com/docs/surrealql/functions/database/search>
- SurrealDB Python SDK docs: <https://surrealdb.com/docs/sdk/python>
- PyPI `surrealdb` package: <https://pypi.org/project/surrealdb/>
- SurrealDB Cloud FAQ: <https://surrealdb.com/docs/cloud/faqs>

Local planning source:

- `/tmp/sibyl-surreal-research/00-synthesis.md`
