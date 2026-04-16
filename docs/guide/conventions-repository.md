---
title: Conventions Repository
description: Maintaining team patterns and feeding them to your knowledge graph
---

# Conventions Repository

A conventions repository is a centralized collection of your team's patterns, tooling decisions, and
hard-won wisdom. When connected to Sibyl, these conventions become searchable context that your AI
agents can access across all projects.

## Why a Conventions Repo?

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

Centralize conventions in one repository:

```
conventions/
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

Then crawl it into Sibyl. Now every agent knows your team's conventions.

## Setting Up Your Conventions Repo

### 1. Create the Repository

```bash
mkdir -p ~/dev/conventions
cd ~/dev/conventions
git init
```

### 2. Structure Your Content

**Recommended structure:**

```
conventions/
├── AGENTS.md               # AI-focused quick reference
├── README.md               # Human-focused overview
├── docs/
│   ├── WISDOM.md           # Index of lessons learned
│   ├── TOOLING.md          # Required tools per language
│   ├── LAYOUTS.md          # Directory structures
│   ├── COMMITS.md          # Commit conventions
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
    └── convention/SKILL.md
```

### 3. Write an AGENTS.md

This is the quick reference your AI agents will read first:

```markdown
# Agent Instructions

This repository contains team conventions. Reference it when setting up new projects or ensuring
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

## Key Conventions

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

## Crawling Conventions into Sibyl

### Method 1: File Crawling

Sibyl can crawl local directories:

```bash
sibyl crawl add "file:///Users/bliss/dev/conventions" --name "Conventions"
sibyl crawl ingest <source_id>
```

This creates `CONVENTION` entities in your knowledge graph.

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

Set up a hook to re-crawl when conventions change:

```bash
# In conventions repo
# .git/hooks/post-commit
#!/bin/bash
sibyl crawl add "file:///Users/bliss/dev/conventions" --name "Conventions"
sibyl crawl ingest <source_id>
```

## Using Conventions in Projects

### Reference in CLAUDE.md

```markdown
## Conventions

This project follows conventions from `~/dev/conventions`.

When starting a new feature:

1. Check `~/dev/conventions/docs/wisdom/` for relevant patterns
2. Search sibyl for "convention" + your topic
3. Use templates from `~/dev/conventions/templates/`
```

### Search for Conventions

```bash
# Find conventions about testing
sibyl search "testing convention" --type convention

# Find patterns from wisdom docs
sibyl search "architecture pattern" --type pattern
```

### Agent Usage

Your agent can query conventions directly:

```
You: "What's our convention for error handling?"

Agent: [Searches sibyl for "error handling convention"]
       Found: "Use Result types for fallible operations.
              Never throw exceptions in business logic.
              Validate at boundaries only."
```

## Example Conventions

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
# Python Conventions

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

## Syncing Conventions Across Projects

### Pattern: Shared Skills

Put Claude Code skills in your conventions repo:

```
conventions/
└── skills/
    ├── check/SKILL.md       # Run quality checks
    ├── commit/SKILL.md      # AI-powered commits
    ├── convention/SKILL.md  # Capture new conventions
    └── review/SKILL.md      # Code review workflow
```

Install to any project:

```bash
cp ~/dev/conventions/skills/* ~/.claude/skills/
```

### Pattern: Template Inheritance

Projects can extend base templates:

```toml
# conventions/templates/python/pyproject.toml
[project]
requires-python = ">=3.12"

[tool.ruff]
line-length = 100
```

```toml
# my-project/pyproject.toml
# Extends conventions, adds project-specific config
[project]
name = "my-project"
requires-python = ">=3.12"  # From conventions

[tool.ruff]
line-length = 100  # From conventions
select = ["ALL"]   # Project-specific
```

## Best Practices

### 1. Keep It Current

Update conventions when you learn something new:

```bash
# Add to wisdom docs
echo "## New Lesson\n\n$LESSON" >> docs/wisdom/debugging.md

# Add to sibyl
sibyl add "New debugging lesson" "$LESSON" --type convention

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

Conventions become stale. Schedule reviews:

```bash
# Find old conventions
sibyl search "" --type convention --before 6m

# Review and update or archive
```

## Next Steps

- [Capturing Knowledge](./capturing-knowledge.md) - What to save
- [Semantic Search](./semantic-search.md) - Finding conventions
- [Skills & Hooks](./skills.md) - Automating convention access
