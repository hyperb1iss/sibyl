---
title: Setting Up Prompts
description: Configure AGENTS.md and CLAUDE.md for effective agent collaboration
---

# Setting Up Prompts

Your agent's instruction file is the most important configuration for collaboration. It is the first
thing the agent reads each session. Use it to establish workflows, project context, and the Sibyl
integration.

Most AI agents read a Markdown instruction file. `AGENTS.md` is the cross-tool convention that
Codex, opencode, and others follow; Claude Code reads `CLAUDE.md`. The structure below applies to
either: keep one file, or keep both with the same content.

## The Two-Level System

Agents read instructions at two levels:

| Level                    | Location                                    | Scope             |
| ------------------------ | ------------------------------------------- | ----------------- |
| **Global instructions**  | `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | All projects      |
| **Project instructions** | `AGENTS.md` or `CLAUDE.md` (repo root)      | This project only |

Both are read at session start. Project-level instructions can override or extend global ones.

## Global CLAUDE.md

Your global instructions apply to every project. This is where you establish:

- Your working style with the agent
- Core tools and workflows
- The Sibyl integration

### Essential Global Setup

Below is the recommended block to paste into your global CLAUDE.md or AGENTS.md. It is also the
exact content `sibyl local setup` prints to the terminal and that `sibyl doctor --append` can
install for you automatically. Each section earns its place — the intent-to-verb bridges in
particular moved agent trigger accuracy from 17% to 88% in the skill-invocation eval, so keep them
intact even if you customize the framing.

```markdown
## Sibyl - Your Persistent Memory

Sibyl is your durable memory across sessions. It is a knowledge graph of decisions, patterns, tasks,
and learnings. Reach it through the `sibyl` CLI or the Sibyl MCP tools, whichever your setup has.

### Session start (MANDATORY)

If your client supports skills, invoke the `sibyl` skill immediately at session start. The skill
points at the version-matched CLI guidance and the current task queue. Without skill support, run
`sibyl context` to confirm the project link and `sibyl task list --status doing` to see active work
before anything else.

### The memory loop: recall, act, remember, reflect

1. **Recall** working context before you act. A past session may have solved this. CLI:
   `sibyl recall "<goal>" --intent build`. MCP: the `search` and `context` tools.
2. **Act** with that context in hand. Use IDs from recall with `sibyl show <id>` when a preview is
   not enough.
3. **Remember** durable knowledge as you learn it: decisions, gotchas, patterns. The next session
   should not have to rediscover it. CLI: `sibyl remember "Title" "What matters" --kind decision`.
   MCP: the `remember` tool.
4. **Reflect** at clean breakpoints to distill session notes into reviewable memory. CLI:
   `sibyl reflect "<notes>" --persist`. MCP: the `reflect` tool.

### Intent -> verb bridges

Recognize these prompt shapes and reach for the verb, not the file system:

- "what am I working on" / "current tasks" -> `sibyl task list --status doing,blocked`
- "where did I leave off" / "pick up from yesterday" -> `sibyl recall "<goal>"`
- "have we hit this before" / "what's our pattern for X" -> `sibyl search "<topic>"` first; only
  `sibyl show <id>` after you have an ID from search or recall
- "remember this" / "write this up" / "save this insight" / "we just learned X" -> `sibyl remember`
- "consolidate this session" / "wrap up" / "save this session for next time" ->
  `sibyl reflect "<notes>" --persist`
- "show me the full content" -> `sibyl show <id>`
- "what's the deal with X" / "X was mentioned" / "tell me about Y" -> `sibyl search "<X>"` before
  answering
- "complete this task: <learning>" without an explicit task id -> `sibyl task list -q "<topic>"`
  once, then `sibyl task complete <id> --learnings "..."`

If the natural-language ask sounds like memory work, it is memory work. Don't default to `Write` for
"write up what we learned"; that's `sibyl remember`. Don't burn turns hunting for an entity by
listing or showing unrelated records; `sibyl search` and `sibyl recall` are how you discover IDs.

### What to capture

**Always:** non-obvious solutions, gotchas, config quirks, architectural decisions. **Skip:**
trivial facts, throwaway hacks, well-documented basics.

Make each memory findable and reusable later:

- Weak: "Fixed the auth bug."
- Strong: "JWT refresh fails silently when the Redis TTL expires. The token service does not handle
  WRONGTYPE. Fix: regenerate the token on that error."

