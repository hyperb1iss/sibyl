---
title: Capturing Knowledge
description: How to effectively capture learnings in Sibyl
---

# Capturing Knowledge

The value of Sibyl grows with every piece of knowledge you capture. This guide explains what to
capture, how to capture it, and patterns for high-quality knowledge entries. Capture is the
**remember** step of the [memory loop](./memory-loop.md).

## The Philosophy

### Context Before Implementing

Before you write code, pull working context:

```bash
sibyl context "what you're building"
sibyl context "debug the error you hit" --intent debug
```

### Capture What You Learn

If it took time to figure out, save it. There are three ways, in rising order of ceremony:

```bash
# Quickest: derive a title from the content
sibyl capture "Surreal embedded mode is single-writer, fine for local dev"

# Typed durable memory with an explicit kind
sibyl remember "Chose SurrealDB" "One engine replaces three backends" --kind decision

# Full control over type and metadata
sibyl remember "Descriptive title" "What, why, how, caveats" --kind procedure
```

`capture` and `remember` are the memory-loop verbs. `add` is the lower-level command with the widest
set of flags.

Knowledge does not always arrive one entry at a time. Transcript ingestion
(`sibyl ingest claude-code`/`codex`) replays past agent sessions into memory, and document
collections (`sibyl docs`) import reference material. Both are additional ways to feed the graph.

### Complete With Learnings

Every task completion is an opportunity:

```bash
sibyl task complete task_xyz --learnings "Key insight: ..."
```

## What to Capture

### Always Capture

- **Non-obvious solutions** - If it took debugging, save the solution
- **Configuration gotchas** - Unexpected settings or requirements
- **Error patterns** - Recurring errors and their fixes
- **Architectural decisions** - Why you chose approach X over Y
- **Integration approaches** - How to connect system A to system B

### Consider Capturing

- **Useful patterns** - Code structures that work well
- **Performance findings** - Optimization discoveries
- **Tool tips** - Useful CLI flags or library features
- **Workarounds** - Temporary fixes with context

### Skip Capturing

- **Trivial information** - Well-documented basics
- **Temporary hacks** - Unless they might recur
- **Personal notes** - Use task notes instead
- **Standard practices** - Unless team-specific

## The `add` Command

### Basic Usage

```bash
sibyl remember "Title" "Content"
```

The default type is `episode` - a temporal learning.

### With Entity Type

```bash
# Create a pattern
sibyl remember "Error boundary procedure" "React error boundary for..." --kind procedure

# Create a rule
sibyl remember "Never commit .env" "Environment files must be gitignored" --kind rule
```

### With Metadata

```bash
sibyl remember "Redis pooling insight" \
  "Connection pool size must be >= concurrent requests" \
  --domain database \
  --tags python,redis
```

## Quality Guidelines

### Good Knowledge Entry

A good entry answers:

1. **What** - The specific issue or discovery
2. **Why** - Root cause or reasoning
3. **How** - The solution or approach
4. **When** - Context where it applies
5. **Caveats** - Edge cases or limitations

### Examples

**Bad:**

```bash
sibyl remember "Fixed the bug" "It works now"
```

**Good:**

```bash
sibyl remember "JWT refresh fails on Redis TTL expiry" \
  "Root cause: Token service doesn't handle WRONGTYPE error when Redis key
expires during refresh. The expired token returns a different data type.

Fix: Add try/except around token retrieval with regeneration fallback:
\`\`\`python
try:
    token = await redis.get(key)
except RedisError as e:
    if 'WRONGTYPE' in str(e):
        return await regenerate_token(user_id)
    raise
\`\`\`

Affects: Authentication service, user session management
Technologies: Redis, JWT, Python"
```

### Template

Use this structure for consistent entries:

```
**Problem/Discovery:**
[What you encountered]

**Root Cause:**
[Why it happened]

**Solution:**
[How to fix/implement]

**Context:**
[When this applies]

**Caveats:**
[Edge cases, limitations]
```

## Task Learnings

### Capturing on Completion

```bash
sibyl task complete task_xyz --learnings "OAuth redirect URIs must match
exactly - including trailing slashes. Google OAuth rejects mismatches
silently, returning a generic error instead of indicating the redirect
URI problem. Always test with the exact production URL."
```

### What to Include in Task Learnings

- **Unexpected challenges** - What was harder than expected?
- **Key insights** - The "aha" moment
- **Future recommendations** - What would you do differently?
- **Related resources** - Docs, articles that helped

### Learning vs Episode

| Task Learning         | Episode           |
| --------------------- | ----------------- |
| Specific to task work | General discovery |
| Linked to task entity | Standalone entity |
| Part of completion    | Created anytime   |
| Brief summary         | Can be detailed   |

## Auto-Linking

When you add knowledge, Sibyl automatically discovers and links related entities:

