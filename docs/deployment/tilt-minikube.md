# Tilt + Minikube Deployment

Local Kubernetes development with Tilt for hot-reload and automatic rebuilds.

## Architecture

```
+------------------------------- Minikube Cluster --------------------------------+
|                                                                                  |
|  +------------------+     +------------------+     +------------------+          |
|  |  Kong Operator   |     |  CNPG Operator   |     |  cert-manager    |          |
|  |  (kong-system)   |     | (cnpg-system)    |     | (cert-manager)   |          |
|  +--------+---------+     +--------+---------+     +--------+---------+          |
|           |                        |                        |                    |
|  +--------v---------+     +--------v---------+              |                    |
|  |  Kong Gateway    |     |    PostgreSQL    |              |                    |
|  |  (kong)          |     |    (sibyl)       |              |                    |
|  +--------+---------+     +------------------+              |                    |
|           |                                                 |                    |
|  +--------v--------------------------------------------+    |                    |
|  |                    sibyl namespace                   |    |                    |
|  |  +----------+  +----------+  +----------+           |    |                    |
|  |  | Backend  |  | Frontend |  |  Worker  |           |    |                    |
|  |  +----------+  +----------+  +----------+           |    |                    |
|  |  +------------------+                               |    |                    |
|  |  |    FalkorDB      |                               |    |                    |
|  |  +------------------+                               |    |                    |
|  +-----------------------------------------------------+    |                    |
|                                                              |                    |
+------------------------------ sibyl.local -------------------+--------------------+
                                    ^
                                    |
                     +--------------+---------------+
                     |   Caddy Proxy (localhost)    |
                     |   :443 -> Kong :8000         |
                     +------------------------------+
                                    ^
                                    |
                          https://sibyl.local
```

## Prerequisites

Install the following tools:

```bash
# macOS with Homebrew
brew install minikube kubectl helm tilt caddy

# Verify installations
minikube version
kubectl version --client
helm version
tilt version
caddy version
```

## Quick Start

```bash
# 1. Start minikube with sufficient resources
minikube start --cpus=4 --memory=8192 --driver=docker

# 2. Export API keys (picked up by Tiltfile)
export OPENAI_API_KEY=sk-...
# or
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Add sibyl.local to /etc/hosts
echo "127.0.0.1 sibyl.local" | sudo tee -a /etc/hosts

# Optional: pin agent-sandbox version
export AGENT_SANDBOX_VERSION=v0.1.1

# 4. Start Tilt (sandbox enabled by default)
tilt up
```

Open http://localhost:10350 to see the Tilt dashboard.

## Components Deployed

The Tiltfile orchestrates deployment of:

| Component          | Namespace    | Purpose                         |
| ------------------ | ------------ | ------------------------------- |
| Gateway API CRDs   | -            | Kubernetes Gateway API          |
| Agent Sandbox CRDs | agent-sandbox-system | Sandbox CRD controller (enabled by default; disable with `--no-sandbox`) |
| cert-manager       | cert-manager | TLS certificate management      |
| Kong Operator      | kong-system  | API Gateway operator            |
| Kong Gateway       | kong         | Ingress/routing                 |
| CNPG Operator      | cnpg-system  | PostgreSQL operator             |
| PostgreSQL Cluster | sibyl        | CNPG-managed database           |
| FalkorDB           | sibyl        | Graph database                  |
| Sibyl Backend      | sibyl        | FastAPI + MCP server            |
| Sibyl Frontend     | sibyl        | Next.js UI                      |
| Sibyl Worker       | sibyl        | arq job processor               |
| Caddy Proxy        | localhost    | TLS termination for sibyl.local |

## Secrets Configuration

The Tiltfile automatically creates secrets from environment variables:

```python
# From Tiltfile - reads from environment
openai_key = os.getenv("SIBYL_OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
anthropic_key = os.getenv("SIBYL_ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
jwt_secret = os.getenv("SIBYL_JWT_SECRET", "dev-jwt-secret-for-local-development-only")
```

For production-like secrets:

```bash
export SIBYL_JWT_SECRET=$(openssl rand -hex 32)
export SIBYL_OPENAI_API_KEY=sk-...
export SIBYL_ANTHROPIC_API_KEY=sk-ant-...
```

## Accessing Services

### Via sibyl.local (Recommended)

With Caddy proxy running (started by Tilt):

| URL                          | Service           |
| ---------------------------- | ----------------- |
| https://sibyl.local          | Frontend UI       |
| https://sibyl.local/api/docs | API Documentation |
| https://sibyl.local/api/*    | REST API          |
| https://sibyl.local/mcp      | MCP Protocol      |

### Direct Port-Forward

If you need direct access to services:

```bash
# Backend API
kubectl port-forward -n sibyl svc/sibyl-backend 3334:3334

# Frontend
kubectl port-forward -n sibyl svc/sibyl-frontend 3337:3337

# FalkorDB
kubectl port-forward -n sibyl svc/falkordb-redis-master 6379:6379

# PostgreSQL
kubectl port-forward -n sibyl svc/sibyl-postgres-rw 5432:5432
```

## Tilt Dashboard

The Tilt UI at http://localhost:10350 shows:

- **Resources**: All deployed components with status
- **Logs**: Aggregated logs from all services
- **Build status**: Image build progress
- **Trigger buttons**: Manual rebuild/restart