If your client supports skills (Claude Code, Codex), run `/sibyl` for the full command reference.
Otherwise `sibyl --help` covers it. Run `sibyl doctor` at any time to verify the recommended agent
setup is in place.
```

### Adding Personal Style

Customize how the agent works with you:

```markdown
## Working Style

- Be concise. No fluff.
- When unsure, ask. Don't guess.
- Prefer editing existing code over creating new files.
- Always check sibyl before implementing. We may have solved this before.

## Notes

I'm ADHD. I will interrupt with random ideas. If I do this while you're mid-task:

1. Quickly note it in a TODO or Sibyl task
2. Let me know you captured it
3. Finish the current work
```

## Project CLAUDE.md

Project-level instructions are checked into your repository. They provide:

- Project-specific context
- Tech stack details
- Key patterns and gotchas
- Team guidance

### Template Structure

```markdown
# Project Name

Brief description of what this project does.

## Project Overview

**Stack:** Python 3.13, FastAPI, SurrealDB, Redis/Valkey **Architecture:** Monorepo with apps/api,
apps/web, packages/core

## Sibyl Integration

This project uses Sibyl as its knowledge repository.

### ALWAYS Use Skills

**Use `/sibyl`** for ALL Sibyl operations. This skill knows the correct patterns.

### The Memory Loop

Every significant task follows this cycle:

**1. RECALL** (before coding)

\`\`\` /sibyl recall "topic" \`\`\`

**2. ACT** (while coding)

\`\`\` /sibyl task start <id> \`\`\`

**3. REMEMBER + REFLECT** (after completing)

\`\`\` /sibyl task complete <id> --learnings "What I learned" /sibyl remember "Pattern Title" "What,
why, how, caveats" \`\`\`

## Quick Reference

### Development Commands

\`\`\`bash pnpm dev # Start dev server pnpm test # Run tests pnpm lint # Lint code \`\`\`

### Key Files

| File               | Purpose                  |
| ------------------ | ------------------------ |
| `src/api/routes/`  | API endpoint handlers    |
| `src/core/models/` | Database models          |
| `src/lib/auth.ts`  | Authentication utilities |

## Common Gotchas

- **Port 3334** is used by Sibyl, not 3000
- **Environment:** Copy `.env.example` to `.env.local`
- **Database:** Run `pnpm db:migrate` after pulling

## Patterns

### Error Handling

Always use the `Result` type from `src/lib/result.ts`:

\`\`\`typescript const result = await doThing(); if (result.isErr()) { return
handleError(result.error); } \`\`\`

### API Responses

Use consistent response format from `src/lib/responses.ts`.
```

## Real-World Examples

### A Real Project CLAUDE.md

Here's how a project that uses Sibyl as its own knowledge repository sets up its `CLAUDE.md`:

```markdown
# Sibyl Development Guide

## Project Overview

**Sibyl** is a SurrealDB-native memory and task workflow server that gives assistants durable
project memory through a unified graph, content, and auth runtime.

## Sibyl Integration

**This project uses Sibyl as its own knowledge repository.**

### ALWAYS Use Skills

**Use `/sibyl`** for ALL Sibyl operations. This skill knows the correct patterns and handles
authentication properly.

### The Memory Loop

Every significant task follows this cycle:

**1. RECALL** (before coding)

\`\`\` /sibyl recall "topic" \`\`\`

**2. ACT** (while coding)

\`\`\` /sibyl task start <id> \`\`\`

**3. REMEMBER + REFLECT** (after completing)

\`\`\` /sibyl task complete <id> --learnings "What I learned" /sibyl remember "Pattern Title" "What,
why, how, caveats" \`\`\`

## Quick Reference

### Monorepo Structure

\`\`\` sibyl/ ├── apps/ │ ├── api/ # sibyld - Server daemon │ ├── cli/ # sibyl - Client CLI │ └──
web/ # Next.js frontend ├── packages/python/ │ └── sibyl-core/ # Shared library └── skills/ # Claude
Code skills \`\`\`

### Development Commands

\`\`\`bash moon run dev # Start the recommended local runtime moon run :lint # Lint current project
moon run :test # Test current project moon run :check # All quality checks \`\`\`

## Key Patterns

### Multi-Tenancy

Every graph operation requires org context:

\`\`\`python manager = EntityManager(client, group_id=str(org.id)) \`\`\`

### SurrealDB Write Concurrency

The SurrealDB driver serializes WebSocket queries per client. Use scoped clients per org and avoid
adding extra application-level locks around graph writes.

## Common Gotchas

- **Port 8000** for local SurrealDB
- **Graph reset** for disposable local data uses `REMOVE NAMESPACE org_<uuid_hex>`
- **Query native records:** use the `entity` and `relates_to` tables for active graph data
```

