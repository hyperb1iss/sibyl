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
| **SurrealDB** | Graph + content + auth (default)    | 8000\* |

\*Default internal ports. External mappings vary by deployment mode.

> **Migration-only:** PostgreSQL is **not** a deployed Sibyl component. It is consumed only by the
> standalone `migrate` CLI when restoring a retained `postgres.sql` archive against an
> operator-managed database (default port `5433`). It never runs as an ambient sidecar in any
> supported deployment.

## Runtime Boundary

- SurrealDB is the only required data service for new local, single-host, and supported production
  deployments.
- Redis/Valkey coordination is explicit opt-in. Use it for multi-process or multi-replica
  deployments by setting `SIBYL_COORDINATION_BACKEND=redis` or Helm `coordinationBackend: redis`.
- PostgreSQL and preserved FalkorDB installs are migration sources or archive-rehearsal inputs, not
  ambient sidecars for current deployments.
- Rollback is bounded by the write-freeze window. Before SurrealDB accepts new production writes,
  traffic can return to the preserved source deployment. After writes reopen on SurrealDB, recovery
  uses Surreal backups, archive receipts, and deliberate replay, not an instant switch back to
  legacy services.

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

PostgreSQL is retained only as an external archive-rehearsal target. See
[storage-modes.md](../guide/storage-modes.md) and
[migrating-from-falkor.md](../guide/migrating-from-falkor.md).

## Deployment Modes

### 1. Local Development (Docker Compose)

**Best for:** Quick local development and testing.

- Single command startup
- Hot reload for backend/frontend
- SurrealDB runs in Docker
- [Docker Compose Guide](docker-compose.md)

### 2. Local Kubernetes (Tilt + Minikube)

**Best for:** Testing Kubernetes manifests locally, developing with full K8s stack.

- Full Kubernetes environment locally
- Kong Gateway for routing
- SurrealDB with TiKV and Valkey coordination
- Automatic image builds on code changes
- [Tilt/Minikube Guide](tilt-minikube.md)

### 3. Production Kubernetes

**Best for:** Production deployments with HA and scaling.

- Helm chart for declarative deployment
- HPA for autoscaling
- PodDisruptionBudgets for availability
- External or in-cluster databases
- [Kubernetes Guide](kubernetes.md)
- [Helm Chart Reference](helm-chart.md)

### 4. Single Host (Ansible)

**Best for:** A personal instance on one small cloud VM.

- One host, no Kubernetes
- Docker Compose stack provisioned by the bundled Ansible role
- Caddy TLS via the Cloudflare DNS-01 challenge
- Pairs with Tailscale for a private, zero-public-port deployment
- [Single-Host Guide](ansible.md)

## Quick Comparison

| Feature               | Docker Compose | Tilt/Minikube | Production K8s | Single Host |
| --------------------- | -------------- | ------------- | -------------- | ----------- |
| Setup time            | 1 minute       | 5-10 minutes  | Varies         | ~10 minutes |
| Hot reload            | Yes            | Yes           | No             | No          |
| Kong Gateway          | No             | Yes           | Yes            | No          |
| TLS                   | No             | Yes (Caddy)   | Yes            | Yes (Caddy) |
| Autoscaling           | No             | No            | Yes (HPA)      | No          |
| Multi-replica         | No             | Yes           | Yes            | No          |
| Resource requirements | Low            | Medium        | High           | Low         |
| Production-like       | No             | Mostly        | Yes            | Mostly      |

## Port Mappings by Environment

### Docker Compose (Local Dev)

| Service             | Host Port | Container Port | Notes                  |
| ------------------- | --------- | -------------- | ---------------------- |
| Backend             | 3334      | 3334           | API + MCP              |
| Frontend            | 3337      | 3337           | Next.js UI             |
| SurrealDB (default) | 8000      | 8000           | ws/http, RPC at `/rpc` |
| Redis/Valkey        | 6381      | 6379           | Optional coordination  |

### Tilt/Minikube

All services accessed via `https://sibyl.local`:

| Path    | Service  | Notes        |
| ------- | -------- | ------------ |
| /api/\* | Backend  | REST API     |
| /mcp    | Backend  | MCP protocol |
| /       | Frontend | Next.js UI   |

## Next Steps

- [Environment Variables](environment.md) - Full configuration reference
- [Monitoring](monitoring.md) - Health checks and observability
- [Troubleshooting](troubleshooting.md) - Common issues and solutions
