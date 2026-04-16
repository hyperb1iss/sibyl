# Production Kubernetes Deployment

Deploy Sibyl to a production Kubernetes cluster using Helm.

## Prerequisites

- Kubernetes cluster (1.27+)
- kubectl configured for your cluster
- Helm 3.x
- External PostgreSQL with pgvector extension
- External FalkorDB or Redis with FalkorDB module

## Architecture Overview

```
+---------------------------- Production Cluster -----------------------------+
|                                                                              |
|  +------------------+     +------------------+     +------------------+      |
|  |  Ingress/Gateway |     |    Backend (n)   |     |   Frontend (n)   |      |
|  |  (Kong/Nginx)    |---->|    HPA: 2-10     |     |   HPA: 2-10      |      |
|  +------------------+     +--------+---------+     +------------------+      |
|                                    |                                         |
|                          +--------+---------+                                |
|                          |    Worker (n)    |                                |
|                          |    HPA: 1-5      |                                |
|                          +------------------+                                |
|                                                                              |
+------------------------------------------------------------------------------+
                |                           |
     +----------+----------+     +----------+----------+
     |   External Postgres |     |   External FalkorDB |
     |   (RDS, Cloud SQL)  |     |   (Redis Cloud)     |
     +---------------------+     +---------------------+
```

## Quick Start

```bash
# Add namespace
kubectl create namespace sibyl

# Create secrets
kubectl create secret generic sibyl-secrets -n sibyl \
  --from-literal=SIBYL_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=SIBYL_OPENAI_API_KEY=sk-... \
  --from-literal=SIBYL_ANTHROPIC_API_KEY=sk-ant-...

kubectl create secret generic sibyl-postgres -n sibyl \
  --from-literal=password=<your-postgres-password>

kubectl create secret generic sibyl-falkordb -n sibyl \
  --from-literal=password=<your-falkordb-password>

# Install with Helm
helm upgrade --install sibyl ./charts/sibyl \
  -n sibyl \
  -f values-production.yaml
```

## Values Configuration

Create a `values-production.yaml`:

```yaml
backend:
  replicaCount: 2

  image:
    repository: ghcr.io/hyperb1iss/sibyl
    tag: "0.1.0"
    pullPolicy: Always

  # Reference pre-created secrets
  existingSecret: sibyl-secrets

  # External PostgreSQL
  database:
    existingSecret: "" # Empty = use manual config below
    host: "your-postgres.example.com"
    port: "5432"
    database: "sibyl"
    user: "sibyl"
    # Password from sibyl-postgres secret

  # External FalkorDB
  falkordb:
    host: "your-falkordb.example.com"
    port: "6379"
    existingSecret: sibyl-falkordb

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

### PostgreSQL Requirements

- PostgreSQL 15+ recommended
- pgvector extension installed
- Sufficient connections (backend.postgres_pool_size \* replicas)

```sql
-- Create database and user
CREATE USER sibyl WITH PASSWORD 'secure-password';
CREATE DATABASE sibyl OWNER sibyl;

-- Connect to sibyl database and enable pgvector
\c sibyl
CREATE EXTENSION IF NOT EXISTS vector;
```

### FalkorDB Requirements

- FalkorDB or Redis with FalkorDB module
- Persistence enabled (RDB or AOF)
- Sufficient memory for graph data

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

The Helm chart includes a migration job that runs as a pre-upgrade hook:

```yaml
migrations:
  enabled: true
  backoffLimit: 3
  ttlSecondsAfterFinished: 600
```

This runs `alembic upgrade head` before deploying new pods.

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
