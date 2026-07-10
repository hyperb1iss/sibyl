---
title: Skills & Hooks
description: Teaching agents how to work with Sibyl
---

# Skills & Hooks

Sibyl's power comes from two complementary systems: **Skills** teach agents structured workflows,
and **Hooks** inject knowledge automatically. Together they give your agent structured workflows and
automatic context from your knowledge graph.

**Skills are not Claude-only.** The loader installs into Claude Code, Codex, and the generic
`~/.agents` convention, so any agent that can run a shell command speaks Sibyl. Hooks, for now, are
specific to Claude Code.

## The Two Systems

| System     | Purpose         | When it Runs                | User Action |
| ---------- | --------------- | --------------------------- | ----------- |
| **Skills** | Teach workflows | On `/skill-name` invocation | Manual      |
| **Hooks**  | Inject context  | Automatically on triggers   | None needed |

Think of it this way:

- **Skills** = Training manual (agent reads when invoked)
- **Hooks** = Invisible assistant (works behind the scenes)

---

## Hooks: Automatic Context

Hooks are the magic that makes Sibyl invisible on Claude Code. They run automatically at specific
moments:

### SessionStart Hook

**Trigger:** When you start a new Claude Code session

**What it does:**

- Packages a wake-up context bundle (`sibyl session bundle`)
- Loads your active tasks (status: `doing`, `blocked`, `review`)
- Shows your project context
- Reminds the agent about the memory loop

```
┌─────────────────────────────────────────────────────────────┐
│  SESSION START                                               │
│                                                              │
│  Active Tasks:                                               │
│  • task_abc123 [doing] Fix authentication token refresh      │
│  • task_def456 [blocked] Add rate limiting (waiting on API)  │
│                                                              │
│  Project: Authentication System (proj_auth)                  │
│  Remember: Use `sibyl remember` to capture learnings!        │
└─────────────────────────────────────────────────────────────┘
```

### What about per-prompt injection?

An earlier `UserPromptSubmit` hook ran on every prompt, generated a semantic search query with
Haiku, and injected a context pack inline. We removed it. The hook gave agents just-enough
sibyl-shaped context to skip invoking the skill or reaching for the CLI on their own, which defeated
the point of the skill. Agents now drive context and capture explicitly via `sibyl context`,
`sibyl remember`, and `sibyl reflect`. The SessionStart bundle remains as a one-time wake-up nudge.

### Installing Hooks

```bash
# Install hooks to ~/.claude/hooks/sibyl
moon run hooks:install

# Restart Claude Code to activate
```

### Uninstalling Hooks

```bash
moon run hooks:uninstall
# Or: rm -rf ~/.claude/hooks/sibyl
```

---

## Skills: Teaching Workflows

Skills are markdown documents that teach an agent specific workflows. On Claude Code and Codex you
invoke them with slash commands:

### sibyl

The unified skill for all Sibyl operations:

```
/sibyl
```

**Teaches the agent:**

- CLI command syntax and patterns
- Search-first workflow
- Task lifecycle management
- Knowledge capture best practices
- Project audits and sprint planning
- Common pitfalls to avoid

## Skill File Format

### SKILL.md Structure

```markdown
---
name: skill-name
description: Brief description of what the skill provides
allowed-tools: Bash, Grep, Glob, Read
---

# Skill Title

Detailed content teaching Claude how to use this skill...

## Quick Reference

Command tables, examples...

## Workflows

Step-by-step processes...

## Best Practices

Guidelines and patterns...
```

### Frontmatter

```yaml
---
name: sibyl
description: Graph-RAG knowledge system with CLI interface
allowed-tools: Bash, Grep, Glob, Read
---
```

| Field           | Description                               |
| --------------- | ----------------------------------------- |
| `name`          | Skill identifier (must be unique)         |
| `description`   | Brief description for skill discovery     |
| `allowed-tools` | Tools available to the agent in the skill |

