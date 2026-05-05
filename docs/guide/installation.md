---
title: Installation
description: Installing Sibyl and its dependencies
---

# Installation

This guide covers the two main ways to run Sibyl:

- install the published CLI and run a local instance with `sibyl local ...`
- work on the monorepo in development mode with `moon run ...`

## Prerequisites

Sibyl requires the following:

- **Python 3.13+** - Core backend language
- **Node.js 22+** - For the web frontend
- **Docker** - For local SurrealDB and optional dev services
- **OpenAI API Key** - For generating embeddings

### Version Management

We recommend using [proto](https://moonrepo.dev/proto) for managing tool versions:

```bash
# Install proto
curl -fsSL https://moonrepo.dev/install/proto.sh | bash

# Proto will automatically use correct versions from .prototools
```

## Quick Install

### Published CLI

The fastest way to run Sibyl locally:

```bash
# Install the published CLI
uv tool install sibyl-dev

# Start local services
sibyl local start

# Install Claude/Codex skills and hooks
sibyl local setup
```

### Monorepo Development

```bash
# Clone the repository
git clone https://github.com/hyperb1iss/sibyl.git
cd sibyl

# Bootstrap toolchain and dependencies
./setup-dev.sh
```

Or manually:

```bash
curl -fsSL https://moonrepo.dev/install/proto.sh | bash
proto use
proto install moon
uv sync --all-groups
pnpm install

# Optional: install repo-local CLI entrypoints into your user tool path
moon run cli:install-dev
moon run api:install-dev
```

## Infrastructure Setup

### Start Local SurrealDB

```bash
# Start the default local data service
moon run docker-up
```

For distributed or multi-process dev, opt into Redis explicitly:

```bash
docker compose --profile redis up -d surrealdb redis
```

Legacy FalkorDB/PostgreSQL services still exist behind the `legacy` profile when you need the older
runtime for migration or debugging work.

### Legacy PostgreSQL Setup

```bash
# Migrations run automatically during moon run dev,
# but you can also run them directly when needed
moon run api:db-migrate
```

## Configuration

### Environment Variables

Create a `.env` file from the repo root example:

```bash
cp .env.example .env
```

Edit the file with your configuration:

```bash
# Required
SIBYL_OPENAI_API_KEY=sk-...        # For embeddings
SIBYL_JWT_SECRET=your-secret-key   # For authentication

# Recommended local runtime
SIBYL_STORE=surreal
SIBYL_COORDINATION_BACKEND=local
SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD=root

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

| Variable                     | Default                  | Description                                 |
| ---------------------------- | ------------------------ | ------------------------------------------- |
| `SIBYL_STORE`                | `surreal`                | Persistence runtime (`surreal` or `legacy`) |
| `SIBYL_COORDINATION_BACKEND` | `auto`                   | `local` or `redis` coordination backend     |
| `SIBYL_SURREAL_URL`          | -                        | SurrealDB server URL                        |
| `SIBYL_LOG_LEVEL`            | `INFO`                   | Logging level                               |
| `SIBYL_EMBEDDING_MODEL`      | `text-embedding-3-small` | OpenAI embedding model                      |
| `SIBYL_SERVER_URL`           | -                        | Public server URL (for OAuth callbacks)     |
| `SIBYL_FRONTEND_URL`         | -                        | Frontend URL (for redirects)                |
| `SIBYL_REDIS_HOST`           | -                        | Redis/Valkey host when `coordination=redis` |
| `SIBYL_FALKORDB_HOST`        | `localhost`              | Legacy graph runtime host                   |
| `SIBYL_POSTGRES_HOST`        | `localhost`              | Legacy PostgreSQL host                      |

## Running Sibyl

### Local CLI Mode

Run the published CLI's local stack:

```bash
sibyl local start
sibyl local status
sibyl local logs
```

### Development Mode

Start the recommended Surreal local-dev stack:

```bash
moon run dev
```

This starts:

- API server on port 3334
- Web frontend on port 3337
- In-process background jobs and schedules

The legacy FalkorDB/PostgreSQL path remains available with `moon run dev-legacy`.

### Individual Services

```bash
# API server only
moon run dev-api

# Web frontend only
moon run dev-web

# Background worker (only when SIBYL_COORDINATION_BACKEND=redis)
moon run api:worker
```

### Direct Commands

```bash
# Start the API server
moon run api:serve

# Start in stdio mode (for MCP subprocess)
cd apps/api
uv run sibyld serve -t stdio
```

## Verify Installation

### Check Server Health

```bash
# If you installed the published CLI
sibyl local status
sibyl local setup --status

# Basic health check
curl http://localhost:3334/api/health
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
| SurrealDB    | 8000 |
| FalkorDB     | 6380 |
| PostgreSQL   | 5433 |

## Troubleshooting

### SurrealDB Connection Failed

```bash
# Check if SurrealDB is running
docker ps | grep surreal

# Check the port
curl http://localhost:8000/health
```

### Graph Corruption

If you encounter graph corruption errors:

```bash
# Nuclear option: delete the graph
redis-cli -p 6380
> GRAPH.DELETE <org-uuid>
```

### Legacy Graph / Migration Errors

```bash
# Start the legacy profile when you need the older graph and auth stack
docker compose --profile legacy up -d falkordb postgres
moon run dev-legacy
```

### OpenAI API Errors

Ensure your API key is set and has credits:

```bash
echo $SIBYL_OPENAI_API_KEY
```

## Docker Deployment

A production Docker Compose configuration is planned. For now, use the individual Docker commands
above.

### Legacy Runtime Notes

Sibyl's current product surface centers on the knowledge graph, tasks, search, and source ingestion.
If you are evaluating Sibyl today, you can ignore older experimental internal-runtime material.

## Next Steps

- [Quick Start](./quick-start.md) - 5-minute tutorial
- [MCP Configuration](./mcp-configuration.md) - Configure Claude Code integration
