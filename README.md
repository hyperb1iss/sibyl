# Sibyl - Oracle of Development Wisdom

A Graphiti-powered MCP server that provides AI agents access to development conventions, patterns, templates, and hard-won wisdom.

## Overview

Sibyl indexes and serves knowledge from the conventions repository:
- **Wisdom docs** - Hard-won lessons and debugging victories
- **Language guides** - TypeScript, Python, Rust, Swift conventions
- **Templates** - Project scaffolds, configs, and code patterns
- **Sacred rules** - Invariants that must never be violated
- **Slash commands** - Claude Code skills and automations

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- Docker (for FalkorDB)
- OpenAI API key (for embeddings)

### Setup

```bash
# Clone and enter directory
cd mcp-server

# Start FalkorDB
docker compose up -d

# Install dependencies
uv sync --all-extras

# Configure environment
cp .env.example .env
# Edit .env with your OpenAI API key

# Check setup
uv run sibyl setup

# Run initial ingestion
uv run sibyl ingest

# Start the server daemon
uv run sibyl serve
```

### Claude Code Integration

Add to your Claude Code MCP configuration (`~/.config/claude/mcp.json` or project `.claude/mcp.json`):

```json
{
  "mcpServers": {
    "sibyl": {
      "type": "http",
      "url": "http://localhost:3334/mcp"
    }
  }
}
```

Or for subprocess mode:

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/path/to/conventions/mcp-server", "run", "sibyl", "serve", "-t", "stdio"],
      "env": {
        "SIBYL_OPENAI_API_KEY": "your-api-key"
      }
    }
  }
}
```

## CLI Commands

```bash
sibyl serve       # Start MCP server daemon (default: localhost:3334)
sibyl serve -t stdio  # Start in subprocess mode
sibyl setup       # Check environment and guide first-time setup
sibyl ingest      # Ingest wisdom documents into the knowledge graph
sibyl search "query"  # Search the knowledge graph
sibyl health      # Check server health status
sibyl stats       # Show knowledge graph statistics
sibyl config      # Show current configuration
sibyl version     # Show version information
```

## Available Tools

### Search Tools

**`search_wisdom`** - Semantic search across all development wisdom
- `query` (required): Natural language search query
- `topic`: Optional topic filter
- `language`: Filter by programming language
- `limit`: Max results (default: 10)
- `include_sacred_rules`: Include rules in results (default: true)

**`search_patterns`** - Find specific coding patterns
- `query` (required): Search query
- `category`: Category filter (e.g., "error-handling", "testing")
- `language`: Language filter
- `limit`: Max results (default: 10)
- `detail_level`: "summary" or "full"

**`find_solution`** - Find solutions for problems/errors
- `problem` (required): Problem or error description
- `context`: Additional context
- `language`: Language filter
- `limit`: Max results (default: 5)

**`search_templates`** - Search code and config templates
- `query` (required): Search query
- `template_type`: Type filter (code, config, project, workflow)
- `language`: Language filter
- `limit`: Max results (default: 10)

### Lookup Tools

**`get_sacred_rules`** - Get all sacred rules for a category/language
- `category`: Category filter (development, type_safety, database, infrastructure, git)
- `language`: Language filter
- `severity`: Severity filter (error, warning, info)

**`get_language_guide`** - Get complete conventions for a language
- `language` (required): Language name (python, typescript, rust, swift)
- `section`: Specific section (tooling, patterns, style, testing, all)

**`get_template`** - Retrieve a specific template
- `name` (required): Template name or identifier
- `template_type`: Type filter
- `language`: Language filter

### Discovery Tools

**`list_patterns`** - List all available patterns
- `category`: Category filter
- `language`: Language filter
- `limit`: Max results (default: 50)

**`list_templates`** - List all available templates
- `template_type`: Type filter
- `language`: Language filter
- `limit`: Max results (default: 50)

**`list_rules`** - List all sacred rules
- `severity`: Severity filter
- `language`: Language filter
- `limit`: Max results (default: 50)

**`list_topics`** - List all knowledge topics
- `parent`: Parent topic filter
- `limit`: Max results (default: 50)

**`get_related`** - Find related entities via graph traversal
- `entity_id` (required): ID of the entity
- `relationship_types`: Filter by relationship types
- `depth`: Traversal depth 1-3 (default: 1)
- `limit`: Max results (default: 20)

### Mutation Tools

**`add_learning`** - Add new wisdom to the graph
- `title` (required): Title of the learning
- `content` (required): Detailed content
- `category` (required): Category (debugging, architecture, performance)
- `languages`: Applicable programming languages
- `related_to`: IDs of related entities
- `source`: Source of the learning

**`record_debugging_victory`** - Record a debugging win
- `problem` (required): The problem encountered
- `root_cause` (required): Root cause discovered
- `solution` (required): How it was solved
- `prevention`: How to prevent in future
- `languages`: Languages involved
- `tools`: Tools involved
- `time_spent`: Time spent debugging

### Admin Tools

**`health_check`** - Check server health and return status

**`sync_wisdom_docs`** - Re-ingest wisdom documentation from files
- `path`: Specific path to sync
- `force`: Force re-process all files

**`rebuild_indices`** - Rebuild graph indices for better query performance
- `index_type`: Type to rebuild (search, relationships, all)

**`get_stats`** - Get detailed statistics about the knowledge graph

## Configuration

All settings can be configured via environment variables (prefix: `SIBYL_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `SIBYL_SERVER_NAME` | `sibyl` | MCP server name |
| `SIBYL_SERVER_HOST` | `localhost` | Server bind host |
| `SIBYL_SERVER_PORT` | `3334` | Server bind port |
| `SIBYL_LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `SIBYL_FALKORDB_HOST` | `localhost` | FalkorDB host |
| `SIBYL_FALKORDB_PORT` | `6380` | FalkorDB port |
| `SIBYL_FALKORDB_PASSWORD` | `conventions` | FalkorDB password |
| `SIBYL_FALKORDB_GRAPH_NAME` | `conventions` | Graph name in FalkorDB |
| `SIBYL_OPENAI_API_KEY` | | OpenAI API key for embeddings |
| `SIBYL_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `SIBYL_WISDOM_PATH` | `docs/wisdom` | Path to wisdom documentation |
| `SIBYL_TEMPLATES_PATH` | `templates` | Path to templates directory |
| `SIBYL_CONFIGS_PATH` | `configs` | Path to config templates |

