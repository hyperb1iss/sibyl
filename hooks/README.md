# Sibyl Claude Code Hooks

Automatic integration between Sibyl and Claude Code.

## Install

```bash
moon run hooks:install
```

Then restart Claude Code.

### Enhanced Query Generation (Optional)

For smarter semantic search queries, set your Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-...
```

With this enabled, the hook uses Haiku 4.5 to generate contextual search queries based on the
conversation history. Falls back to keyword extraction without it.

No SDK needed - uses raw HTTP requests.

## What It Does

| Hook                 | Trigger                  | Action                                        |
| -------------------- | ------------------------ | --------------------------------------------- |
| **SessionStart**     | Session begins           | Prints the current session bundle and next-step reminder |
| **UserPromptSubmit** | Before processing prompt | Searches Sibyl, injects relevant knowledge    |

## Uninstall

```bash
moon run hooks:uninstall
```

Or manually: `rm -rf ~/.claude/hooks/sibyl`

## Files

- `session-start.py` - Prints the session bundle (active tasks, recent memory, next step)
- `user-prompt-submit.py` - Searches Sibyl and injects relevant memory into the prompt
- `configure.py` - Registers the SessionStart and UserPromptSubmit hooks in `~/.claude/settings.json`, preserving any non-Sibyl hooks already present
