# Docker Compose Deployment

Local development using Docker Compose for database services with the Python/Node applications
running natively.

## Architecture

Docker Compose runs the database services while applications run natively for hot reload:

```
+------------------+     +------------------+
|   Native Apps    |     |   Docker Compose |
|------------------|     |------------------|
| Backend (:3334)  |---->| SurrealDB (:8000)|
| Frontend (:3337) |     | Redis* (:6381)   |
| Jobs + Schedules |     |                  |
+------------------+     +------------------+
```

`Redis` is opt-in for distributed or multi-process dev. The default local path only needs SurrealDB.

## Prerequisites

- Docker and Docker Compose
- Python 3.13+
- Node.js 24 and pnpm
- uv (Python package manager)

## Quick Start

```bash
# 1. Start database services
docker compose --env-file /dev/null up -d surrealdb

# 2. Install dependencies
uv sync                         # Python packages
cd apps/web && pnpm install     # Frontend packages

# 3. Configure your shell
export SIBYL_OPENAI_API_KEY=sk-...
# SIBYL_JWT_SECRET is auto-generated in dev.

# 4. Start all services
moon run dev
```

For Redis-backed coordination, opt into the `redis` profile explicitly:

```bash
docker compose --env-file /dev/null --profile redis up -d surrealdb redis
SIBYL_COORDINATION_BACKEND=redis moon run dev
```

## Service Definitions

The root `docker-compose.yml` defines a Surreal-first local stack plus an opt-in `redis` profile. It
runs only the data services; the API and web apps run natively for hot reload.

```yaml
services:
  surrealdb:
    image: ${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.0}
    container_name: ${COMPOSE_PROJECT_NAME:-sibyl}-surrealdb
    command:
      [
        "start",
        "--log",
        "info",
        "--user",
        "${SIBYL_SURREAL_USERNAME:-root}",
        "--pass",
        "${SIBYL_SURREAL_PASSWORD:-root}",
        "${SIBYL_SURREAL_PATH:-rocksdb:///data/sibyl.db}",
      ]
    ports:
      - "${SIBYL_SURREAL_PORT:-8000}:8000"
    volumes:
      # `:U` chowns the bind mount to the container UID under rootless podman
      - "${SURREAL_DATA_DIR:-./.moon/cache/surreal-dev}:/data:U"

  redis:
    image: valkey/valkey:8-alpine
    container_name: ${COMPOSE_PROJECT_NAME:-sibyl}-redis
    profiles: ["redis"]
    ports:
      - "${SIBYL_REDIS_PORT:-6381}:6379"
    command: ["valkey-server", "--save", "", "--appendonly", "no"]
```

The default image is pinned to SurrealDB server `v3.2.0` for reproducible local and CI behavior.
Override `SIBYL_SURREAL_IMAGE` when rehearsing a newer server patch. The root compose stores
SurrealDB data in a bind mount under `.moon/cache/surreal-dev` so it survives container churn and is
easy to inspect.

## Port Mappings

| Service   | Host Port | Container Port | Purpose                       |
| --------- | --------- | -------------- | ----------------------------- |
| SurrealDB | 8000      | 8000           | Default local graph runtime   |
| Redis     | 6381      | 6379           | Optional coordination backend |

Ports are offset from defaults to avoid conflicts with local services.

## Moonrepo Commands

```bash
# Start databases only
moon run docker-up

# Stop databases
moon run docker-down

# Start recommended Surreal local-dev stack
moon run dev

# Start API only with in-process jobs (no frontend)
moon run dev-api

# Start frontend only
moon run dev-web

# Start a standalone worker process (used with SIBYL_COORDINATION_BACKEND=redis)
moon run api:worker

# Stop all services
moon run stop
```

## Full Stack Compose

For a complete containerized deployment (backend + frontend + databases), use
`docker-compose.prod.yml`:

```bash
# Copy the production env-file example outside the repo
cp .env.example ~/.sibyl/prod.env

# Edit ~/.sibyl/prod.env with required secrets:
#   SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>
#   SIBYL_OPENAI_API_KEY=sk-...

# Start the repo production compose directly
docker compose --env-file ~/.sibyl/prod.env -f docker-compose.prod.yml up -d

# Or generate ~/.sibyl/docker/.env and a pinned compose file
sibyl docker init

# Optional: run a separate worker with Redis/Valkey coordination
sibyl docker init --force --with-worker

# Start, follow logs, and stop
sibyl docker up
sibyl docker logs
sibyl docker down
```

