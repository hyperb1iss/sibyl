# doctor

Diagnose Sibyl config, daemon health, locks, write readiness, and agent setup. `doctor` runs a
battery of checks against your active context and your local assistant integration, printing a
pass/warn/fail table. It exits non-zero when any check fails, so it works as a preflight gate.

## Synopsis

```bash
sibyl doctor [options]
```

## Options

| Option                             | Short | Default | Description                                                       |
| ---------------------------------- | ----- | ------- | ----------------------------------------------------------------- |
| `--json`                           | `-j`  | false   | Output as JSON                                                    |
| `--timeout`                        |       | 2.0     | Network timeout in seconds                                        |
| `--write-test` / `--no-write-test` |       | on      | Run the authenticated write probe                                 |
| `--skip-agent`                     |       | false   | Skip agent-setup checks (skill stub, hooks, CLAUDE.md)            |
| `--append`                         |       | (none)  | Append the recommended agent-setup block to a CLAUDE.md/AGENTS.md |

## Checks

`doctor` reports on three areas:

| Area        | Checks                                                                             |
| ----------- | ---------------------------------------------------------------------------------- |
| Config      | Config file is readable, an active context resolves                                |
| Runtime     | API health, local port reachability, embedded SurrealDB lock, write probe          |
| Agent setup | Skill stub installed, SessionStart hook present, no legacy hook, CLAUDE.md content |

For remote contexts the port and embedded-lock probes are skipped automatically. The write probe
authenticates and performs a round-trip write, so it needs a logged-in context; disable it with
`--no-write-test` when you only want read-side checks.

## Examples

```bash
# Full health and setup report
sibyl doctor

# Skip the authenticated write probe
sibyl doctor --no-write-test

# Machine-readable output for CI
sibyl doctor --json | jq '.ok'

# Add the managed memory-loop block to your project CLAUDE.md
sibyl doctor --append ./CLAUDE.md
```

## Notes

- When the agent-prompt check does not pass and `--append` is not given, `doctor` prints the
  recommended agent-setup block so you can paste it in by hand.
- `--append` writes an idempotent, marker-delimited block, so re-running it updates the same block
  in place rather than duplicating it.
- A non-zero exit means at least one check failed; warnings alone do not fail the run.

## Related Commands

- [`sibyl init`](./init.md) - Create the context `doctor` checks
- [`sibyl skill`](./skill.md) - Install the skill stub `doctor` looks for
- [`sibyl local`](./local.md) - Set up Claude/Codex hooks with `local setup`
