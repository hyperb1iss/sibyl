# init

Create an explicit local or remote context for first-run setup. `init` writes a named context into
your Sibyl config and makes it active, so subsequent commands know which server to talk to. Run it
once when you first set Sibyl up, then verify with [`sibyl doctor`](./doctor.md).

A context is either local (a Sibyl daemon on this machine) or remote (a hosted Sibyl server). Pass
`--local` for the former, `--remote <url>` for the latter; the two cannot be combined.

## Synopsis

```bash
sibyl init [options]
```

## Options

| Option       | Short | Default          | Description                               |
| ------------ | ----- | ---------------- | ----------------------------------------- |
| `--remote`   |       | (none)           | Remote Sibyl server URL for CLI-only mode |
| `--local`    |       | false            | Create a localhost context                |
| `--name`     | `-n`  | `local`/`remote` | Context name                              |
| `--org`      | `-o`  | (auto)           | Organization slug                         |
| `--project`  | `-p`  | (none)           | Default project ID                        |
| `--insecure` | `-k`  | false            | Skip SSL verification for this context    |
| `--force`    | `-f`  | false            | Update an existing context                |
| `--json`     | `-j`  | false            | Output as JSON                            |

When `--remote` is omitted the server URL defaults to `http://localhost:3334`. The context name
defaults to `remote` when `--remote` is set and `local` otherwise.

## Examples

```bash
# Local first-run setup
sibyl init --local

# Point the CLI at a hosted server
sibyl init --remote https://your-sibyl-host --org acme

# Update an existing context in place
sibyl init --remote https://your-sibyl-host --force

# Name a context and set a default project
sibyl init --local --name dev --project sibyl
```

## Notes

- `init` refuses to overwrite an existing context unless you pass `--force`.
- After a local init, the suggested next step is `sibyl serve` then `sibyl doctor`. After a remote
  init, it is `sibyl auth login` then `sibyl doctor`.
- `--insecure` disables SSL verification for the context and is intended for self-signed dev servers
  only.

## Related Commands

- [`sibyl doctor`](./doctor.md) - Verify config and daemon health after init
- [`sibyl context`](./context.md) - List, switch, and edit contexts
- [`sibyl auth`](./auth.md) - Log in to a remote context
- [`sibyl local`](./local.md) - Run a Docker-based local instance instead
