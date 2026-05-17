---
title: External Sources
description: Ingest external documentation and make it searchable
---

# External Sources

Sibyl can ingest external documentation (API references, framework guides, library docs) and make
them searchable alongside your knowledge graph. Your agents can reference React docs, AWS
documentation, or any web content as easily as they search your own patterns.

## The Big Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                     External Documentation                       │
│   React Docs • AWS API Reference • Your Internal Wiki           │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Source Crawler                           │
│           Depth-limited crawling • Intelligent chunking          │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Embedding & Storage                         │
│       Vector embeddings • SurrealDB content storage            │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Unified Search                              │
│   Knowledge graph + Documents merged and ranked by relevance    │
└─────────────────────────────────────────────────────────────────┘
```

## Adding Sources

### Via CLI

```bash
# Add a documentation source
sibyl crawl add "https://react.dev/reference/react" --name "React Reference"

# Add with crawl depth
sibyl crawl add "https://docs.aws.amazon.com/lambda/" \
  --name "AWS Lambda Docs" \
  --depth 2

# Add with specific include patterns
sibyl crawl add "https://nextjs.org/docs" \
  --name "Next.js Docs" \
  --include "docs/**"
```

`--include` is the preferred spelling for crawl filters. `--pattern` still works for backward
compatibility.

### Via Web UI

![Managing Sources](/screenshots/web-sources.png)

Navigate to **Sources** in the web UI to:

- Add new documentation sources
- View crawl status and progress
- Trigger manual crawls
- See document counts per source

### Via MCP

```python
add(
    title="React Documentation",
    content="https://react.dev/reference/react",
    entity_type="source",
    metadata={
        "url": "https://react.dev/reference/react",
        "crawl_depth": 2
    }
)
```

## Crawling Process

When you add a source, Sibyl:

1. **Fetches** the root URL and extracts content
2. **Discovers** linked pages within the domain
3. **Crawls** pages up to the specified depth
4. **Chunks** content into searchable segments
5. **Embeds** each chunk using OpenAI embeddings
6. **Stores** chunks in SurrealDB for semantic search

### Crawl Depth

| Depth | Behavior            | Best For                   |
| ----- | ------------------- | -------------------------- |
| 1     | Root page only      | Single reference pages     |
| 2     | Root + linked pages | Section of documentation   |
| 3+    | Deep crawl          | Entire documentation sites |

::: warning Crawl Responsibly Higher depths exponentially increase crawl time and storage. Start
with depth 2 and increase only if needed. :::

### Crawl Triggers

```bash
# Trigger a crawl after adding a source
sibyl crawl ingest source_abc123

# Re-run a crawl when documentation changes
sibyl crawl ingest source_abc123
```

## Document Structure

Crawled content is stored as `document` entities:

```
Source (e.g., "React Docs")
├── Document (e.g., "useState Hook")
│   ├── Chunk 1: "useState is a React Hook..."
│   ├── Chunk 2: "Updating state based on..."
│   └── Chunk 3: "Pitfalls: Calling useState..."
├── Document (e.g., "useEffect Hook")
│   ├── Chunk 1: ...
│   └── Chunk 2: ...
└── ...
```

Each document chunk includes:

- **Content**: The actual text
- **Embedding**: Vector for semantic search
- **Metadata**: Source URL, title, position
- **Source reference**: Link to parent source

## Searching Documents

Documents are automatically included in search results:

```bash
# Search across everything (graph memory + crawled docs)
sibyl search "useState dependency array"

# Search only crawled docs, not graph memory
sibyl search "useState" --docs-only

# Search only graph memory, skip crawled docs
sibyl search "useState" --graph-only
```

To narrow a search to one crawled source by name, use the MCP `search` tool's `source_name` filter.

### Search Result Types

When you search, results come from two places:

| Source          | Result Type                  | Content                |
| --------------- | ---------------------------- | ---------------------- |
| Knowledge Graph | pattern, episode, rule, etc. | Your team's learnings  |
| Document Store  | document                     | External documentation |

Results are merged and ranked by semantic relevance.

## Managing Sources

### List Sources

```bash
sibyl crawl list
```

Output:

```
ID              Name              URL                               Documents  Last Sync
source_abc123   React Docs        https://react.dev/reference       142        2024-01-15 10:30
source_def456   AWS Lambda        https://docs.aws.amazon.com/...   89         2024-01-14 14:22
source_ghi789   Internal Wiki     https://wiki.company.com/dev      56         2024-01-15 09:00
```

### View Source Details

```bash
sibyl crawl show source_abc123
```

Output:

```
Source: React Docs (source_abc123)
  URL:        https://react.dev/reference/react
  Depth:      2
  Documents:  142
  Last Sync:  2024-01-15 10:30:00

  Status: Ready
  Chunks: 1,247

  Top Documents:
  - useState Hook (24 chunks)
  - useEffect Hook (31 chunks)
  - useContext Hook (18 chunks)
