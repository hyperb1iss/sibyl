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
| **SessionStart** | Session begins | Loads active tasks + relevant patterns |
| **UserPromptSubmit** | Before processing prompt | Injects relevant Sibyl knowledge |
| **PostToolUse** | After Write/Edit | Logs file changes to Sibyl |
| **Stop** | Session ends | Logs session marker |

## Uninstall

```bash
moon run uninstall-hooks
```

Or manually: `rm -rf ~/.claude/hooks/sibyl`

## Files

- `session-start.py` - Context loading at session start
- `user-prompt-submit.py` - Knowledge injection into prompts
- `post-tool-use.py` - File change capture
- `stop.py` - Session summary logging
- `configure.py` - Updates `~/.claude/settings.json`
