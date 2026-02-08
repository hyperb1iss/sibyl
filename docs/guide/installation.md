---
title: Installation
description: Installing Sibyl and its dependencies
---

# Installation

This guide covers installing Sibyl for both local development and production use.

## Prerequisites

Sibyl requires the following:

- **Python 3.13+** - Core backend language
- **Node.js 22+** - For the web frontend
- **FalkorDB** - Graph database (runs on port 6380)
- **PostgreSQL** - Relational storage for users and documents
- **OpenAI API Key** - For generating embeddings

### Version Management

We recommend using [proto](https://moonrepo.dev/proto) for managing tool versions:

```bash
# Install proto
curl -fsSL https://moonrepo.dev/install/proto.sh | bash

# Proto will automatically use correct versions from .prototools
```

## Quick Install

### Using uv (Recommended)

The fastest way to install Sibyl for development:

```bash
# Clone the repository
git clone https://github.com/hyperb1iss/sibyl.git
cd sibyl

# Install Python dependencies
uv sync

# Install the CLI tool globally
uv tool install sibyl-cli

# Or install in development mode (editable)
moon run install-dev
```

### Install Individual Components

```bash
# Install just the CLI
uv tool install sibyl-cli

# Install sibyl-core library
uv add sibyl-core
```

## Infrastructure Setup

### Start FalkorDB

Sibyl uses FalkorDB (a Redis-compatible graph database) on port 6380:

```bash
# Using Docker
docker run -d \
  --name falkordb \
  -p 6380:6379 \
  -v falkordb-data:/data \
  falkordb/falkordb:latest

# Or using moon
moon run docker-up
```

::: warning Port 6380 FalkorDB runs on port **6380** (not 6379) to avoid conflicts with a standard
Redis installation. :::

### PostgreSQL Setup

PostgreSQL stores users, sessions, API keys, and crawled documents:

```bash
# Using Docker
docker run -d \
  --name sibyl-postgres \
  -e POSTGRES_USER=sibyl \
  -e POSTGRES_PASSWORD=sibyl \
  -e POSTGRES_DB=sibyl \
  -p 5432:5432 \
  postgres:16

# Run migrations
cd apps/api
uv run alembic upgrade head
```

## Configuration

### Environment Variables

Create a `.env` file in `apps/api/`:

```bash
cp apps/api/.env.example apps/api/.env
```

Edit the file with your configuration:

```bash
# Required
SIBYL_OPENAI_API_KEY=sk-...        # For embeddings
SIBYL_JWT_SECRET=your-secret-key   # For authentication

# FalkorDB
SIBYL_FALKORDB_HOST=localhost
SIBYL_FALKORDB_PORT=6380
SIBYL_FALKORDB_PASSWORD=conventions

# PostgreSQL
SIBYL_DATABASE_URL=postgresql+asyncpg://sibyl:sibyl@localhost:5432/sibyl

# Optional
SIBYL_LOG_LEVEL=INFO
SIBYL_EMBEDDING_MODEL=text-embedding-3-small
SIBYL_ANTHROPIC_API_KEY=...        # For LLM operations
```

### Required Environment Variables

| Variable               | Description                              |
| ---------------------- | ---------------------------------------- |
| `SIBYL_OPENAI_API_KEY` | OpenAI API key for generating embeddings |
| `SIBYL_JWT_SECRET`     | Secret key for JWT token signing         |

### Optional Environment Variables

| Variable                | Default                  | Description                             |
| ----------------------- | ------------------------ | --------------------------------------- |
| `SIBYL_FALKORDB_HOST`   | `localhost`              | FalkorDB hostname                       |
| `SIBYL_FALKORDB_PORT`   | `6380`                   | FalkorDB port                           |
| `SIBYL_DATABASE_URL`    | -                        | PostgreSQL connection string            |
| `SIBYL_LOG_LEVEL`       | `INFO`                   | Logging level                           |
| `SIBYL_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model                  |
| `SIBYL_SERVER_URL`      | -                        | Public server URL (for OAuth callbacks) |
| `SIBYL_FRONTEND_URL`    | -                        | Frontend URL (for redirects)            |

## Running Sibyl

### Development Mode

Start all services with a single command:

```bash
moon run dev
```

This starts:

- API server on port 3334
- Background worker for async jobs
- Web frontend on port 3337

### Individual Services

```bash
# API server only
moon run dev-api

# Web frontend only
moon run dev-web

# Background worker
cd apps/api && uv run arq sibyl.jobs.WorkerSettings
```

### Direct Commands

```bash
# Start the API server
cd apps/api
uv run sibyl-serve

# Start in stdio mode (for MCP subprocess)
uv run sibyl-serve -t stdio
```

## Verify Installation

### Check Server Health

```bash
curl http://localhost:3334/api/health
```

### Check CLI

```bash
sibyl health
sibyl version
```

### Access Web UI

Open [http://localhost:3337](http://localhost:3337) in your browser.

## Ports Reference

| Service      | Port |
| ------------ | ---- |
| API + MCP    | 3334 |
| Web Frontend | 3337 |
| FalkorDB     | 6380 |
| PostgreSQL   | 5432 |

## Troubleshooting

### FalkorDB Connection Failed

```bash
# Check if FalkorDB is running
docker ps | grep falkordb

# Check the port
redis-cli -p 6380 ping
```

### Graph Corruption

If you encounter graph corruption errors:

```bash
# Nuclear option: delete the graph
redis-cli -p 6380
> GRAPH.DELETE <org-uuid>
```

### Database Migration Errors

```bash
# Reset and re-run migrations
cd apps/api
uv run alembic downgrade base
uv run alembic upgrade head
```

### OpenAI API Errors

Ensure your API key is set and has credits:

```bash
echo $SIBYL_OPENAI_API_KEY
```

## Docker Deployment

[SCREENSHOT: Docker compose architecture diagram]

A production Docker Compose configuration is planned. For now, use the individual Docker commands
above.

## Sandbox Setup (Optional)

Sibyl's sandbox system provides isolated execution environments for AI agents. This is optional
for basic usage but required for distributed task execution.

### Enabling Sandbox Mode

Set the sandbox mode in your environment:

```bash
# Shadow mode — observe and log sandbox operations without enforcement
export SIBYL_SANDBOX_MODE=shadow

# Enforced mode — require sandbox for all task execution
export SIBYL_SANDBOX_MODE=enforced
```

### Runner Registration

Register a runner to execute tasks:

```bash
# Register via API
curl -X POST http://localhost:3334/api/runners/register \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name": "my-runner", "hostname": "dev-machine", "capabilities": ["docker"]}'
```

### Runner Daemon

Install and start the runner daemon:

```bash
# Install
moon run runner:install

# Start with connection to Sibyl API
sibyl-runner --server-url http://localhost:3334 --runner-id <runner-id>
```

### Sandbox Configuration

| Variable                             | Default                                   | Description                            |
| ------------------------------------ | ----------------------------------------- | -------------------------------------- |
| `SIBYL_SANDBOX_MODE`                 | `off`                                     | Sandbox policy: off, shadow, enforced  |
| `SIBYL_SANDBOX_DEFAULT_IMAGE`        | `ghcr.io/hyperb1iss/sibyl-sandbox:latest` | Default container image                |
| `SIBYL_SANDBOX_IDLE_TTL_SECONDS`     | `1800`                                    | Auto-suspend after idle (seconds)      |
| `SIBYL_SANDBOX_MAX_LIFETIME_SECONDS` | `14400`                                   | Maximum sandbox lifetime (seconds)     |
| `SIBYL_SANDBOX_K8S_NAMESPACE`        | `default`                                 | Kubernetes namespace for pods          |
| `SIBYL_SANDBOX_RECONCILE_ENABLED`    | `true`                                    | Enable reconciliation loop             |

For detailed architecture, see [Sandbox Architecture](../design/sandbox-architecture.md).

## Next Steps

- [Quick Start](./quick-start.md) - 5-minute tutorial
- [MCP Configuration](./mcp-configuration.md) - Configure Claude Code integration
