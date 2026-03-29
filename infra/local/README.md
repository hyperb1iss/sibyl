# Sibyl Local Infrastructure

Local Kubernetes development environment using minikube.

## Prerequisites

- [minikube](https://minikube.sigs.k8s.io/docs/start/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [helm](https://helm.sh/docs/intro/install/)
- [tilt](https://docs.tilt.dev/install.html)

## Quick Start

```bash
# Start minikube with enough resources
minikube start --cpus=4 --memory=8192 --driver=docker

# From project root, run Tilt
tilt up
```

## Components

| Component     | Chart                | Version | Purpose                                  |
| ------------- | -------------------- | ------- | ---------------------------------------- |
| CNPG Operator | cnpg/cloudnative-pg  | 0.27.0  | PostgreSQL operator                      |
| CNPG Cluster  | cnpg/cluster         | 0.5.0   | PostgreSQL database                      |
| FalkorDB      | bitnami/redis        | latest  | Graph database (Redis + FalkorDB module) |
| Kong Operator | kong/kong-operator   | latest  | API Gateway                              |
| Gateway API   | k8s-sigs/gateway-api | v1.4.1  | Gateway CRDs                             |
| Sibyl         | ./charts/sibyl       | 0.1.0   | Application                              |

## Manual Setup (without Tilt)

```bash
# Add Helm repos
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add kong https://charts.konghq.com
helm repo update

# Install Gateway API CRDs
kubectl apply -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.1/standard-install.yaml

# Install operators
helm upgrade --install cnpg cnpg/cloudnative-pg -n cnpg-system --create-namespace -f cnpg-operator-values.yaml
helm upgrade --install kong-operator kong/kong-operator -n kong-system --create-namespace

# Install databases
kubectl apply -f postgres-cluster.yaml
helm upgrade --install falkordb bitnami/redis -n sibyl --create-namespace -f falkordb-values.yaml

# Install Kong Gateway
kubectl apply -f kong/

# Install Sibyl
helm upgrade --install sibyl ../../charts/sibyl -n sibyl -f sibyl-values.yaml
```

## Accessing Services

With minikube:

```bash
# Sibyl frontend
minikube service sibyl-frontend -n sibyl

# Or use port forwarding
kubectl port-forward svc/sibyl-frontend 3337:3337 -n sibyl
kubectl port-forward svc/sibyl-backend 3334:3334 -n sibyl
```

## Secrets

Create the secrets before deploying Sibyl:

```bash
kubectl create secret generic sibyl-secrets -n sibyl \
  --from-literal=SIBYL_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=SIBYL_OPENAI_API_KEY=sk-... \
  --from-literal=SIBYL_ANTHROPIC_API_KEY=sk-ant-...

kubectl create secret generic sibyl-postgres-secret -n sibyl \
  --from-literal=SIBYL_POSTGRES_PASSWORD=sibyl_dev

kubectl create secret generic sibyl-falkordb-secret -n sibyl \
  --from-literal=SIBYL_FALKORDB_PASSWORD=conventions
```
