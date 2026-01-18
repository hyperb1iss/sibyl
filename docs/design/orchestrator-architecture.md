# Sibyl Orchestrator Architecture

**Status**: Planning **Last Updated**: 2026-01-12

## Executive Summary

Sibyl's agent orchestration follows a **flexible three-tier pattern** with manual creation at every
level:

1. **MetaOrchestrator** - ONE per project (singleton), persists across sessions, coordinates big
   picture
2. **TaskOrchestrator** - Per-task coordinator, manages build loop, can be created manually
3. **Worker Agents** - Execute actual work, can be created manually, can be "promoted" to tasks

**Key Principles:**

- **Every tier is directly accessible** - Users can create at any level
- **Association is optional** - Entities can exist independently, be linked later
- **Promotion model** - Workers can become Tasks, Tasks report to Meta
- **No forced hierarchy** - Quick help doesn't need orchestration overhead

---

## Flexible Creation Model

### Entry Points

Users can start at ANY level depending on their need:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  USER ENTRY POINTS                                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  "Build OAuth for my app"          "Fix the login bug"      "Help me        │
│         │                                  │                 understand      │
│         │ Complex multi-task               │ Single task     this code"     │
│         │                                  │ with QA               │        │
│         ▼                                  ▼                       │        │
│  ┌──────────────┐               ┌──────────────────┐              │        │
│  │    META      │               │ TASK ORCHESTRATOR│              │        │
│  │ ORCHESTRATOR │               │    (manual)      │              │        │
│  │  (project)   │               └────────┬─────────┘              │        │
│  └──────┬───────┘                        │                        │        │
│         │                                │                        ▼        │
│         │ decomposes                     │ spawns         ┌────────────┐   │
│         ▼                                ▼                │   WORKER   │   │
│  ┌──────────────┐               ┌──────────────┐         │  (direct)  │   │
│  │ Task Orch 1  │               │    Worker    │         └────────────┘   │
│  │ Task Orch 2  │               └──────────────┘                          │
│  │ Task Orch 3  │                                                          │
│  └──────────────┘                                                          │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Association & Promotion

Entities created independently can be linked later:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROMOTION MODEL                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. WORKER PROMOTION                                                         │
│     ─────────────────                                                        │
│     Worker created directly ──▶ User works with it                          │
│                                      │                                       │
│                                      ▼                                       │
│                            "Make this a proper task"                         │
│                                      │                                       │
│                                      ▼                                       │
│                          TaskOrchestrator ADOPTS worker                      │
│                          • Creates Sibyl task                                │
│                          • Runs quality gates on existing work               │
│                          • Continues with build loop                         │
│                                                                              │
│  2. TASK ORCHESTRATOR REGISTRATION                                           │
│     ────────────────────────────────                                         │
│     TaskOrchestrator created manually ──▶ MetaOrchestrator sees it          │
│                                           • Adds to project view             │
│                                           • Tracks for merge coordination    │
│                                           • Incorporates learnings           │
│                                                                              │
│  3. LATERAL ADOPTION                                                         │
│     ─────────────────                                                        │
│     Orphan Worker exists ──▶ User assigns to existing TaskOrchestrator      │
│                              • Worker joins task's build loop                │
│                              • Previous work becomes task context            │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### MetaOrchestrator as Project Singleton

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROJECT: sibyl-api                                                          │
│  ═══════════════════════════════════════════════════════════════════════    │
│                                                                              │
│  META-ORCHESTRATOR (1 per project, persists across sessions)                │
│  ├── Status: monitoring                                                      │
│  ├── Created: 2026-01-10                                                     │
│  ├── Sessions: 47                                                            │
│  │                                                                           │
│  ├── Active TaskOrchestrators:                                               │
│  │   ├── OAuth Implementation (implementing)                                 │
│  │   ├── Dashboard Refactor (reviewing)                                      │
│  │   └── API Rate Limiting (human_review)                                    │
│  │                                                                           │
│  ├── Standalone Workers (not in tasks):                                      │
│  │   └── agent_abc123 (exploring codebase)                                   │
│  │                                                                           │
│  ├── Completed Tasks (this week): 12                                         │
│  │                                                                           │
│  └── Project Patterns Learned:                                               │
│       ├── "Always add migration for model changes"                           │
│       ├── "Frontend components need accessibility tests"                     │
│       └── "Auth changes require security review"                             │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Design Principles