### Knowledge Repository Pattern

If you maintain a knowledge repository (patterns across projects):

```markdown
## Guidance

This project follows guidance from `~/dev/knowledge`.

### Key Guidance

| Tool    | Choice   | Why                       |
| ------- | -------- | ------------------------- |
| Linter  | Biome    | Fast, strict, zero config |
| Package | pnpm     | Strict, disk-efficient    |
| Commits | git-iris | AI-powered, contextual    |

### References

- [Tooling Guide](~/dev/knowledge/docs/TOOLING.md)
- [Architecture Patterns](~/dev/knowledge/docs/wisdom/architecture.md)
- [Hard-Won Wisdom](~/dev/knowledge/docs/WISDOM.md)
```

## Prompt Design Patterns

### Be Explicit About Workflow

```markdown
### Before Implementing ANYTHING

1. Search sibyl for existing patterns
2. Check if there's an active task
3. If no task, create one first
```

### Call Out Common Mistakes

```markdown
## Don't

- Use `sibyl task add` (wrong command, use `sibyl task create`)
- Commit without --no-verify
- Start implementing without searching first
```

### Include Troubleshooting

```markdown
## Troubleshooting

### Can't Connect to Sibyl

1. Check server: `sibyl health`
2. Verify port 3334 is available
3. Check `SIBYL_API_URL` environment variable

### No Search Results

1. Verify you're in the right project context
2. Try broader terms
3. Check entity types exist with `sibyl entity list`
```

### Reference Related Documentation

```markdown
## References

- [README.md](README.md) - Project setup
- [apps/api/README.md](apps/api/README.md) - API documentation
- [Skills Guide](docs/guide/skills.md) - Skill development
```

## Tips for Effective Prompts

### 1. Start with Context

Tell the agent what it's working on:

```markdown
## Project Overview

**Sibyl** is a knowledge graph for durable project memory. We use SurrealDB for graph, content,
auth, and memory data, and OpenAI for embeddings.
```

### 2. Be Specific About Commands

Show exact syntax, not vague descriptions:

```markdown
# Good

\`\`\`bash sibyl task list --status todo,doing --project proj_auth \`\`\`

# Less good

"Use the task list command with appropriate filters"
```

### 3. Explain the "Why"

Help the agent understand intent:

```markdown
### Multi-Tenancy

Every query MUST include org scope. Forgetting this queries the wrong graph or breaks isolation.

\`\`\`python

# WRONG - will query global graph

manager = EntityManager(client)

# RIGHT - scoped to organization

manager = EntityManager(client, group_id=str(org.id)) \`\`\`
```

### 4. Keep It Updated

Your CLAUDE.md should evolve with the project. When you discover a new gotcha:

1. Add it to CLAUDE.md
2. Also capture it in Sibyl for searchability

## Installation

### Global Setup

```bash
mkdir -p ~/.claude
# Create or edit ~/.claude/CLAUDE.md with your global instructions
```

### Project Setup

```bash
# In your project root
touch CLAUDE.md
# Edit with project-specific instructions
```

### Verify Installation

Run `sibyl doctor` from any directory. It reports both the daemon side (config, server health, auth,
write probe) and the agent side (skill stub installed and canonical, SessionStart hook registered,
no orphan UserPromptSubmit hook, CLAUDE.md/AGENTS.md contains the recommended memory-loop bridges).
Each failing check ships with a one-line remediation.

If the `agent-prompt` check is not green, doctor prints the full recommended snippet so you can copy
it into your file. If you'd rather have it written for you, point doctor at a target path and it
inserts the snippet between managed markers that subsequent runs replace in place:

```bash
sibyl doctor --append ~/.claude/CLAUDE.md
```

The markers are `<!-- sibyl:agent-setup -->` and `<!-- /sibyl:agent-setup -->`. Everything else in
the file is untouched. You can edit around the markers freely; the next `--append` run only rewrites
what is between them.

Once doctor is fully green, start a fresh agent session. The agent should:

1. Read your instruction file automatically.
2. Understand the project context and the memory loop.
3. Reach for `sibyl recall`, `sibyl remember`, and the other verbs on the prompts you'd expect.

## Next Steps

- [Working with Agents](./working-with-agents.md) - The human guide
- [Skills & Hooks](./skills.md) - Automatic context injection
- [Capturing Knowledge](./capturing-knowledge.md) - What to save
