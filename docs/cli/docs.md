# docs

Import and list document collections. `docs` brings local files, directories, and URLs into Sibyl as
documents, and pastes ad-hoc text straight into a collection. Imported documents are chunked,
embedded, and surfaced through [`sibyl search`](./search.md).

This is distinct from [`sibyl crawl documents`](./crawl.md#crawl-documents), which browses pages
captured by the web crawler. Use `docs` to push content in; use `crawl` to pull a site down.

## Commands

| Command                           | Description                               |
| --------------------------------- | ----------------------------------------- |
| [`sibyl docs add`](#docs-add)     | Import a document file, directory, or URL |
| [`sibyl docs paste`](#docs-paste) | Import pasted text as a document          |
| [`sibyl docs list`](#docs-list)   | List imported document collections        |

---

## docs add

Import a document file, a directory of documents, or a URL. Directory imports require `--recursive`.

```bash
sibyl docs add <source> [options]
```

| Argument | Required | Description                      |
| -------- | -------- | -------------------------------- |
| `source` | Yes      | Document file, directory, or URL |

| Option                    | Short | Default | Description                             |
| ------------------------- | ----- | ------- | --------------------------------------- |
| `--recursive`             | `-r`  | false   | Import a directory recursively          |
| `--collection`            | `-c`  | (none)  | Collection label                        |
| `--project`               | `-p`  | (auto)  | Target project id or name               |
| `--batch-size`            |       | 100     | Import batch size                       |
| `--drain`                 |       | false   | Wait for the background drain to finish |
| `--poll-interval`         |       | 1.0     | Seconds between drain status checks     |
| `--timeout`               |       | (none)  | Maximum seconds to wait when draining   |
| `--allow-private-network` |       | false   | Allow URL imports from private hosts    |
| `--json`                  | `-j`  | false   | JSON output for scripting               |

### Examples

```bash
# Import a single file into the linked project
sibyl docs add ./runbook.md

# Import a directory tree into a named collection
sibyl docs add ./design-notes/ --recursive --collection "Design Notes"

# Import a public URL and wait for it to finish
sibyl docs add https://example.com/spec.html --drain
```

---

## docs paste

Import pasted text as a document. Reads from an argument, from `--file`, or from stdin with `-`.

```bash
sibyl docs paste [text] [options]
```

| Argument | Required | Description                     |
| -------- | -------- | ------------------------------- |
| `text`   | No       | Document text, or `-` for stdin |

| Option            | Short | Default | Description                             |
| ----------------- | ----- | ------- | --------------------------------------- |
| `--file`          | `-f`  | (none)  | Read document text from a file          |
| `--title`         | `-t`  | (none)  | Document title                          |
| `--collection`    | `-c`  | (none)  | Collection label                        |
| `--project`       | `-p`  | (auto)  | Target project id or name               |
| `--batch-size`    |       | 100     | Import batch size                       |
| `--drain`         |       | false   | Wait for the background drain to finish |
| `--poll-interval` |       | 1.0     | Seconds between drain status checks     |
| `--timeout`       |       | (none)  | Maximum seconds to wait when draining   |
| `--json`          | `-j`  | false   | JSON output for scripting               |

### Examples

```bash
# Paste inline text with a title
sibyl docs paste "Release checklist: tag, build, publish" --title "Release Checklist"

# Pipe content from stdin
cat NOTES.md | sibyl docs paste - --collection "Notes"

# Import from a file
sibyl docs paste --file ./meeting.txt --title "Standup 2026-06-23"
```

---

## docs list

List imported document collections with their document counts.

```bash
sibyl docs list [options]
```

| Option   | Short | Default | Description               |
| -------- | ----- | ------- | ------------------------- |
| `--json` | `-j`  | false   | JSON output for scripting |

## Notes

- File and directory sources must not contain symlinks; the path is fully resolved before upload.
- A target project is required. `docs` uses the project linked to the current directory unless you
  pass `--project`.
- Imports queue and drain in the background. Pass `--drain` to block until the import reaches a
  terminal state.
- URL imports refuse private hosts by default; pass `--allow-private-network` to override.

## Related Commands

- [`sibyl search`](./search.md) - Search documents alongside graph memory
- [`sibyl crawl`](./crawl.md) - Crawl websites into the content store
- [`sibyl ingest`](./ingest.md) - Import agent transcript JSONL into raw memory
