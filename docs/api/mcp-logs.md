# MCP Tool: logs

Read recent server logs for debugging and development. The `logs` tool exposes the server's
in-memory log ring buffer to authorized callers.

## Overview

`logs` returns recent entries from the server's ring buffer, letting an agent debug Sibyl behavior
without direct access to the host. It is a developer-introspection tool.

This tool requires the **OWNER** role (the super-admin equivalent). Callers without OWNER
membership receive an authorization error.

## Input Schema

```typescript
interface LogsInput {
  limit?: number; // Max entries to return, clamped 1-500 (default 50)
  service?: string; // Filter by service name ("api", "worker")
  level?: string; // Filter by log level ("debug", "info", "warning", "error")
}
```

The `limit` argument is clamped to the range 1-500 regardless of the value supplied. The ring
buffer retains the most recent entries only; older entries are dropped as new ones arrive.

## Response Schema

The tool returns a list of log entries, newest last.

```typescript
type LogsResponse = LogEntry[];

interface LogEntry {
  timestamp: string; // ISO 8601 timestamp
  service: string; // "api" or "worker"
  level: string; // "debug", "info", "warning", "error"
  event: string; // Log event name
  context: Record<string, any>; // Structured log context fields
}
```

## Usage Examples

### Recent Entries

```json
{
  "name": "logs",
  "arguments": {}
}
```

Returns the last 50 entries.

### More Entries

```json
{
  "name": "logs",
  "arguments": { "limit": 200 }
}
```

### Worker Logs Only

```json
{
  "name": "logs",
  "arguments": { "service": "worker" }
}
```

### Errors Only

```json
{
  "name": "logs",
  "arguments": { "level": "error", "limit": 100 }
}
```

### Worker Errors

```json
{
  "name": "logs",
  "arguments": { "service": "worker", "level": "error" }
}
```

## Notes

- The buffer is in-memory and per-process. In a multi-process deployment, `logs` returns entries
  from the process that handled the call.
- Use `level: "error"` first when diagnosing a failure, then widen to `warning` or `info`.
- The CLI `sibyl logs tail` command exposes the same buffer with streaming support
  (`sibyl logs tail -f`).
- The REST equivalent is `GET /api/logs`, with a WebSocket stream at `/api/logs/stream`.

## Error Handling

| Error                              | Cause                          | Resolution                          |
| ----------------------------------- | ------------------------------ | ----------------------------------- |
| `Organization context required`     | No org-scoped token            | Authenticate with an org-scoped token |
| `OWNER role required for log access` | Caller is not an org OWNER     | Use an OWNER credential             |

## Related

- [mcp-manage.md](./mcp-manage.md) - Admin actions (`health`, `stats`)
- [auth-authorization.md](./auth-authorization.md) - Roles and the OWNER role
