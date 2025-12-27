# Sibyl Knowledge System (embed in agent system prompts)

## Sibyl - Your Persistent Memory

You have access to Sibyl, a knowledge graph that serves as your extended memory.

**Preferred: Use CLI via the `sibyl-knowledge` skill.** CLI outputs clean JSON optimized for LLM
parsing.

**Before acting:** Search for relevant patterns, past learnings, known issues **During work:** Track
tasks, update status, note blockers **After completing:** Capture what you learned for future agents

### Core CLI Commands

**Search for knowledge:**

```bash
uv run sibyl search "authentication patterns"
uv run sibyl search "error message" --type error_pattern
```

**Task management:**

```bash
uv run sibyl task list --status todo           # JSON output
uv run sibyl task list --project <id> --table  # Human-readable
uv run sibyl task start <id>
uv run sibyl task complete <id> --learnings "Key insight: ..."
uv run sibyl task update <id> --status done --priority high  # Direct update
```

**Capture knowledge:**

```bash
uv run sibyl add "Short title" "Detailed explanation of what you learned"
uv run sibyl add "Pattern name" "Description..." --type pattern
```

**Explore graph:**

```bash
uv run sibyl entity list --type pattern
uv run sibyl entity related <id>
```

### Mandatory Behaviors

1. **Task-Driven Work**: Never do significant work without a task. Create one if needed.

2. **Research First**: Before implementing:

   ```bash
   uv run sibyl search "relevant topic"
   uv run sibyl search "error or domain" --type error_pattern
   ```

3. **Capture Learnings**: When you solve something non-obvious:

   ```bash
   uv run sibyl add "Short searchable title" "Detailed: what, why, how, caveats" -c domain
   ```

4. **Track Progress**: Update task status as you work:
   ```bash
   uv run sibyl task start <id>
   uv run sibyl task complete <id> --learnings "..."
   ```

### What to Capture

**Always:** Non-obvious solutions, gotchas, configuration quirks, architectural decisions
**Consider:** Useful patterns, performance findings, integration approaches **Skip:** Trivial info,
temporary hacks, well-documented basics

### Quality Bar for Episodes

**Bad:** "Fixed the auth bug" **Good:** "JWT refresh tokens fail silently when Redis TTL expires.
Root cause: token service doesn't handle WRONGTYPE error. Fix: Add try/except with token
regeneration fallback."

---

Remember: The graph should be smarter after every session. Search often. Add generously. Track
everything.