### 1. Hierarchical Control with Direct Access

- Meta-orchestrator coordinates the big picture (1 per project)
- Task orchestrators own individual task lifecycles
- Users can create workers directly for quick help
- Clear accountability at each level, but no forced path

### 2. Quality-First Execution

- Every implementation passes through review gates
- TaskOrchestrator owns the review → rework loop
- Heavy UI features require human review checkpoint
- Test coverage gates before merge

### 3. Intelligent Parallelism

- Independent tasks run in parallel (worktree isolation)
- Each TaskOrchestrator operates independently
- Meta-orchestrator handles cross-task coordination

### 4. Persistent Memory

- All decisions logged to knowledge graph
- Cross-session continuity
- Pattern learning from completed work

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER INTERFACE                                  │
│  (Chat UI / CLI / API)                                                      │
│                                                                              │
│  User can interact with ANY level:                                          │
│  • Meta-orchestrator for high-level coordination                            │
│  • Task orchestrator for specific task progress                             │
│  • Worker agent directly for hands-on collaboration                         │
└────────────────┬─────────────────────┬─────────────────────┬────────────────┘
                 │                     │                     │
                 │ primary             │ task-level          │ direct
                 │ interaction         │ interaction         │ interaction
                 ▼                     │                     │
┌─────────────────────────────────────────────────────────────────────────────┐
│                          META-ORCHESTRATOR                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  Decompose   │  │   Spawn      │  │   Merge      │  │   Memory     │    │
│  │  Requests    │  │   Tasks      │  │   Coordinate │  │   (Sibyl)    │    │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘    │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
           ┌──────────────────────────┼──────────────────────────┐
           │                          │                          │
           ▼                          ▼                          ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│  TASK ORCHESTRATOR  │  │  TASK ORCHESTRATOR  │  │  TASK ORCHESTRATOR  │
│  (OAuth Backend)    │◀─┼──(OAuth Frontend)   │  │  (Tests)            │
│                     │  │                     │  │                     │
│  ┌───────────────┐  │  │  ┌───────────────┐  │  │  ┌───────────────┐  │
│  │ implement     │  │  │  │ implement     │  │  │  │ implement     │  │
│  │     ↓         │  │  │  │     ↓         │  │  │  │     ↓         │  │
│  │ review        │  │  │  │ review        │  │  │  │ review        │  │
│  │     ↓         │  │  │  │     ↓         │  │  │  │     ↓         │  │
│  │ rework?       │──┼──┼──│ rework?       │──┼──┼──│ rework?       │  │
│  │     ↓         │  │  │  │     ↓         │  │  │  │     ↓         │  │
│  │ complete      │  │  │  │ complete      │  │  │  │ complete      │  │
│  └───────────────┘  │  │  └───────────────┘  │  │  └───────────────┘  │
└──────────┬──────────┘  └──────────┬──────────┘  └──────────┬──────────┘
           │                        │                        │
           ▼                        ▼                        ▼
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│   WORKER AGENT      │  │   WORKER AGENT      │  │   WORKER AGENT      │
│   (Implementer)     │  │   (Implementer)     │  │   (Tester)          │
│                     │  │                     │  │                     │
│   User can chat     │◀─┼── User can chat     │◀─┼── User can chat     │
│   directly here     │  │   directly here     │  │   directly here     │
│                     │  │                     │  │                     │
│   Isolated worktree │  │   Isolated worktree │  │   Isolated worktree │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
```

---

## Tier 1: MetaOrchestrator Responsibilities

The MetaOrchestrator handles high-level coordination across multiple tasks.

### Phase 1: Request Decomposition

```
INPUT:  User request (natural language)
OUTPUT: Task breakdown with dependencies
```

1. Parse user intent
2. Query Sibyl for relevant patterns/context
3. Break into atomic tasks
4. Identify dependencies
5. Estimate complexity (for parallelism decisions)

### Phase 2: TaskOrchestrator Spawning

```
INPUT:  Task breakdown
OUTPUT: Running TaskOrchestrators for each task
```

1. Spawn TaskOrchestrator per task
2. Configure quality gate requirements
3. Set up worktree strategy
4. Establish inter-task dependencies

### Phase 3: Cross-Task Coordination

```
INPUT:  Running TaskOrchestrators
OUTPUT: Coordinated progress, resolved conflicts
```

1. Monitor TaskOrchestrator status
2. Handle cross-task dependencies
3. Route high-level approvals to users
4. Manage resource allocation across tasks

### Phase 4: Merge Coordination

```
INPUT:  Completed tasks
OUTPUT: Merged, integrated codebase
```

1. Determine merge order (respecting dependencies)
2. Detect and resolve conflicts
3. Coordinate final integration
4. Log learnings to Sibyl

---

## Tier 2: TaskOrchestrator Responsibilities

Each task gets its own TaskOrchestrator that owns the full build lifecycle.

### The Build Loop

```
┌─────────────────────────────────────────────────────────────────┐
│                     TASK ORCHESTRATOR LOOP                       │
│                                                                  │
│     ┌──────────┐                                                │
│     │  START   │                                                │
│     └────┬─────┘                                                │
│          │                                                       │
│          ▼                                                       │
│     ┌──────────┐     spawn worker                               │
│     │IMPLEMENT │────────────────────▶ Worker Agent              │
│     └────┬─────┘                                                │
│          │ worker completes                                      │
│          ▼                                                       │
│     ┌──────────┐     run gates                                  │
│     │  REVIEW  │────────────────────▶ Lint, Type, Test, AI      │
│     └────┬─────┘                                                │
│          │                                                       │
│          ▼                                                       │
│     ┌──────────┐                                                │
│     │  PASS?   │                                                │
│     └────┬─────┘                                                │
│    NO ───┴─── YES                                               │
│     │         │                                                  │
│     ▼         ▼                                                  │
│ ┌──────────┐  ┌──────────┐                                      │
│ │  REWORK  │  │ COMPLETE │──▶ Notify MetaOrchestrator           │
│ └────┬─────┘  └──────────┘                                      │
│      │                                                           │
│      │ send feedback to worker                                   │
│      └─────────────────────────────────────────▶ Worker Agent   │
│                                                       │          │
│      ◀────────────────────────────────────────────────┘          │
│      │ worker fixes issues                                       │
│      │                                                           │
│      └──────────▶ back to REVIEW                                │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Ralph Loop Safety Patterns

