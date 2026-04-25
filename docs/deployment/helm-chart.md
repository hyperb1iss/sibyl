# Helm Chart Reference

Complete reference for the Sibyl Helm chart (`charts/sibyl`).

## Chart Info

```yaml
apiVersion: v2
name: sibyl
description: Knowledge graph and task workflow for durable development memory
type: application
version: 0.1.0
appVersion: "0.1.0"
```

## Installation

```bash
# From local chart
helm upgrade --install sibyl ./charts/sibyl \
  -n sibyl \
  --create-namespace \
  -f values.yaml

# Dry run
helm template sibyl ./charts/sibyl -f values.yaml
```

## Global Settings

```yaml
global:
  # Image pull secrets for private registries
  imagePullSecrets: []
```

## Database Migrations

```yaml
migrations:
  # Enable migration job (runs as pre-upgrade/pre-install hook)
  enabled: true
  # Number of retries before marking migration as failed
  backoffLimit: 3
  # Seconds to keep completed job before cleanup
  ttlSecondsAfterFinished: 600
```

## Backend Configuration

### Basic Settings

```yaml
backend:
  # Number of replicas (ignored if autoscaling is enabled)
  replicaCount: 1

  image:
    repository: ghcr.io/hyperb1iss/sibyl
    pullPolicy: IfNotPresent
    # Defaults to chart appVersion if empty
    tag: ""
```

### Service

```yaml
backend:
  service:
    type: ClusterIP
    port: 3334
    annotations: {}
    # Session affinity for MCP stateful connections
    # Set to "ClientIP" for sticky sessions (recommended for multi-replica)
    sessionAffinity: ""
    sessionAffinityConfig:
      clientIP:
        timeoutSeconds: 10800 # 3 hours
```

### Autoscaling

```yaml
backend:
  autoscaling:
    enabled: false
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80
    behavior:
      scaleDown:
        stabilizationWindowSeconds: 300
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
          - type: Pods
            value: 4
            periodSeconds: 15
        selectPolicy: Max
```

### Pod Disruption Budget

```yaml
backend:
  pdb:
    enabled: false
    # Minimum available pods (mutually exclusive with maxUnavailable)
    minAvailable: 1
    # maxUnavailable: 1
```

### Pod Anti-Affinity

```yaml
backend:
  podAntiAffinity:
    # Spreads pods across nodes
    enabled: false
    # "soft" (preferred) or "hard" (required)
    type: soft
    topologyKey: kubernetes.io/hostname
```

### Resources

```yaml
backend:
  resources:
    limits:
      cpu: 1000m
      memory: 1Gi
    requests:
      cpu: 100m
      memory: 256Mi
```

### Health Probes

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

### Environment Variables

```yaml
backend:
  env:
    SIBYL_SERVER_HOST: "0.0.0.0"
    SIBYL_SERVER_PORT: "3334"
    SIBYL_ENVIRONMENT: "production"
    SIBYL_LLM_PROVIDER: "anthropic"
    SIBYL_LLM_MODEL: "claude-haiku-4-5"
    SIBYL_EMBEDDING_MODEL: "text-embedding-3-small"
    SIBYL_EMBEDDING_DIMENSIONS: "1536"
```

### Secrets

```yaml
backend:
  # Reference to existing secret for sensitive env vars
  # Must contain: SIBYL_JWT_SECRET, SIBYL_OPENAI_API_KEY, SIBYL_ANTHROPIC_API_KEY
  existingSecret: ""
```

### Storage Mode

Pick the active persistence runtime at the top level:

```yaml
store: "surreal" # or "legacy" for FalkorDB
authStore: "surreal" # or "postgres" for relational auth
coordinationBackend: "auto"
```

See [storage-modes.md](../guide/storage-modes.md) for the mode matrix.

### SurrealDB Connection (default)

```yaml
backend:
  surreal:
    url: "ws://surrealdb:8000/rpc"
    username: "root"
    # Reference to secret containing password (auto-generated if empty)
    existingSecret: ""
    secretKey: "password"
    namespacePrefix: "org_"
    database: "graph"
```

### PostgreSQL Connection (legacy/mixed auth)

Used when `authStore: "postgres"`.

#### Option 1: CNPG Secret (Recommended)

```yaml
backend:
  database:
    # CNPG auto-generates this secret with host, port, dbname, username, password
    existingSecret: "sibyl-postgres-app"
```

#### Option 2: Manual Configuration

```yaml
backend:
  database:
    existingSecret: "" # Leave empty
    host: "postgres.example.com"
    port: "5432"
    database: "sibyl"
    user: "sibyl"
    # Password must be in a separate secret
```

### FalkorDB Connection (legacy)

Used only when `store: "legacy"`.

```yaml
backend:
  falkordb:
    host: "falkordb"
    port: "6379"
    # Reference to secret containing password
    existingSecret: ""
    # Key name in the secret (default: "password")
    secretKey: "password"
```

### Security Contexts

```yaml
backend:
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000

  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop:
        - ALL
```

### Pod Placement

```yaml
backend:
  nodeSelector: {}
  tolerations: []
  # Custom affinity (overridden by podAntiAffinity if enabled)
  affinity: {}
  podAnnotations: {}
```

## Frontend Configuration

