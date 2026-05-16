# crawl

Web crawling and documentation ingestion. `crawl` registers documentation sources, ingests them
into the content store, and links the crawled chunks into the knowledge graph so they surface in
[`sibyl search`](./search.md).

## Commands

| Command                                          | Description                                |
| ------------------------------------------------- | ------------------------------------------ |
| [`sibyl crawl list`](#crawl-list)                 | List crawl sources                         |
| [`sibyl crawl add`](#crawl-add)                   | Add a new documentation source             |
| [`sibyl crawl ingest`](#crawl-ingest)             | Start crawling a source                    |
| [`sibyl crawl status`](#crawl-status)             | Get crawl status for a source              |
| [`sibyl crawl show`](#crawl-show)                 | Show crawl source details                  |
| [`sibyl crawl stats`](#crawl-stats)               | Show crawling statistics                   |
| [`sibyl crawl health`](#crawl-health)             | Check crawl system health                  |
| [`sibyl crawl delete`](#crawl-delete)             | Delete a source and all its documents      |
| [`sibyl crawl link-status`](#crawl-link-status)   | Show pending graph linking work per source |
| [`sibyl crawl link-graph`](#crawl-link-graph)     | Link crawled chunks into the graph         |
| [`sibyl crawl documents`](#crawl-documents)       | Browse crawled documents                   |

## Workflow

```
add  ->  ingest  ->  link-graph  ->  search
 |         |           |
 source    documents   graph entities
```

Register a source with `add`, crawl it with `ingest`, then `link-graph` so the chunks become graph
entities. `link-status` shows what still needs linking.

---

## crawl list

List crawl sources.

```bash
sibyl crawl list [options]
```

| Option     | Short | Default | Description                  |
| ---------- | ----- | ------- | ---------------------------- |
| `--status` | `-s`  | (all)   | Filter by status             |
| `--limit`  | `-n`  | 20      | Max results                  |
| `--json`   | `-j`  | false   | JSON output                  |

---

## crawl add

Add a new documentation source.

```bash
sibyl crawl add <url> [options]
```

| Argument | Required | Description                  |
| -------- | -------- | ---------------------------- |
| `url`    | Yes      | Documentation URL to add     |

| Option                  | Short | Default   | Description                            |
| ----------------------- | ----- | --------- | -------------------------------------- |
| `--name`                | `-n`  | (derived) | Source name                            |
| `--type`                | `-T`  | `website` | Source type: `website`, `github`, `api_docs` |
| `--depth`               | `-d`  | 2         | Crawl depth                            |
| `--pattern` / `--include` | `-p` | (none)   | URL patterns to include                |
| `--json`                | `-j`  | false     | JSON output                            |

### Example

```bash
sibyl crawl add https://docs.example.com \
  --name "Example Docs" --type website --depth 3 \
  --pattern "/guide/*"
```

---

## crawl ingest

Start crawling a documentation source.

```bash
sibyl crawl ingest <source_id> [options]
```

| Argument    | Required | Description           |
| ----------- | -------- | --------------------- |
| `source_id` | Yes      | Source ID to crawl    |

| Option        | Short | Default | Description                  |
| ------------- | ----- | ------- | ---------------------------- |
| `--max-pages` | `-p`  | 50      | Maximum pages to crawl       |
| `--depth`     | `-d`  | 3       | Maximum link depth           |
| `--no-embed`  |       | false   | Skip embedding generation    |
| `--json`      | `-j`  | false   | JSON output                  |

### Examples

```bash
sibyl crawl ingest abc123 --max-pages 100
sibyl crawl ingest abc123 --depth 2 --no-embed
```

---

## crawl status

Get the status of a crawl source using the current source-status contract.

```bash
sibyl crawl status <source_id> [options]
```

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

---

## crawl show

Show crawl source details.

```bash
sibyl crawl show <source_id> [options]
```

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

---

## crawl stats

Show crawling statistics across all sources.

```bash
sibyl crawl stats [--json]
```

---

## crawl health

Check crawl system health.

```bash
sibyl crawl health [--json]
```

---

## crawl delete

Delete a crawl source and all its documents.

```bash
sibyl crawl delete <source_id> [options]
```

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | JSON output |

---

## crawl link-status

Show pending graph linking work per source. Use this to see how many crawled chunks still need to
be linked into the graph.

```bash
sibyl crawl link-status [--json]
```

---

## crawl link-graph

Link crawled chunks into the graph. Pass a source ID, or `all` to process every source.

```bash
sibyl crawl link-graph [source_id] [options]
```

| Argument    | Required | Description                          |
| ----------- | -------- | ------------------------------------ |
| `source_id` | No       | Source ID, or `all` for all sources  |

| Option         | Short | Default | Description                                  |
| -------------- | ----- | ------- | -------------------------------------------- |
| `--batch`      | `-b`  | 50      | Batch size                                   |
| `--dry-run`    | `-n`  | false   | Show what would be processed                 |
| `--create-new` |       | false   | Create graph entities for unlinked extractions |
| `--json`       | `-j`  | false   | JSON output                                  |

### Examples

```bash
# Dry-run linking for one source
sibyl crawl link-graph abc123 --dry-run

# Link all sources, creating entities for new extractions
sibyl crawl link-graph all --create-new
```

---

## crawl documents

Browse crawled documents.

### crawl documents list

List crawled documents.

```bash
sibyl crawl documents list [options]
```

| Option     | Short | Default | Description           |
| ---------- | ----- | ------- | --------------------- |
| `--source` | `-s`  | (all)   | Filter by source ID   |
| `--limit`  | `-n`  | 20      | Max results           |
| `--json`   | `-j`  | false   | JSON output           |

### crawl documents show

Show full document content. Use the `document_id` from search result metadata.

```bash
sibyl crawl documents show <document_id> [options]
```

| Argument      | Required | Description                            |
| ------------- | -------- | -------------------------------------- |
| `document_id` | Yes      | Document ID from search result metadata |

| Option   | Short | Description                |
| -------- | ----- | -------------------------- |
| `--raw`  | `-r`  | Show raw markdown content  |
| `--json` | `-j`  | JSON output                |

### Example

```bash
sibyl search "proto config"
# note the document_id in result metadata
sibyl crawl documents show 22d4cf79-8561-4be0-8067-da8673e3439d
```

## Related Commands

- [`sibyl search`](./search.md) - Search graph and crawled docs together
- [`sibyl explore`](./explore.md) - Traverse linked document entities
