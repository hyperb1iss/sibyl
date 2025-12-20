<h1 align="center">
  Sibyl
</h1>

<p align="center">
  <strong>Graph-RAG Knowledge Oracle for AI Agents</strong>
</p>

<p align="center">
  <a href="https://github.com/hyperb1iss/sibyl/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/hyperb1iss/sibyl/ci.yml?branch=main&style=for-the-badge&logo=github&logoColor=white&label=CI" alt="CI Status">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/releases">
    <img src="https://img.shields.io/github/v/release/hyperb1iss/sibyl?style=for-the-badge&logo=github&logoColor=white" alt="Latest Release">
  </a>
  <a href="https://github.com/hyperb1iss/sibyl/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/hyperb1iss/sibyl?style=for-the-badge&logo=apache&logoColor=white" alt="License">
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/sibyl">
    <img src="https://img.shields.io/pypi/v/sibyl?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI">
  </a>
  <a href="https://pypi.org/project/sibyl">
    <img src="https://img.shields.io/pypi/pyversions/sibyl?style=for-the-badge&logo=python&logoColor=white" alt="Python Version">
  </a>
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#the-4-tool-architecture">Architecture</a> •
  <a href="#installation">Installation</a> •
  <a href="#documentation">Documentation</a>
</p>

---

A Graphiti-powered MCP server that transforms scattered development wisdom into a queryable knowledge graph. Built for AI agents that need contextual, interconnected knowledge—not just documents.

## Features

- **Unified Entity Model** — Patterns, rules, tasks, projects, and crawled documentation are all nodes in a single graph
- **Semantic Discovery** — Find knowledge by meaning across all entity types
- **Graph Traversal** — Navigate relationships to discover hidden connections
- **Temporal Memory** — Graphiti-style episodic knowledge with versioning
- **Auto-Linking** — New knowledge automatically connects to related entities
- **Task Intelligence** — Workflow state machine with learning capture

## The 4-Tool Architecture

Sibyl exposes exactly 4 MCP tools. Agents auto-discover capabilities through rich descriptions:

| Tool | Purpose | Use For |
|------|---------|---------|
| `search` | Semantic discovery | Find knowledge by meaning across all entity types |
| `explore` | Graph navigation | List, traverse, and discover relationships |
| `add` | Knowledge creation | Create entities with auto-discovered links |
| `manage` | Lifecycle & admin | Task workflows, crawling, health checks |

### search

Find knowledge by meaning across all entity types:

```python
# Find authentication patterns
search("OAuth 2.0 implementation", types=["pattern", "template"])

# Find open tasks
search("", types=["task"], status="doing", assignee="alice")

# Search crawled documentation
search("hooks state management", source="react-docs")
```

### explore

Navigate the knowledge graph structure:

```python
# List all projects
explore(mode="list", types=["project"])

# Find knowledge related to an entity
explore(mode="related", entity_id="pattern_oauth")

# Task dependency chains
explore(mode="dependencies", entity_id="task_abc")
```

### add

Create new knowledge with auto-discovered relationships:

```python
# Record a learning (creates episode)
add("Redis connection pooling insight",
    "Discovered that connection pool needs...",
    category="debugging", technologies=["redis", "python"])

# Create a task
add("Implement OAuth login", "Add Google and GitHub OAuth...",
    entity_type="task", project="proj_auth", priority="high")
```

### manage

Handle workflows and administrative operations:

```python
# Task workflow
manage("start_task", entity_id="task_abc")
manage("block_task", entity_id="task_abc", data={"reason": "Waiting on API keys"})
manage("unblock_task", entity_id="task_abc")
manage("submit_review", entity_id="task_abc", data={"pr_url": "github.com/..."})
manage("complete_task", entity_id="task_abc", data={
    "actual_hours": 4.5,
    "learnings": "OAuth redirect URIs must match exactly..."
})

# Documentation crawling
manage("crawl", data={"url": "https://vercel.com/docs", "depth": 3})
manage("sync", entity_id="source_abc")  # Re-crawl existing source

# Analysis
manage("estimate", entity_id="task_abc")  # Effort estimation
manage("detect_cycles", entity_id="proj_abc")  # Find circular dependencies
manage("suggest", entity_id="task_abc")  # Get knowledge suggestions

# Admin
manage("health")  # Server health check
manage("stats")   # Graph statistics
```

#### Task Workflow States