```python
# Internally, new entities are linked to:
# - Patterns with similar content
# - Rules that apply
# - Topics mentioned
# - Technologies referenced
```

You can also explicitly link:

```bash
# Via MCP
add(
    title="OAuth session management",
    content="...",
    related_to=["pattern_session", "topic_auth"]
)
```

## Entity Type Selection

| Scenario                        | Type            | Example                          |
| ------------------------------- | --------------- | -------------------------------- |
| Figured out why something broke | `episode`       | "Redis WRONGTYPE on TTL expiry"  |
| Found a reusable approach       | `pattern`       | "Retry with exponential backoff" |
| Discovered a constraint         | `rule`          | "Never store PII in logs"        |
| Common error with solution      | `error_pattern` | "CORS preflight fails on POST"   |
| External docs to reference      | `source`        | "Next.js docs"                   |

## Organizational Patterns

### Category Usage

Use consistent categories across your org:

```bash
# Domain categories
--domain authentication
--domain database
--domain api
--domain frontend
--domain devops
--domain testing

# Type categories
--domain debugging
--domain performance
--domain security
--domain integration
```

### Tag Patterns

Tags enable cross-cutting discovery:

```bash
--tags python,redis,performance
--tags security,authentication,oauth
--tags bug,production,hotfix
```

## Search-Friendly Content

Write content that will be found later:

### Include Synonyms

```bash
sibyl remember "Connection pool exhaustion" \
  "Also known as: pool starvation, connection leak.
Pool exhaustion occurs when all connections are in use..."
```

### Include Error Messages

```bash
sibyl remember "Redis WRONGTYPE error on key reuse" \
  "Error: WRONGTYPE Operation against a key holding the wrong kind of value

This occurs when a key is read as a type it was not written as..."
```

### Include Technology Names

```bash
sibyl remember "Async task cancellation" \
  "Python asyncio task cancellation pattern.

When cancelling asyncio tasks, you must handle CancelledError..."
```

## Workflow Integration

### Research Phase

```bash
# Before implementing
sibyl context "what you're building"
sibyl context "common issues with X"
```

### Implementation Phase

```bash
# While working
sibyl task note task_xyz "Found issue with OAuth scopes"
```

### Completion Phase

```bash
# After completing
sibyl task complete task_xyz --learnings "Detailed insight..."

# If discovery was significant enough for standalone entry
sibyl remember "OAuth scope discovery" "Detailed content..."
```

## MCP Knowledge Capture

### Using `add` Tool

```python
# Quick episode
add(
    title="Redis timeout solution",
    content="Increase timeout to 30s for large operations...",
    category="database",
    languages=["python"]
)

# Structured pattern
add(
    title="Retry pattern with backoff",
    content="Implementation of exponential backoff...",
    entity_type="pattern",
    category="resilience",
    languages=["python", "typescript"]
)
```

### Using `remember` and `reflect`

For the memory-loop tools:

```python
# Capture typed durable memory
remember(
    title="Chose SurrealDB for the runtime",
    content="One engine replaces three backends...",
    kind="decision"
)

# Distill raw session notes into reviewable candidates
reflect(content="Long session notes...", persist=True, review=True)
```

### From Task Completion

```python
manage(
    action="complete_task",
    entity_id="task_xyz",
    data={
        "learnings": "Key insight about OAuth implementation..."
    }
)
```

## Team Knowledge Building

### Shared Patterns

Create patterns that the whole team can use:

```bash
sibyl remember "Team API response format" \
  "All APIs must return: { data, error, meta }

data: The response payload or null
error: Error object with code/message or null
meta: Pagination, version info, etc.

Example:
\`\`\`json
{
  \"data\": { \"user\": {...} },
  \"error\": null,
  \"meta\": { \"version\": \"1.0\" }
}
\`\`\`" \
  --kind procedure \
  --domain api
```

### Shared Rules

Document team constraints:

```bash
sibyl remember "No direct database access from handlers" \
  "Route handlers must not directly query the database.
Use service layer methods instead.

WRONG: await db.query('SELECT * FROM users')
RIGHT: await user_service.get_users()

Reason: Keeps business logic testable and reusable." \
  --kind rule \
  --domain architecture
```

## Measuring Knowledge Quality

### Good Indicators

- Entries are found when searching
- Team members reference captured knowledge
- Debugging time decreases over time
- Onboarding uses knowledge graph

### Review Checklist

- [ ] Title is descriptive and searchable
- [ ] Content explains why, not just what
- [ ] Appropriate entity type selected
- [ ] Category and languages tagged
- [ ] Related entities linked

## Next Steps

- [The Memory Loop](./memory-loop.md) - Capture in the full cycle
- [Entity Types](./entity-types.md) - Choose the right type
- [Semantic Search](./semantic-search.md) - Find your knowledge
- [Task Management](./task-management.md) - Capture in workflow
