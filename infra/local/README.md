# Sibyl Local Infrastructure

Local Kubernetes development for a small scalable Sibyl fleet.

The Tilt path runs Sibyl in its default SurrealDB-native mode, with TiKV as SurrealDB's distributed
datastore and Valkey as the coordination plane for jobs, locks, pub/sub, and rate limits.

## Components

| Component | Chart or manifest | Purpose |
| --- | --- | --- |
| Gateway API | upstream CRDs | Gateway resources for Kong |
| cert-manager | `jetstack/cert-manager` | Local TLS certificate plumbing |
| Kong Operator | `kong/kong-operator` | Gateway API implementation |
| TiDB Operator | `pingcap/tidb-operator` | Manages the TiKV cluster |
| TiKV/PD | `infra/local/tidb-cluster.yaml` | Distributed datastore for SurrealDB |
| SurrealDB | `surrealdb/surrealdb` | Graph, content, and auth store |
| Valkey | `valkey/valkey` | Distributed coordination for Sibyl replicas |
| Sibyl | `../../charts/sibyl` | Backend, worker, and frontend deployments |

## Shape

- 3 PD pods and 3 TiKV pods for the datastore demo
- 2 SurrealDB pods connected to `tikv://sibyl-tikv-pd:2379`
- 3 Valkey pods: one primary plus two replicas
- 2 Sibyl backend pods
- 2 Sibyl worker pods
- 2 Sibyl frontend pods

## Quick Start

```bash
# Start your Kubernetes environment first. Minikube still works:
minikube start --cpus=6 --memory=12288 --driver=docker

# Or use Podman Desktop's Kind integration:
# Settings > Resources > Kind > Create new
kubectl config get-contexts
export SIBYL_K8S_CONTEXT=kind-sibyl # replace with your kind-<cluster> context

# Or use OrbStack's Kubernetes runtime:
orb start k8s
export SIBYL_K8S_CONTEXT=orbstack

podman --version # or docker version

export SIBYL_JWT_SECRET="$(openssl rand -hex 32)"
export SIBYL_SURREAL_PASSWORD="sibyl-local-dev"
export SIBYL_REDIS_PASSWORD="sibyl-local-dev"
export ANTHROPIC_API_KEY="sk-ant-..."

tilt up
```

The Tiltfile creates the local `sibyl-secrets` Secret from those environment variables.

Image loading is selected from the Kubernetes context. `minikube` uses `minikube image load`;
`kind-*` contexts use `kind load image-archive`, which matches Podman Desktop's Kind-powered
Kubernetes flow. `orbstack` uses the shared OrbStack container engine directly, so Tilt builds with
Docker and skips an explicit image load. Override with `SIBYL_CONTAINER_BUILDER=podman|docker` or
`SIBYL_IMAGE_LOADER=minikube|kind|none` when using a custom local cluster or registry.

## Manual Render Checks

```bash
helm template surrealdb surrealdb/surrealdb \
  --version 0.4.0 \
  -n sibyl \
  -f surrealdb-values.yaml

helm template valkey valkey/valkey \
  --version 0.9.4 \
  -n sibyl \
  -f valkey-values.yaml

helm template sibyl ../../charts/sibyl \
  -n sibyl \
  -f sibyl-values.yaml
```

## Access

```bash
kubectl port-forward -n sibyl svc/surrealdb 8000:8000
kubectl port-forward -n sibyl svc/valkey 6379:6379
kubectl port-forward -n sibyl svc/sibyl-backend 3334:3334
kubectl port-forward -n sibyl svc/sibyl-frontend 3337:3337
```
