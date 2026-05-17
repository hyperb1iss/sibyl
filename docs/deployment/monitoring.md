# Monitoring and Observability

Health checks, logging, and monitoring for Sibyl deployments.

## Health Check Endpoints

### Public Health Check

```
GET /api/health
```

Simple health check for load balancers and monitoring. No authentication required.

**Response:**

```json
{
  "status": "healthy"
}
```

Used by:

- Kubernetes liveness/readiness probes
- Load balancer health checks
- Frontend connection checks

### Detailed Health Check

```
GET /api/admin/health
```

Detailed health information with database connectivity status. Requires authentication (org member
role).

**Response:**

```json
{
  "status": "healthy",
  "server_name": "sibyl",
  "uptime_seconds": 3600,
  "graph_connected": true,
  "entity_counts": {
    "task": 150,
    "pattern": 42,
    "episode": 89,
    "project": 5
  },
  "errors": []
}
```

### Stats Endpoint

```
GET /api/admin/stats
```

Knowledge graph statistics. Requires authentication.

**Response:**

```json
{
  "entity_counts": {
    "task": 150,
    "pattern": 42,
    "episode": 89,
    "project": 5,
    "epic": 3,
    "rule": 12,
    "source": 8,
    "document": 45
  },
  "total_entities": 354
}
```

## Kubernetes Probes

The Helm chart configures liveness and readiness probes:

```yaml
backend:
  livenessProbe:
    httpGet:
      path: /api/health
      port: http
    initialDelaySeconds: 10
    periodSeconds: 30

  readinessProbe:
    httpGet:
      path: /api/health
      port: http
    initialDelaySeconds: 5
    periodSeconds: 10
```

### Probe Behavior

| Probe     | Purpose                     | Failure Action      |
| --------- | --------------------------- | ------------------- |
| Liveness  | Is the process alive?       | Restart container   |
| Readiness | Can the pod accept traffic? | Remove from service |

## Logging

### Log Format

