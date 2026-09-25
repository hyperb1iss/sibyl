# Deployment Overview

Sibyl can be deployed in multiple configurations, from local development to production Kubernetes
clusters.

## Architecture

Sibyl consists of four components plus one unified storage backend:

| Component     | Purpose                             | Port   |
| ------------- | ----------------------------------- | ------ |
| **Backend**   | FastAPI + MCP server (sibyld serve) | 3334   |
| **Worker**    | arq job queue processor             | -      |
| **Frontend**  | Next.js 16 web UI                   | 3337   |
| **SurrealDB** | Graph + content + auth              | 8000\* |

\*Default internal ports. External mappings vary by deployment mode.

## Runtime Boundary

- SurrealDB is the only data service Sibyl needs, for local, single-host, and production deployments
  alike.
- Redis/Valkey coordination is explicit opt-in. Use it for multi-process or multi-replica
  deployments by setting `SIBYL_COORDINATION_BACKEND=redis` or Helm `coordinationBackend: redis`.
- Recovery uses SurrealDB backups and Sibyl archive restores. See
  [Backup And Restore](../admin/backup-restore.md).

```
                                   +------------------+
                                   |    Frontend      |
                                   |   (Next.js 16)   |
                                   |     :3337        |
                                   +--------+---------+
                                            |
+------------------+               +--------+---------+
|    MCP Client    |               |      Kong /      |
| (Claude, etc.)   +-------------->+     Ingress      |
+------------------+     /mcp      +--------+---------+
                                            |
                         /api/*    +--------+---------+
                         +-------->+     Backend      |
                                   | (FastAPI + MCP)  |
                                   |     :3334        |
                                   +--------+---------+
                                            |
                                            | ws://:8000/rpc
                                            v
                                   +------------------+
                                   |     SurrealDB    |
                                   |  graph + content |
                                   |      + auth      |
                                   |      :8000       |
                                   +--------+---------+
                                            ^
                                            |
                                   +--------+---------+
                                   |     Worker       |
                                   |  (arq processor) |
                                   +------------------+
```

See [Storage Modes](../guide/storage-modes.md) for how each process connects to SurrealDB.

## Deployment Modes

### 1. Local Development (Docker Compose)

**Best for:** Quick local development and testing.

- Single command startup
- Hot reload for backend/frontend
- SurrealDB runs in Docker
- [Docker Compose Guide](docker-compose.md)

### 2. Production Kubernetes

**Best for:** Production deployments with HA and scaling.

- Helm chart for declarative deployment
- HPA for autoscaling
- PodDisruptionBudgets for availability
- External or in-cluster databases
- [Kubernetes Guide](kubernetes.md)
- [Helm Chart Reference](helm-chart.md)

### 3. Single Host (Ansible)

**Best for:** A personal instance on one small cloud VM.

- One host, no Kubernetes
- Docker Compose stack provisioned by the bundled Ansible role
- Caddy TLS via the Cloudflare DNS-01 challenge
- Pairs with Tailscale for a private, zero-public-port deployment
- [Single-Host Guide](ansible.md)

## Quick Comparison

| Feature               | Docker Compose | Production K8s | Single Host |
| --------------------- | -------------- | -------------- | ----------- |
| Setup time            | 1 minute       | Varies         | ~10 minutes |
| Hot reload            | Yes            | No             | No          |
| Kong Gateway          | No             | Yes            | No          |
| TLS                   | No             | Yes            | Yes (Caddy) |
| Autoscaling           | No             | Yes (HPA)      | No          |
| Multi-replica         | No             | Yes            | No          |
| Resource requirements | Low            | High           | Low         |
| Production-like       | No             | Yes            | Mostly      |

## Port Mappings by Environment

### Docker Compose (Local Dev)

| Service             | Host Port | Container Port | Notes                  |
| ------------------- | --------- | -------------- | ---------------------- |
| Backend             | 3334      | 3334           | API + MCP              |
| Frontend            | 3337      | 3337           | Next.js UI             |
| SurrealDB (default) | 8000      | 8000           | ws/http, RPC at `/rpc` |
| Redis/Valkey        | 6381      | 6379           | Optional coordination  |

## Next Steps

- [Environment Variables](environment.md) - Full configuration reference
- [Monitoring](monitoring.md) - Health checks and observability
- [Troubleshooting](troubleshooting.md) - Common issues and solutions