Our build loop is essentially a **Ralph Wiggum loop** - the iterative AI pattern that feeds failures
back into the model until success. This carries known risks we must mitigate:

#### The Problem: Iterative Safety Degradation

Research shows that pure LLM feedback loops can cause **code quality to degrade** after ~3
iterations:

- Initially secure code may introduce vulnerabilities (auth bypasses, SQL injection)
- "Context rot" - LLMs get worse as context fills up
- "Over-optimizing" or "direction drift" beyond the sweet spot

#### Our Safeguards

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  RALPH LOOP SAFETY CONTROLS                                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  1. HARD LIMITS                                                              │
│     • max_rework_attempts = 3 (configurable)                                │
│     • Beyond 3 iterations → human escalation, NOT more AI                   │
│     • Token/cost budgets per TaskOrchestrator                               │
│                                                                              │
│  2. DEGRADATION DETECTION                                                    │
│     • Track quality metrics across iterations                                │
│     • If iteration N is WORSE than N-1 → flag for human review              │
│     • Security scan on EVERY iteration, not just final                      │
│                                                                              │
│  3. CONTEXT MANAGEMENT                                                       │
│     • Summarize previous attempts before next iteration                      │
│     • Don't dump full error logs - extract actionable feedback              │
│     • Fresh worker context if rework_count > 2                              │
│                                                                              │
│  4. VERIFICATION-DRIVEN EXIT                                                 │
│     • Explicit completion criteria, not "AI thinks it's done"               │
│     • Tests must PASS, not just "look fixed"                                │
│     • Human verification for security-sensitive changes                      │
│                                                                              │
│  5. FEEDBACK INJECTION                                                       │
│     • Failed gate reasons become structured context                          │
│     • "Fix X" not "here's the full stack trace"                             │
│     • Specific, actionable, minimal                                          │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### Iteration Strategy

```
Iteration 1: Full autonomy, standard gates
Iteration 2: Targeted feedback, increased scrutiny
Iteration 3: Human-assisted, fresh context option
Iteration 4+: BLOCKED - requires human intervention
             "The AI is stuck. Human needed."
```