### Production Compose Services

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    ports:
      - "3334:3334"
    environment:
      SIBYL_SURREAL_URL: ws://surrealdb:8000/rpc
      SIBYL_JWT_SECRET: ${SIBYL_JWT_SECRET}
      SIBYL_OPENAI_API_KEY: ${SIBYL_OPENAI_API_KEY}
    depends_on:
      surrealdb:
        condition: service_healthy

  worker:
    profiles: ["redis"]
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    command: ["sibyld", "worker"]
    environment:
      SIBYL_COORDINATION_BACKEND: redis
      SIBYL_REDIS_HOST: redis
      SIBYL_REDIS_PORT: 6379
      SIBYL_SURREAL_URL: ws://surrealdb:8000/rpc
      SIBYL_JWT_SECRET: ${SIBYL_JWT_SECRET}
      SIBYL_OPENAI_API_KEY: ${SIBYL_OPENAI_API_KEY}
    depends_on:
      surrealdb:
        condition: service_healthy
      redis:
        condition: service_started

  redis:
    profiles: ["redis"]
    image: valkey/valkey:8-alpine
    ports:
      - "6381:6379"

  frontend:
    build:
      context: ./apps/web
      dockerfile: Dockerfile
    ports:
      - "3337:3337"
    environment:
      NEXT_PUBLIC_API_URL: ${SIBYL_SERVER_URL:-http://localhost:3334}
      SIBYL_API_URL: http://backend:3334/api
    depends_on:
      backend:
        condition: service_healthy

  surrealdb:
    image: ${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.0}
    ports:
      - "${SIBYL_SURREAL_PORT:-8000}:8000"
    volumes:
      - surreal_data:/data
    healthcheck:
      test: ["CMD", "/surreal", "is-ready", "--endpoint", "http://localhost:8000"]

volumes:
  surreal_data:
```

The production compose persists SurrealDB to a named Docker volume (`surreal_data`) rather than a
bind mount. `NEXT_PUBLIC_API_URL` is the browser-facing API URL; `SIBYL_API_URL` is the in-network
URL the Next.js server uses for SSR fetches.

## Quickstart Compose (Zero-Config)

For individual developers who want a running stack in minutes using pre-built images, use
`docker-compose.quickstart.yml`. No `.env` file is required; API keys are entered through the web UI
during onboarding and stored encrypted in the database.

```bash
# Start with pre-built images from ghcr.io
docker compose --env-file /dev/null -f docker-compose.quickstart.yml up -d

# Open http://localhost:3337 and complete onboarding

# Run alongside an existing dev setup on offset ports
docker compose --env-file /dev/null \
  -f docker-compose.quickstart.yml \
  -f docker-compose.quickstart.test.yml up -d
```

Differences from the production compose:

- Pulls `ghcr.io/hyperb1iss/sibyl-api` and `sibyl-web` images instead of building locally
- `SIBYL_JWT_SECRET` and `SIBYL_SETTINGS_KEY` auto-generate when unset (persisted in the
  `sibyl_secrets` volume mounted at `/home/sibyl/.sibyl`)
- Runs with `SIBYL_ENVIRONMENT=development` and a `sibyl_quickstart` default Surreal password
- The backend service is named `api` and the frontend `web`

When upgrading from a pre-1.0 RC quickstart compose file by hand, keep the `secrets-init` service
from the current compose file. It chowns the persisted `/home/sibyl/.sibyl` volume to the current
API service UID (`10001`) so older `1000:1000` volumes stay writable after the image upgrade. If you
intentionally pin an older API image, set `SIBYL_API_UID=1000` and `SIBYL_API_GID=1000` while that
older image is running.

## Volume Persistence

The root local-dev compose persists SurrealDB data in the bind mount configured by
`SURREAL_DATA_DIR`, defaulting to `.moon/cache/surreal-dev`.

```bash
# Inspect the default local Surreal data directory
ls .moon/cache/surreal-dev
```

The production and quickstart compose files persist SurrealDB to named Docker volumes
(`surreal_data` and `sibyl_surreal`):

```bash
docker volume ls | grep sibyl

# Remove volumes (DESTROYS DATA)
sibyl docker down --volumes
```

## Connecting to Databases

### SurrealDB Health

```bash
curl http://localhost:8000/health
```

### Redis CLI

```bash
# Only when the redis profile is running
docker exec -it sibyl-redis redis-cli
```

### Migration Archive Rehearsal

The default compose file no longer ships a PostgreSQL sidecar. Historical `postgres.sql` archive
rehearsal must point at an explicitly managed external database, then run the migration command with
`--restore-database-dump --source-type legacy-archive --target-mode postgres-rehearsal`.

## Troubleshooting

### Port Conflicts

If ports 8000 or 6381 are in use:

```bash
# Check what's using the port
lsof -i :8000
lsof -i :6381

# Stop conflicting services or modify docker-compose.yml ports
```

### Database Not Starting

```bash
# Check container logs
docker compose --env-file /dev/null logs surrealdb
docker compose --env-file /dev/null logs redis

# Restart with clean state
docker compose --env-file /dev/null down -v
docker compose --env-file /dev/null up -d surrealdb
```

### Connection Refused

Ensure your shell environment uses the correct ports:

```bash
SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
SIBYL_REDIS_PORT=6381        # Only when coordination backend is redis
```

## Next Steps

- [Environment Variables](environment.md) - Full configuration options
- [Troubleshooting](troubleshooting.md) - Common issues
