# Production Kubernetes Deployment

Deploy Sibyl to a production Kubernetes cluster using Helm.

## Prerequisites

- Kubernetes cluster (1.27+)
- kubectl configured for your cluster
- Helm 3.x
- **SurrealDB** instance (in-cluster StatefulSet, Surreal Cloud, or another external host)
- Legacy mode only: External PostgreSQL with pgvector, external FalkorDB/Redis+FalkorDB module

## Architecture Overview

```
+---------------------------- Production Cluster -----------------------------+
|                                                                              |
|  +------------------+     +------------------+     +------------------+      |
|  |  Ingress/Gateway |     |    Backend (n)   |     |   Frontend (n)   |      |
|  |  (Kong/Nginx)    |---->|    HPA: 2-10     |     |   HPA: 2-10      |      |
|  +------------------+     +--------+---------+     +------------------+      |
|                                    |                                         |
|                          +---------+--------+                                |
|                          |    Worker (n)    |                                |
|                          |    HPA: 1-5      |                                |
|                          +------------------+                                |
|                                                                              |
+------------------------------------------------------------------------------+
                                    |
                         +----------+-----------+
                         |      SurrealDB       |
                         | graph + content +    |
                         | auth (unified)       |
                         +----------------------+
```

For the legacy stack, swap SurrealDB for two external dependencies (PostgreSQL with pgvector and
FalkorDB). See [storage-modes.md](../guide/storage-modes.md).

## Quick Start

```bash
# Add namespace
kubectl create namespace sibyl

# Create secrets (Surreal default)
kubectl create secret generic sibyl-secrets -n sibyl \
  --from-literal=SIBYL_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=SIBYL_OPENAI_API_KEY=sk-... \
  --from-literal=SIBYL_ANTHROPIC_API_KEY=sk-ant-...

kubectl create secret generic sibyl-surreal -n sibyl \
  --from-literal=password=<your-surreal-password>

# Install with Helm
helm upgrade --install sibyl ./charts/sibyl \
  -n sibyl \
  -f values-production.yaml
```

For legacy mode, substitute `sibyl-surreal` with `sibyl-postgres` and `sibyl-falkordb` secrets, and
set `store: legacy` + `authStore: postgres` in your values file.

## Values Configuration

Create a `values-production.yaml` (Surreal default):

```yaml
# Storage mode (default is already "surreal" / "surreal")
store: "surreal"
authStore: "surreal"
coordinationBackend: "redis"

backend:
  replicaCount: 2

  image:
    repository: ghcr.io/hyperb1iss/sibyl
    tag: "0.1.0"
    pullPolicy: Always

  # Reference pre-created secrets
  existingSecret: sibyl-secrets

  # SurrealDB connection
  surreal:
    url: "ws://your-surrealdb.example.com:8000/rpc"
    username: "root"
    existingSecret: sibyl-surreal
    namespacePrefix: "org_"
    database: "graph"

  # Valkey/Redis coordination for multi-replica deployments
  redis:
    host: "valkey.example.svc.cluster.local"
    port: "6379"
    jobsDb: "1"
    rateLimitDb: "4"
    existingSecret: sibyl-secrets
    secretKey: SIBYL_REDIS_PASSWORD

  env:
    SIBYL_SERVER_HOST: "0.0.0.0"
    SIBYL_SERVER_PORT: "3334"
    SIBYL_ENVIRONMENT: "production"
    SIBYL_PUBLIC_URL: "https://sibyl.example.com"
    SIBYL_LLM_PROVIDER: "anthropic"
    SIBYL_LLM_MODEL: "claude-haiku-4-5"

  # Enable autoscaling
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80

  # Enable PodDisruptionBudget
  pdb:
    enabled: true
    minAvailable: 1

  # Spread pods across nodes
  podAntiAffinity:
    enabled: true
    type: soft
    topologyKey: kubernetes.io/hostname

  resources:
    limits:
      cpu: 2000m
      memory: 2Gi
    requests:
      cpu: 500m
      memory: 512Mi

frontend:
  enabled: true
  replicaCount: 2

  image:
    repository: ghcr.io/hyperb1iss/sibyl-web
    tag: "0.1.0"

  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10

  pdb:
    enabled: true
    minAvailable: 1

worker:
  enabled: true
  replicaCount: 2

  autoscaling:
    enabled: true
    minReplicas: 1
    maxReplicas: 5

  pdb:
    enabled: true
    minAvailable: 1

# Ingress (adjust for your ingress controller)
# NOTE: The Helm chart does not include an ingress template.
# Ingress must be created separately (see examples below) or
# an ingress template must be added to the chart.
ingress:
  enabled: true
  className: "nginx"
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
  hosts:
    - host: sibyl.example.com
      paths:
        - path: /api
          pathType: Prefix
          service: backend
        - path: /mcp
          pathType: Prefix
          service: backend
        - path: /
          pathType: Prefix
          service: frontend
  tls:
    - secretName: sibyl-tls
      hosts:
        - sibyl.example.com
```

## Database Setup

### SurrealDB Requirements (default)

- SurrealDB 2.x
- TiKV-backed storage for replicated SurrealDB pods, or RocksDB-backed storage for a single pod
- Root credentials set at start (`--user` / `--pass`)
- Persistence and backups live in the selected SurrealDB storage engine