#### Why 3 Iterations?

Studies show most tasks converge within 1-3 iterations. Beyond that:

- Diminishing returns
- Increased risk of degradation
- Cost inefficiency
- Sign that the task needs human insight, not more compute

### TaskOrchestrator States

```python
TASK_ORCHESTRATOR_STATES = {
    "initializing":  "Setting up worktree, spawning worker",
    "implementing":  "Worker agent actively coding",
    "reviewing":     "Running quality gates",
    "reworking":     "Worker fixing review feedback",
    "human_review":  "Awaiting human approval",
    "complete":      "All gates passed, ready for merge",
    "failed":        "Max rework attempts exceeded",
    "paused":        "User paused this task"
}
```

### Key Behaviors

1. **Owns the worker lifecycle** - Spawns, monitors, and terminates worker agent
2. **Manages quality loop** - Runs gates, sends feedback, tracks rework attempts
3. **Handles human touchpoints** - Requests approval when needed
4. **Reports to MetaOrchestrator** - Status updates, completion signals
5. **Allows user interaction** - User can chat with TaskOrchestrator about progress

---

## Tier 3: Worker Agent Responsibilities

Worker agents do the actual implementation work.

### Capabilities

- **Code writing** - Implements features, fixes bugs
- **Test writing** - Creates unit/integration tests
- **Investigation** - Debugs issues, analyzes code
- **Direct user chat** - User can jump in anytime

### Worker States

```python
WORKER_STATES = {
    "working":       "Actively executing task",
    "waiting":       "Awaiting user/orchestrator input",
    "paused":        "User initiated pause",
    "reworking":     "Fixing review feedback",
    "complete":      "Finished assigned work"
}
```

### User Interaction Model

Users can interact directly with workers:

```
USER ──▶ "Hey, can you explain that auth check?"
         │
         ▼
    ┌─────────────┐
    │   WORKER    │ ◀── Responds directly to user
    │   AGENT     │     while TaskOrchestrator watches
    └─────────────┘
         │
         │ (TaskOrchestrator monitors but doesn't interrupt)
         ▼
    TaskOrchestrator gets transcript for context
```

---

## Quality Gates (Run by TaskOrchestrator)

Quality gates are configurable per task type:

- **Lint/Type check**: Automatic, blocking
- **Test suite**: Automatic, blocking
- **AI Code Review**: Automatic, may trigger rework
- **Human Review**: Manual, for significant changes
- **Integration Test**: Automatic, pre-merge

---

## Quality Gate Loops

### Standard Implementation Loop

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│    ┌──────────┐     ┌──────────┐     ┌──────────┐         │
│    │IMPLEMENT │────▶│  REVIEW  │────▶│  PASS?   │         │
│    └──────────┘     └──────────┘     └────┬─────┘         │
│         ▲                                  │               │
│         │           ┌──────────┐          │               │
│         └───────────│  REWORK  │◀─────────┘ NO            │
│                     └──────────┘                          │
│                                            │ YES          │
│                                            ▼              │
│                                     ┌──────────┐          │
│                                     │  MERGE   │          │
│                                     └──────────┘          │
└─────────────────────────────────────────────────────────────┘
```

### Heavy Feature Loop (UI/UX)

```
IMPLEMENT ──▶ AI REVIEW ──▶ HUMAN PREVIEW ──▶ APPROVE? ──▶ MERGE
     ▲                            │                │
     └────────────────────────────┴── REWORK ◀────┘
```

### Critical Path Loop (Security/Data)

```
IMPLEMENT ──▶ SECURITY SCAN ──▶ AI REVIEW ──▶ HUMAN REVIEW ──▶ MERGE
     ▲              │                │              │
     └──────────────┴────────────────┴── REWORK ◀──┘