## Development

```bash
# Install dev dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=src --cov-report=html

# Type checking
uv run mypy src

# Linting
uv run ruff check src tests

# Format
uv run ruff format src tests

# Pre-commit hooks
uv run pre-commit install
uv run pre-commit run --all-files
```

## Architecture

```
src/sibyl/
├── main.py           # Entry point
├── server.py         # MCP server definition
├── config.py         # Settings management
├── errors.py         # Custom exceptions
├── models/           # Pydantic models
│   ├── entities.py   # Entity definitions
│   ├── tools.py      # Tool input schemas
│   └── responses.py  # Response schemas
├── graph/            # Knowledge graph operations
│   ├── client.py     # Graphiti client wrapper
│   ├── entities.py   # Entity CRUD
│   └── relationships.py
├── tools/            # MCP tool implementations
│   ├── search.py     # Semantic search tools
│   ├── lookup.py     # Direct access tools
│   ├── discovery.py  # Listing and exploration
│   ├── mutation.py   # Add new knowledge
│   └── admin.py      # Maintenance tools
├── ingestion/        # Content ingestion pipeline
│   ├── parser.py     # Markdown parsing
│   ├── chunker.py    # Semantic chunking
│   ├── extractor.py  # Entity extraction
│   ├── relationships.py # Relationship building
│   ├── cataloger.py  # Template/config cataloging
│   └── pipeline.py   # Main ingestion pipeline
└── utils/            # Utilities
    └── resilience.py # Retry and timeout utilities
```

## Entity Types

The knowledge graph uses these entity types:

- **Pattern** - Coding patterns and best practices
- **Rule** - Sacred rules and invariants
- **Template** - Code and config templates
- **Tool** - Development tools and utilities
- **Language** - Programming language conventions
- **Topic** - Knowledge organization topics
- **Episode** - Learning episodes and discoveries
- **KnowledgeSource** - Source documents
- **ConfigFile** - Configuration file templates
- **SlashCommand** - Claude Code slash commands

## Relationship Types

Entities are connected by these relationship types:

- `APPLIES_TO` - Pattern/rule applies to topic/language
- `REQUIRES` - Entity requires another
- `CONFLICTS_WITH` - Mutually exclusive entities
- `SUPERSEDES` - Newer version replaces older
- `DOCUMENTED_IN` - Entity documented in source
- `ENABLES` - Entity enables functionality
- `BREAKS` - Entity can break another (anti-patterns)

## Troubleshooting

### FalkorDB Connection Issues

```bash
# Check if FalkorDB is running
docker compose ps

# View FalkorDB logs
docker compose logs falkordb

# Restart FalkorDB
docker compose restart falkordb
```

### Embedding Errors

Ensure your OpenAI API key is set:
```bash
export SIBYL_OPENAI_API_KEY="sk-..."
```

### Search Returns Empty Results

1. Run initial ingestion: `sibyl ingest`
2. Check health: `sibyl health`
3. Verify entity counts with `sibyl stats`

### Performance Issues

1. Rebuild indices: Use `rebuild_indices` tool with `index_type="all"`
2. Check FalkorDB memory: `docker stats`
3. Reduce search limits for faster responses

## License

Apache-2.0
