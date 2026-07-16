# Helm Chart Reference

Complete reference for the Sibyl Helm chart (`charts/sibyl`).

## Chart Info

```yaml
apiVersion: v2
name: sibyl
description: Knowledge graph and task workflow for durable development memory
type: application
version: 1.0.2
appVersion: "1.0.2"
```

Release builds update `version` and `appVersion` from the repository `VERSION` file.

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

## Schema Bootstrap

Sibyl bootstraps SurrealDB schemas at application startup. The chart does not run Alembic or
PostgreSQL migration jobs.

## Authentication Defaults

The chart defaults match the self-hosted single-user path. Local username/password login is enabled,
the first setup signup creates the owner/admin user, post-setup account creation is invite-only, and
OIDC is empty until an operator configures it.

```yaml
auth:
  # Local username/password login is the default simple path.
  localAuthEnabled: true
  # Public account creation after setup stays invite-only by default.
  publicSignupsEnabled: false

oidc:
  providers: []
  silent_refresh_enabled: false
  extra_providers_enabled: false

breakGlass:
  enabled: false
```

For enterprise SSO deployments, configure a corporate OIDC provider and set
`auth.localAuthEnabled=false` only after an owner has successfully signed in through OIDC.
Break-glass access remains a separate, bounded opt-in.

Each configured provider requires an exact organization binding:

```yaml
oidc:
  providers:
    - name: entra
      issuer: "https://login.microsoftonline.com/<tenant-id>/v2.0"
      client_id: "<app-client-id>"
      client_secret_env: "SIBYL_OIDC_ENTRA_CLIENT_SECRET"
      organization_slug: "acme"
```

The organization must already exist and must not be personal. Helm rejects providers without this
binding, and Sibyl fails closed if the bound organization cannot be resolved.

## Backend Configuration

### Basic Settings

```yaml
backend:
  # Number of replicas (ignored if autoscaling is enabled)
  replicaCount: 1

  image:
    repository: ghcr.io/hyperb1iss/sibyl-api
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
    initialDelaySeconds: 60
    periodSeconds: 30

  readinessProbe:
    httpGet:
      path: /api/health/ready
      port: http
    initialDelaySeconds: 5
    periodSeconds: 10
```

The readiness probe must hit `/api/health/ready`, the deep readiness endpoint that returns `503`
when SurrealDB is unreachable. Liveness stays on `/api/health`, which only asserts the process is
up.

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
    # BLAS/OpenMP thread caps keep native math libraries from oversubscribing pods
    OPENBLAS_NUM_THREADS: "1"
    OMP_NUM_THREADS: "1"
    MKL_NUM_THREADS: "1"
    NUMEXPR_NUM_THREADS: "1"
```

### Secrets

```yaml
backend:
  # Reference to existing secret for sensitive env vars
  # Must contain: SIBYL_JWT_SECRET and SIBYL_SETTINGS_KEY.
  # Add provider and LLM API keys as needed.
  existingSecret: ""
```

Production and read-only containers must set `existingSecret`. Both required keys are non-optional
pod references, so a missing key fails before Sibyl starts instead of silently generating an
ephemeral replacement. Keep `SIBYL_SETTINGS_KEY` stable across upgrades and pod replacements;
changing it makes encrypted settings unreadable.

### Storage Mode

The active persistence runtime is fixed to SurrealDB. Use Redis/Valkey coordination for multi-pod
deployments:

```yaml
coordinationBackend: "auto" # use "redis" for multi-pod deployments
```

See [storage-modes.md](../guide/storage-modes.md) for the mode matrix.

### SurrealDB Connection (default)

```yaml
backend:
  surreal:
    # ws:// or http:// URL to an external SurrealDB instance
    url: "ws://surrealdb:8000/rpc"
    username: "root"
    # Reference to a secret containing the password.
    # When empty, a password is auto-generated and stored in `<release>-surreal`.
    existingSecret: ""
    # Secret key holding the password (only used when existingSecret is set)
    secretKey: "password"
    # Inline password (ignored when existingSecret is set; auto-generated otherwise)
    password: ""
    namespacePrefix: "org_"
    database: "graph"
```

### Redis or Valkey Coordination

Used when `coordinationBackend: "redis"`. This is recommended when running more than one backend or
worker pod because it backs arq jobs, distributed locks, WebSocket pub/sub, and shared rate limits.

```yaml
backend:
  redis:
    host: "valkey"
    port: "6379"
    jobsDb: "1"
    rateLimitDb: "4"
    # Reference to a secret containing the Redis/Valkey password
    existingSecret: ""
    secretKey: "password"
    # Inline password (not recommended; prefer existingSecret)
    password: ""
    # Shared rate-limit storage URL.
    # Leave empty to derive a redis:// URL from host/port/rateLimitDb.
    rateLimitStorage: ""
```

The chart emits a password-free `SIBYL_RATE_LIMIT_STORAGE` ConfigMap value and Sibyl injects the
Redis password from `SIBYL_REDIS_PASSWORD` at runtime.

### Security Contexts

The default UIDs match the packaged images for this chart version. If you intentionally pin older
image tags, keep the image tag and security context in lockstep: pre-service-UID API/worker images
use `1000:1000`, and pre-service-UID web images use `1001:65533`.

```yaml
backend:
  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001

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

  # Defaults to http://<release>-backend:<port>/api when empty
  apiUrl: ""
  # Optional browser-visible API URL. Leave empty for same-origin ingress.
  publicApiUrl: ""

  podSecurityContext:
    runAsNonRoot: true
    runAsUser: 10002
    runAsGroup: 10002
    fsGroup: 10002

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
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001

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

