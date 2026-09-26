# Helm Chart Reference

Complete reference for the Sibyl Helm chart (`charts/sibyl`).

## Chart Info

```yaml
apiVersion: v2
name: sibyl
description: Knowledge graph and task workflow for durable development memory
type: application
version: 1.4.1
appVersion: "1.4.1"
```

Release builds update `version` and `appVersion` from the repository `VERSION` file.

## Installation

Release charts are published to the `sibyl` Helm repository on the `gh-pages` branch. The repository
serves two charts: `sibyl` (the application) and `sibyl-surrealdb` (a SurrealDB wrapper).

Provision a persistent claim before installation and set `backend.validationReceipts.existingClaim`
in `values.yaml`. API and worker must share that claim; multi-node replicas require ReadWriteMany
storage.

```bash
# From the published Helm repository (recommended)
helm repo add sibyl https://raw.githubusercontent.com/hyperb1iss/sibyl/gh-pages
helm repo update

helm upgrade --install sibyl sibyl/sibyl \
  -n sibyl \
  --create-namespace \
  -f values.yaml
```

To install from a source checkout instead:

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

Sibyl bootstraps SurrealDB schemas at application startup. The chart runs no separate migration job.

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
publicUrl: "https://sibyl.example.com"

backend:
  # Number of replicas (ignored if autoscaling is enabled)
  replicaCount: 1

  # Empty selects Recreate for a single fixed replica, RollingUpdate otherwise
  strategy: {}

  image:
    repository: ghcr.io/hyperb1iss/sibyl-api
    pullPolicy: IfNotPresent
    # Defaults to chart appVersion if empty
    tag: ""
```

Set `publicUrl` to the external application origin. The chart passes the value to the backend for
callbacks, password resets, and redirects. It also derives the browser API and WebSocket origin. Use
`frontend.publicApiUrl` only when the browser reaches the API through a different origin.

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

### Trusted Proxies

Behind an ingress controller or Gateway, every request reaches the backend from the proxy pod's
address. The login route allows five attempts per minute per client address, so until the backend
trusts the proxy, all users share one bucket and the sixth login in a minute from anyone locks
everyone out. The `backend.forwardedAllowIps` value names the proxies whose `X-Forwarded-For` header
the backend believes. The chart renders it into `SIBYL_FORWARDED_ALLOW_IPS`.

| Value                       | Default | Description                                            |
| --------------------------- | ------- | ------------------------------------------------------ |
| `backend.forwardedAllowIps` | `""`    | IPs and CIDR ranges of trusted proxies, string or list |

```yaml
backend:
  # A range only the ingress controller pods draw their addresses from, never the pod CIDR.
  forwardedAllowIps: "10.250.0.0/28"
  # A list works too:
  # forwardedAllowIps:
  #   - 10.250.0.0/28
  #   - 10.250.1.0/28