For a scalable in-cluster shape, deploy the TiDB Operator, create a small TiKV/PD cluster, and point
the official SurrealDB Helm chart at `tikv://<pd-service>:2379`. For single-pod installs, a
RocksDB-backed PVC is simpler.

### Coordination Requirements

Set `coordinationBackend: "redis"` when running multiple backend or worker replicas. Use Valkey or
Redis for arq jobs, distributed locks, WebSocket pub/sub, and shared rate limits. The local Tilt
demo uses the official `valkey/valkey` Helm chart.

### Legacy Stack (opt-in)

**PostgreSQL** — PostgreSQL 15+ with pgvector:

```sql
CREATE USER sibyl WITH PASSWORD 'secure-password';
CREATE DATABASE sibyl OWNER sibyl;
\c sibyl
CREATE EXTENSION IF NOT EXISTS vector;
```

**FalkorDB** — FalkorDB or Redis + FalkorDB module with RDB/AOF persistence and enough memory for
graph data.

## Secrets Management

### Option 1: Kubernetes Secrets

```bash
# Create from literal values
kubectl create secret generic sibyl-secrets -n sibyl \
  --from-literal=SIBYL_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=SIBYL_OPENAI_API_KEY=sk-... \
  --from-literal=SIBYL_ANTHROPIC_API_KEY=sk-ant-...
```

### Option 2: External Secrets Operator

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: sibyl-secrets
  namespace: sibyl
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: SecretStore
  target:
    name: sibyl-secrets
  data:
    - secretKey: SIBYL_JWT_SECRET
      remoteRef:
        key: sibyl/jwt-secret
    - secretKey: SIBYL_OPENAI_API_KEY
      remoteRef:
        key: sibyl/openai-key
```

### Option 3: Sealed Secrets

```bash
# Create SealedSecret
kubeseal --format=yaml < sibyl-secrets.yaml > sibyl-secrets-sealed.yaml
kubectl apply -f sibyl-secrets-sealed.yaml
```

## Database Migrations

The Helm chart ships an Alembic migration job that runs as a pre-upgrade hook **only when
`authStore: postgres`**. Fully Surreal deployments bootstrap their schema inline at startup and skip
this step entirely.

```yaml
migrations:
  enabled: true
  backoffLimit: 3
  ttlSecondsAfterFinished: 600
```

When the hook runs, it executes `alembic upgrade head` before deploying new pods.

To run migrations manually:

```bash
kubectl run sibyl-migration \
  --rm -it --restart=Never \
  --image=ghcr.io/hyperb1iss/sibyl:0.1.0 \
  -n sibyl \
  --env-from=secret/sibyl-secrets \
  --env-from=configmap/sibyl-config \
  -- alembic upgrade head
```

## Ingress Configuration

### Kong Gateway

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: sibyl
  namespace: sibyl
spec:
  parentRefs:
    - name: production-gateway
      namespace: kong
  hostnames:
    - sibyl.example.com
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api
        - path:
            type: PathPrefix
            value: /mcp
      backendRefs:
        - name: sibyl-backend
          port: 3334
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: sibyl-frontend
          port: 3337
```

### NGINX Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sibyl
  namespace: sibyl
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "100m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - sibyl.example.com
      secretName: sibyl-tls
  rules:
    - host: sibyl.example.com
      http:
        paths:
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: sibyl-backend
                port:
                  number: 3334
          - path: /mcp
            pathType: Prefix
            backend:
              service:
                name: sibyl-backend
                port:
                  number: 3334
          - path: /
            pathType: Prefix
            backend:
              service:
                name: sibyl-frontend
                port:
                  number: 3337
```

## Health Checks

The chart configures liveness and readiness probes:

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

## Scaling

### Manual Scaling

```bash
kubectl scale deployment sibyl-backend -n sibyl --replicas=5
```

### HPA Configuration

With autoscaling enabled:

```yaml
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300 # Wait 5min before scaling down
      policies:
        - type: Percent
          value: 10
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
```

## Monitoring

### Check Deployment Status

```bash
# All resources
kubectl get all -n sibyl

# Pods status
kubectl get pods -n sibyl -o wide

# HPA status
kubectl get hpa -n sibyl

# Events
kubectl get events -n sibyl --sort-by='.lastTimestamp'
```

### View Logs

```bash
# Backend logs
kubectl logs -n sibyl -l app.kubernetes.io/component=backend -f

# Worker logs
kubectl logs -n sibyl -l app.kubernetes.io/component=worker -f

# All Sibyl logs
kubectl logs -n sibyl -l app.kubernetes.io/name=sibyl -f
```

## Upgrades

```bash
# Update values
helm upgrade sibyl ./charts/sibyl \
  -n sibyl \
  -f values-production.yaml \
  --set backend.image.tag=0.2.0

# Rollback if needed
helm rollback sibyl -n sibyl
```

## Uninstall

```bash
# Remove Helm release
helm uninstall sibyl -n sibyl

# Remove namespace (DELETES ALL DATA)
kubectl delete namespace sibyl
```

## Next Steps

- [Helm Chart Reference](helm-chart.md) - Complete values documentation
- [Environment Variables](environment.md) - All configuration options
- [Monitoring](monitoring.md) - Observability setup