Ingress is split into a shared route table (`ingress.hosts`) plus two independently toggled
renderers: classic `networking.k8s.io/v1` Ingress under `ingress.classic`, and Gateway API
`gateway.networking.k8s.io/v1` HTTPRoute under `ingress.gatewayApi`. Enable whichever your cluster
uses. The legacy `ingress.enabled` flag still works as a compatibility toggle for classic Ingress.

```yaml
ingress:
  # Shared route table consumed by both classic Ingress and Gateway API HTTPRoute.
  hosts:
    - host: sibyl.local
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
  classic:
    # Render a networking.k8s.io/v1 Ingress.
    enabled: false
    # Ingress class name. Empty leaves controller selection to cluster policy.
    className: ""
    annotations: {}
    tls: []
    # - secretName: sibyl-tls
    #   hosts:
    #     - sibyl.example.com
  gatewayApi:
    # Render a gateway.networking.k8s.io/v1 HTTPRoute.
    enabled: false
    annotations: {}
    parentRefs: []
    # - name: shared-gateway
    #   namespace: gateway-system
    #   sectionName: https
    # Optional hostnames override. Defaults to ingress.hosts[*].host.
    hostnames: []
```

## Network Policy

Renders a default-deny NetworkPolicy plus explicit allows for frontend/backend ingress and SurrealDB
(and optional Redis/Valkey) egress. Leave disabled unless your cluster enforces NetworkPolicies.

```yaml
networkPolicy:
  # Enable default-deny plus explicit app allows.
  enabled: false
  ingress:
    # Sources allowed to reach frontend/backend, usually ingress controller pods.
    from: []
    # - namespaceSelector:
    #     matchLabels:
    #       kubernetes.io/metadata.name: ingress-nginx
  egress:
    # Allow DNS egress for selected Sibyl pods.
    allowDns: true
    dnsPorts:
      - 53
    # Extra egress rules appended to backend/worker policies.
    extra: []
  surrealdb:
    # Destination selectors for the SurrealDB service/pods.
    to: []
    ports:
      - 8000
  redis:
    # Enable Redis/Valkey egress allow rules.
    enabled: false
    to: []
    ports:
      - 6379
```

## Pod Security

Labels the release namespace with Pod Security Admission enforce/audit/warn at the `restricted`
level. Enable for hardened multi-tenant clusters.

```yaml
podSecurity:
  # Label the release namespace with Pod Security restricted enforce/audit/warn.
  enforceRestricted: false
  version: "latest"
```

## Bootstrap

A post-install/post-upgrade Job that seeds the first organization and an optional default memory
space, so a fresh install lands ready to use. Disabled by default.

```yaml
bootstrap:
  enabled: false
  organization:
    name: "Sibyl"
    slug: ""
  memorySpace:
    enabled: true
    name: "Default memory"
    scope: "private"
    scopeKey: ""
  job:
    annotations: {}
    backoffLimit: 3
    ttlSecondsAfterFinished: 300
    podAnnotations: {}
```

## Break-Glass

Bounded emergency local-owner login for SSO outages. Keep disabled in normal operation; when
enabled, both `expiresAt` (no more than four hours out) and `allowedIPs` are required.

```yaml
breakGlass:
  enabled: false
  # Source CIDRs allowed to use break-glass login when enabled.
  allowedIPs: []
  # UTC timestamp after which app-level break-glass login is denied.
  expiresAt: ""
  # Existing secret containing owner bootstrap fields.
  existingSecret: ""
  ownerEmailKey: "owner-email"
  ownerPasswordKey: "owner-password"
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

coordinationBackend: "auto"

backend:
  replicaCount: 3
  image:
    repository: ghcr.io/hyperb1iss/sibyl-api
    tag: "1.0.2"
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
  apiUrl: "http://sibyl-backend:3334/api"
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
  classic:
    enabled: true
    className: "nginx"
    annotations:
      cert-manager.io/cluster-issuer: "letsencrypt-prod"
      nginx.ingress.kubernetes.io/proxy-body-size: "100m"
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

| Template                 | Purpose                                     |
| ------------------------ | ------------------------------------------- |
| backend-deployment.yaml  | Backend Deployment                          |
| backend-service.yaml     | Backend ClusterIP Service                   |
| backend-hpa.yaml         | Backend HorizontalPodAutoscaler             |
| frontend-deployment.yaml | Frontend Deployment                         |
| frontend-service.yaml    | Frontend ClusterIP Service                  |
| frontend-hpa.yaml        | Frontend HorizontalPodAutoscaler            |
| worker-deployment.yaml   | Worker Deployment                           |
| worker-hpa.yaml          | Worker HorizontalPodAutoscaler              |
| pdb.yaml                 | PodDisruptionBudgets                        |
| configmap.yaml           | Non-secret environment config               |
| surreal-secret.yaml      | Auto-generated Surreal secret (default)     |
| redis-secret.yaml        | Auto-generated Redis/Valkey secret          |
| bootstrap-job.yaml       | Post-install tenant bootstrap Job           |
| ingress.yaml             | Classic networking.k8s.io/v1 Ingress        |
| httproute.yaml           | Gateway API HTTPRoute                       |
| networkpolicy.yaml       | Default-deny plus app-allow NetworkPolicies |
| podsecurity.yaml         | Namespace Pod Security enforcement labels   |
| serviceaccount.yaml      | ServiceAccount                              |

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