### Resource Groups

- **infrastructure**: Gateway API CRDs, agent-sandbox, cert-manager, Kong, CNPG, FalkorDB
- **application**: Backend, Frontend, Worker
- **networking**: Kong port-forward, Caddy proxy
- **tools**: Convenience commands

## Configuration

### Local Values Override

The `infra/local/sibyl-values.yaml` configures Sibyl for local development:

```yaml
backend:
  image:
    repository: sibyl-backend
    pullPolicy: Never # Use local images
    tag: dev

  existingSecret: sibyl-secrets

  database:
    existingSecret: sibyl-postgres-app # CNPG auto-generated

  falkordb:
    host: falkordb-redis-master
    port: "6379"
    existingSecret: sibyl-falkordb

  env:
    SIBYL_ENVIRONMENT: "development"
    SIBYL_PUBLIC_URL: "https://sibyl.local"
    SIBYL_LLM_PROVIDER: "anthropic"
    SIBYL_LLM_MODEL: "claude-haiku-4-5"

  # Relaxed security for local dev
  podSecurityContext:
    runAsNonRoot: false
  securityContext:
    readOnlyRootFilesystem: false

frontend:
  image:
    repository: sibyl-frontend
    pullPolicy: Never
    tag: dev

  env:
    NODE_ENV: "development"
    SIBYL_API_URL: "http://sibyl-backend:3334/api"
```

### Skip Infrastructure

To skip infrastructure deployment (use existing databases):

```bash
tilt up -- --skip-infra
```

### Sandbox Mode (agent-sandbox, default on)

Sandbox mode is enabled by default in Tilt:

```bash
export AGENT_SANDBOX_VERSION=v0.1.1
tilt up
```

Tilt installs the upstream manifest from:
`https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/manifest.yaml`

Disable sandbox mode only when needed:

```bash
tilt up -- --no-sandbox
```

Verify installation:

```bash
kubectl get crd sandboxes.agents.x-k8s.io
kubectl rollout status -n agent-sandbox-system statefulset/agent-sandbox-controller
```

## Development Workflow

### Rebuild Backend

After code changes, Tilt automatically detects and rebuilds. For manual trigger:

1. Click "backend" in Tilt UI
2. Click "Trigger Update"

Or use the CLI:

```bash
tilt trigger backend
```

### View Logs

In Tilt UI, click on any resource to see its logs.

Or via kubectl:

```bash
kubectl logs -n sibyl -l app.kubernetes.io/component=backend -f
```

### Shell into Pod

```bash
kubectl exec -it -n sibyl deploy/sibyl-backend -- /bin/bash
```

## Database Access

### PostgreSQL

CNPG creates a secret with credentials:

```bash
# Get connection info
kubectl get secret -n sibyl sibyl-postgres-app -o yaml

# Connect via psql
kubectl exec -it -n sibyl sibyl-postgres-1 -- psql -U sibyl sibyl
```

### FalkorDB

```bash
kubectl exec -it -n sibyl falkordb-0 -- redis-cli -a sibyl-local-dev
```

## Kong Gateway

HTTPRoutes define routing:

```yaml
# infra/local/kong/httproutes.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: sibyl-api
  namespace: sibyl
spec:
  parentRefs:
    - name: sibyl-gateway
      namespace: kong
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api
      backendRefs:
        - name: sibyl-backend
          port: 3334
    - matches:
        - path:
            type: PathPrefix
            value: /mcp
      backendRefs:
        - name: sibyl-backend
          port: 3334
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: sibyl-frontend
  namespace: sibyl
spec:
  parentRefs:
    - name: sibyl-gateway
      namespace: kong
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: sibyl-frontend
          port: 3337
```

## Cleanup

```bash
# Stop Tilt (Ctrl+C in terminal running tilt up)

# Delete all resources but keep minikube
tilt down

# Delete minikube entirely
minikube delete
```

## Troubleshooting

### Tilt Stuck on Infrastructure

Infrastructure deployment can take 3-5 minutes on first run. Check the Tilt UI for progress.

### Kong Gateway Not Ready

```bash
# Check Kong operator
kubectl get pods -n kong-system

# Check gateway
kubectl get gateway -n kong

# Check dataplane
kubectl get pods -n kong
```

### CNPG Database Not Starting

```bash
# Check operator
kubectl get pods -n cnpg-system

# Check cluster status
kubectl get cluster -n sibyl sibyl-postgres

# Check postgres pods
kubectl get pods -n sibyl -l cnpg.io/cluster=sibyl-postgres
```

### Agent Sandbox Not Ready

```bash
# Check CRD
kubectl get crd sandboxes.agents.x-k8s.io

# Check controller status and logs
kubectl get pods -n agent-sandbox-system
kubectl logs -n agent-sandbox-system statefulset/agent-sandbox-controller
```

### Can't Reach sibyl.local

1. Verify /etc/hosts entry: `grep sibyl.local /etc/hosts`
2. Check Caddy proxy is running in Tilt UI
3. Check Kong port-forward is running in Tilt UI
4. Trust the Caddy CA certificate (macOS):
   ```bash
   sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/.local/share/caddy/pki/authorities/local/root.crt
   ```

## Next Steps

- [Kubernetes Production Deployment](kubernetes.md)
- [Helm Chart Reference](helm-chart.md)
