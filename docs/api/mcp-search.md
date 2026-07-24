# MCP Tool: search

Unified semantic search across Sibyl's knowledge graph AND crawled documentation. Results are merged
and ranked by relevance score.

## Overview

The `search` tool is the primary discovery mechanism for AI agents. It searches:

- **Knowledge Graph**: Patterns, rules, episodes, tasks, projects
- **Crawled Documentation**: Surreal-backed similarity search on document chunks

## Input Schema

```typescript
interface SearchInput {
  // Required
  query: string; // Natural language search query

  // Entity Filtering
  types?: string[]; // Entity types to search
  language?: string; // Programming language filter
  category?: string; // Category/domain filter

  // Task-Specific Filters
  status?: string; // Task status filter
  project?: string; // Project ID filter
  assignee?: string; // Assignee name filter

  // Document-Specific Filters
  source?: string; // Alias for source_name
  source_id?: string; // Document source UUID filter
  source_name?: string; // Document source name (partial match)

  // Temporal
  since?: string; // ISO date or relative (7d, 2w)
  temporal_decay_days?: number; // Half-life in days for recency decay

  // Pagination
  limit?: number; // 1-50, default 10

  // Control Flags
  include_content?: boolean; // Include full content (default true)
  include_documents?: boolean; // Search docs (default true)
  include_graph?: boolean; // Search graph (default true)
  use_enhanced?: boolean; // Use enhanced retrieval with reranking (default true)
  boost_recent?: boolean; // Temporal boosting (default true)
}
```

The MCP `search` tool does not accept an `offset` argument. For paginated traversal use the REST
`POST /api/search` endpoint, which exposes `offset`.

### Entity Types

| Type       | Description                        |
| ---------- | ---------------------------------- |
| `pattern`  | Coding patterns and best practices |
| `rule`     | Rules and guidelines               |
| `template` | Code templates and boilerplate     |
| `topic`    | Knowledge topics                   |
| `episode`  | Temporal learnings and discoveries |
| `task`     | Work items with workflow state     |
| `project`  | Project containers                 |
| `document` | Crawled documentation chunks       |

### Task Status Values

```
backlog, todo, doing, blocked, review, done, archived
```

## Response Schema

```typescript
interface SearchResponse {
  results: SearchResult[];
  total: number;
  query: string;
  filters: Record<string, any>;
  graph_count: number; // Results from knowledge graph
  document_count: number; // Results from crawled documents
  raw_memory_count: number; // Results from raw memories
  limit: number;
  offset: number; // Always 0 for MCP calls
  has_more: boolean;
  usage_hint: string; // Guidance for fetching full content
}

interface SearchResult {
  id: string; // Entity/chunk ID, use with `sibyl show <id>`
  type: string; // Entity type or "document"
  name: string;
  content: string; // Truncated preview
  score: number; // Relevance score (0-1)
  source?: string; // Source name for documentation results
  url?: string; // URL for documents
  result_origin: "graph" | "document" | "raw_memory";
  metadata: Record<string, any>;
}
```

Raw memories are searched by default alongside graph and documents. The raw-memory filter fields
(`include_raw_memory`, `memory_scope`, `scope_key`, `participants`, `labels`, `thread_id`,
`occurred_after`/`occurred_before`) are REST-only; see [rest-search.md](./rest-search.md).

## Usage Examples

### Basic Search

```json
{
  "name": "search",
  "arguments": {
    "query": "OAuth implementation patterns"
  }
}
```

**Response:**

```json
{
  "results": [
    {
      "id": "pattern_abc123",
      "type": "pattern",
      "name": "OAuth 2.0 PKCE Flow",
      "content": "The recommended OAuth flow for SPAs and mobile apps uses PKCE...",
      "score": 0.92,
      "result_origin": "graph",
      "metadata": {
        "category": "authentication",
        "languages": ["typescript", "python"]
      }
    }
  ],
  "total": 5,
  "query": "OAuth implementation patterns",
  "filters": {},
  "graph_count": 3,
  "document_count": 2,
  "has_more": false,
  "usage_hint": "Results show previews. To get full content, use: sibyl show <id>"
}
```

### Search Tasks by Status

```json
{
  "name": "search",
  "arguments": {
    "query": "authentication",
    "types": ["task"],
    "project": "proj_abc123",
    "status": "todo,doing"
  }
}
```

### Search Documentation Only

```json
{
  "name": "search",
  "arguments": {
    "query": "Next.js middleware configuration",
    "source_name": "next-docs",
    "include_graph": false
  }
}
```

### Search with Language Filter

```json
{
  "name": "search",
  "arguments": {
    "query": "error handling patterns",
    "types": ["pattern", "rule"],
    "language": "python"
  }
}
```

### Search Recent Knowledge

```json
{
  "name": "search",
  "arguments": {
    "query": "debugging insights",
    "types": ["episode"],
    "since": "7d"
  }
}
```

## Workflow Patterns

### Task Management

For task searches, always include project context:

```
1. First: explore(mode="list", types=["project"])  --> Find the project
2. Then: search("query", types=["task"], project="<project_id>")
```

### Documentation Discovery

```json
{
  "name": "search",
  "arguments": {
    "query": "API reference",
    "types": ["document"],
    "source_name": "api-docs"
  }
}
```

### Fetching Full Content

Search results contain previews. To get full content:

```json
// After finding a result with id "pattern_abc123"
// Use the entities endpoint or MCP explore tool
{
  "name": "explore",
  "arguments": {
    "mode": "related",
    "entity_id": "pattern_abc123"
  }
}
```

## Enhanced Retrieval

When `use_enhanced: true` (default), search uses hybrid retrieval:

1. **Vector Search**: Semantic similarity via embeddings
2. **Graph Context**: Boost entities with relevant relationships
3. **Temporal Boost**: Prefer recently updated content (when `boost_recent: true`)

## Performance Considerations

- **Limit**: Keep under 20 for interactive use
- **Enhanced Mode**: Slightly slower but more relevant results
- **Document Search**: Requires embeddings and SurrealDB content storage
- **Timeout**: Search operations timeout after 30 seconds

## Error Handling

| Error                      | Cause                   | Resolution                      |
| -------------------------- | ----------------------- | ------------------------------- |
| `organization_id required` | No org context in token | Ensure valid JWT with org claim |
| `Search failed`            | Internal search error   | Retry or check server logs      |
| `Invalid token`            | Auth failure            | Re-authenticate                 |

## Related

- [mcp-explore.md](./mcp-explore.md) - Graph traversal without search
- [mcp-context.md](./mcp-context.md) - Compile a structured context pack for a goal
- [mcp-add.md](./mcp-add.md) - Create new knowledge
- [rest-search.md](./rest-search.md) - REST equivalent endpoint
