# docker

Manage a self-hosted Sibyl Docker deployment. `docker` generates a pinned compose stack under
`~/.sibyl/docker`, starts and stops it, tails its logs, and upgrades it to new image tags. It is the
production-leaning path: explicit image tags, generated secrets, and optional worker and crawler
services.

For the simpler, batteries-included local instance, use [`sibyl local`](./local.md) (and its
`sibyl up` / `sibyl down` aliases) instead.

## Commands

| Command                                   | Description                                           |
| ----------------------------------------- | ----------------------------------------------------- |
| [`sibyl docker init`](#docker-init)       | Generate pinned compose files under `~/.sibyl/docker` |
| [`sibyl docker up`](#docker-up)           | Start the Docker deployment                           |
| [`sibyl docker logs`](#docker-logs)       | Show Docker deployment logs                           |
| [`sibyl docker down`](#docker-down)       | Stop the Docker deployment                            |
| [`sibyl docker upgrade`](#docker-upgrade) | Pull current images and recreate containers           |

---

## docker init

Generate pinned compose and env files under `~/.sibyl/docker` and create a matching context.

```bash
sibyl docker init [options]
```

| Option                         | Short | Default       | Description                       |
| ------------------------------ | ----- | ------------- | --------------------------------- |
| `--api-port`                   |       | 3334          | Host API port                     |
| `--web-port`                   |       | 3337          | Host web port                     |
| `--surreal-port`               |       | 8000          | Host SurrealDB port               |
| `--tag`                        |       | (CLI version) | Sibyl image tag                   |
| `--with-worker`                |       | false         | Add Valkey and a worker service   |
| `--with-crawler`               |       | false         | Use the crawler-enabled API image |
| `--context`                    |       | `docker`      | Context name to create            |
| `--activate` / `--no-activate` |       | on            | Set the created context active    |
| `--force`                      | `-f`  | false         | Overwrite existing files          |

### Examples

```bash
# Generate the default stack and activate its context
sibyl docker init

# Pin a tag and add the worker + crawler services
sibyl docker init --tag 1.1.2 --with-worker --with-crawler

# Regenerate over an existing install
sibyl docker init --force
```

---

## docker up

Start the deployment. Requires an initialized runtime and a working Docker install.

```bash
sibyl docker up [options]
```

| Option   | Default | Description                 |
| -------- | ------- | --------------------------- |
| `--pull` | false   | Pull images before starting |

---

## docker logs

Show deployment logs. Pass a service name to scope the output.

```bash
sibyl docker logs [service] [options]
```

| Argument  | Required | Description           |
| --------- | -------- | --------------------- |
| `service` | No       | Optional service name |

| Option                     | Short | Default | Description             |
| -------------------------- | ----- | ------- | ----------------------- |
| `--follow` / `--no-follow` | `-f`  | on      | Follow log output       |
| `--tail`                   |       | 100     | Number of lines to show |

---

## docker down

Stop the deployment.

```bash
sibyl docker down [options]
```

| Option      | Short | Default | Description         |
| ----------- | ----- | ------- | ------------------- |
| `--volumes` | `-v`  | false   | Also remove volumes |

---

## docker upgrade

Pull current images and recreate containers, optionally writing a new image tag first.

```bash
sibyl docker upgrade [options]
```

| Option  | Default | Description           |
| ------- | ------- | --------------------- |
| `--tag` | (none)  | Write a new image tag |

### Example

```bash
sibyl docker upgrade --tag 1.1.2
```

## Notes

- Ports bind to `127.0.0.1` only. The API is published on the API port, the web UI on the web port,
  and SurrealDB on the SurrealDB port.
- `init` generates a SurrealDB password and JWT secret into a `0600` env file; keep that file safe.
- The worker service requires Valkey, which `--with-worker` adds and wires up automatically.
- Most subcommands refuse to run until you have run `docker init`.

## Related Commands

- [`sibyl local`](./local.md) - Simpler local Docker instance with `sibyl up`/`down`
- [`sibyl service`](./service.md) - Run a native host daemon instead of Docker
- [`sibyl update`](./update.md) - Pull newer container images
