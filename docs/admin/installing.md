---
title: Installing Sibyl
description: Enterprise Kubernetes installation guide for Sibyl admins
---

# Installing Sibyl

The enterprise install shape is a Kubernetes deployment behind your corporate identity provider,
with SurrealDB as the active data plane, Valkey or Redis for coordination when running multiple
replicas, and an ingress or Gateway API controller for TLS and routing.

## Prerequisites

- Kubernetes cluster with a block-storage CSI driver.
- Helm 3.
- A Gateway API-compatible controller or classic Ingress controller.
- A secrets injector such as External Secrets Operator, Sealed Secrets, or your cloud KMS-backed
  secret manager.
- SurrealDB 3.x, either through `charts/surrealdb` or a separately managed endpoint.
- Valkey or Redis when backend or worker replicas are greater than one.
- Corporate OIDC app registration with MFA enforced at the provider.

## Install The Charts

Render both charts before installing:

```bash
helm lint charts/sibyl charts/surrealdb
helm template sibyl charts/sibyl -n sibyl -f values-enterprise.yaml
helm template sibyl-surrealdb charts/surrealdb -n sibyl -f values-surrealdb.yaml
```

Install SurrealDB first when using the wrapper chart:

```bash
helm upgrade --install sibyl-surrealdb charts/surrealdb \
  -n sibyl \
  --create-namespace \
  -f values-surrealdb.yaml
```

Then install Sibyl:

```bash
helm upgrade --install sibyl charts/sibyl \
  -n sibyl \
  -f values-enterprise.yaml
```

## Minimal Enterprise Values

```yaml
coordinationBackend: redis

oidc:
  providers:
    - name: entra
      issuer: "https://login.microsoftonline.com/<tenant-id>/v2.0"
      client_id: "<app-client-id>"
      client_secret_env: "SIBYL_OIDC_ENTRA_CLIENT_SECRET"
      scopes: ["openid", "profile", "email"]
  role_claim: "roles"
  session_minutes: 60
  silent_refresh_enabled: true
  extra_providers_enabled: false

backend:
  existingSecret: sibyl-secrets
  env:
    SIBYL_ENVIRONMENT: "production"
    SIBYL_PUBLIC_URL: "https://sibyl.example.com"
    SIBYL_LOCAL_AUTH_ENABLED: "false"
  surreal:
    url: "ws://sibyl-surrealdb:8000/rpc"
    existingSecret: sibyl-surreal
  redis:
    host: valkey.sibyl.svc.cluster.local
    existingSecret: sibyl-redis

ingress:
  gatewayApi:
    enabled: true
    parentRefs:
      - name: shared-gateway
        namespace: gateway-system
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

networkPolicy:
  enabled: true

podSecurity:
  enforceRestricted: true
```

Keep cloud-specific annotations, certificate issuers, secret references, object storage mounts, and
SIEM wiring in a deployment overlay.

## OIDC Provider Setup

Use one corporate provider in production. Non-corporate providers such as Google or GitHub require
`oidc.extra_providers_enabled=true`; leave it false for enterprise installs.

| Provider           | Admin setup                                                                                                                            |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Microsoft Entra ID | Create App Roles named `Sibyl.Member`, `Sibyl.Admin`, and `Sibyl.Owner`; assign users or groups; require MFA with Conditional Access.  |
| Okta               | Map groups or app assignments into the configured role claim, often `groups`; include any required scope in `oidc.providers[].scopes`. |
| Auth0              | Use an Action to copy RBAC roles into a namespaced custom claim such as `urn:sibyl:roles`.                                             |
| Keycloak           | Configure a mapper or set `oidc.role_claim` to a dotted path such as `resource_access.sibyl.roles`.                                    |

The redirect URI must match the provider name:

```text
https://sibyl.example.com/api/auth/oidc/<provider>/callback
```

For silent refresh, also allow:

```text
https://sibyl.example.com/api/auth/oidc/<provider>/refresh
```

## Secrets

At minimum, the backend secret should provide:

```text
SIBYL_JWT_SECRET
SIBYL_SETTINGS_KEY
SIBYL_OIDC_ENTRA_CLIENT_SECRET
SIBYL_OPENAI_API_KEY
SIBYL_ANTHROPIC_API_KEY
```

Use a generated 32-byte or stronger `SIBYL_JWT_SECRET`. Keep `SIBYL_SETTINGS_KEY` stable so
encrypted settings can be read after a restart.

## First Owner

Use a break-glass owner for initial bootstrap and emergencies, then keep routine access on OIDC
accounts:

```yaml
breakGlass:
  enabled: true
  existingSecret: sibyl-break-glass
```

After the OIDC owner signs in successfully, disable local password login in production values unless
you are actively running the break-glass path.