```

---

## Agent Types

### Orchestration Layer (No Worktree)

| Type                 | Purpose                                | Spawned By       |
| -------------------- | -------------------------------------- | ---------------- |
| **MetaOrchestrator** | Decompose requests, spawn tasks, merge | User request     |
| **TaskOrchestrator** | Manage single task through build loop  | MetaOrchestrator |

### Worker Layer (Isolated Worktree)

| Type             | Purpose         | Spawned By       | User Interaction |
| ---------------- | --------------- | ---------------- | ---------------- |
| **Implementer**  | Write code      | TaskOrchestrator | Direct chat OK   |
| **Tester**       | Write/run tests | TaskOrchestrator | Direct chat OK   |
| **Investigator** | Debug, analyze  | TaskOrchestrator | Direct chat OK   |
| **Reviewer**     | AI code review  | TaskOrchestrator | View results     |

---

## State Machines

### MetaOrchestrator States

```python
META_ORCHESTRATOR_STATES = {
    "idle":        "Waiting for user input",
    "analyzing":   "Parsing request, querying Sibyl",
    "decomposing": "Breaking into tasks",
    "spawning":    "Creating TaskOrchestrators",
    "monitoring":  "TaskOrchestrators running",
    "merging":     "Coordinating final integration",
    "completing":  "Logging learnings, cleanup",
    "failed":      "Error state, needs intervention"
}
```

### TaskOrchestrator States

```python
TASK_ORCHESTRATOR_STATES = {
    "initializing":  "Setting up worktree",
    "implementing":  "Worker actively coding",
    "reviewing":     "Running quality gates",
    "reworking":     "Worker fixing feedback",
    "human_review":  "Awaiting human approval",
    "complete":      "Ready for merge",
    "failed":        "Max attempts exceeded",
    "paused":        "User paused"
}
```

### Worker Agent States

```python
WORKER_STATES = {
    "spawning":    "Initializing in worktree",
    "working":     "Executing task",
    "waiting":     "Awaiting input",
    "reworking":   "Fixing review feedback",
    "complete":    "Finished work",
    "failed":      "Unrecoverable error"
}
```

---

## User Journeys

### Journey 1: Feature Development

```
USER: "Add OAuth2 login with Google and GitHub"

ORCHESTRATOR:
  1. Query Sibyl for auth patterns
  2. Create tasks:
     - task_1: Backend OAuth routes (implementer)
     - task_2: Frontend login UI (implementer)
     - task_3: Integration tests (tester)
  3. Spawn task_1 + task_2 in parallel
  4. On completion → AI review both
  5. If review clean → spawn task_3
  6. If tests pass → human review (UI feature)
  7. On approval → merge all worktrees
  8. Log learnings to Sibyl
```

### Journey 2: Bug Fix

```
USER: "Users can't upload files > 10MB"

ORCHESTRATOR:
  1. Query Sibyl for upload patterns
  2. Spawn implementer to investigate
  3. Agent finds root cause, proposes fix
  4. Request approval for fix approach
  5. On approval → implement
  6. AI review → likely clean (small change)
  7. Test pass → merge
  8. Log fix pattern to Sibyl
```

### Journey 3: Multi-Day Project

```
USER: "Build a dashboard for usage analytics"

Day 1:
  - Orchestrator creates epic with 12 tasks
  - Starts with data layer (3 tasks parallel)
  - Reviews and merges backend

Day 2:
  - Spawns UI component tasks (4 parallel)
  - Human reviews component designs
  - Iterates on feedback

Day 3:
  - Integration and polish tasks
  - Full test suite run
  - Final human review
  - Deploy
```

### Journey 4: Emergency Response

```
USER: "Production is down! Error rate spiked 10x"

ORCHESTRATOR:
  1. Query Sibyl for similar incidents
  2. Spawn investigator agent (no worktree)
  3. Real-time updates to user
  4. On root cause → rapid fix branch
  5. Minimal review gate (emergency mode)
  6. Deploy with rollback ready
  7. Post-incident: full review, learnings