## Installing Skills

Install the stable loader skill with:

```bash
sibyl skill install
```

This drops the tiny `/sibyl` loader into every agent skill root it knows: Claude Code
(`~/.claude/skills`), Codex (`~/.codex/skills`), and the generic `~/.agents/skills` convention. The
same workflow follows you across tools. Pass `--force` to replace existing symlinked or
non-directory targets.

### Skill packs live in the CLI

The loader is deliberately tiny. It points the agent back at the installed CLI, which serves the
full, version-matched guidance as **skill packs built into the binary**:

| Pack        | What it covers                                                        |
| ----------- | --------------------------------------------------------------------- |
| `core`      | The full context → act → remember → reflect loop and command contract |
| `quick`     | Minimal verb table for subagents (~500 tokens)                        |
| `workflows` | Longer task, project, memory, and debugging workflows                 |
| `examples`  | Concrete CLI examples for context, tasks, memory, and projects        |
| `migration` | Legacy Graphiti/FalkorDB migration guidance                           |

```bash
sibyl skill list           # List the packs this CLI version can serve
sibyl skill get core       # Print the core pack before the first write intent
sibyl skill get workflows  # Any other pack on demand
```

Because the packs ship inside the CLI, the guidance always matches the exact Sibyl version on the
machine. Upgrade the CLI and the skill content upgrades with it. No stale copies drift out of sync,
and a subagent on any host gets the same source of truth from one command.

Hooks are separate from skills because they execute automatically on session events. Install hooks
only when that automation is explicitly desired, and only on Claude Code.

### Manual Installation

```bash
# Copy skill directory
cp -r skills/sibyl ~/.claude/skills/

# Or create symlink
ln -s /path/to/sibyl/skills/sibyl ~/.claude/skills/
```

## Skill Location

The loader installs to each agent's skill root:

```
~/.claude/skills/sibyl/SKILL.md     # Claude Code
~/.codex/skills/sibyl/SKILL.md      # Codex
~/.agents/skills/sibyl/SKILL.md     # generic agents
```

`SKILL.md` is just the loader. The full version-matched guidance is served from the installed CLI
with `sibyl skill get core` and the related pack commands above.

## Creating Custom Skills

### 1. Create Directory

```bash
mkdir -p skills/my-skill
```

### 2. Create SKILL.md

```markdown
---
name: my-skill
description: Custom skill for specific workflow
allowed-tools: Bash
---

# My Custom Skill

## Purpose

Explain what this skill helps Claude do...

## Commands

### Primary Command

\`\`\`bash command example \`\`\`

### Secondary Command

\`\`\`bash another command \`\`\`

## Workflows

### Common Workflow

1. First step
2. Second step
3. Third step

## Best Practices

- Guideline one
- Guideline two
```

### 3. Install

```bash
cp -r skills/my-skill ~/.claude/skills/
```

### 4. Use

```
/my-skill
```

## Skill Design Patterns

### Command Reference Pattern

Provide clear command tables:

```markdown
## CLI Reference

| Command                 | Description     |
| ----------------------- | --------------- |
| `sibyl context "query"` | Semantic search |
| `sibyl task list`       | List tasks      |
```

### Workflow Pattern

Describe step-by-step processes:

```markdown
## Task Workflow

1. **Find Tasks** \`\`\`bash sibyl task list --status todo \`\`\`

2. **Start Working** \`\`\`bash sibyl task start task_xyz \`\`\`

3. **Complete** \`\`\`bash sibyl task complete task_xyz --learnings "..." \`\`\`
```

### Common Mistakes Pattern

Help Claude avoid errors:

```markdown
## Common Pitfalls

| Wrong                    | Correct                           |
| ------------------------ | --------------------------------- |
| `sibyl task add "..."`   | `sibyl task create --title "..."` |
| `sibyl task list --todo` | `sibyl task list --status todo`   |
```

### Memory Loop Pattern