```
┌─────────┐    start     ┌───────┐    submit    ┌────────┐
│ backlog │───────────────│ doing │──────────────│ review │
└─────────┘               └───────┘              └────────┘
     │                        │                       │
     │                        │ block                 │ complete
     v                        v                       v
┌─────────┐               ┌─────────┐            ┌──────┐
│  todo   │               │ blocked │            │ done │
└─────────┘               └─────────┘            └──────┘
                              │                       │
                              │ unblock               │ archive
                              v                       v
                          ┌───────┐              ┌──────────┐
                          │ doing │              │ archived │
                          └───────┘              └──────────┘
```

**Actions:**
| Action | Description | Creates Episode |
|--------|-------------|-----------------|
| `start_task` | Begin work (→ doing) | No |
| `block_task` | Mark blocked with reason | No |
| `unblock_task` | Resume work (→ doing) | No |
| `submit_review` | Submit for review | No |
| `complete_task` | Mark done with learnings | Yes |
| `archive_task` | Archive without completing | No |
| `update_task` | Update task fields | No |

## Installation

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- Docker (for FalkorDB)
- OpenAI API key (for embeddings)

### Setup

```bash
# Start FalkorDB
docker compose up -d

# Install dependencies
uv sync --all-extras

# Configure environment
cp .env.example .env
# Edit .env with your OpenAI API key

# Verify setup
uv run sibyl setup

# Start the server
uv run sibyl serve
```

### Claude Code Integration

Add to your MCP configuration:

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

Or subprocess mode:

```json
{
  "mcpServers": {
    "sibyl": {
      "command": "uv",
      "args": ["--directory", "/path/to/sibyl", "run", "sibyl", "serve", "-t", "stdio"],
      "env": { "SIBYL_OPENAI_API_KEY": "your-api-key" }
    }
  }
}
```

## Entity Types

| Type | Description |
|------|-------------|
| `pattern` | Coding patterns and best practices |
| `rule` | Sacred rules and invariants |
| `template` | Code templates and boilerplates |
| `episode` | Temporal knowledge snapshots |
| `task` | Work items with workflow states |
| `project` | Container for related work |
| `source` | Knowledge source (URL, repo, file path) |
| `document` | Crawled/ingested content |

## CLI Commands

```bash
sibyl serve           # Start MCP server (default: localhost:3334)
sibyl serve -t stdio  # Subprocess mode for Claude Code
sibyl setup           # Verify environment
sibyl ingest          # Ingest wisdom documents
sibyl search "query"  # Quick search
sibyl health          # Health check
sibyl stats           # Graph statistics
```

## Configuration

Environment variables (prefix with `SIBYL_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `FALKORDB_HOST` | `localhost` | FalkorDB host |
| `FALKORDB_PORT` | `6380` | FalkorDB port |
| `FALKORDB_PASSWORD` | `conventions` | FalkorDB password |
| `FALKORDB_GRAPH_NAME` | `conventions` | Graph database name |
| `OPENAI_API_KEY` | (required) | OpenAI API key for embeddings |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `EMBEDDING_DIMENSIONS` | `1536` | Embedding vector size |
| `LOG_LEVEL` | `INFO` | Logging level |

## Documentation

- **[Architecture](docs/CONSOLIDATED_ARCHITECTURE.md)** — Complete system design
- **[Diagrams](docs/TASK_ARCHITECTURE_DIAGRAM.md)** — Visual architecture
- **[Graph-RAG Research](docs/graph-rag-sota-research.md)** — SOTA research summary
- **[Implementation Roadmap](docs/graph-rag-implementation-roadmap.md)** — Phase-by-phase plan
- **[Examples](examples/)** — Sample scripts for common workflows

## Development

```bash
uv sync --all-extras
uv run pytest
uv run mypy src
uv run ruff check src tests
uv run ruff format src tests
```

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

---

<p align="center">
  Created by <a href="https://hyperbliss.tech">Stefanie Jane</a>
</p>

<p align="center">
  <a href="https://github.com/hyperb1iss">
    <img src="https://img.shields.io/badge/GitHub-hyperb1iss-181717?style=for-the-badge&logo=github" alt="GitHub">
  </a>
  <a href="https://bsky.app/profile/hyperbliss.tech">
    <img src="https://img.shields.io/badge/Bluesky-@hyperbliss.tech-1185fe?style=for-the-badge&logo=bluesky" alt="Bluesky">
  </a>
</p>