```

### Change Source Settings

Adjust crawl settings by deleting and re-adding the source with the new options, or use the web UI
to edit the source interactively.

### Delete Source

```bash
# Remove source and all its documents
sibyl crawl delete source_abc123
```

::: danger Destructive Operation Deleting a source removes all associated documents and chunks. This
cannot be undone. :::

## Graph Linking

After documents are crawled, you can link them to your knowledge graph:

```bash
# Link document chunks to related entities
sibyl crawl link-graph source_abc123
```

This creates `DOCUMENTED_IN` relationships between your patterns/episodes and relevant documentation
chunks. For example:

- Your "OAuth token refresh" pattern links to AWS Cognito docs
- Your "Redis connection pooling" episode links to Redis documentation

### Check Linking Status

```bash
sibyl crawl link-status
```

## Source Import

Crawling pulls documentation from the web. **Source import** is the other ingestion path: it brings
structured external records into raw memory through an adapter.

The first shipped adapter is the **mailbox adapter**, which ingests an mbox archive. Each message
becomes a source record with its headers, body, and attachments captured and privacy-classified.
Other adapters follow the same contract.

Source import jobs are **resumable**. Each job checkpoints its progress, so a large archive can be
ingested across multiple runs without re-processing records that already landed.

The [memory workspace](./memory-workspace.md) tracks import jobs at `/memory/imports`, showing
checkpoint progress and the records each source produced. Imported records are raw memory, scoped
like every other memory and ready for reflection and recall.

Configure where importable archives live with the `SIBYL_SOURCE_IMPORT_DIR` environment variable.

## Best Practices

### Choose Sources Wisely

**Good sources:**

- Official framework documentation
- API references you use frequently
- Internal wikis with tribal knowledge
- Frequently-referenced guides

**Avoid:**

- General content (blogs, news)
- Massive documentation sites (start with specific sections)
- Frequently-changing content (requires constant re-sync)

### Keep Sources Updated

Documentation changes. Re-run crawls when your sources update:

```bash
# Re-crawl when you know docs changed
sibyl crawl ingest source_abc123
```

### Use Meaningful Names

```bash
# Good
sibyl crawl add "https://react.dev/reference" --name "React 19 Reference"

# Less good
sibyl crawl add "https://react.dev/reference" --name "docs"
```

### Monitor Crawl Health

Check the Sources page in the web UI regularly. Look for:

- Sources stuck in "crawling" state
- Sources with zero documents
- Large time gaps since last sync

## Web UI Source Management

![Sources Dashboard](/screenshots/web-sources.png)

The Sources page shows:

- **All sources** with document counts and sync status
- **Crawl progress** for active crawls
- **Quick actions** to crawl, edit, or delete sources
- **Document browser** to explore crawled content

### Adding a Source via UI

1. Click **Add Source**
2. Enter the URL and name
3. Set crawl depth (start with 2)
4. Click **Start Crawl**

### Monitoring Crawls

Active crawls show:

- Progress percentage
- Pages discovered vs crawled
- Current URL being processed
- Estimated time remaining

## Troubleshooting

### Source Stuck Crawling

```bash
# Check crawl status
sibyl crawl status source_abc123

# If stuck, restart the local dev stack
moon run stop && moon run dev
```

### No Documents After Crawl

Possible causes:

- JavaScript-rendered content (Sibyl needs static HTML)
- Robots.txt blocking
- Rate limiting by the source
- Network issues during crawl

### Search Not Finding Documents

```bash
# Verify documents exist
sibyl crawl show source_abc123

# Check embeddings are generated
sibyl stats

# Try direct document search
sibyl crawl documents list --source source_abc123
```

## Next Steps

- [Memory Workspace](./memory-workspace.md). Track import jobs in the web UI
- [Semantic Search](./semantic-search.md). Search across documents and knowledge
- [Entity Types](./entity-types.md). Understand document vs other types
- [Capturing Knowledge](./capturing-knowledge.md). Add your own learnings