Sibyl uses [structlog](https://www.structlog.org/) for structured logging with a custom
SilkCircuit-themed renderer.

Console output (development):

```
14:32:15 INFO  api     request path=/api/health status=200 duration_ms=1.23
14:32:16 INFO  api     surreal_query_complete statement=select elapsed_ms=1.92
14:32:17 WARN  api     rate_limit_exceeded client=192.168.1.100
```

JSON output (production):

```json
{
  "timestamp": "2025-01-15T14:32:15Z",
  "level": "info",
  "service": "api",
  "event": "request",
  "path": "/api/health",
  "status": 200,
  "duration_ms": 1.23
}
```

### Log Configuration

```python
from sibyl_core.logging import configure_logging

configure_logging(
    service_name="api",      # Service identifier
    level="INFO",            # DEBUG, INFO, WARNING, ERROR
    colors=True,             # Auto-detect TTY
    json_output=False,       # JSON for production log aggregation
    show_service=True,       # Show service prefix
)
```

### Log Levels

| Level   | When to Use                           |
| ------- | ------------------------------------- |
| DEBUG   | Detailed debugging information        |
| INFO    | General operational events            |
| WARNING | Unexpected but recoverable situations |
| ERROR   | Errors that need attention            |

Set via environment:

```bash
SIBYL_LOG_LEVEL=DEBUG
```

### Suppressed Loggers

The following noisy loggers are suppressed to WARNING level:

- `uvicorn.access`
- `uvicorn.error`
- `graphiti_core`
- `httpx`, `httpcore`
- `arq.worker`, `arq.jobs`
- `mcp`, `fastmcp`
- `neo4j`

### Access Logging

Every HTTP request is logged with timing:

```python
log.info(
    "request",
    method=request.method,
    path=request.url.path,
    status=response.status_code,
    duration_ms=round(duration_ms, 2),
    client=request.client.host,
)
```

## Error Tracking

### Unhandled Exceptions

All unhandled exceptions are caught and logged with a reference ID:

```python
log.error(
    "unhandled_exception",
    error_id=error_id,
    path=request.url.path,
    method=request.method,
    error_type=type(exc).__name__,
    error_message=str(exc),
)
```

Clients receive a safe error response:

```json
{
  "detail": "An internal error occurred. Please try again later. (ref: a1b2c3d4)"
}
```

### Exception Context

Exceptions include full stack traces in logs:

```
14:32:15 ERROR api     unhandled_exception error_id=a1b2c3d4 path=/api/tasks
    Traceback (most recent call last):
      File "...", line 123, in handler
        result = await process()
    ConnectionError: Database connection lost
```

## Metrics Endpoints

### Runtime Telemetry

```
GET /api/telemetry/summary
GET /api/telemetry/prometheus
GET /metrics
```

Sibyl records its own runtime telemetry in-process and persists bounded minute rollups in SurrealDB.
The JSON summary powers the web overview page, while `/metrics` exposes Prometheus-compatible
counters, gauges, and latency summaries for scrapers. Set `SIBYL_METRICS_SCRAPE_TOKEN` for non-local
scraping and pass it as a bearer token or `X-Sibyl-Metrics-Token` header.

Captured surfaces:

- HTTP request counts, status families, and p50/p95/p99 latency
- SurrealDB query counts, query hashes, slow-query counts, retries, and latency
- Memory, context, recall, search, and explore operations
- Background job enqueue and completion metrics
- Crawler run duration, document counts, chunk counts, and errors
- LLM call duration and token counts where providers report usage
- Queue depth and worker health
- WebSocket connection and broadcast activity
- Process uptime, RSS, max RSS, and active thread count

### Project Metrics

```
GET /api/metrics/projects/{project_id}
```

Returns metrics for a specific project:

- Task counts by status (backlog, todo, doing, done, etc.)
- Priority distribution
- Assignee statistics
- Velocity trend (last 14 days)
- Tasks created/completed in last 7 days

### Organization Metrics

```
GET /api/metrics
```

Aggregated metrics across all projects:

- Total projects and tasks
- Status/priority distributions
- Top assignees
- Velocity trend
- Project summaries

## Monitoring Stack Recommendations

### Prometheus + Grafana

For Kubernetes deployments, consider adding:

1. **Prometheus** for metrics collection
2. **Grafana** for dashboards
3. **Loki** for log aggregation

Example ServiceMonitor (if using Prometheus Operator):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: sibyl
  namespace: sibyl
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: sibyl
  endpoints:
    - port: http
      path: /metrics
      interval: 30s
      bearerTokenSecret:
        name: sibyl-metrics
        key: token
```

### Datadog

For Datadog integration:

```yaml
backend:
  podAnnotations:
    ad.datadoghq.com/backend.logs: '[{"source": "python", "service": "sibyl-api"}]'
```

### New Relic

For New Relic APM:

```yaml
backend:
  env:
    NEW_RELIC_LICENSE_KEY: "<license-key>"
    NEW_RELIC_APP_NAME: "sibyl-api"
```

## Log Aggregation

### Kubernetes Logging

Logs are written to stdout/stderr and collected by the node's container runtime.

View logs with kubectl:

```bash
# All backend logs
kubectl logs -n sibyl -l app.kubernetes.io/component=backend -f

# Worker logs
kubectl logs -n sibyl -l app.kubernetes.io/component=worker -f

# All Sibyl logs
kubectl logs -n sibyl -l app.kubernetes.io/name=sibyl -f

# Previous container logs (after restart)
kubectl logs -n sibyl deploy/sibyl-backend --previous
```

### Structured Log Queries

With JSON output enabled, logs can be queried in aggregation systems:

```sql
-- Grafana Loki LogQL
{namespace="sibyl", app="backend"} |= "error"

-- Datadog
service:sibyl-api status:error

-- Elasticsearch
{
  "query": {
    "bool": {
      "must": [
        {"match": {"service": "api"}},
        {"match": {"level": "error"}}
      ]
    }
  }
}
```

## Alerting Recommendations

### Critical Alerts

| Condition                    | Threshold | Action       |
| ---------------------------- | --------- | ------------ |
| Pod not ready                | > 2 min   | Page on-call |
| Error rate                   | > 5%      | Page on-call |
| Response time P95            | > 5s      | Notify team  |
| Database connection failures | > 3       | Page on-call |

### Warning Alerts

| Condition               | Threshold | Action      |
| ----------------------- | --------- | ----------- |
| High memory usage       | > 80%     | Notify team |
| High CPU usage          | > 80%     | Notify team |
| Rate limiting triggered | > 100/min | Investigate |

## Startup Validation

On startup, Sibyl logs connection status:

```
14:32:15 INFO  api     SurrealDB running on surrealdb:8000
14:32:15 INFO  api     WebSocket pub/sub enabled for multi-pod broadcasts
14:32:15 INFO  api     Distributed entity locks enabled
```

If connections fail, warnings are logged but startup continues:

```
14:32:15 WARN  api     SurrealDB unavailable at startup error="Connection refused"
14:32:15 WARN  api     Redis pub/sub unavailable - WebSocket broadcasts will be local only
```

## Recovery on Startup

Sibyl automatically recovers stuck crawl sources on startup:

```
14:32:15 WARN  api     Found stuck IN_PROGRESS sources count=2 sources=["docs", "wiki"]
14:32:15 INFO  api     Recovered stuck source source_name=docs old_status=IN_PROGRESS new_status=COMPLETED
14:32:15 INFO  api     Startup recovery complete recovered=2 completed=1 reset_to_pending=1
```
