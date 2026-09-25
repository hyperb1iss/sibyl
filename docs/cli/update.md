# update

Check for and apply Sibyl updates. `update` upgrades the CLI, moves a running container runtime to
the server image that matches it, and refreshes Claude/Codex skills and hooks. It only manages
easy-install deployments installed via `uv tool`; when run from a source checkout it tells you to
`git pull` and re-run `moon run install-dev` instead.

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
| `--containers` |       | false   | Only upgrade the container runtime  |
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

# Bring a running container runtime in line with the installed CLI
sibyl update --containers

# Refresh only skills and hooks
sibyl update --skills
```

## Notes

- The CLI version check compares the installed `sibyl-dev` against PyPI.
- The container check finds each runtime by its compose file: `~/.sibyl/local/` for
  [`sibyl up`](./local.md) and `~/.sibyl/docker/` for [`sibyl docker`](./docker.md). Its current
  version is the image tag of its running API container, or the tag its compose file pins when no
  API container runs, so an interrupted upgrade that left the pin ahead still counts as behind. A
  runtime is behind when that tag is older than the one the CLI runs, which is the CLI version (or
  `SIBYL_IMAGE_TAG` when set). When the CLI is upgraded in the same run, containers follow the
  version that actually installed, which can differ from PyPI when the tool install is pinned. A
  newer or custom tag is left alone, and so is every runtime when the CLI is a dev, local, or
  post-release build, since no image is published for those.
- `update` hands the upgrade to the runtime's own command, run through the `sibyl` on PATH with the
  tag passed explicitly: [`sibyl docker upgrade --tag <tag>`](./docker.md#docker-upgrade) or
  [`sibyl local upgrade --tag <tag>`](./local.md#local-upgrade). Both pull the new images before
  they restart anything, so a failed pull leaves the running server alone. `update` then re-reads
  the pin and the running API image and only reports success when both reached the target, so an API
  that exits right after the start counts as a failure. When the new images pulled but did not
  start, neither command rolls back, and `update` points at the runtime's logs.
- `update` never starts a stopped runtime. A stopped local runtime runs the new tag on its next
  `sibyl up`; a stopped Docker deployment keeps its pin until you run
  `sibyl docker upgrade --tag <tag>`, which also starts it.
- If the CLI upgrade fails, the container step is skipped so the server never lands ahead of the
  CLI. Re-run `sibyl update --containers` once the CLI is current.
- A successful CLI upgrade re-installs the skill stub automatically, so guidance stays
  version-matched.
- In development mode (skills symlinked, or run from the Sibyl repo) `update` exits early with the
  source-update instructions; nothing is changed.

## Related Commands

- [`sibyl skill`](./skill.md) - Install or refresh the skill stub directly
- [`sibyl local`](./local.md) - Manage the local Docker instance
- [`sibyl docker`](./docker.md) - Manage a self-hosted Docker deployment
