---
title: Installation
description: Installing Sibyl and its dependencies
---

# Installation

This guide covers the main ways to run Sibyl:

- run the shell installer and start the local server UI
- install the Homebrew formula on systems where you already use Homebrew
- run a self-hosted Docker stack with `sibyl docker ...`
- work on the monorepo in development mode with `moon run ...`

## Prerequisites

Sibyl requires the following for development:

- **Python 3.13+** - Core backend language
- **Node.js 24** - For the web frontend
- **Docker** - For local SurrealDB and optional dev services
- **Anthropic API Key** - For LLM entity extraction and synthesis
- **OpenAI or Gemini API Key** - For generating embeddings

### Version Management

We recommend using [proto](https://moonrepo.dev/proto) for managing tool versions:

```bash
# Install proto
curl -fsSL https://moonrepo.dev/install/proto.sh | bash

# Proto will automatically use correct versions from .prototools
```

## Quick Install

### Local Server

The recommended local install starts the API, web UI, and SurrealDB, then opens the setup UI when it
is ready:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh
```

Homebrew installs the CLI and daemon. Start the full local server UI with one command:

```bash
brew install hyperb1iss/tap/sibyl
sibyl up
```

### Remote CLI

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --remote
sibyl init --remote https://sibyl.example.com
sibyl auth login
sibyl doctor
```

### Docker Self-Host

```bash
sibyl up --no-browser
```

For lower-level compose management:

```bash
sibyl docker init
sibyl docker up
sibyl docker logs
```

### Monorepo Development

```bash
# Clone the repository
git clone https://github.com/hyperb1iss/sibyl.git
cd sibyl

# Bootstrap toolchain and dependencies
./setup-dev.sh              # macOS / Linux
pwsh -File .\setup-dev.ps1  # Windows (PowerShell 7+)
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

Direct `uv tool install` commands are developer and CI escape hatches. User-facing installs should
go through the shell installer, Homebrew, or Docker flow so the CLI and daemon stay paired.

## Infrastructure Setup

### Start Local SurrealDB

```bash
# Start the default local data service
moon run docker-up
```

For distributed or multi-process dev, opt into Redis explicitly:

```bash
docker compose --env-file /dev/null --profile redis up -d surrealdb redis
```

Historical `postgres.sql` archive rehearsal now uses an explicitly managed external PostgreSQL
database. The default compose file starts only SurrealDB, plus Redis when that profile is requested.

## Configuration

### Environment Variables

Export runtime settings in your shell:

```bash
export SIBYL_OPENAI_API_KEY=sk-...        # For embeddings (OpenAI provider)
# export SIBYL_GEMINI_API_KEY=...         # Alternative embedding provider (Gemini)
# SIBYL_JWT_SECRET is auto-generated in dev.

# Recommended local runtime
export SIBYL_STORE=surreal
export SIBYL_COORDINATION_BACKEND=local
export SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
export SIBYL_SURREAL_USERNAME=root
export SIBYL_SURREAL_PASSWORD=root

# Optional
export SIBYL_LOG_LEVEL=INFO
export SIBYL_EMBEDDING_MODEL=text-embedding-3-small
export SIBYL_ANTHROPIC_API_KEY=...        # For LLM entity extraction and synthesis
```

### Required Environment Variables

| Variable               | Description                                                          |
| ---------------------- | -------------------------------------------------------------------- |
| `SIBYL_OPENAI_API_KEY` | OpenAI API key for embeddings (or `SIBYL_GEMINI_API_KEY` for Gemini) |
| `SIBYL_JWT_SECRET`     | Secret key for production JWT signing                                |

### Optional Environment Variables

| Variable                     | Default                  | Description                                  |
| ---------------------------- | ------------------------ | -------------------------------------------- |
| `SIBYL_STORE`                | `surreal`                | Active persistence runtime                   |
| `SIBYL_COORDINATION_BACKEND` | `auto`                   | `local` or `redis` coordination backend      |
| `SIBYL_SURREAL_URL`          | -                        | SurrealDB server URL                         |
| `SIBYL_LOG_LEVEL`            | `INFO`                   | Logging level                                |
| `SIBYL_EMBEDDING_MODEL`      | `text-embedding-3-small` | OpenAI embedding model                       |
| `SIBYL_PUBLIC_URL`           | `http://localhost:3337`  | Public base URL (OAuth callbacks, redirects) |
| `SIBYL_SERVER_URL`           | derived                  | Override API base URL (defaults to public)   |
| `SIBYL_FRONTEND_URL`         | derived                  | Override frontend URL (defaults to public)   |
| `SIBYL_REDIS_HOST`           | `127.0.0.1`              | Redis/Valkey host when `coordination=redis`  |

## Running Sibyl

### Local Host Mode

The embedded daemon path is available when you want API-only local memory without the web UI:

```bash
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --daemon
```

Or run it manually:

```bash
sibyl init --local
sibyl serve
sibyl doctor
```

Use `sibyl start`/`sibyl stop` for a background daemon. To install a native user-service file
without starting it automatically, run `sibyl service install`.

### Development Mode

Start the recommended Surreal local-dev stack:

```bash
moon run dev
```

This starts:

- API server on port 3334
- Web frontend on port 3337
- In-process background jobs and schedules

The local FalkorDB/PostgreSQL dev fallback was retired after the v0.6.0 compatibility release. Use
`sibyld migrate import <archive> --source-type surreal-archive --target-mode surreal` to restore an
exported Surreal-native archive.

> **Note (2026-07):** The `--source-type legacy-archive` on-ramp for historical FalkorDB/PostgreSQL
> migration archives was removed in the v0.6–v1.0 line (v0.10.0 was the last release that shipped
> it). If you still hold a legacy archive, see
> [Migrating from FalkorDB](./migrating-from-falkor.md).

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
sibyld serve -t stdio
```

## Verify Installation

### Check Server Health

```bash
# If you installed the published CLI
sibyl doctor

# Basic health check
curl http://localhost:3334/api/health
sibyl health
sibyl version
sibyld --version
```

### Access Web UI

Open [http://localhost:3337](http://localhost:3337) in your browser.

## Ports Reference

| Service      | Port |
| ------------ | ---- |
| API + MCP    | 3334 |
| Web Frontend | 3337 |
| SurrealDB    | 8000 |

## Troubleshooting

### SurrealDB Connection Failed

```bash
# Check if SurrealDB is running
docker ps | grep surreal

# Check the port
curl http://localhost:8000/health
```

### Local Graph Reset

If a disposable local graph gets corrupted, stop Sibyl and remove the local SurrealDB data directory
or reset the affected org namespace from SurrealQL.

```surql
REMOVE NAMESPACE org_<uuid_hex>;
```

### Legacy Graph / Migration Errors

Restore a retained Surreal-native archive with
`sibyld migrate import <archive> --source-type surreal-archive --target-mode surreal`.

> **Note (2026-07):** The historical `--source-type legacy-archive`, `--restore-database-dump`, and
> `--target-mode postgres-rehearsal` flags were removed in the v0.6–v1.0 line and no longer exist.
> Legacy archives must be imported from a release that still carries the on-ramp; see
> [Migrating from FalkorDB](./migrating-from-falkor.md).

### OpenAI API Errors

Ensure your API key is set and has credits:

```bash
echo $SIBYL_OPENAI_API_KEY
```

## Docker Deployment

The CLI writes a pinned compose bundle and generated secrets under `~/.sibyl/docker`:

```bash
sibyl docker init --with-worker
sibyl docker up --pull
sibyl docker upgrade --tag 1.1.0
```

When `--tag` is provided, `upgrade` updates the generated `.env` and the pinned Sibyl image
references in `~/.sibyl/docker/docker-compose.yml`.

### Legacy Runtime Notes

Sibyl's current product surface centers on the knowledge graph, tasks, search, and source ingestion.
If you are evaluating Sibyl today, you can ignore older experimental internal-runtime material.

## Next Steps

- [Quick Start](./quick-start.md) - 5-minute tutorial
- [MCP Configuration](./mcp-configuration.md) - Configure Claude Code integration
