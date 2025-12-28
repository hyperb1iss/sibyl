# sibyl-cli

Command-line interface for Sibyl knowledge graph.

## Overview

This package provides the client-side CLI for interacting with a Sibyl server.
All commands communicate via REST API - no direct database access required.

## Installation

```bash
# As a dependency
uv add sibyl-cli

# For development
uv pip install -e packages/python/sibyl-cli
```

## Commands

### Core Commands

```bash
sibyl health              # Check server health
sibyl search "query"      # Search knowledge graph
sibyl add "title" "content"  # Add knowledge
sibyl stats               # Show statistics
sibyl version             # Show version
```

### Subcommand Groups

- `sibyl task` - Task lifecycle management
- `sibyl epic` - Epic/feature grouping
- `sibyl project` - Project operations
- `sibyl entity` - Generic entity CRUD
- `sibyl explore` - Graph traversal
- `sibyl source` - Documentation sources
- `sibyl crawl` - Web crawling
- `sibyl auth` - Authentication
- `sibyl org` - Organization management
- `sibyl config` - Configuration
- `sibyl context` - Project context

## Configuration

The CLI uses `~/.sibyl/config.toml` for configuration:

```toml
[server]
url = "http://localhost:3334/api"

[paths]
"/home/user/project" = "project_abc123"
```

## Environment Variables

- `SIBYL_API_URL` - Server URL (default: http://localhost:3334/api)
- `SIBYL_CONTEXT` - Override project context

## Server Commands

Server-side commands (serve, dev, db, generate, etc.) are available
in the `sibyl-server` package (apps/api).