```yaml
frontend:
  enabled: true
  replicaCount: 1

  image:
    repository: ghcr.io/hyperb1iss/sibyl-web
    pullPolicy: IfNotPresent
    tag: ""

  service:
    type: ClusterIP
    port: 3337

  autoscaling:
    enabled: false
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80
    behavior:
      scaleDown:
        stabilizationWindowSeconds: 300
        policies:
          - type: Percent
            value: 10
            periodSeconds: 60

  pdb:
    enabled: false
    minAvailable: 1

  podAntiAffinity:
    enabled: false
    type: soft
    topologyKey: kubernetes.io/hostname

  resources:
    limits:
      cpu: 500m
      memory: 512Mi
    requests:
      cpu: 50m
      memory: 128Mi

  livenessProbe:
    httpGet:
      path: /
      port: http
    initialDelaySeconds: 10
    periodSeconds: 30

  readinessProbe:
    httpGet:
      path: /
      port: http
    initialDelaySeconds: 5
    periodSeconds: 10

  env:
    NODE_ENV: "production"
    NEXT_TELEMETRY_DISABLED: "1"

  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000

  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: false # Next.js needs write access
    capabilities:
      drop:
        - ALL

  nodeSelector: {}
  tolerations: []
  affinity: {}
  podAnnotations: {}
```

## Worker Configuration

```yaml
worker:
  enabled: true
  replicaCount: 1

  # Uses same image as backend
  # (worker is sibyl backend container with different entrypoint)

  autoscaling:
    enabled: false
    minReplicas: 1
    maxReplicas: 5
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80
    behavior:
      scaleDown:
        stabilizationWindowSeconds: 300
        policies:
          - type: Percent
            value: 10
            periodSeconds: 60

  pdb:
    enabled: false
    minAvailable: 1

  podAntiAffinity:
    enabled: false
    type: soft
    topologyKey: kubernetes.io/hostname

  resources:
    limits:
      cpu: 500m
      memory: 512Mi
    requests:
      cpu: 50m
      memory: 128Mi

  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000

  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop:
        - ALL

  nodeSelector: {}
  tolerations: []
  affinity: {}
  podAnnotations: {}
```

## Ingress Configuration

```yaml
ingress:
  enabled: false
  className: "kong"
  annotations: {}
  hosts:
    - host: sibyl.local
      paths:
        - path: /api
          pathType: Prefix
          service: backend
        - path: /
          pathType: Prefix
          service: frontend
  tls: []
  # - secretName: sibyl-tls
  #   hosts:
  #     - sibyl.example.com
```

## Service Account

```yaml
serviceAccount:
  create: true
  name: ""
  annotations: {}
```

## Production Example

Complete production-ready values:

```yaml
global:
  imagePullSecrets:
    - name: ghcr-pull-secret

store: "surreal"
authStore: "surreal"
coordinationBackend: "auto"

backend:
  replicaCount: 3
  image:
    repository: ghcr.io/hyperb1iss/sibyl
    tag: "0.1.0"
    pullPolicy: Always
  existingSecret: sibyl-secrets
  surreal:
    url: "ws://prod-surrealdb.internal:8000/rpc"
    username: "root"
    existingSecret: sibyl-surreal
    namespacePrefix: "org_"
    database: "graph"
  env:
    SIBYL_ENVIRONMENT: "production"
    SIBYL_PUBLIC_URL: "https://sibyl.example.com"
  autoscaling:
    enabled: true
    minReplicas: 3
    maxReplicas: 20
  pdb:
    enabled: true
    minAvailable: 2
  podAntiAffinity:
    enabled: true
    type: hard
  resources:
    limits:
      cpu: 4000m
      memory: 4Gi
    requests:
      cpu: 1000m
      memory: 1Gi

frontend:
  enabled: true
  replicaCount: 2
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
  pdb:
    enabled: true
  podAntiAffinity:
    enabled: true

worker:
  enabled: true
  replicaCount: 2
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 8
  pdb:
    enabled: true
  podAntiAffinity:
    enabled: true

ingress:
  enabled: true
  className: "nginx"
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/proxy-body-size: "100m"
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

serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789:role/sibyl
```

## Chart Templates

The chart includes these templates:

| Template                 | Purpose                                      |
| ------------------------ | -------------------------------------------- |
| backend-deployment.yaml  | Backend Deployment                           |
| backend-service.yaml     | Backend ClusterIP Service                    |
| backend-hpa.yaml         | Backend HorizontalPodAutoscaler              |
| frontend-deployment.yaml | Frontend Deployment                          |
| frontend-service.yaml    | Frontend ClusterIP Service                   |
| frontend-hpa.yaml        | Frontend HorizontalPodAutoscaler             |
| worker-deployment.yaml   | Worker Deployment                            |
| worker-hpa.yaml          | Worker HorizontalPodAutoscaler               |
| pdb.yaml                 | PodDisruptionBudgets                         |
| configmap.yaml           | Non-secret environment config                |
| surreal-secret.yaml      | Auto-generated Surreal secret (default)      |
| falkordb-secret.yaml     | Auto-generated FalkorDB secret (legacy only) |
| migration-job.yaml       | Alembic migration Job (postgres auth only)   |
| serviceaccount.yaml      | ServiceAccount                               |

## Debugging

```bash
# Render templates locally
helm template sibyl ./charts/sibyl -f values.yaml

# Debug with notes
helm install sibyl ./charts/sibyl --debug --dry-run

# Get release values
helm get values sibyl -n sibyl

# Get all manifests
helm get manifest sibyl -n sibyl
```
