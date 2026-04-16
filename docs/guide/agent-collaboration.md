---
title: Agent Collaboration
description: Multi-agent patterns with shared knowledge
---

# Agent Collaboration

Sibyl enables multiple AI agents to share knowledge and coordinate work. This guide covers patterns
for multi-agent collaboration.

## Shared Knowledge Graph

### How It Works

All agents in an organization share the same knowledge graph:

```
Organization Graph
├── Agent A's discoveries
├── Agent B's discoveries
├── Agent C's discoveries
└── Shared patterns, rules, tasks
```

When Agent A captures knowledge:

```python
add("OAuth redirect insight", "URIs must match exactly...")
```

Agent B can immediately find it:

```python
search("OAuth redirect")  # Returns Agent A's insight
```

## Collaboration Patterns

### Knowledge Handoff

When one agent completes work, another can continue:

**Agent A (Morning Session):**

```python
# Complete task with detailed learnings
manage("complete_task", entity_id="task_oauth",
       data={
           "learnings": """
           OAuth implementation notes:
           1. Redirect URIs must match exactly
           2. State parameter must be cryptographic
           3. Token refresh should be proactive
           """
       })
```

**Agent B (Afternoon Session):**

```python
# Search for context from previous work
search("OAuth implementation", types=["task"])
# Returns completed task with learnings

# Continue related work
search("OAuth", types=["episode"])
# Returns any additional insights captured
```

### Task Coordination

Multiple agents can work on related tasks:

**Agent A:**

```python
# Claim a task
manage("start_task", entity_id="task_frontend_auth")

# Add progress note
manage("update_task", entity_id="task_frontend_auth",
       data={"notes": "Working on login form"})
```

**Agent B:**

```python
# Find in-progress work
explore(mode="list", types=["task"], status="doing")
# Shows: task_frontend_auth (Agent A is working on it)

# Work on complementary task
manage("start_task", entity_id="task_backend_auth")
```

### Blocking and Dependencies

Track cross-agent blockers:

**Agent A:**

```python
# Block on dependency
manage("block_task", entity_id="task_frontend",
       data={"reason": "Waiting on task_backend API endpoints"})
```

**Agent B:**

```python
# Find blocked tasks
explore(mode="list", types=["task"], status="blocked")
# Shows: task_frontend blocked by task_backend

# Complete blocker
manage("complete_task", entity_id="task_backend",
       data={"learnings": "API endpoints ready at /api/v1/auth/*"})

# Notify (via captured knowledge)
add("Backend auth API ready",
    "Endpoints available: /api/v1/auth/login, /api/v1/auth/refresh",
    related_to=["task_frontend"])
```

## Real-Time Coordination

### Check Before Acting

Always check current state before starting work:

```python
# 1. Check what's in progress
explore(mode="list", types=["task"], status="doing", project="proj_abc")

# 2. Check for blocked dependencies
explore(mode="dependencies", entity_id="task_xyz")

# 3. Then start your task
manage("start_task", entity_id="task_abc")
```

### Leave Breadcrumbs

Help future agents (including yourself) by documenting state:

```python
# Before ending session
add("Session summary - Auth implementation",
    """
    Completed:
    - OAuth callback handler implemented
    - Token refresh logic added

    In progress:
    - User profile endpoint (task_profile)

    Blocked:
    - Email verification (waiting on SMTP config)

    Next steps:
    - Test OAuth flow end-to-end
    - Add rate limiting to auth endpoints
    """,
    category="session-summary")
```

## Conflict Resolution

### Concurrent Edits

Sibyl uses distributed locks to prevent conflicts:

```python
# If another agent is editing, you'll get a 409 error
# Retry after a short delay
```

### Knowledge Conflicts

When agents capture conflicting knowledge:

1. Both entries are preserved
2. Newer entries have more recent timestamps
3. Use temporal boosting to prefer recent

**Resolution Pattern:**

```python
# Search returns both entries
search("conflicting topic")

# Check timestamps and merge manually if needed
add("Merged insight",
    "Combined understanding from multiple sources...")
```

## Agent Specialization

### Pattern: Specialized Agents

Different agents can focus on different domains:

**Backend Agent:**

```python
# Focus on backend patterns
search("API design", types=["pattern"])
add("REST pagination pattern", "...", category="backend")
```

**Frontend Agent:**

```python
# Focus on frontend patterns
search("React hooks", types=["pattern"])
add("Custom hook pattern", "...", category="frontend")
```

**DevOps Agent:**

