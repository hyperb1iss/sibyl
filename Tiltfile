# -*- mode: Python -*-
# Sibyl Local Development with Tilt
# Run with: tilt up

# The scalable local stack uses minikube by default, and also supports
# Podman Desktop's Kind-powered Kubernetes plus OrbStack.
k8s_context_name = os.getenv('SIBYL_K8S_CONTEXT', 'minikube')
k8s_context(k8s_context_name)

# Increase timeout for large charts and suppress known-intentional image warnings.
update_settings(
    k8s_upsert_timeout_secs=300,
)

# Load extensions
load('ext://helm_resource', 'helm_resource', 'helm_repo')

# Configuration
config.define_bool("skip-infra")
cfg = config.parse()


def local_k8s_image_build_cmd(dockerfile):
    return '''
        set -eu
        image_ref="docker.io/$EXPECTED_IMAGE:$EXPECTED_TAG"
        archive=""
        context="${SIBYL_K8S_CONTEXT:-''' + k8s_context_name + '''}"
        loader="${SIBYL_IMAGE_LOADER:-auto}"
        builder="${SIBYL_CONTAINER_BUILDER:-auto}"
        kind_cluster="${SIBYL_KIND_CLUSTER:-}"

        cleanup() {
            if [ -n "$archive" ]; then
                rm -f "$archive"
            fi
        }
        trap cleanup EXIT

        if [ "$loader" = "auto" ]; then
            case "$context" in
                minikube)
                    loader="minikube"
                    ;;
                kind|kind-*)
                    loader="kind"
                    ;;
                orbstack|orb|orb-*)
                    loader="none"
                    ;;
                *)
                    echo "Set SIBYL_IMAGE_LOADER=minikube, kind, or none for Kubernetes context $context" >&2
                    exit 1
                    ;;
            esac
        fi

        if [ "$builder" = "auto" ]; then
            case "$context" in
                orbstack|orb|orb-*)
                    builder="docker"
                    ;;
                *)
                    if command -v podman >/dev/null 2>&1; then
                        builder="podman"
                    else
                        builder="docker"
                    fi
                    ;;
            esac
        fi

        case "$builder" in
            podman)
                command -v podman >/dev/null 2>&1 || {
                    echo "podman is required for SIBYL_CONTAINER_BUILDER=podman" >&2
                    exit 1
                }
                podman build -t "$image_ref" -f ''' + dockerfile + ''' .
                ;;
            docker)
                command -v docker >/dev/null 2>&1 || {
                    echo "docker is required for SIBYL_CONTAINER_BUILDER=docker" >&2
                    exit 1
                }
                docker build -t "$image_ref" -f ''' + dockerfile + ''' .
                ;;
            *)
                echo "Unknown SIBYL_CONTAINER_BUILDER: $builder" >&2
                exit 1
                ;;
        esac

        if [ "$loader" != "none" ]; then
            archive="$(mktemp -t sibyl-image.XXXXXX.tar)"
            case "$builder" in
                podman)
                    podman save "$image_ref" -o "$archive"
                    ;;
                docker)
                    docker save "$image_ref" -o "$archive"
                    ;;
            esac
        fi

        case "$loader" in
            minikube)
                command -v minikube >/dev/null 2>&1 || {
                    echo "minikube is required for SIBYL_IMAGE_LOADER=minikube" >&2
                    exit 1
                }
                minikube image load "$archive"
                ;;
            kind)
                command -v kind >/dev/null 2>&1 || {
                    echo "kind is required for SIBYL_IMAGE_LOADER=kind" >&2
                    exit 1
                }
                if [ -z "$kind_cluster" ]; then
                    kind_cluster="${context#kind-}"
                fi
                kind load image-archive "$archive" --name "$kind_cluster"
                ;;
            none)
                ;;
            *)
                echo "Unknown SIBYL_IMAGE_LOADER: $loader" >&2
                exit 1
                ;;
        esac
    '''

# =============================================================================
# HELM REPOSITORIES
# =============================================================================

helm_repo('kong', 'https://charts.konghq.com')
helm_repo('jetstack', 'https://charts.jetstack.io')
helm_repo('surrealdb-helm', 'https://helm.surrealdb.com')
helm_repo('pingcap', 'https://charts.pingcap.org')
helm_repo('valkey-helm', 'https://valkey-io.github.io/valkey-helm')

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
    surreal_password = os.getenv("SIBYL_SURREAL_PASSWORD", "sibyl-local-dev")
    redis_password = os.getenv("SIBYL_REDIS_PASSWORD", "sibyl-local-dev")

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
  SIBYL_SURREAL_PASSWORD: "{surreal_password}"
  SIBYL_REDIS_PASSWORD: "{redis_password}"
