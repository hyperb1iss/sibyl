# Docker Compose Deployment

Local development using Docker Compose for database services with the Python/Node applications
running natively.

## Architecture

Docker Compose runs the database services while applications run natively for hot reload:

```
+------------------+     +------------------+
|   Native Apps    |     |   Docker Compose |
|------------------|     |------------------|
| Backend (:3334)  |---->| FalkorDB (:6380) |
| Frontend (:3337) |     | Postgres (:5433) |
| Worker           |     +------------------+
+------------------+
```

## Prerequisites

- Docker and Docker Compose
- Python 3.13+
- Node.js 20+ and pnpm
- uv (Python package manager)

## Quick Start

```bash
# 1. Start database services
docker compose up -d

# 2. Install dependencies
uv sync                         # Python packages
cd apps/web && pnpm install     # Frontend packages

# 3. Configure environment
cp apps/api/.env.example apps/api/.env
# Edit .env and add:
#   SIBYL_JWT_SECRET=<random-secret>
#   SIBYL_OPENAI_API_KEY=sk-...

# 4. Run migrations
cd apps/api && uv run alembic upgrade head

# 5. Start all services
moon run dev
```

## Service Definitions

The `docker-compose.yml` defines database services:

```yaml
services:
  falkordb:
    image: falkordb/falkordb:latest
    container_name: sibyl-falkordb
    ports:
      - "6380:6379" # Redis port (mapped to avoid conflicts)
      - "3335:3000" # FalkorDB Browser UI
    volumes:
      - falkordb_data:/var/lib/falkordb/data
    environment:
      - FALKORDB_ARGS=--requirepass ${SIBYL_FALKORDB_PASSWORD:-conventions}
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${SIBYL_FALKORDB_PASSWORD:-conventions}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  postgres:
    image: pgvector/pgvector:pg18
    container_name: sibyl-postgres
    ports:
      - "5433:5432" # Mapped to avoid conflicts
    volumes:
      - postgres_data:/var/lib/postgresql
    environment:
      POSTGRES_USER: ${SIBYL_POSTGRES_USER:-sibyl}
      POSTGRES_PASSWORD: ${SIBYL_POSTGRES_PASSWORD:-sibyl_dev}
      POSTGRES_DB: ${SIBYL_POSTGRES_DB:-sibyl}

volumes:
  falkordb_data:
  postgres_data:
```

## Port Mappings

| Service     | Host Port | Container Port | Purpose           |
| ----------- | --------- | -------------- | ----------------- |
| FalkorDB    | 6380      | 6379           | Graph database    |
| FalkorDB UI | 3335      | 3000           | Browser interface |
| PostgreSQL  | 5433      | 5432           | Relational data   |

Ports are offset from defaults to avoid conflicts with local services.

## Moonrepo Commands

```bash
# Start databases only
moon run docker-up

# Stop databases
moon run docker-down

# Start full development stack
moon run dev

# Start API + Worker only (no frontend)
moon run dev-api

# Start frontend only
moon run dev-web

# Stop all services
moon run stop
```

## Full Stack Compose

For a complete containerized deployment (backend + frontend + databases), use
`docker-compose.prod.yml`:

```bash
# Copy environment file
cp apps/api/.env.example .env

# Edit .env with required secrets:
#   SIBYL_JWT_SECRET=<generate with: openssl rand -hex 32>
#   SIBYL_OPENAI_API_KEY=sk-...

# Start all services
docker compose -f docker-compose.prod.yml up -d

# View logs
docker compose -f docker-compose.prod.yml logs -f

# Stop all services
docker compose -f docker-compose.prod.yml down
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
      SIBYL_POSTGRES_HOST: postgres
      SIBYL_POSTGRES_PORT: 5432
      SIBYL_POSTGRES_USER: ${SIBYL_POSTGRES_USER:-sibyl}
      SIBYL_POSTGRES_PASSWORD: ${SIBYL_POSTGRES_PASSWORD:-sibyl_dev}
      SIBYL_POSTGRES_DB: ${SIBYL_POSTGRES_DB:-sibyl}
      SIBYL_FALKORDB_HOST: falkordb
      SIBYL_FALKORDB_PORT: 6379
      SIBYL_JWT_SECRET: ${SIBYL_JWT_SECRET}
      SIBYL_OPENAI_API_KEY: ${SIBYL_OPENAI_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      falkordb:
        condition: service_healthy

  worker:
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    command: ["sibyld", "worker"]
    environment:
      SIBYL_POSTGRES_HOST: postgres
      SIBYL_POSTGRES_PORT: 5432
      SIBYL_POSTGRES_USER: ${SIBYL_POSTGRES_USER:-sibyl}
      SIBYL_POSTGRES_PASSWORD: ${SIBYL_POSTGRES_PASSWORD:-sibyl_dev}
      SIBYL_POSTGRES_DB: ${SIBYL_POSTGRES_DB:-sibyl}
      SIBYL_FALKORDB_HOST: falkordb
      SIBYL_FALKORDB_PORT: 6379
      SIBYL_JWT_SECRET: ${SIBYL_JWT_SECRET}
      SIBYL_OPENAI_API_KEY: ${SIBYL_OPENAI_API_KEY}
    depends_on:
      postgres:
        condition: service_healthy
      falkordb:
        condition: service_healthy

  frontend:
    build:
      context: ./apps/web
      dockerfile: Dockerfile
    ports:
      - "3337:3337"
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:3334
    depends_on:
      backend:
        condition: service_healthy

  # ... databases as above
```

## Volume Persistence

Data is persisted in Docker volumes:

```bash
# List volumes
docker volume ls | grep sibyl

# Inspect volume
docker volume inspect sibyl_falkordb_data

# Remove volumes (DESTROYS DATA)
docker compose down -v
```

## Connecting to Databases

### FalkorDB CLI

```bash
# Connect via Docker
docker exec -it sibyl-falkordb redis-cli -a conventions

# Or from host (requires redis-cli installed)
redis-cli -h localhost -p 6380 -a conventions
```

### PostgreSQL

```bash
# Connect via Docker
docker exec -it sibyl-postgres psql -U sibyl sibyl

# Or from host (requires psql installed)
psql -h localhost -p 5433 -U sibyl sibyl
```

### FalkorDB Browser UI

Open http://localhost:3335 in your browser.

## Troubleshooting

### Port Conflicts

If ports 6380 or 5433 are in use:

```bash
# Check what's using the port
lsof -i :6380
lsof -i :5433

# Stop conflicting services or modify docker-compose.yml ports
```

### Database Not Starting

```bash
# Check container logs
docker compose logs falkordb
docker compose logs postgres

# Restart with clean state
docker compose down -v
docker compose up -d
```

### Connection Refused

Ensure your `.env` uses the correct ports:

```bash
SIBYL_FALKORDB_PORT=6380  # Not 6379!
SIBYL_POSTGRES_PORT=5433  # Not 5432!
```

## Next Steps

- [Environment Variables](environment.md) - Full configuration options
- [Troubleshooting](troubleshooting.md) - Common issues
