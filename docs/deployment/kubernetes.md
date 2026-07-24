# Production Kubernetes Deployment

Deploy Sibyl to a production Kubernetes cluster using Helm.

## Prerequisites

- Kubernetes cluster (1.27+)
- kubectl configured for your cluster
- Helm 3.x
- **SurrealDB** instance (in-cluster StatefulSet, Surreal Cloud, or another external host)

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

SurrealDB is the active runtime. See [storage-modes.md](../guide/storage-modes.md) for local archive
rehearsal notes.

## Quick Start

```bash
# Add namespace
kubectl create namespace sibyl

# Create secrets (Surreal default)
kubectl create secret generic sibyl-secrets -n sibyl \
  --from-literal=SIBYL_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=SIBYL_SETTINGS_KEY=$(openssl rand -base64 32 | tr '+/' '-_') \
  --from-literal=SIBYL_OPENAI_API_KEY=sk-... \
  --from-literal=SIBYL_ANTHROPIC_API_KEY=sk-ant-...

kubectl create secret generic sibyl-surreal -n sibyl \
  --from-literal=password=<your-surreal-password>

# Install with Helm from the published repository
helm repo add sibyl https://raw.githubusercontent.com/hyperb1iss/sibyl/gh-pages
helm repo update

helm upgrade --install sibyl sibyl/sibyl \
  -n sibyl \
  -f values-production.yaml
```

Installing from a source checkout works the same way with the local chart path: replace
`sibyl/sibyl` with `./charts/sibyl`.

The Helm chart deploys the SurrealDB-backed runtime only. Keep PostgreSQL archive rehearsal sidecars
and preserved FalkorDB source deployments outside this release chart.

## Values Configuration

Create a `values-production.yaml` (Surreal default):

```yaml
# Coordination mode
coordinationBackend: "redis"

backend:
  replicaCount: 2

  image:
    repository: ghcr.io/hyperb1iss/sibyl-api
    tag: "1.0.2"
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
    existingSecret: sibyl-redis
    secretKey: password

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
    tag: "1.0.2"

  apiUrl: "http://sibyl-backend:3334/api"

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
# ingress.hosts is the shared route table. Enable ingress.classic for a classic
# networking.k8s.io/v1 Ingress, or ingress.gatewayApi for a Gateway API HTTPRoute.
ingress:
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
  # Classic Ingress (NGINX and similar):
  classic:
    enabled: true
    className: "nginx"
    annotations:
      cert-manager.io/cluster-issuer: "letsencrypt-prod"
    tls:
      - secretName: sibyl-tls
        hosts:
          - sibyl.example.com
  # Gateway API HTTPRoute (Kong and similar): enable this instead of classic.
  gatewayApi:
    enabled: false
    parentRefs:
      - name: production-gateway
        namespace: kong
```

## Database Setup

### SurrealDB Requirements (default)

- SurrealDB 3.x. Pin an explicit tested image tag for production instead of floating on `latest`.
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

### Archive Rehearsal Sidecars

PostgreSQL is no longer part of the active Kubernetes runtime. Keep PostgreSQL and preserved
FalkorDB source deployments outside the release chart. Bring them up only for explicit legacy
`postgres.sql` archive rehearsal or rollback validation during a write-freeze window.

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

## Schema Bootstrap

Sibyl bootstraps SurrealDB schema inline at startup. The Helm chart no longer runs an Alembic
pre-upgrade hook for the active runtime.

## Ingress Configuration

The chart renders from the shared `ingress.hosts` route table. Set `ingress.classic.enabled=true`
for a classic `networking.k8s.io/v1` Ingress, or `ingress.gatewayApi.enabled=true` (with
`parentRefs`) for a Gateway API HTTPRoute. The standalone manifests below are equivalent
hand-written forms if you prefer to manage routing outside the chart.

### Kong Gateway (standalone HTTPRoute)

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

### NGINX Ingress (standalone manifest)

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
      path: /api/health/ready
      port: http
    initialDelaySeconds: 5
    periodSeconds: 10
```

The readiness probe targets `/api/health/ready`, the deep readiness endpoint that returns `503` when
SurrealDB is unreachable so the pod is pulled from service until it can serve. Liveness stays on
`/api/health`.

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

Keep image tags and security contexts in lockstep. Current Sibyl images run backend/worker as
`10001:10001` and frontend as `10002:10002`; older pinned images need matching overrides in
`backend.podSecurityContext`, `frontend.podSecurityContext`, and `worker.podSecurityContext`.

```bash
# Update values (use ./charts/sibyl instead when installing from source)
helm repo update
helm upgrade sibyl sibyl/sibyl \
  -n sibyl \
  -f values-production.yaml \
  --set backend.image.tag=1.0.2 \
  --set frontend.image.tag=1.0.2

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
