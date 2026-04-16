---
title: Setting Up Prompts
description: Configure CLAUDE.md for effective agent collaboration
---

# Setting Up Prompts

Your `CLAUDE.md` file is the most important configuration for agent collaboration. It's the first
thing your AI agent reads. Use it to establish workflows, project context, and the Sibyl integration.

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

### Workflow

1. **Research first**: Search for patterns, past learnings, known issues before implementing
2. **Track tasks**: Never do significant work without a task. Update status as you go
3. **Capture learnings**: When you solve something non-obvious, add it to the graph

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
- Team conventions

### Template Structure

```markdown
# Project Name

Brief description of what this project does.

## Project Overview

**Stack:** Python 3.12, FastAPI, PostgreSQL, Redis **Architecture:** Monorepo with apps/api,
apps/web, packages/core

## Sibyl Integration

This project uses Sibyl as its knowledge repository.

### ALWAYS Use Skills

**Use `/sibyl`** for ALL Sibyl operations. This skill knows the correct patterns.

### Research → Do → Reflect Cycle

Every significant task follows this cycle:

**1. RESEARCH** (before coding)

\`\`\` /sibyl search "topic" /sibyl explore patterns \`\`\`

**2. DO** (while coding)

\`\`\` /sibyl task start <id> \`\`\`

**3. REFLECT** (after completing)

\`\`\` /sibyl task complete <id> --learnings "What I learned" /sibyl add "Pattern Title" "What, why,
how, caveats" \`\`\`

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

### Sibyl's Own CLAUDE.md

Here's the actual CLAUDE.md from the Sibyl project:

```markdown
# Sibyl Development Guide

## Project Overview

**Sibyl** is a knowledge graph and task workflow server that gives assistants durable project memory
through a Graphiti-powered knowledge graph.

## Sibyl Integration

**This project uses Sibyl as its own knowledge repository.**

### ALWAYS Use Skills

**Use `/sibyl`** for ALL Sibyl operations. This skill knows the correct patterns and handles
authentication properly.

### Research → Do → Reflect Cycle

Every significant task follows this cycle:

**1. RESEARCH** (before coding)

\`\`\` /sibyl search "topic" /sibyl explore patterns \`\`\`

**2. DO** (while coding)

\`\`\` /sibyl task start <id> \`\`\`

**3. REFLECT** (after completing)

\`\`\` /sibyl task complete <id> --learnings "What I learned" /sibyl add "Pattern Title" "What, why,
how, caveats" \`\`\`

## Quick Reference

### Monorepo Structure

\`\`\` sibyl/ ├── apps/ │ ├── api/ # sibyld - Server daemon │ ├── cli/ # sibyl - Client CLI │ └──
web/ # Next.js frontend ├── packages/python/ │ └── sibyl-core/ # Shared library └── skills/ # Claude
Code skills \`\`\`

### Development Commands

\`\`\`bash moon run dev # Start everything moon run :lint # Lint current project moon run :test #
Test current project moon run :check # All quality checks \`\`\`

## Key Patterns

### Multi-Tenancy

Every graph operation requires org context:

\`\`\`python manager = EntityManager(client, group_id=str(org.id)) \`\`\`

### FalkorDB Write Concurrency

FalkorDB's connection pool handles write concurrency natively. No application-level locking
is needed.

## Common Gotchas

- **Port 6380** for FalkorDB (not 6379)
- **Graph corruption** can crash - nuke with `GRAPH.DELETE <org-uuid>`
- **Always query both labels:** `(n:Episodic OR n:Entity)`
```

### Conventions Repository Pattern

If you maintain a conventions repo (patterns across projects):

```markdown
## Conventions

This project follows conventions from `~/dev/conventions`.

### Key Conventions

| Tool    | Choice   | Why                       |
| ------- | -------- | ------------------------- |
| Linter  | Biome    | Fast, strict, zero config |
| Package | pnpm     | Strict, disk-efficient    |
| Commits | git-iris | AI-powered, contextual    |

### References

- [Tooling Guide](~/dev/conventions/docs/TOOLING.md)
- [Architecture Patterns](~/dev/conventions/docs/wisdom/architecture.md)
- [Hard-Won Wisdom](~/dev/conventions/docs/WISDOM.md)
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

**Sibyl** is a knowledge graph for durable project memory. We use FalkorDB for the graph database
and OpenAI for embeddings.
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
