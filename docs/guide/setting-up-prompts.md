---
title: Setting Up Prompts
description: Configure CLAUDE.md for effective agent collaboration
---

# Setting Up Prompts

Your `CLAUDE.md` file is the most important configuration for agent collaboration. It's the first
thing your AI agent reads. Use it to establish workflows, project context, and the Sibyl
integration.

## The Two-Level System

Claude Code uses two levels of instructions:

| Level                    | Location                  | Scope             |
| ------------------------ | ------------------------- | ----------------- |
| **Global instructions**  | `~/.claude/CLAUDE.md`     | All projects      |
| **Project instructions** | `./CLAUDE.md` (repo root) | This project only |

Both are read at session start. Project-level instructions can override or extend global ones.

## Global CLAUDE.md

Your global instructions apply to every project. This is where you establish:

- Your working style with the agent
- Core tools and workflows
- The Sibyl integration

### Essential Global Setup

```markdown
## Sibyl - Your Persistent Memory

Sibyl is your knowledge graph, extended memory that persists across sessions.

### Session Start (MANDATORY)

**Run `/sibyl` at the start of every session.** The skill provides full CLI guidance, task context,
and relevant patterns. No exceptions.

### The Memory Loop

1. **Recall first**: Pull working context before you act, past sessions may have solved this
2. **Act**: Do the work in a task so progress survives the session
3. **Remember**: When you solve something non-obvious, capture it as durable memory
4. **Reflect**: Distill session notes into reviewable memory candidates

### What to Capture

**Always:** Non-obvious solutions, gotchas, configuration quirks, architectural decisions
**Consider:** Useful patterns, performance findings, integration approaches **Skip:** Trivial info,
temporary hacks, well-documented basics

### Quality Bar

**Bad:** "Fixed the auth bug" **Good:** "JWT refresh tokens fail silently when Redis TTL expires.
Root cause: token service doesn't handle WRONGTYPE error. Fix: Add try/except with token
regeneration fallback."
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

Here's how a project that uses Sibyl as its own knowledge repository sets up its
`CLAUDE.md`:

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
- **Always query both labels:** `(n:Episodic OR n:Entity)`
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

Start a new Claude Code session. The agent should:

1. Read your CLAUDE.md automatically
2. Understand the project context
3. Be ready to use the Sibyl workflow

## Next Steps

- [Working with Agents](./working-with-agents.md) - The human guide
- [Skills & Hooks](./skills.md) - Automatic context injection
- [Capturing Knowledge](./capturing-knowledge.md) - What to save
