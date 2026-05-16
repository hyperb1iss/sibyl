---
title: Knowledge Repository
description: Maintaining team patterns and feeding them to your knowledge graph
---

# Knowledge Repository

A knowledge repository is a centralized collection of your team's patterns, tooling decisions, and
hard-won wisdom. When connected to Sibyl, this guidance becomes searchable context that your AI
agents can access across projects.

## Why a Knowledge Repo?

### The Problem

Every project has tribal knowledge:

- Why you chose Biome over ESLint
- The auth pattern that finally worked
- The deployment gotcha that wasted a week

This knowledge lives in:

- Slack messages (lost)
- Developer memories (fragile)
- Random READMEs (scattered)

### The Solution

Centralize guidance in one repository:

```
knowledge/
├── docs/
│   ├── WISDOM.md           # Hard-won lessons
│   ├── TOOLING.md          # Required tools
│   └── wisdom/
│       ├── architecture.md # Architecture patterns
│       ├── debugging.md    # Debugging approaches
│       └── testing.md      # Testing strategies
├── templates/
│   ├── python/             # Python project templates
│   ├── typescript/         # TypeScript templates
│   └── rust/               # Rust templates
└── AGENTS.md               # Quick reference for AI agents
```

Then crawl it into Sibyl. Now every agent can find your team's guidance.

## Setting Up Your Knowledge Repo

### 1. Create the Repository

```bash
mkdir -p ~/dev/knowledge
cd ~/dev/knowledge
git init
```

### 2. Structure Your Content

**Recommended structure:**

```
knowledge/
├── AGENTS.md               # AI-focused quick reference
├── README.md               # Human-focused overview
├── docs/
│   ├── WISDOM.md           # Index of lessons learned
│   ├── TOOLING.md          # Required tools per language
│   ├── LAYOUTS.md          # Directory structures
│   ├── COMMITS.md          # Commit guidance
│   └── wisdom/
│       ├── architecture.md # Architecture patterns
│       ├── debugging.md    # Debugging approaches
│       ├── testing.md      # Testing strategies
│       ├── errors.md       # Error handling
│       └── languages/
│           ├── python.md
│           ├── typescript.md
│           └── rust.md
├── templates/
│   ├── python/
│   │   └── pyproject.toml
│   ├── typescript/
│   │   ├── package.json
│   │   └── tsconfig.json
│   └── shared/
│       ├── .gitignore
│       └── .editorconfig
└── skills/                 # Claude Code skills
    ├── commit/SKILL.md
    ├── review/SKILL.md
    └── guide/SKILL.md
```

### 3. Write an AGENTS.md

This is the quick reference your AI agents will read first:

```markdown
# Agent Instructions

This repository contains team guidance. Reference it when setting up new projects or ensuring
consistency.

## How to Use This Repo

**Read → Think → Write**

1. Read the relevant template(s) from this repo
2. Understand the patterns and adapt for the target project
3. Write customized files directly to the target project

Never copy files verbatim. Templates contain placeholders.

## Where to Look

| Need            | Directory                   |
| --------------- | --------------------------- |
| Python config   | `templates/python/`         |
| TypeScript      | `templates/typescript/`     |
| CI/CD workflows | `templates/github-actions/` |
| Hard-won wisdom | `docs/wisdom/`              |

## Key Guidance

### Tooling - Non-Negotiable

| Language   | Package Manager | Linter | Formatter |
| ---------- | --------------- | ------ | --------- |
| Python     | **uv**          | Ruff   | Ruff      |
| TypeScript | **pnpm**        | Biome  | Biome     |
| Rust       | cargo           | Clippy | rustfmt   |

### Sacred Rules

- **Never** use ESLint (use Biome)
- **Never** use pip (use uv)
- **Never** use npm or yarn (use pnpm)
- **Always** use `--no-verify` on commits
- **Always** search sibyl before implementing
```

### 4. Write WISDOM.md

Capture hard-won lessons:

```markdown
# Hard-Won Wisdom

Lessons learned the hard way. Each one represents hours of debugging.

## Sacred Rules

**These are non-negotiable.**

### Database & Auth

- Better Auth user IDs are TEXT, never UUID
- RLS policies must use `auth.jwt() ->> 'sub'`
- All queries filter by organization_id

### Infrastructure

- Never restart ArgoCD without confirmation
- Never auto-apply in Kubernetes
- Never use `--force` without approval

## Meta-Lessons

### Verify, Don't Assume

Theory: "173 concurrent queries = thundering herd" Reality: "Other prod with more activity works
fine" Truth: Cold cache after restart (measured via EXPLAIN ANALYZE)

### Investigation vs Fix Mode

Ask explicitly: "Am I investigating or fixing?"

- **Investigation:** Read-only, thorough, no changes
- **Fixing:** Requires confirmed root cause
```

## Crawling Guidance into Sibyl

### Method 1: File Crawling

Sibyl can crawl local directories:

```bash
sibyl crawl add "file:///Users/bliss/dev/knowledge" --name "Guidance"
sibyl crawl ingest <source_id>
```

This creates `guide` entities in your knowledge graph.

### Method 2: Manual Import

For critical patterns, add them explicitly:

```bash
sibyl add "Biome over ESLint" \
  "Always use Biome for TypeScript/JavaScript linting.
   Reasons: faster, simpler config, better defaults.
   ESLint requires dozens of plugins for the same functionality." \
  --type pattern \
  --category tooling
```

### Method 3: Crawl on Change

Set up a hook to re-crawl when guidance changes:

```bash
# In knowledge repository
# .git/hooks/post-commit
#!/bin/bash
sibyl crawl add "file:///Users/bliss/dev/knowledge" --name "Guidance"
sibyl crawl ingest <source_id>
```

## Using Guidance in Projects

### Reference in CLAUDE.md

```markdown
## Guidance

This project follows guidance from `~/dev/knowledge`.

When starting a new feature:

1. Check `~/dev/knowledge/docs/wisdom/` for relevant patterns
2. Search sibyl for "guide" + your topic
3. Use templates from `~/dev/knowledge/templates/`
```

### Search for Guidance

```bash
# Find guidance about testing
sibyl search "testing guide" --type guide

# Find patterns from wisdom docs
sibyl search "architecture pattern" --type pattern
```

### Agent Usage

Your agent can query guidance directly:

```
You: "What's our guide for error handling?"

Agent: [Searches sibyl for "error handling guide"]
       Found: "Use Result types for fallible operations.
              Never throw exceptions in business logic.
              Validate at boundaries only."
```

## Example Guidance

### Architecture Patterns

```markdown
# Architecture Patterns

## State Management

**Isolation beats merging.** When multiple processes modify shared state, merging becomes fragile.

\`\`\` Bad: Clone parent context → Modify → Merge back (context explosion) Good: Unique IDs +
Database-backed state reconstruction (true isolation) \`\`\`

- Each worker/agent/task owns its identity completely
- Reconstruct state from immutable facts (events, database records)
- Three context strategies: `isolated` (minimal), `summary` (condensed), `full` (rare)

## Event-Driven Architecture

\`\`\` Event → Queue → Reducer → (NewState, SideEffects) ↓ Effect Executor \`\`\`

- State transitions are pure functions
- Side effects returned as data, not executed in reducer
```

### Language-Specific Patterns

```markdown
# Python Guidance

## Package Management

**Always use uv.** Never pip.

\`\`\`bash

# Create project

uv init my-project

# Add dependencies

uv add fastapi

# Dev dependencies

uv add --dev pytest ruff mypy \`\`\`

## Type Checking

**Strict mode, always:**

\`\`\`toml [tool.mypy] strict = true warn_return_any = true \`\`\`

## Linting

**Ruff with full rule set:**

\`\`\`toml [tool.ruff] line-length = 100 target-version = "py312"

[tool.ruff.lint] select = ["ALL"] ignore = ["D", "ANN101", "ANN102"] \`\`\`
```

### Debugging Patterns

```markdown
# Debugging Wisdom

## General Approach

1. **Reproduce consistently** before investigating
2. **Binary search** the problem space
3. **Verify assumptions** at each layer
4. **Measure, don't guess**

## Common Traps

### The Config File Trap

Wrong config file being read. Always verify:

\`\`\`bash

# Print config source

echo "Config loaded from: $CONFIG_PATH" \`\`\`

### The Cache Trap

Stale cache causing issues. Clear caches explicitly:

\`\`\`bash

# Node

rm -rf node_modules/.cache

# Python

find . -type d -name **pycache** -exec rm -rf {} + \`\`\`

### The Environment Trap

Wrong environment variables. Print them:

\`\`\`bash env | grep MYAPP\_ \`\`\`
```

## Syncing Guidance Across Projects

### Pattern: Shared Skills

Put Claude Code skills in your knowledge repository:

```
knowledge/
└── skills/
    ├── check/SKILL.md       # Run quality checks
    ├── commit/SKILL.md      # AI-powered commits
    ├── guide/SKILL.md       # Capture new guidance
    └── review/SKILL.md      # Code review workflow
```

Install to any project:

```bash
cp ~/dev/knowledge/skills/* ~/.claude/skills/
```

### Pattern: Template Inheritance

Projects can extend base templates:

```toml
# knowledge/templates/python/pyproject.toml
[project]
requires-python = ">=3.12"

[tool.ruff]
line-length = 100
```

```toml
# my-project/pyproject.toml
# Extends guidance, adds project-specific config
[project]
name = "my-project"
requires-python = ">=3.12"  # From guidance

[tool.ruff]
line-length = 100  # From guidance
select = ["ALL"]   # Project-specific
```

## Best Practices

### 1. Keep It Current

Update guidance when you learn something new:

```bash
# Add to wisdom docs
echo "## New Lesson\n\n$LESSON" >> docs/wisdom/debugging.md

# Add to sibyl
sibyl add "New debugging lesson" "$LESSON" --type guide

# Commit
git commit -am "Add debugging lesson about cache invalidation"
```

### 2. Categorize Well

Use consistent categories:

| Category       | What Goes Here             |
| -------------- | -------------------------- |
| `architecture` | System design patterns     |
| `debugging`    | Troubleshooting approaches |
| `tooling`      | Tool choices and config    |
| `languages/*`  | Language-specific patterns |
| `testing`      | Testing strategies         |

### 3. Include Context

Don't just say what. Explain why:

```markdown
# Bad

Use uv for Python.

# Good

Use uv for Python package management.

**Why:**

- 10-100x faster than pip
- Better dependency resolution
- Built-in virtual environment management
- Compatible with pip's requirements.txt
```

### 4. Review Periodically

Guidance becomes stale. Schedule reviews:

```bash
# List captured guidance entities
sibyl entity list --type guide

# Review and update or archive stale entries
```

## Next Steps

- [The Memory Loop](./memory-loop.md) - How captured guidance gets recalled
- [Capturing Knowledge](./capturing-knowledge.md) - What to save
- [Semantic Search](./semantic-search.md) - Finding guidance
- [Skills & Hooks](./skills.md) - Automating guide access