```

On the command line, pass a list so Helm does not split on the comma:
`--set 'backend.forwardedAllowIps={10.250.0.0/28,10.250.1.0/28}'`.

Empty (the default) keeps the backend's loopback-only trust. When `backend.forwardedAllowIps` is
empty, a `SIBYL_FORWARDED_ALLOW_IPS` entry under `backend.env` still works; when both are set, the
dedicated value wins.

**Every trusted address must be a proxy, never something that could be a client.** A trusted peer
can name any client address it likes, and an ingress that appends to `X-Forwarded-For` instead of
replacing it extends that power to everyone who calls through it from a trusted address. Envoy-based
gateways, AWS ALB, and nginx's `$proxy_add_x_forwarded_for` all append. With the pod CIDR trusted, a
pod at `10.244.9.9` that sends `X-Forwarded-For: 100.101.102.103` through such an ingress arrives as
`100.101.102.103, 10.244.9.9`; the backend skips the pod's own address as trusted and resolves the
forged one. A NetworkPolicy cannot stop this, because the request really does come from the
controller. So never trust the cluster pod CIDR (it covers every pod, and with
`networkPolicy.enabled` at its default of `false` any of them can also reach the backend directly),
node ranges, or the VPC CIDR: each of them holds clients.

What to set:

- **Trust only the ingress controller pods.** Give them addresses nothing else draws from, such as a
  CNI IP pool selected by the controller's namespace or a node pool reserved for the controller with
  its own pod range, and list that range. Running
  `kubectl get pods -n <controller-namespace> -o wide` shows the addresses it has to cover.
- **Make the controller replace `X-Forwarded-For` from untrusted peers rather than append to it.**
  ingress-nginx does by default, as long as `use-forwarded-headers` and `compute-full-forwarded-for`
  stay off. With a controller that can only append, the trust list is all that keeps a forged entry
  out, so it must hold nothing but the controller.
- **A load balancer in front of the controller:** resolve the client at the controller rather than
  widening the backend's list. Have the controller trust only the load balancer's dedicated subnets
  (in ingress-nginx, `use-forwarded-headers: true` with `proxy-real-ip-cidr` set to them, because
  its default is `0.0.0.0/0`), so it still hands the backend a single resolved address, and keep the
  backend trusting the controller alone.
- **An L4 load balancer or a node hop that SNATs:** do not trust node addresses. The controller then
  only ever sees node IPs, and trusting them hands the entry the client wrote the final say.
  Preserve the source address instead (`externalTrafficPolicy: Local` on the controller's Service,
  or PROXY protocol from the load balancer) and trust only the controller pods.
- **Enable `networkPolicy`** with `networkPolicy.ingress.from` naming the controller, so nothing can
  reach the backend directly and write the header itself.

::: warning Trusting a range lets anything inside it choose its client address

Any peer inside `forwardedAllowIps` can claim to be any client, dodge the per-address rate limits,
and satisfy `breakGlass.allowedIPs`, and so can any caller behind an appending ingress whose own
address is trusted. List the controller and nothing else. Keep `/api` and `/mcp` routed straight to
the backend service (the default route table): the Next.js frontend passes a client-supplied
`X-Forwarded-For` through unchanged, so it must never be the hop the backend trusts. A value of
`"*"`, or any range broader than an IPv4 `/8` or an IPv6 `/32`, trusts every peer or nearly every
one and makes the backend log `forwarded_allow_ips_trusts_every_peer` at startup; with every hop
trusted, the leftmost header entry wins, and a client writes that one unless every proxy in front
overwrites the header.

:::

To confirm the setting took, sign in through the ingress and check that the backend's `request` log
lines carry your own address in `client`, not the controller pod's. The
[environment reference](./environment.md#trusted-proxies) explains how the backend resolves the
address and which values it accepts.

### Secrets

```yaml
backend:
  # Reference to existing secret for sensitive env vars
  # Must contain: SIBYL_JWT_SECRET and SIBYL_SETTINGS_KEY.
  # Add provider and LLM API keys as needed; Amazon Bedrock needs none.
  existingSecret: ""
```

Production and read-only containers must set `existingSecret`. The chart enforces this at render
time: with `backend.env.SIBYL_ENVIRONMENT` set to `production` and no `backend.existingSecret`,
`helm template` and `helm install` both abort with an error naming the value to set. That guard
exists because an unset `SIBYL_JWT_SECRET` makes the backend sign sessions with an empty key and
makes MCP auth disable itself under the default `mcp_auth_mode: auto`. The chart deliberately does
not auto-generate the secret in production, since a per-render value would rotate on every upgrade
and diverge across the backend, worker, and bootstrap pods.

Passing the signing key as `backend.env.SIBYL_JWT_SECRET` fails the render as well, and so does the
unprefixed `backend.env.JWT_SECRET` alias that the server falls back to. Every key under
`backend.env` is written into the plaintext `<release>-config` ConfigMap, which is readable under
broader RBAC than a Secret and routinely committed to GitOps repositories. Key names are matched
case-insensitively, because pydantic-settings resolves environment variables that way.

Production also rejects `backend.env.SIBYL_MCP_AUTH_MODE: off`, which would serve every MCP tool
unauthenticated no matter how well the JWT secret is provisioned. Use `auto` to enforce Bearer auth
once a secret is set, or `on` to enforce it unconditionally. The server refuses to boot on the same
condition, so the mode cannot be switched off in production by editing the ConfigMap after install.

The escape hatch for a trial install is `backend.env.SIBYL_ENVIRONMENT: development`, which is
unsuitable for real traffic: the server then derives a JWT secret per process, and the chart's
default `readOnlyRootFilesystem: true` blocks the write that would persist it, so sessions break on
every restart and across replicas.

Both required keys are non-optional pod references, so a missing key fails before Sibyl starts
instead of silently generating an ephemeral replacement. Keep `SIBYL_SETTINGS_KEY` stable across
upgrades and pod replacements; changing it makes encrypted settings unreadable.

### Storage Mode

The active persistence runtime is fixed to SurrealDB. Local and auto coordination run jobs in the
API process and do not render a separate worker. Set Redis coordination explicitly for multi-pod
deployments:

```yaml
coordinationBackend: "redis"
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

