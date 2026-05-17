# pending-writes

Inspect and replay locally buffered writes. When the CLI cannot reach the server, write operations
are buffered to a secure local store with idempotency keys instead of being lost. `pending-writes`
lists, replays, and discards that buffer.

## Commands

| Command                                                   | Description                                        |
| --------------------------------------------------------- | -------------------------------------------------- |
| [`sibyl pending-writes list`](#pending-writes-list)       | List buffered writes (no sensitive payload bodies) |
| [`sibyl pending-writes flush`](#pending-writes-flush)     | Replay buffered writes                             |
| [`sibyl pending-writes discard`](#pending-writes-discard) | Discard buffered writes without replaying          |

## How Buffering Works

A write that fails to reach the server (offline, server down, transient error) is appended to the
local pending-writes buffer with an idempotency key. The key means a later `flush` can replay the
write safely without creating a duplicate if part of it already landed. Inspect the buffer with
`list`, replay it with `flush` once connectivity is back, or drop entries you no longer want with
`discard`.

---

## pending-writes list

List buffered writes without printing sensitive payload bodies. Output shows write IDs, the target
operation, and timestamps, but not the captured content itself.

### Synopsis

```bash
sibyl pending-writes list [options]
```

### Options

| Option   | Short | Description |
| -------- | ----- | ----------- |
| `--json` | `-j`  | Output JSON |

### Example

```bash
sibyl pending-writes list
```

---

## pending-writes flush

Replay buffered writes. With no arguments, flushes the entire buffer. Pass IDs or prefixes to replay
a subset.

### Synopsis

```bash
sibyl pending-writes flush [write_ids]...
```

### Arguments

| Argument    | Required | Description                                      |
| ----------- | -------- | ------------------------------------------------ |
| `write_ids` | No       | Pending write IDs or prefixes. Omit to flush all |

### Examples

```bash
# Replay everything in the buffer
sibyl pending-writes flush

# Replay specific writes by prefix
sibyl pending-writes flush a1b2 c3d4
```

---

## pending-writes discard

Discard buffered writes without replaying them. Use this to drop entries you no longer want, for
example after deciding a captured note is stale.

### Synopsis

```bash
sibyl pending-writes discard <write_ids>...
```

### Arguments

| Argument    | Required | Description                   |
| ----------- | -------- | ----------------------------- |
| `write_ids` | Yes      | Pending write IDs or prefixes |

### Example

```bash
sibyl pending-writes discard a1b2c3
```

## Related Commands

- [`sibyl remember`](./remember.md) - Capture durable memory
- [`sibyl capture`](./capture.md) - Quick capture
- [`sibyl health`](./index.md) - Check server connectivity
