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
sibyl docker init --tag 1.4.13 --with-worker --with-crawler

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

Move the deployment to new images: pull them, then write the new pins and recreate the containers.
With `--tag` it moves the Sibyl images to that tag; without it, it re-pulls the current pins.

Upgrade also moves the SurrealDB image up to the server this CLI release runs (`v4.0.0`), so upgrade
the CLI before you upgrade the deployment. It pulls every new image before it touches the compose
file, so a failed pull exits non-zero and leaves the deployment and its pins as they were. Compose
then recreates SurrealDB and waits for it to report healthy before it starts the new API. If that
start fails, upgrade exits non-zero with the new pins kept, because SurrealDB may already have
opened the data on the new version; start it again with `sibyl docker up`. A `SIBYL_SURREAL_IMAGE`
set in `~/.sibyl/docker/.env` or the shell still wins, and a SurrealDB image you wrote by hand, or
one newer than the CLI's, stays as it is. Upgrade warns in both cases.

Upgrade never moves SurrealDB to a new major version, so a deployment on `v3` keeps its whole
SurrealDB service (image, store path, and settings) while the Sibyl images move. Upgrade says so and
names the command that does the move, `sibyl docker upgrade --surreal-major`, which converts the
data in place, one way:

1. It asks first (the default is no; `--yes` skips the question) and runs on its own, without
   `--tag`. It refuses with nothing changed in these cases:
   - The deployment runs a Sibyl API older than 1.4.14, the first release whose API rebuilds a
     converted store's indexes, or one whose tag is not a release (a custom build). Move it first
     with `sibyl docker upgrade --tag <release>`, 1.4.14 or newer; the refusal prints the exact
     command. `SIBYL_IMAGE_TAG` does not change this.
   - SurrealDB keeps its data anywhere but the `sibyl_surreal` volume as `init` writes it: a bind
     mount, another or renamed volume, or a second mount over `/data`. Move such storage by hand
     with the runbook.
   - The SurrealDB command was edited by hand, or `SIBYL_SURREAL_IMAGE` is set.
   - The [`sibyl local`](./local.md) runtime still runs `v3` on the `sibyl_surreal` volume both
     runtimes share.
2. It pulls the new image, stops the API, worker, web, and SurrealDB, copies the `sibyl_surreal`
   volume into `sibyl_surreal_3x_backup` (refusing a backup volume that already holds data), saves
   the compose file as `docker-compose.yml.surreal3x.bak`, and checks that about twice the store's
   size is free. A failure up to here starts the stack again on `v3` with the store untouched.
3. It starts SurrealDB `v4` with the conversion switched on and waits for it to answer, for 30
   seconds per GB of store plus 5 minutes, and at least 10 minutes. Nothing listens while it
   converts.
4. It switches the conversion off, starts the stack, and waits for the API to report ready: the API
   rebuilds the auth and content indexes before it serves. Each organization's graph rebuilds its
   indexes on its first use, so the first requests for each organization can be slow.

When it finishes, it prints the commands that reclaim the space the `v3` copies hold, to run once
you have checked your data, and how to roll back from the cold copy until then. A failure after
SurrealDB `v4` starts stops the command with the conversion or the rebuild possibly still running,
says where it stands, and prints the same rollback; nothing is ever deleted on a failure. Never
start SurrealDB `v3` on the converted volume: it opens it as an empty database. The
[SurrealDB 4 upgrade runbook](../deployment/surrealdb-4-upgrade.md) covers the move in depth.
[`sibyl update`](./update.md) reports a deployment held on an older major and never crosses it.

```bash
sibyl docker upgrade [options]
```

| Option            | Short | Default | Description                                                 |
| ----------------- | ----- | ------- | ----------------------------------------------------------- |
| `--tag`           |       | (none)  | Sibyl image tag to move to                                  |
| `--surreal-major` |       | false   | Move SurrealDB to this CLI's major version (one way)        |
| `--yes`           | `-y`  | false   | Skip the `--surreal-major` confirmation (it defaults to no) |

### Examples

```bash
sibyl docker upgrade --tag 1.4.13

# Move SurrealDB from v3 to v4 by converting its data in place
sibyl docker upgrade --surreal-major
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
- [`sibyl update`](./update.md) - Upgrade a running deployment in step with the CLI
