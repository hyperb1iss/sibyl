---
title: Skills & Hooks
description: Teaching agents how to work with Sibyl
---

# Skills & Hooks

Sibyl's power comes from two complementary systems: **Skills** teach agents structured workflows, and **Hooks** inject knowledge automatically. Together, they transform Claude from a capable assistant into a knowledge-connected collaborator.

## The Two Systems

| System | Purpose | When it Runs | User Action |
|--------|---------|--------------|-------------|
| **Skills** | Teach workflows | On `/skill-name` invocation | Manual |
| **Hooks** | Inject context | Automatically on triggers | None needed |

Think of it this way:
- **Skills** = Training manual (agent reads when invoked)
- **Hooks** = Invisible assistant (works behind the scenes)

---

## Hooks: Automatic Context

Hooks are the magic that makes Sibyl invisible. They run automatically at specific moments:

### SessionStart Hook

**Trigger:** When you start a new Claude Code session

**What it does:**
- Loads your active tasks (status: `doing`, `blocked`, `review`)
- Shows your project context
- Reminds the agent about capturing learnings

```
┌─────────────────────────────────────────────────────────────┐
│  SESSION START                                               │
│                                                              │
│  Active Tasks:                                               │
│  • task_abc123 [doing] Fix authentication token refresh      │
│  • task_def456 [blocked] Add rate limiting (waiting on API)  │
│                                                              │
│  Project: Authentication System (proj_auth)                  │
│  Remember: Use `sibyl add` to capture learnings!             │
└─────────────────────────────────────────────────────────────┘
```

### UserPromptSubmit Hook

**Trigger:** Before processing every user prompt

**What it does:**
- Searches Sibyl for relevant knowledge
- Injects matching patterns and learnings into context
- Agent sees relevant knowledge without asking

Example: You type "help me fix the OAuth redirect issue"

The hook automatically:
1. Searches: `sibyl search "OAuth redirect"`
2. Finds: Pattern about OAuth callback URL matching
3. Injects: The pattern content into Claude's context

**Result:** Claude already knows your team's OAuth gotchas before writing code.

### Installing Hooks

```bash
# Install hooks to ~/.claude/hooks/sibyl
moon run install-hooks

# Restart Claude Code to activate
```

### Uninstalling Hooks

```bash
moon run uninstall-hooks
# Or: rm -rf ~/.claude/hooks/sibyl
```

---

## Skills: Teaching Workflows

Skills are markdown documents that teach Claude specific workflows. Invoke them with slash commands:

### sibyl-knowledge

The primary skill for knowledge operations:

```
/sibyl-knowledge
```

**Teaches Claude:**
- CLI command syntax and patterns
- Search-first workflow
- Task lifecycle management
- Knowledge capture best practices
- Common pitfalls to avoid

### sibyl-project-manager

A specialized skill for project management:

```
/sibyl-project-manager
```

**Teaches Claude:**
- Task auditing against codebase
- Stale work cleanup
- Priority rebalancing
- Sprint planning (6-day cycles)
- Data hygiene patterns

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
name: sibyl-knowledge
description: Graph-RAG knowledge oracle with CLI interface
allowed-tools: Bash
---
```

| Field | Description |
|-------|-------------|
| `name` | Skill identifier (must be unique) |
| `description` | Brief description for skill discovery |
| `allowed-tools` | Tools Claude can use when skill is active |

## Installing Skills

### Using moon

```bash
moon run install-skills
```

This copies skills to `~/.claude/skills/`.

### Manual Installation

```bash
# Copy skill directory
cp -r skills/sibyl-knowledge ~/.claude/skills/

# Or create symlink
ln -s /path/to/sibyl/skills/sibyl-knowledge ~/.claude/skills/
```

## Skill Location

Skills are stored in:

```
~/.claude/skills/
├── sibyl-knowledge/
│   └── SKILL.md
└── sibyl-project-manager/
    └── SKILL.md
```

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

\`\`\`bash
command example
\`\`\`

### Secondary Command

\`\`\`bash
another command
\`\`\`

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

| Command | Description |
|---------|-------------|
| `sibyl search "query"` | Semantic search |
| `sibyl task list` | List tasks |
```

### Workflow Pattern

Describe step-by-step processes:

```markdown
## Task Workflow

1. **Find Tasks**
   \`\`\`bash
   sibyl task list --status todo
   \`\`\`

2. **Start Working**
   \`\`\`bash
   sibyl task start task_xyz
   \`\`\`

3. **Complete**
   \`\`\`bash
   sibyl task complete task_xyz --learnings "..."
   \`\`\`
```

### Common Mistakes Pattern

Help Claude avoid errors:

```markdown
## Common Pitfalls

| Wrong | Correct |
|-------|---------|
| `sibyl task add "..."` | `sibyl task create --title "..."` |
| `sibyl task list --todo` | `sibyl task list --status todo` |
```

### Agent Loop Pattern

Teach feedback loops:

```markdown
## The Agent Feedback Loop

\`\`\`
1. SEARCH FIRST     -> sibyl search "topic"
2. CHECK TASKS      -> sibyl task list --status doing
3. WORK & CAPTURE   -> sibyl add (for learnings)
4. COMPLETE         -> sibyl task complete --learnings "..."
\`\`\`
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
sibyl search "authentication" --type pattern

# Result:
# pattern_abc  OAuth callback handling  0.95
# pattern_xyz  JWT token refresh       0.89
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
\`\`\`bash
sibyl task list --project proj_auth --status todo
\`\`\`

### If Working in `api` Project
\`\`\`bash
sibyl task list --project proj_api --status todo
\`\`\`
```

### Integration with Other Skills

```markdown
## Related Skills

- `/sibyl-project-manager` - For task auditing
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
sibyl search "code being reviewed" --type pattern

# 2. Check applicable rules
sibyl entity list --type rule

# 3. Start review task
sibyl task start task_review_xyz
\`\`\`

## Review Workflow

### 1. Prepare

\`\`\`bash
# Find patterns for the domain
sibyl search "domain of code" --type pattern

# Check rules
sibyl entity list --type rule --category "domain"
\`\`\`

### 2. Review

Check code against:
- Discovered patterns
- Applicable rules
- Past learnings (episodes)

### 3. Document

\`\`\`bash
# Capture new discoveries
sibyl add "Review finding" "What was discovered..."

# Complete review task
sibyl task complete task_xyz --learnings "Key insights from review..."
\`\`\`

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

### Claude Ignoring Skill Guidance

1. Make instructions more explicit
2. Use numbered steps
3. Add "IMPORTANT" markers for critical points

## Next Steps

- [Claude Code Integration](./claude-code.md) - Full MCP setup
- [Agent Collaboration](./agent-collaboration.md) - Multi-agent patterns
- [Capturing Knowledge](./capturing-knowledge.md) - What to teach Claude
