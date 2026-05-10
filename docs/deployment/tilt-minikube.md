# Tilt + Local Kubernetes

Local Kubernetes development with Tilt for a small scalable Sibyl fleet.

The current Tiltfile targets Minikube by default because the image builds use
`minikube image build`. Override with `SIBYL_K8S_CONTEXT` only if you also adapt the image builder.

## Architecture

```text
Gateway API + cert-manager + Kong
        |
        v
Sibyl frontend x2
Sibyl backend  x2  -> SurrealDB x2 -> TiKV/PD x3
Sibyl worker   x2  -> Valkey x3
```

SurrealDB is the unified graph, content, and auth store. TiKV is the distributed storage engine for
SurrealDB. Valkey is the coordination layer for arq jobs, distributed entity locks, WebSocket
pub/sub, and shared rate limits.

Legacy FalkorDB and PostgreSQL are not deployed by Tilt.

## Prerequisites

```bash
brew install minikube kubectl helm tilt caddy

kubectl version --client
helm version
tilt version
podman --version # or docker version
```

For Minikube, give the demo enough room:

```bash
minikube start --cpus=6 --memory=12288 --driver=docker
```

Tilt builds images with Podman when available and falls back to Docker, then loads the image archive
into Minikube.

## Quick Start

```bash
export SIBYL_JWT_SECRET="$(openssl rand -hex 32)"
export SIBYL_SURREAL_PASSWORD="sibyl-local-dev"
export SIBYL_REDIS_PASSWORD="sibyl-local-dev"
export ANTHROPIC_API_KEY="sk-ant-..."

echo "127.0.0.1 sibyl.local" | sudo tee -a /etc/hosts

tilt up
```

Open the Tilt dashboard at `http://localhost:10350`.

## Components

| Component        | Namespace      | Source                          |
| ---------------- | -------------- | ------------------------------- |
| Gateway API CRDs | cluster        | upstream Gateway API manifest   |
| cert-manager     | `cert-manager` | `jetstack/cert-manager`         |
| Kong Operator    | `kong-system`  | `kong/kong-operator`            |
| TiDB Operator    | `tidb-admin`   | `pingcap/tidb-operator`         |
| TiKV/PD          | `sibyl`        | `infra/local/tidb-cluster.yaml` |
| SurrealDB        | `sibyl`        | `surrealdb/surrealdb`           |
| Valkey           | `sibyl`        | `valkey/valkey`                 |
| Sibyl            | `sibyl`        | `charts/sibyl`                  |
| Caddy            | local process  | `infra/local/Caddyfile`         |

## Replica Shape

| Layer          | Local demo shape       |
| -------------- | ---------------------- |
| PD             | 3 pods                 |
| TiKV           | 3 pods                 |
| SurrealDB      | 2 pods                 |
| Valkey         | 1 primary + 2 replicas |
| Sibyl backend  | 2 pods                 |
| Sibyl worker   | 2 pods                 |
| Sibyl frontend | 2 pods                 |

## Values Files

- `infra/local/tidb-cluster.yaml` defines the TiKV/PD cluster.
- `infra/local/surrealdb-values.yaml` points SurrealDB at `tikv://sibyl-tikv-pd:2379`.
- `infra/local/valkey-values.yaml` configures the official Valkey chart with ACL auth and replicas.
- `infra/local/sibyl-values.yaml` sets `store: surreal`, `authStore: surreal`, and
  `coordinationBackend: redis`.

## Access

With Caddy and Kong running:

| URL                            | Service     |
| ------------------------------ | ----------- |
| `https://sibyl.local`          | Frontend UI |
| `https://sibyl.local/api/docs` | API docs    |
| `https://sibyl.local/api/*`    | REST API    |
| `https://sibyl.local/mcp`      | MCP         |

Direct port-forwarding:

```bash
kubectl port-forward -n sibyl svc/sibyl-backend 3334:3334
kubectl port-forward -n sibyl svc/sibyl-frontend 3337:3337
kubectl port-forward -n sibyl svc/surrealdb 8000:8000
kubectl port-forward -n sibyl svc/valkey 6379:6379
```

## Checks

```bash
kubectl get tidbcluster -n sibyl sibyl-tikv
kubectl get pods -n sibyl
kubectl get deploy -n sibyl sibyl-backend sibyl-worker sibyl-frontend
kubectl get statefulset -n sibyl valkey
kubectl get svc -n sibyl surrealdb valkey
```

SurrealDB:

```bash
kubectl port-forward -n sibyl svc/surrealdb 8000:8000
surreal sql \
  --conn http://localhost:8000 \
  --user root \
  --pass "$SIBYL_SURREAL_PASSWORD"
```

Valkey:

```bash
kubectl port-forward -n sibyl svc/valkey 6379:6379
valkey-cli -a "$SIBYL_REDIS_PASSWORD" ping
```

## Cleanup

```bash
tilt down
```

TiKV persistent volumes use `Retain` in the local manifest so the demo survives pod churn. Delete
PVCs manually when you want a clean datastore.