```python
# Focus on infrastructure
search("deployment", types=["pattern"])
add("Blue-green deployment", "...", category="devops")
```

### Cross-Domain Queries

Agents can still access all knowledge:

```python
# Frontend agent needs backend context
search("API response format", types=["pattern"])
# Returns backend agent's patterns
```

## Task Distribution

### Sprint Planning Pattern

**Project Manager Agent:**

```python
# Review all tasks
explore(mode="list", types=["task"], project="proj_abc")

# Prioritize
manage("update_task", entity_id="task_1", data={"priority": "critical"})
manage("update_task", entity_id="task_2", data={"priority": "high"})

# Create epic for sprint
add("Sprint 5 - Auth Feature",
    "Tasks: task_1, task_2, task_3",
    entity_type="epic",
    project="proj_abc")
```

**Worker Agents:**

```python
# Find highest priority work
explore(mode="list", types=["task"],
        project="proj_abc", status="todo", priority="critical,high")

# Claim and start
manage("start_task", entity_id="task_1")
```

### Load Balancing Pattern

```python
# Check distribution
explore(mode="list", types=["task"], status="doing")

# Find unassigned high-priority work
explore(mode="list", types=["task"],
        status="todo", priority="high")

# Self-assign
manage("start_task", entity_id="available_task")
```

## Knowledge Curation

### Review Pattern

**Curator Agent:**

```python
# Find recent additions
explore(mode="list", types=["episode"], since="7d")

# Promote valuable episodes to patterns
add("Promoted pattern",
    "Content from episode...",
    entity_type="pattern",
    related_to=["original_episode_id"])

# Add rules from learnings
add("New team rule",
    "Based on discovered issues...",
    entity_type="rule")
```

### Cleanup Pattern

```python
# Find outdated entries
search("deprecated", since="1y")

# Archive or update
manage("archive_task", entity_id="outdated_pattern",
       data={"reason": "Superseded by new approach"})
```

## Multi-Tenant Collaboration

### Organization Boundaries

Each organization has isolated graphs:

```
Organization A (Isolated)
├── Team A knowledge
└── Team A tasks

Organization B (Isolated)
├── Team B knowledge
└── Team B tasks
```

### Cross-Org Patterns

For shared knowledge across organizations:

1. Create shared patterns as code (version controlled)
2. Import into each org's graph
3. Keep org-specific knowledge separate

## Best Practices

### 1. Communicate via Graph

Leave notes and summaries that help other agents:

```python
add("Auth implementation status",
    "Current state: OAuth working, JWT pending...")
```

### 2. Check Before Acting

Always search for existing work:

```python
search("what you're about to do")
explore(mode="list", types=["task"], status="doing")
```

### 3. Complete with Context

Detailed learnings help future agents:

```python
manage("complete_task", entity_id="task_xyz",
       data={"learnings": "Detailed, actionable insights..."})
```

### 4. Use Dependencies

Model task relationships explicitly:

```python
add("Task B",
    "Requires Task A to be complete",
    entity_type="task",
    depends_on=["task_a"])
```

### 5. Review and Curate

Periodically clean up and promote knowledge:

```python
# Promote episodes to patterns
# Archive outdated entries
# Merge duplicate knowledge
```

## Example: Day in Multi-Agent Life

**Morning - Agent A (Developer):**

```python
# Start session
explore(mode="list", types=["task"], project="proj_auth", status="todo")
manage("start_task", entity_id="task_oauth")

# Work and capture
add("OAuth state parameter insight", "...")

# End session
manage("update_task", entity_id="task_oauth",
       data={"status": "review", "notes": "Ready for review"})
```

**Afternoon - Agent B (Reviewer):**

```python
# Find work to review
explore(mode="list", types=["task"], status="review")

# Get context
search("task_oauth", types=["episode"])

# Complete review
manage("complete_task", entity_id="task_oauth",
       data={"learnings": "OAuth implementation approved.
             Note: Add refresh token rotation in follow-up."})
```

**Evening - Agent C (Planner):**

```python
# Review completed work
explore(mode="list", types=["task"], status="done", since="1d")

# Plan follow-ups
add("Add refresh token rotation",
    "Follow-up from OAuth review...",
    entity_type="task",
    project="proj_auth",
    related_to=["task_oauth"])
```

## Next Steps

- [Claude Code Integration](./claude-code.md) - Single agent setup
- [Task Management](./task-management.md) - Task coordination
- [Capturing Knowledge](./capturing-knowledge.md) - What to capture
