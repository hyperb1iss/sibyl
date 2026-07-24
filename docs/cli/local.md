# local

Manage a local Sibyl instance (Docker-based). `local` runs the full Sibyl stack (API, worker, web
UI, and SurrealDB) in Docker with batteries included: first run prompts for API keys, generates
secrets, writes a compose file under `~/.sibyl/local`, and opens the web UI.

The top-level [`sibyl up`](#local-start) and [`sibyl down`](#local-stop) commands are aliases of
`local start` and `local stop`. For a pinned, production-leaning stack with explicit image tags, use
[`sibyl docker`](./docker.md) instead.

## Commands

| Command                               | Description                                   |
| ------------------------------------- | --------------------------------------------- |
| [`sibyl local start`](#local-start)   | Start the local instance (alias: `sibyl up`)  |
| [`sibyl local stop`](#local-stop)     | Stop the local instance (alias: `sibyl down`) |
| [`sibyl local status`](#local-status) | Show status of local services                 |
| [`sibyl local logs`](#local-logs)     | Show logs from local services                 |
| [`sibyl local reset`](#local-reset)   | Reset the instance (removes all data)         |
| [`sibyl local setup`](#local-setup)   | Set up Claude/Codex integration               |

---

## local start

Start the local instance. On first run, prompts for API keys and generates secrets; later runs reuse
saved configuration. Also available as `sibyl up`.

```bash
sibyl local start [options]
sibyl up [options]
```

| Option         | Default | Description                           |
| -------------- | ------- | ------------------------------------- |
| `--no-browser` | false   | Don't open the browser after starting |
| `--pull`       | false   | Pull latest images before starting    |

### Examples

```bash
# Start and open the web UI
sibyl up

# Start headless and pull fresh images
sibyl up --no-browser --pull
```

---

## local stop

Stop the local instance. Also available as `sibyl down`.

```bash
sibyl local stop [options]
sibyl down [options]
```

| Option      | Default | Description                            |
| ----------- | ------- | -------------------------------------- |
| `--destroy` | false   | Also remove volumes (deletes all data) |

---

## local status

Show the status of local Sibyl services.

```bash
sibyl local status
```

---

## local logs

Show logs from local Sibyl services. Pass a service name to scope the output.

```bash
sibyl local logs [service] [options]
```

| Argument  | Required | Description                                        |
| --------- | -------- | -------------------------------------------------- |
| `service` | No       | Service name (`api`, `web`, `worker`, `surrealdb`) |

| Option     | Short | Default | Description             |
| ---------- | ----- | ------- | ----------------------- |
| `--follow` | `-f`  | on      | Follow log output       |
| `--tail`   |       | 100     | Number of lines to show |

---

## local reset

Reset the local instance: stop containers, delete all data, and remove saved configuration. Prompts
for confirmation unless `--force` is given.

```bash
sibyl local reset [options]
```

| Option    | Short | Default | Description       |
| --------- | ----- | ------- | ----------------- |
| `--force` | `-f`  | false   | Skip confirmation |

---

## local setup

Set up Claude/Codex integration: skills for Claude Code and Codex, plus Claude Code hooks
(session-start and prompt injection). In development mode it symlinks; in package mode it copies.

```bash
sibyl local setup [options]
```

| Option      | Short | Default | Description                                     |
| ----------- | ----- | ------- | ----------------------------------------------- |
| `--status`  | `-s`  | false   | Only show current installation status           |
| `--snippet` |       | false   | Show the prompt snippet for Claude/Codex config |

## Notes

- Services bind to `127.0.0.1`: web UI on `3337`, API on `3334`, SurrealDB on `8000`.
- First-run setup reads `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` from the environment when present;
  otherwise configure keys from the web UI.
- `local reset` and `local stop --destroy` delete the SurrealDB volume and all data; use them
  deliberately.

## Related Commands

- [`sibyl docker`](./docker.md) - Pinned, production-leaning Docker stack
- [`sibyl service`](./service.md) - Native host daemon service files
- [`sibyl doctor`](./doctor.md) - Verify health after starting
- [`sibyl skill`](./skill.md) - Install the skill stub without hooks