```

---

## Memory Integration

### What Orchestrator Stores

| Event                 | Stored As | Purpose            |
| --------------------- | --------- | ------------------ |
| Task completion       | Episode   | Future reference   |
| Review feedback       | Episode   | Pattern learning   |
| Bug root cause        | Pattern   | Prevent recurrence |
| Architecture decision | Pattern   | Consistency        |
| User preference       | Entity    | Personalization    |

### What Orchestrator Queries

| Phase     | Query             | Example                          |
| --------- | ----------------- | -------------------------------- |
| Planning  | Similar tasks     | "authentication implementations" |
| Execution | Known gotchas     | "OAuth token refresh issues"     |
| Review    | Past feedback     | "code style preferences"         |
| Merge     | Conflict patterns | "package.json merge strategies"  |

---

## SOTA Comparison

| Feature            | AutoClaude | Devin   | Cursor | **Sibyl**        |
| ------------------ | ---------- | ------- | ------ | ---------------- |
| Multi-agent        | Limited    | Yes     | No     | **Yes**          |
| Worktree isolation | No         | Unknown | No     | **Yes**          |
| Persistent memory  | Partial    | Partial | No     | **Yes (Graph)**  |
| Human-in-loop      | Basic      | Yes     | Yes    | **Yes (Rich)**   |
| Quality gates      | No         | Basic   | No     | **Configurable** |
| Self-hosted        | No         | No      | No     | **Yes**          |

### Sibyl's Differentiators

1. **Graph-based memory**: Not just conversation history—semantic knowledge
2. **Configurable quality**: Different gates for different work types
3. **Worktree isolation**: True parallel development
4. **Enterprise-ready**: Multi-tenant, audit trail, approval workflows

---

## API Design

### Manual Creation Endpoints

```python
# ═══════════════════════════════════════════════════════════════════════════
# TIER 3: WORKER (Direct Creation)
# ═══════════════════════════════════════════════════════════════════════════

POST /api/projects/{project_id}/workers
Body: {
    "prompt": "Help me understand the auth middleware",
    "create_worktree": false,  # Optional, for exploration
}
Response: {
    "worker_id": "agent_abc123",
    "thread_id": "thread_xyz",
    "status": "working"
}

# Promote worker to task
POST /api/workers/{worker_id}/promote
Body: {
    "title": "Refactor auth middleware",
    "create_sibyl_task": true,
    "run_quality_gates": true
}
Response: {
    "task_orchestrator_id": "taskorch_def456",
    "task_id": "task_abc123",
    "worker_adopted": true
}

# ═══════════════════════════════════════════════════════════════════════════
# TIER 2: TASK ORCHESTRATOR (Manual Creation)
# ═══════════════════════════════════════════════════════════════════════════

POST /api/projects/{project_id}/task-orchestrators
Body: {
    "task_id": "task_abc123",        # Existing Sibyl task, or...
    "title": "Fix login bug",         # ...create new task with this title
    "quality_gates": ["lint", "test", "ai_review"],
    "max_rework_attempts": 3
}
Response: {
    "task_orchestrator_id": "taskorch_def456",
    "worker_id": "agent_ghi789",      # Auto-spawned worker
    "status": "implementing"
}

# Adopt existing worker into task
POST /api/task-orchestrators/{id}/adopt
Body: {
    "worker_id": "agent_abc123"
}
Response: {
    "adopted": true,
    "previous_work_preserved": true
}

# ═══════════════════════════════════════════════════════════════════════════
# TIER 1: META ORCHESTRATOR (Project Singleton)
# ═══════════════════════════════════════════════════════════════════════════

# Get or create project's MetaOrchestrator
GET /api/projects/{project_id}/meta-orchestrator
Response: {
    "meta_orchestrator_id": "metaorch_abc123",
    "status": "idle",
    "active_task_orchestrators": [...],
    "standalone_workers": [...],
    "project_patterns": [...]
}

# Submit complex request for decomposition
POST /api/meta-orchestrators/{id}/requests
Body: {
    "prompt": "Build OAuth2 with Google and GitHub support"
}
Response: {
    "request_id": "req_xyz",
    "decomposed_tasks": [
        {"title": "OAuth data model", "task_orch_id": "taskorch_1"},
        {"title": "Google OAuth flow", "task_orch_id": "taskorch_2"},
        {"title": "GitHub OAuth flow", "task_orch_id": "taskorch_3"},
    ],
    "dependency_graph": {...}
}
```

### UI Actions Mapping

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  UI ACTION                    │  API CALL                    │ RESULT       │
├───────────────────────────────┼──────────────────────────────┼──────────────┤
│  "Quick Help" button          │  POST /workers               │  Worker chat │
│  "New Task" button            │  POST /task-orchestrators    │  Task + QA   │
│  "Plan Feature" button        │  POST /meta-orch/requests    │  Multi-task  │
│  "Promote to Task" in thread  │  POST /workers/{id}/promote  │  TaskOrch    │
│  "Add to Task" context menu   │  POST /task-orch/{id}/adopt  │  Adoption    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Model Updates

### New Entity Types

```python
class EntityType(StrEnum):
    # ... existing types ...
    META_ORCHESTRATOR = "meta_orchestrator"
    TASK_ORCHESTRATOR = "task_orchestrator"
