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
apiVersion: v1
kind: Secret
metadata:
  name: sibyl-db-secret
  namespace: sibyl
type: Opaque
stringData:
  SIBYL_POSTGRES_PASSWORD: "sibyl_dev"
---
apiVersion: v1
kind: Secret
metadata:
  name: sibyl-falkordb-secret
  namespace: sibyl
type: Opaque
stringData:
  SIBYL_FALKORDB_PASSWORD: "conventions"
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
        resource_deps=['gateway-api-crds']
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
        kubectl apply -f infra/local/kong/httproutes.yaml
        ''',
        deps=['infra/local/kong/'],
        resource_deps=['kong-operator'],
        trigger_mode=TRIGGER_MODE_AUTO
    )

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
        resource_deps=['kong-gateway-manifests']
    )

    # PostgreSQL Cluster
    local_resource(
        'postgres',
        cmd='''
        echo "⏳ Waiting for CNPG operator..."
        kubectl wait --for=condition=available --timeout=120s \
            deployment/cnpg-cloudnative-pg \
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
    k8s_yaml(blob("""
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: falkordb
  namespace: sibyl
spec:
  serviceName: falkordb
  replicas: 1
  selector:
    matchLabels:
      app: falkordb
  template:
    metadata:
      labels:
        app: falkordb
    spec:
      containers:
        - name: falkordb
          image: falkordb/falkordb:latest
          args: ["--requirepass", "conventions"]
          ports:
            - containerPort: 6379
          resources:
            limits:
              cpu: 500m
              memory: 512Mi
            requests:
              cpu: 100m
              memory: 256Mi
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 2Gi
---
apiVersion: v1
kind: Service
metadata:
  name: falkordb-redis-master
  namespace: sibyl
spec:
  selector:
    app: falkordb
  ports:
    - port: 6379
      targetPort: 6379
"""))

    k8s_resource(
        workload='falkordb',
        labels=['infrastructure'],
        port_forwards=['6380:6379'],
        resource_deps=['kong-gateway-manifests']
    )


# =============================================================================
# APPLICATION: Backend
# =============================================================================

docker_build(
    'sibyl-backend',
    context='.',
    dockerfile='Dockerfile',
    only=[
        'src/',
        'pyproject.toml',
        'uv.lock',
        'alembic/',
        'alembic.ini',
    ],
    live_update=[
        sync('src/sibyl/', '/app/src/sibyl/'),
        run(
            'cd /app && uv sync --frozen',
            trigger=['pyproject.toml', 'uv.lock'],
        ),
    ],
)

k8s_yaml(
    helm(
        'charts/sibyl',
        name='sibyl',
        namespace='sibyl',
        values=['infra/local/sibyl-values.yaml'],
        set=[
            'frontend.enabled=false',
        ],
    )
)

backend_deps = ['postgres', 'falkordb'] if not cfg.get('skip-infra') else []
k8s_resource(
    workload='sibyl-backend',
    new_name='backend',
    labels=['application'],
    port_forwards=['3334:3334'],
    resource_deps=backend_deps,
    links=[
        link('http://localhost:3334/api/docs', 'API Docs'),
        link('http://localhost:3334/api/health', 'Health Check'),
    ],
)


# =============================================================================
# APPLICATION: Frontend
# =============================================================================

docker_build(
    'sibyl-frontend',
    context='web',
    dockerfile='web/Dockerfile',
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
    live_update=[
        sync('web/src/', '/app/src/'),
        sync('web/public/', '/app/public/'),
        run(
            'cd /app && pnpm install --frozen-lockfile',
            trigger=['package.json', 'pnpm-lock.yaml'],
        ),
    ],
)

k8s_yaml(blob("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sibyl-frontend
  namespace: sibyl
  labels:
    app.kubernetes.io/name: sibyl
    app.kubernetes.io/component: frontend
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: sibyl
      app.kubernetes.io/component: frontend
  template:
    metadata:
      labels:
        app.kubernetes.io/name: sibyl
        app.kubernetes.io/component: frontend
    spec:
      containers:
        - name: frontend
          image: sibyl-frontend
          imagePullPolicy: Never
          ports:
            - containerPort: 3337
          env:
            - name: NODE_ENV
              value: "development"
            - name: NEXT_PUBLIC_API_URL
              value: "http://sibyl-backend:3334"
            - name: HOSTNAME
              value: "0.0.0.0"
            - name: PORT
              value: "3337"
          resources:
            limits:
              cpu: 500m
              memory: 512Mi
            requests:
              cpu: 50m
              memory: 128Mi
---
apiVersion: v1
kind: Service
metadata:
  name: sibyl-frontend
  namespace: sibyl
spec:
  selector:
    app.kubernetes.io/name: sibyl
    app.kubernetes.io/component: frontend
  ports:
    - port: 3337
      targetPort: 3337
"""))

frontend_deps = ['backend'] if not cfg.get('skip-infra') else ['backend']
k8s_resource(
    workload='sibyl-frontend',
    new_name='frontend',
    labels=['application'],
    port_forwards=['3337:3337'],
    resource_deps=frontend_deps,
    links=[
        link('http://localhost:3337', 'Sibyl UI'),
    ],
)


# =============================================================================
# DATABASE MIGRATIONS
# =============================================================================

local_resource(
    'db-migrate',
    cmd='kubectl exec -n sibyl deploy/sibyl-backend -- python -m alembic upgrade head',
    deps=['alembic/'],
    resource_deps=['backend'],
    labels=['application'],
    auto_init=False,
)


# =============================================================================
# DEVELOPMENT TOOLS
# =============================================================================

local_resource(
    'open-api-docs',
    cmd='open http://localhost:3334/api/docs',
    auto_init=False,
    labels=['tools'],
)

local_resource(
    'open-frontend',
    cmd='open http://localhost:3337',
    auto_init=False,
    labels=['tools'],
)

local_resource(
    'falkordb-cli',
    cmd='echo "Use: kubectl exec -it -n sibyl falkordb-redis-master-0 -- redis-cli -a conventions"',
    auto_init=False,
    labels=['tools'],
)

local_resource(
    'psql',
    cmd='echo "Use: kubectl exec -it -n sibyl sibyl-postgres-1 -- psql -U sibyl sibyl"',
    auto_init=False,
    labels=['tools'],
)
