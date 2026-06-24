# update

Check for and apply Sibyl updates. `update` upgrades the CLI, pulls newer Docker container images,
and refreshes Claude/Codex skills and hooks. It only manages easy-install deployments installed via
`uv tool`; when run from a source checkout it tells you to `git pull` and re-run
`moon run install-dev` instead.

Called with no flags, `update` checks every component, shows a status panel, and prompts before
applying changes. Scope it to one component with `--cli`, `--containers`, or `--skills`.

## Synopsis

```bash
sibyl update [options]
```

## Options

| Option         | Short | Default | Description                         |
| -------------- | ----- | ------- | ----------------------------------- |
| `--check`      | `-c`  | false   | Only check for updates, don't apply |
| `--cli`        |       | false   | Only update the CLI                 |
| `--containers` |       | false   | Only update Docker containers       |
| `--skills`     |       | false   | Only update skills and hooks        |
| `--yes`        | `-y`  | false   | Skip the confirmation prompt        |

When none of `--cli`, `--containers`, or `--skills` is given, all three are considered.

## Examples

```bash
# Check what's available without changing anything
sibyl update --check

# Apply all available updates without prompting
sibyl update --yes

# Update only the CLI
sibyl update --cli

# Refresh only skills and hooks
sibyl update --skills
```

## Notes

- The CLI version check compares the installed `sibyl-dev` against PyPI; the container check
  compares local and remote image digests for the managed compose stack under `~/.sibyl`.
- A successful CLI upgrade re-installs the skill stub automatically, so guidance stays
  version-matched.
- In development mode (skills symlinked, or run from the Sibyl repo) `update` exits early with the
  source-update instructions; nothing is changed.

## Related Commands

- [`sibyl skill`](./skill.md) - Install or refresh the skill stub directly
- [`sibyl local`](./local.md) - Manage the local Docker instance
- [`sibyl docker`](./docker.md) - Manage a self-hosted Docker deployment
