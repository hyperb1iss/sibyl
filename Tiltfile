# -*- mode: Python -*-
# Sibyl Local Development with Tilt
# Run with: tilt up

# Force minikube context
k8s_context('minikube')

# Increase timeout for large charts
update_settings(k8s_upsert_timeout_secs=300)

# Load extensions
load('ext://helm_resource', 'helm_resource', 'helm_repo')

# Configuration
config.define_bool("skip-infra")
cfg = config.parse()

# =============================================================================
# HELM REPOSITORIES
# =============================================================================

helm_repo('cnpg', 'https://cloudnative-pg.github.io/charts')
helm_repo('bitnami', 'https://charts.bitnami.com/bitnami')
helm_repo('kong', 'https://charts.konghq.com')
helm_repo('jetstack', 'https://charts.jetstack.io')

# =============================================================================
# INFRASTRUCTURE
# =============================================================================

if not cfg.get("skip-infra"):
    # -------------------------------------------------------------------------
    # Gateway API CRDs
    # -------------------------------------------------------------------------
    local_resource(
        'gateway-api-crds',
        cmd='''
        kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.1/standard-install.yaml

        echo "⏳ Waiting for Gateway API CRDs to be established..."
        kubectl wait --for=condition=Established crd/gatewayclasses.gateway.networking.k8s.io --timeout=60s
        kubectl wait --for=condition=Established crd/gateways.gateway.networking.k8s.io --timeout=60s
        kubectl wait --for=condition=Established crd/httproutes.gateway.networking.k8s.io --timeout=60s

        echo "✅ Gateway API CRDs installed and ready"
        ''',
        allow_parallel=True
    )

    # -------------------------------------------------------------------------
    # Namespaces
    # -------------------------------------------------------------------------
    k8s_yaml("infra/local/namespace.yaml")

    # -------------------------------------------------------------------------
    # cert-manager
    # -------------------------------------------------------------------------
    helm_resource(
        'cert-manager',
        chart='jetstack/cert-manager',
        namespace='cert-manager',
        flags=[
            '--create-namespace',
            '--wait',
            '--timeout=5m',
            '--set=crds.enabled=true',
        ],
        resource_deps=['gateway-api-crds']
    )

    # Self-signed ClusterIssuer and Certificate for sibyl.local
    local_resource(
        'cert-manager-issuer',
        cmd='''
        echo "⏳ Waiting for cert-manager webhook..."
        kubectl wait --for=condition=available --timeout=120s \
            deployment/cert-manager-webhook \
            -n cert-manager

        sleep 3

        echo "✅ Applying ClusterIssuer and Certificate..."
        kubectl apply -f infra/local/cert-manager.yaml
        ''',
        deps=['infra/local/cert-manager.yaml'],
        resource_deps=['cert-manager'],
    )

    # -------------------------------------------------------------------------
    # Secrets from environment
    # -------------------------------------------------------------------------
    openai_key = os.getenv("SIBYL_OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    anthropic_key = os.getenv("SIBYL_ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
    jwt_secret = os.getenv("SIBYL_JWT_SECRET", "dev-jwt-secret-for-local-development-only")

    if not openai_key and not anthropic_key:
        warn("⚠️  No API keys found! Set OPENAI_API_KEY or ANTHROPIC_API_KEY environment variable.")

    k8s_yaml(blob("""
apiVersion: v1
kind: Secret
metadata:
  name: sibyl-secrets
  namespace: sibyl
type: Opaque
stringData:
  SIBYL_JWT_SECRET: "{jwt_secret}"
  SIBYL_OPENAI_API_KEY: "{openai_key}"
  SIBYL_ANTHROPIC_API_KEY: "{anthropic_key}"
---
# Note: Database password comes from CNPG auto-generated secret (sibyl-postgres-app)
---
# FalkorDB credentials (used by both FalkorDB StatefulSet and Sibyl apps)
apiVersion: v1
kind: Secret
metadata:
  name: sibyl-falkordb
  namespace: sibyl
type: Opaque
stringData:
  password: "sibyl-local-dev"
""".format(jwt_secret=jwt_secret, openai_key=openai_key, anthropic_key=anthropic_key)))

    # -------------------------------------------------------------------------
    # Kong Operator
    # -------------------------------------------------------------------------
    helm_resource(
        'kong-operator',
        chart='kong/kong-operator',
        namespace='kong-system',
        flags=[
            '--create-namespace',
            '--wait',
            '--timeout=5m',
            '--skip-crds',  # We install Gateway API CRDs separately
        ],
        resource_deps=['cert-manager-issuer']  # Wait for cert-manager + issuer
    )

    # Apply Kong Gateway manifests after webhook is ready
    local_resource(
        'kong-gateway-manifests',
        cmd='''
        echo "⏳ Waiting for Kong operator webhook to be ready..."
        kubectl wait --for=condition=available --timeout=120s \
            deployment/kong-operator-kong-operator-controller-manager \
            -n kong-system

        sleep 5

        echo "✅ Applying Kong Gateway manifests..."
        kubectl apply -f infra/local/kong/gateway-class.yaml
        kubectl apply -f infra/local/kong/gateway.yaml
        kubectl apply -f infra/local/kong/reference-grant.yaml
        kubectl apply -f infra/local/kong/httproutes.yaml
        ''',
        deps=['infra/local/kong/'],
        resource_deps=['kong-operator'],
        trigger_mode=TRIGGER_MODE_AUTO
    )

    # Kong Gateway DataPlane is created dynamically by Kong operator
    # Tilt will auto-discover it once created
    # Access via: kubectl port-forward -n kong svc/dataplane-sibyl-gateway-proxy 8080:80 8443:443

    # -------------------------------------------------------------------------
    # CNPG Operator
    # -------------------------------------------------------------------------
    helm_resource(
        'cnpg-operator',
        chart='cnpg/cloudnative-pg',
        namespace='cnpg-system',
        flags=[
            '--create-namespace',
            '--wait',
            '--timeout=5m'
        ],
        resource_deps=['gateway-api-crds']  # Parallel with Kong - only needs CRDs for timing
    )

    # PostgreSQL Cluster
    local_resource(
        'postgres',
        cmd='''
        echo "⏳ Waiting for CNPG operator..."
        kubectl wait --for=condition=available --timeout=120s \
            deployment/cnpg-operator-cloudnative-pg \
            -n cnpg-system

        echo "✅ Applying PostgreSQL cluster..."
        kubectl apply -f infra/local/postgres-cluster.yaml
        ''',
        deps=['infra/local/postgres-cluster.yaml'],
        resource_deps=['cnpg-operator'],
        trigger_mode=TRIGGER_MODE_AUTO
    )

    # -------------------------------------------------------------------------
    # FalkorDB (direct deployment - Bitnami chart conflicts with FalkorDB image)
    # -------------------------------------------------------------------------
    k8s_yaml("infra/local/falkordb.yaml")

    k8s_resource(
        workload='falkordb',
        labels=['infrastructure'],
        # No port-forward - FalkorDB stays in-cluster only
        # Use Docker Compose FalkorDB for standalone local dev
        resource_deps=['gateway-api-crds']  # Parallel with Kong/CNPG - just needs namespace
    )


# =============================================================================
# APPLICATION: Backend
# =============================================================================

docker_build(
    'sibyl-backend',
    context='.',
    dockerfile='Dockerfile',
    only=[
        'pyproject.toml',
        'uv.lock',
        'README.md',
        'apps/api/',
        'packages/python/sibyl-core/',
    ],
)

k8s_yaml(
    helm(
        'charts/sibyl',
        name='sibyl',
        namespace='sibyl',
        values=['infra/local/sibyl-values.yaml'],
    )
)

backend_deps = ['postgres', 'falkordb'] if not cfg.get('skip-infra') else []
k8s_resource(
    workload='sibyl-backend',
    new_name='backend',
    labels=['application'],
    # No port-forward - access via Kong gateway at sibyl.local
    resource_deps=backend_deps,
    trigger_mode=TRIGGER_MODE_MANUAL,
)


# =============================================================================
# APPLICATION: Frontend
# =============================================================================

docker_build(
    'sibyl-frontend',
    context='apps/web',
    dockerfile='apps/web/Dockerfile',
    only=[
        'src/',
        'public/',
        'package.json',
        'pnpm-lock.yaml',
        'next.config.ts',
        'tailwind.config.ts',
        'postcss.config.mjs',
        'tsconfig.json',
    ],
)

frontend_deps = ['backend'] if not cfg.get('skip-infra') else ['backend']
k8s_resource(
    workload='sibyl-frontend',
    new_name='frontend',
    labels=['application'],
    resource_deps=frontend_deps,
    trigger_mode=TRIGGER_MODE_MANUAL,
)


# =============================================================================
# APPLICATION: Worker (arq job queue processor)
# =============================================================================

worker_deps = ['backend'] if not cfg.get('skip-infra') else ['backend']
k8s_resource(
    workload='sibyl-worker',
    new_name='worker',
    labels=['application'],
    resource_deps=worker_deps,
    trigger_mode=TRIGGER_MODE_MANUAL,
)


# =============================================================================
# LOCAL ACCESS: Port-forward + Caddy Proxy
# =============================================================================

# Port-forward Kong ingress to localhost:8000 (avoids port 80 conflicts)
local_resource(
    'kong-port-forward',
    serve_cmd='''
    echo "⏳ Waiting for Kong ingress service..."
    while ! kubectl get svc -n kong -o name 2>/dev/null | grep -q ingress; do
        sleep 2
    done
    SVC=$(kubectl get svc -n kong -o name | grep ingress | head -1 | cut -d/ -f2)
    echo "✅ Found Kong ingress: $SVC"
    echo "🔗 Port-forwarding to localhost:8000..."
    exec kubectl port-forward -n kong "svc/$SVC" 8000:80
    ''',
    labels=['networking'],
    resource_deps=['kong-gateway-manifests'] if not cfg.get('skip-infra') else [],
)

# Caddy reverse proxy for sibyl.local with automatic TLS
local_resource(
    'caddy-proxy',
    serve_cmd='caddy run --config infra/local/Caddyfile',
    deps=['infra/local/Caddyfile'],
    labels=['networking'],
    resource_deps=['kong-port-forward'],
    links=[
        link('https://sibyl.local', 'Sibyl UI'),
        link('https://sibyl.local/api/docs', 'API Docs'),
    ],
)


# =============================================================================
# DEVELOPMENT TOOLS
# =============================================================================

local_resource(
    'open-api-docs',
    cmd='open https://sibyl.local/api/docs',
    auto_init=False,
    labels=['tools'],
)

local_resource(
    'open-frontend',
    cmd='open https://sibyl.local',
    auto_init=False,
    labels=['tools'],
)

local_resource(
    'falkordb-cli',
    cmd='echo "Use: kubectl exec -it -n sibyl falkordb-0 -- redis-cli -a sibyl-local-dev"',
    auto_init=False,
    labels=['tools'],
)

local_resource(
    'psql',
    cmd='echo "Use: kubectl exec -it -n sibyl sibyl-postgres-1 -- psql -U sibyl sibyl"',
    auto_init=False,
    labels=['tools'],
)