```

### MetaOrchestratorRecord

```python
class MetaOrchestratorRecord(Entity):
    """Project-level orchestration singleton."""

    entity_type: EntityType = EntityType.META_ORCHESTRATOR

    # Identity (ONE per project)
    project_id: str = Field(..., description="Owning project")

    # State
    status: MetaOrchestratorStatus = Field(default="idle")

    # Active work
    active_task_orchestrator_ids: list[str] = Field(default_factory=list)
    standalone_worker_ids: list[str] = Field(default_factory=list)

    # History & Learning
    completed_task_ids: list[str] = Field(default_factory=list)
    project_patterns: list[str] = Field(default_factory=list)

    # Session tracking
    session_count: int = Field(default=0)
    last_active: datetime | None = Field(default=None)
```

### TaskOrchestratorRecord

```python
class TaskOrchestratorRecord(Entity):
    """Per-task build loop coordinator."""

    entity_type: EntityType = EntityType.TASK_ORCHESTRATOR

    # Ownership
    project_id: str = Field(..., description="Project UUID")
    meta_orchestrator_id: str | None = Field(default=None, description="Parent, if any")
    task_id: str = Field(..., description="Sibyl task being worked")

    # Worker management
    worker_id: str | None = Field(default=None, description="Current worker")
    worktree_id: str | None = Field(default=None, description="Worker's worktree")

    # Build loop state
    status: TaskOrchestratorStatus = Field(default="initializing")
    current_phase: str = Field(default="implement")  # implement, review, rework
    rework_count: int = Field(default=0)
    max_rework_attempts: int = Field(default=3)

    # Quality gates
    gate_config: list[str] = Field(default_factory=lambda: ["lint", "test", "ai_review"])
    gate_results: list[dict] = Field(default_factory=list)

    # Human review
    pending_approval_id: str | None = Field(default=None)
```

### Updated AgentRecord

```python
class AgentRecord(Entity):
    # ... existing fields ...

    # Association (optional)
    task_orchestrator_id: str | None = Field(
        default=None,
        description="TaskOrchestrator managing this worker, if any"
    )

    # Promotion tracking
    promoted_from: str | None = Field(
        default=None,
        description="Original worker ID if this was promoted"
    )
    standalone: bool = Field(
        default=True,
        description="True if created directly, False if spawned by orchestrator"
    )
```

---

## Implementation Phases

### Phase 1: Worker Foundation (Current)

- [x] WorktreeManager activation
- [x] Approval system
- [x] Basic agent spawning
- [ ] Direct worker creation API
- [ ] Worker thread view in UI

### Phase 2: TaskOrchestrator

- [ ] TaskOrchestratorRecord model
- [ ] Build loop state machine (implement → review → rework)
- [ ] Quality gate runner
- [ ] Manual TaskOrchestrator creation API
- [ ] Worker adoption/promotion API
- [ ] TaskOrchestrator view in UI

### Phase 3: MetaOrchestrator

- [ ] MetaOrchestratorRecord model (1 per project)
- [ ] Request decomposition engine
- [ ] TaskOrchestrator spawning
- [ ] Cross-task coordination
- [ ] Merge orchestration
- [ ] Project-level pattern learning

### Phase 4: Integration & Polish

- [ ] Seamless tier transitions in UI
- [ ] Real-time status across all tiers
- [ ] Resource management
- [ ] Enterprise governance

---

## Open Questions

1. **MetaOrchestrator Activation**: Auto-create on first use, or explicit "enable orchestration"?
2. **Worker Promotion UX**: What happens to existing conversation when promoting?
3. **Cross-Task Dependencies**: How does MetaOrchestrator handle blocked tasks?
4. **Standalone Worker Merge**: Can a standalone worker's changes be merged without
   TaskOrchestrator?
5. **Cost Attribution**: How to track costs across all tiers?

---

## Next Steps

1. Add `task_orchestrator_id` field to AgentRecord
2. Create TaskOrchestratorRecord model
3. Implement build loop state machine
4. Add quality gate runner
5. Create manual creation APIs
6. Build promotion flow (worker → task)
7. Add MetaOrchestratorRecord model
8. Implement project-level coordination
