---
title: Installation
description: Installing Sibyl and its dependencies
---

# Installation

Connecting to a Sibyl server someone already runs takes one line. The rest of this page covers
running a server yourself and working on the monorepo.

## Connect to a Sibyl Server

Install the CLI, then run [`sibyl setup`](../cli/setup.md) with your server's URL. The web app's
Connect card shows the same line with your server's URL filled in:

```bash
# macOS
brew install hyperb1iss/tap/sibyl && sibyl setup https://sibyl.example.com

# Linux
uv tool install --upgrade sibyl-dev && sibyl setup https://sibyl.example.com

# Windows (PowerShell)
uv tool install --upgrade sibyl-dev; sibyl setup 'https://sibyl.example.com'

# No uv yet: the installer bootstraps it, then runs sibyl setup
curl -fsSL https://raw.githubusercontent.com/hyperb1iss/sibyl/main/install.sh | sh -s -- --remote https://sibyl.example.com
```

`sibyl setup` signs you in through the browser, installs the Sibyl skill for Claude Code, Codex, and
other agents, and adds the Claude Code SessionStart hook. Keep `--upgrade` on the uv line: a plain
`uv tool install` leaves an older CLI in place. Connecting needs no provider API keys and no MCP
config; the server's operator configures the models once.

To let an agent do it instead, give it this sentence:

```text
Set up Sibyl on this machine by following https://sibyl.example.com/agent
```

Copy the exact sentence from the web app's Connect card: `/agent` is served by the web app, so on a
deployment where the web app and API have separate origins the card points at `/api/setup/agent.md`
on the API instead. See [Hand It To An Agent](../cli/setup.md#hand-it-to-an-agent).

## Run a Server

This covers the ways to run Sibyl yourself:

- run the shell installer and start the local server UI
- install the Homebrew formula on systems where you already use Homebrew
- run a self-hosted Docker stack with `sibyl docker ...`
- work on the monorepo in development mode with `moon run ...`

## Prerequisites

Running Sibyl from source requires:

- **Python 3.13+** - Core backend language
- **Node.js 24** - For the web frontend
- **Docker** - For local SurrealDB and optional dev services
- **A language model** - Anthropic, OpenAI, Gemini, or Claude through Amazon Bedrock, for entity
  extraction and synthesis
- **An embedding provider** - OpenAI, Gemini, or Cohere Embed v4 through Amazon Bedrock

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

When the setup wizard finishes, run `sibyl setup` to connect this machine. Without a URL it connects
the server the CLI already talks to, `http://localhost:3334` on a fresh install.

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

The uv install (`uv tool install --upgrade sibyl-dev`) puts only the `sibyl` CLI on a machine, which
is all a machine that connects to a server needs. To run a server, go through the shell installer,
Homebrew, or Docker flow so the CLI and daemon stay paired; installing `sibyld` directly is a
developer and CI escape hatch.

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

The default compose file starts only SurrealDB, plus Redis when that profile is requested.

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

| Variable               | Description                                               |
| ---------------------- | --------------------------------------------------------- |
| `SIBYL_OPENAI_API_KEY` | OpenAI key for the default embedding provider (see below) |
| `SIBYL_JWT_SECRET`     | Secret key for production JWT signing                     |

Gemini (`SIBYL_GEMINI_API_KEY`) and Amazon Bedrock also serve embeddings, and Bedrock needs no API
key. See [Embeddings](../deployment/environment.md#embeddings) and
[Amazon Bedrock](../deployment/environment.md#amazon-bedrock).

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

To restore an archive exported from another Sibyl instance, run
`sibyld migrate import <archive> --source-type surreal-archive --target-mode surreal`.

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

### Archive Restore Errors

Restore accepts only Sibyl's own archives (from `sibyld migrate export`, `merge`, or `consolidate`)
and API backups. Validate the archive before restoring it:

```bash
sibyld migrate check <archive>
sibyld migrate import <archive> --source-type surreal-archive --target-mode surreal
```

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
sibyl docker upgrade --tag 1.4.1
```

When `--tag` is provided, `upgrade` updates the generated `.env` and the pinned Sibyl image
references in `~/.sibyl/docker/docker-compose.yml`.

## Next Steps

- [Quick Start](./quick-start.md) - 5-minute tutorial
- [`sibyl setup`](../cli/setup.md) - Connect a machine and its agents
- [MCP Configuration](./mcp-configuration.md) - Configure MCP-only clients
