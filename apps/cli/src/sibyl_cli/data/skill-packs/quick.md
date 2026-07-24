# Sibyl Quick Reference

Minimal contract for subagents and small-context workers. Load `core` instead when you own the
session.

## Rules

1. Never append `2>/dev/null` to sibyl commands, and never pipe `--json` output into `jq`/`grep`
   without checking the exit code first. Errors print as `✗ <message>` and exit non-zero;
   suppressing them causes blind retries.
2. Search previews truncate. Follow up with `sibyl show <id>` before acting on a result.
3. Capture non-obvious learnings without asking permission: `sibyl remember` or `--learnings` on
   task completion.
4. On connection errors run `sibyl health` once. If the server is down, report it and move on
   instead of retrying the same command. Do not run `auth login` while health fails.
5. Never invent subcommands, flags, or enum values. Unsure? `sibyl <group> --help`. `remember`
   title/body are positional (no `--title`/`--body`); `insight` and `discovery` are not kinds, so
   use `episode`, `error_pattern`, or `note`.

## Verbs

| Intent                           | Command                                                 |
| -------------------------------- | ------------------------------------------------------- |
| Working context before acting    | `sibyl context "<goal>" --intent build`                 |
| Lean brief for a subagent prompt | `sibyl brief "<goal>" --budget 1500`                    |
| Find knowledge by meaning        | `sibyl context "<topic>" --intent general`              |
| Full content by ID               | `sibyl show <id>`                                       |
| Save a durable memory            | `sibyl remember "Title" "Body" --kind decision`         |
| Quick learning capture           | `sibyl remember "Title" "What you learned" --kind note` |
| Current work                     | `sibyl task list --status doing,blocked`                |
| Finish with learnings            | `sibyl task complete <id> --learnings "..."`            |

## Pattern

context → act → remember. Context shows IDs; `show` fetches full content; `remember` makes it
durable for the next agent.