The chart renders `backend.surreal.url` into `SIBYL_SURREAL_URL`. Point it at a SurrealDB server
(`ws://`, `wss://`, `http://`, or `https://`). The backend refuses an unsupported scheme at startup:
`rocksdb://` and `tikv://` are storage arguments for the SurrealDB server, not client URLs. See
[SurrealDB URL forms](./environment.md#surrealdb-url-forms).

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
    fsGroupChangePolicy: OnRootMismatch

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
  # Optional browser-visible API URL. When empty, publicUrl supplies <origin>/api.
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

The chart renders this deployment only when both `worker.enabled` is true and `coordinationBackend`
is `redis`. Local and auto coordination keep job execution in the API process.

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
    fsGroupChangePolicy: OnRootMismatch

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
enabled, both `expiresAt` (no more than four hours out) and `allowedIPs` are required. The allowlist
is matched against the resolved client address, so behind an ingress set `backend.forwardedAllowIps`
(see [Trusted Proxies](#trusted-proxies)); without it the backend only ever sees the controller
pod's address.

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

The backend, worker, bootstrap and frontend pods all run as this service account, so an annotation
here reaches every pod that calls a model provider.

## Amazon Bedrock

On EKS, Sibyl can run Claude and Cohere embeddings through Amazon Bedrock with no provider API keys.
Bind an IAM role to the service account with IRSA, set a Region, and select the `bedrock` provider:

```yaml
serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<account-id>:role/sibyl-bedrock

backend:
  # Still required in production, but it only needs SIBYL_JWT_SECRET and SIBYL_SETTINGS_KEY.
  existingSecret: sibyl-secrets
  env:
    AWS_REGION: "us-west-2"
    SIBYL_LLM_PROVIDER: "bedrock"
    SIBYL_LLM_MODEL: "claude-haiku-4-5"
    SIBYL_LLM_MEMORY_MODEL: "claude-opus-5-5"
    SIBYL_BEDROCK_INFERENCE_SCOPE: "us"
    SIBYL_EMBEDDING_PROVIDER: "bedrock"
    SIBYL_EMBEDDING_MODEL: "cohere.embed-v4:0"
    SIBYL_EMBEDDING_DIMENSIONS: "1536"
    SIBYL_GRAPH_EMBEDDING_PROVIDER: "bedrock"
```

The IRSA webhook injects `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` into each pod, and the AWS
credential chain exchanges that token for role credentials and refreshes them before they expire.
EKS Pod Identity works the same way through an association on the service account, with no
annotation. Both paths leave `SIBYL_ANTHROPIC_API_KEY` and `SIBYL_OPENAI_API_KEY` unset: the chart
references them from `backend.existingSecret` as optional keys, so the Secret does not need them.

The role needs `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the inference
profiles and foundation models it routes to, such as `us.anthropic.claude-*`, `anthropic.claude-*`
and `cohere.embed-v4:0`. See [Amazon Bedrock](./environment.md#amazon-bedrock) for every setting,
the inference scopes and the features Bedrock accepts per model. The admin AI settings page **Test**
button proves the role works from inside the pod.

With `networkPolicy.enabled`, add an `egress.extra` rule that allows HTTPS (443) to the Bedrock
runtime and STS endpoints, or through the proxy or VPC endpoints your cluster uses for AWS APIs.
Switching an embedding provider changes the vector space, so plan a re-embed before trusting mixed
results.

## Production Example

Complete production-ready values:

```yaml
global:
  imagePullSecrets:
    - name: ghcr-pull-secret

coordinationBackend: "redis"
publicUrl: "https://sibyl.example.com"

backend:
  replicaCount: 3
  image:
    repository: ghcr.io/hyperb1iss/sibyl-api
    tag: "1.4.1"
    pullPolicy: Always
  existingSecret: sibyl-secrets
  # Only the ingress controller pods' own range, so each user gets their own login
  # rate-limit bucket. Never the pod CIDR (see Trusted Proxies).
  forwardedAllowIps: "10.250.0.0/28"
  validationReceipts:
    existingClaim: sibyl-validation-receipts
  surreal:
    url: "ws://prod-surrealdb.internal:8000/rpc"
    username: "root"
    existingSecret: sibyl-surreal
    namespacePrefix: "org_"
    database: "graph"
  redis:
    host: "prod-valkey.internal"
    port: "6379"
    existingSecret: sibyl-redis
  env:
    SIBYL_ENVIRONMENT: "production"
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
helm install sibyl ./charts/sibyl -f values.yaml --debug --dry-run

# Get release values
helm get values sibyl -n sibyl

# Get all manifests
helm get manifest sibyl -n sibyl
```

## Durable Validation Receipts

Provision a persistent claim and set `backend.validationReceipts.existingClaim` before installing or
upgrading. The chart rejects an absent claim at render time. API and worker mount the same claim.
Multi-node replicas require a ReadWriteMany volume; provision it with the storage class supported by
your cluster. The service user (UID/GID 10001) must be able to create a private child directory. No
temporary volume or single-replica fallback is used.

Two chart defaults keep a ReadWriteOnce block volume (EBS, Persistent Disk, Azure Disk) working
across restarts and rollouts:

- `fsGroupChangePolicy: OnRootMismatch` on the backend and worker pod security contexts. Block CSI
  drivers re-apply `fsGroup` on every mount by default, adding group permission bits to every file
  and directory, and Sibyl refuses a receipts directory or file with any group or other bits. Keep
  this key if you override `podSecurityContext`.
- An empty `backend.strategy` or `worker.strategy` rolls a single fixed replica with `maxSurge: 0`
  and `maxUnavailable: 1`, because a surge pod scheduled onto another node cannot attach a
  ReadWriteOnce claim. The old pod stops before its replacement starts; if the replacement lands on
  another node, it waits for the volume to detach and then starts on its own. With more replicas or
  autoscaling the Kubernetes `RollingUpdate` default applies, which assumes ReadWriteMany storage.
  An explicit strategy passes through unchanged.

The single-replica default stays `type: RollingUpdate` rather than `Recreate` on purpose. The API
server fills in `rollingUpdate` on every existing Deployment, no applier owns that field, and
server-side apply (the Helm 4 default, Argo CD with `ServerSideApply=true`, Flux) can never remove
it, so an upgrade that switches `type` to `Recreate` is rejected with
`spec.strategy.rollingUpdate: Forbidden`. To opt into `Recreate` on an existing release, remove the
field once first:

```bash
kubectl -n sibyl patch deploy sibyl-backend --type=json \
  -p '[{"op":"remove","path":"/spec/strategy/rollingUpdate"},{"op":"replace","path":"/spec/strategy/type","value":"Recreate"}]'
```

Content schema 40 retains a per-execution receipt key and erases it when a source is purged. Older
servers cannot recover these pending receipts. Schema repair must not replace the key-erasing purge
event with the historical definition. Recover pending validation receipts with the current server
before a planned downgrade, retaining the shared volume and a coherent database backup. Removing the
volume loses outage recovery for pending results; replay still refuses to repeat an unresolved
provider dispatch.