""".format(
    jwt_secret=jwt_secret,
    openai_key=openai_key,
    anthropic_key=anthropic_key,
    surreal_password=surreal_password,
    redis_password=redis_password,
)))

    k8s_resource(
        objects=['sibyl-secrets:secret:sibyl'],
        new_name='sibyl-secrets',
        labels=['infrastructure'],
        pod_readiness='ignore',
    )

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
    # Access via: kubectl port-forward -n kong svc/<ingress service> 18080:80

    local_resource(
        'tidb-crds',
        cmd='''
        kubectl apply --server-side --force-conflicts -f https://raw.githubusercontent.com/pingcap/tidb-operator/v1.6.5/manifests/crd.yaml
        kubectl wait --for=condition=Established crd/tidbclusters.pingcap.com --timeout=120s
        ''',
        allow_parallel=True,
    )

    helm_resource(
        'tidb-operator',
        chart='pingcap/tidb-operator',
        namespace='tidb-admin',
        flags=[
            '--create-namespace',
            '--wait',
            '--timeout=5m',
            '--version=v1.6.5',
            '--set=scheduler.create=false',
        ],
        resource_deps=['tidb-crds'],
    )

    k8s_yaml("infra/local/tidb-cluster.yaml")

    k8s_resource(
        objects=['sibyl-tikv'],
        new_name='sibyl-tikv',
        labels=['infrastructure'],
        resource_deps=['tidb-operator'],
        pod_readiness='ignore',
    )

    local_resource(
        'tikv-ready',
        cmd='''
        kubectl wait --for=jsonpath='{.status.pd.statefulSet.readyReplicas}'=3 tidbcluster/sibyl-tikv -n sibyl --timeout=15m
        kubectl wait --for=jsonpath='{.status.tikv.statefulSet.readyReplicas}'=3 tidbcluster/sibyl-tikv -n sibyl --timeout=15m
        ''',
        deps=['infra/local/tidb-cluster.yaml'],
        labels=['infrastructure'],
        resource_deps=['sibyl-tikv'],
    )

    helm_resource(
        'surrealdb',
        chart='surrealdb-helm/surrealdb',
        namespace='sibyl',
        flags=[
            '--create-namespace',
            '--wait',
            '--timeout=5m',
            '--version=0.4.0',
            '--values=infra/local/surrealdb-values.yaml',
        ],
        resource_deps=['tikv-ready', 'sibyl-secrets']
    )

    helm_resource(
        'valkey',
        chart='valkey-helm/valkey',
        namespace='sibyl',
        flags=[
            '--create-namespace',
            '--wait',
            '--timeout=5m',
            '--version=0.9.4',
            '--values=infra/local/valkey-values.yaml',
        ],
        resource_deps=['sibyl-secrets']
    )


# =============================================================================
# APPLICATION: Backend
# =============================================================================

custom_build(
    'sibyl-backend',
    local_k8s_image_build_cmd('apps/api/Dockerfile'),
    deps=[
        'pyproject.toml',
        'uv.lock',
        'VERSION',
        'README.md',
        'apps/api/',
        'packages/python/sibyl-core/',
    ],
    skips_local_docker=True,
)

k8s_yaml(
    helm(
        'charts/sibyl',
        name='sibyl',
        namespace='sibyl',
        values=['infra/local/sibyl-values.yaml'],
    )
)

backend_deps = ['surrealdb', 'valkey'] if not cfg.get('skip-infra') else []
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

custom_build(
    'sibyl-frontend',
    local_k8s_image_build_cmd('apps/web/Dockerfile'),
    deps=[
        'VERSION',
        'pnpm-lock.yaml',
        'pnpm-workspace.yaml',
        'apps/web/',
    ],
    skips_local_docker=True,
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

# Port-forward Kong ingress to localhost:18080 (avoids port 80 and SurrealDB 8000 conflicts)
local_resource(
    'kong-port-forward',
    serve_cmd='''
    echo "⏳ Waiting for Kong ingress service..."
    while ! kubectl get svc -n kong -o name 2>/dev/null | grep -q ingress; do
        sleep 2
    done
    SVC=$(kubectl get svc -n kong -o name | grep ingress | head -1 | cut -d/ -f2)
    echo "✅ Found Kong ingress: $SVC"
    echo "🔗 Port-forwarding to localhost:18080..."
    exec kubectl port-forward -n kong "svc/$SVC" 18080:80
    ''',
    labels=['networking'],
    resource_deps=['kong-gateway-manifests'] if not cfg.get('skip-infra') else [],
)

# Caddy reverse proxy for sibyl.local with automatic TLS
local_resource(
    'caddy-proxy',
    serve_cmd='''
    if ! command -v caddy >/dev/null 2>&1; then
        echo "Caddy not found; skipping https://sibyl.local proxy."
        echo "Use kubectl port-forward or install caddy for local TLS access."
        while true; do sleep 3600; done
    fi

    exec caddy run --config infra/local/Caddyfile
    ''',
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
    'surrealdb-cli',
    cmd='echo "Use: kubectl port-forward -n sibyl svc/surrealdb 8000:8000, then surreal sql --conn http://localhost:8000 --user root --pass $(kubectl get secret -n sibyl sibyl-secrets -o jsonpath={.data.SIBYL_SURREAL_PASSWORD} | base64 -d)"',
    auto_init=False,
    labels=['tools'],
)
