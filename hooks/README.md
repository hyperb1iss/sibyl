# Sibyl Claude Code Hooks

Automatic integration between Sibyl and Claude Code.

## Install

`sibyl setup <url>` registers the SessionStart hook when Claude Code is installed, so most machines
need nothing else. From a Sibyl checkout, install the repo's hooks with:

```bash
moon run hooks:install
```

Then restart Claude Code.

## What It Does

| Hook             | Trigger        | Action                                                   |
| ---------------- | -------------- | -------------------------------------------------------- |
| **SessionStart** | Session begins | Prints the current session bundle and next-step reminder |

The per-prompt context-injection hook (`UserPromptSubmit`) was removed because it substituted for
the `sibyl` skill instead of nudging the agent to load it. Agents should invoke the skill on their
own and call `sibyl context "<goal>"` when they need working memory. Re-running `sibyl local setup`
cleans the legacy hook out of `~/.claude/settings.json` and removes the orphan script from
`~/.claude/hooks/sibyl/`.

## Uninstall

```bash
moon run hooks:uninstall
```

Or manually: `rm -rf ~/.claude/hooks/sibyl`

## Files

- `session-start.py` - Prints the session bundle (active tasks, recent memory, next step)
- `configure.py` - Registers the SessionStart hook in `~/.claude/settings.json`, preserving any
  non-Sibyl hooks already present
