# Sibyl Claude Code Hooks

Automatic integration between Sibyl and Claude Code.

## Install

```bash
moon run install-hooks
```

Then restart Claude Code.

## What It Does

| Hook | Trigger | Action |
|------|---------|--------|
| **SessionStart** | Session begins | Loads active tasks, reminds about `sibyl add` |
| **UserPromptSubmit** | Before processing prompt | Searches Sibyl, injects relevant knowledge |
| **Stop** | Claude stops | LLM evaluates for uncaptured learnings, blocks if needed |

The Stop hook uses Claude's `type: "prompt"` feature - an LLM (Haiku) analyzes the
session transcript and blocks Claude from stopping until valuable learnings are captured.

## Uninstall

```bash
moon run uninstall-hooks
```

Or manually: `rm -rf ~/.claude/hooks/sibyl`

## Files

- `session-start.py` - Context loading at session start
- `user-prompt-submit.py` - Knowledge injection into prompts
- `configure.py` - Updates `~/.claude/settings.json` (includes Stop hook prompt)