Teach the context, act, remember, reflect cycle:

```markdown
## The Memory Loop

\`\`\`

1. CONTEXT -> sibyl context "topic"
2. ACT -> sibyl task start <id>
3. REMEMBER -> sibyl remember "Title" "What, why, how"
4. REFLECT -> sibyl reflect --persist --review \`\`\`
```

## Skill Content Guidelines

### Be Specific

```markdown
# GOOD

sibyl task list --status todo,doing,blocked

# LESS GOOD

sibyl task list (various options available)
```

### Show Examples

```markdown
# Search for patterns

sibyl context "implement authentication using existing patterns" --intent build

# Result:

# pattern_abc OAuth callback handling 0.95

# pattern_xyz JWT token refresh 0.89
```

### Include Error Handling

```markdown
## Troubleshooting

### Connection Error

If you see "connection refused":

1. Check server is running: `sibyl health`
2. Verify URL in config
```

### Provide Context

```markdown
## When to Use

Use `episode` type for:

- Debugging discoveries
- One-time learnings
- Context-specific insights

Use `pattern` type for:

- Reusable approaches
- Best practices
- Standard solutions
```

## Advanced Skill Features

### Tool Restrictions

Limit tools for safety:

```yaml
allowed-tools: Bash, Read
# Claude can only use Bash and Read when this skill is active
```

### Conditional Guidance

```markdown
## Project-Specific Commands

### If Working in `auth` Project

\`\`\`bash sibyl task list --project proj_auth --status todo \`\`\`

### If Working in `api` Project

\`\`\`bash sibyl task list --project proj_api --status todo \`\`\`
```

### Integration with Other Skills

```markdown
## Related Skills

- `/sibyl` - For all Sibyl operations (also handles auditing)
- `/git-workflow` - For commit patterns
```

## Example: Complete Skill

```markdown
---
name: sibyl-code-review
description: Code review workflow using Sibyl knowledge graph
allowed-tools: Bash, Read, Grep
---

# Sibyl Code Review Skill

Guide Claude through code review using Sibyl's knowledge graph.

## Purpose

Use Sibyl to:

- Find relevant patterns for the code being reviewed
- Check for applicable rules
- Track review tasks

## Quick Start

\`\`\`bash

# 1. Search for relevant patterns

sibyl context "review the code and relevant prior decisions" --intent review

# 2. Check applicable rules

sibyl entity list --type rule

# 3. Start review task

sibyl task start task_review_xyz \`\`\`

## Review Workflow

### 1. Prepare

\`\`\`bash

# Find patterns for the domain

sibyl context "review domain rules for this code" --intent review

# Check rules

sibyl entity list --type rule --category "domain" \`\`\`

### 2. Review

Check code against:

- Discovered patterns
- Applicable rules
- Past learnings (episodes)

### 3. Document

\`\`\`bash

# Capture new discoveries

sibyl remember "Review finding" "What was discovered..."

# Complete review task

sibyl task complete task_xyz --learnings "Key insights from review..." \`\`\`

## Best Practices

- Search before reviewing
- Reference specific patterns in feedback
- Capture reusable insights
- Complete review tasks with learnings
```

## Debugging Skills

### Skill Not Found

1. Check file location: `~/.claude/skills/skill-name/SKILL.md`
2. Verify frontmatter syntax
3. Restart Claude Code

### Skill Not Working as Expected

1. Review skill content for clarity
2. Add more specific examples
3. Include command output examples

### Agent Ignoring Skill Guidance

1. Make instructions more explicit
2. Use numbered steps
3. Add "IMPORTANT" markers for critical points

## Next Steps

- [The Memory Loop](./memory-loop.md) - The cycle hooks support
- [Claude Code Integration](./claude-code.md) - Full MCP setup
- [Agent Collaboration](./agent-collaboration.md) - Shared-assistant patterns
- [Capturing Knowledge](./capturing-knowledge.md) - What to teach your agent
