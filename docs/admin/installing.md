---
title: Installing Sibyl
description: Enterprise Kubernetes installation guide for Sibyl admins
---

# Installing Sibyl

Sibyl has two supported install shapes:

- **Default single-user install:** local username/password auth is enabled, the first setup signup
  creates the owner/admin user, and later account creation is invite-only unless public signups are
  explicitly enabled.
- **Enterprise SSO install:** Kubernetes deployment behind a corporate identity provider, with OIDC
  configured explicitly and local password login disabled only after an OIDC owner can sign in.

SurrealDB is the active data plane in both shapes. Valkey or Redis is only needed for coordination
when running multiple backend or worker replicas. Enterprise installs add an ingress or Gateway API
controller for TLS and routing.

## Prerequisites

- Kubernetes cluster with a block-storage CSI driver.
- Helm 3.
- A Gateway API-compatible controller or classic Ingress controller.
- A secrets injector such as External Secrets Operator, Sealed Secrets, or your cloud KMS-backed
  secret manager.
- SurrealDB 3.x, either through `charts/surrealdb` or a separately managed endpoint.
- Valkey or Redis when backend or worker replicas are greater than one.
- For enterprise SSO only: corporate OIDC app registration with MFA enforced at the provider.

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

auth:
  localAuthEnabled: false
  publicSignupsEnabled: false

oidc:
  providers:
    - name: entra
      issuer: "https://login.microsoftonline.com/<tenant-id>/v2.0"
      client_id: "<app-client-id>"
      client_secret_env: "SIBYL_OIDC_ENTRA_CLIENT_SECRET"
      scopes: ["openid", "profile", "email"]
  role_claim: "roles"
  session_minutes: 60
  silent_refresh_enabled: false
  extra_providers_enabled: false

backend:
  existingSecret: sibyl-secrets
  env:
    SIBYL_ENVIRONMENT: "production"
    SIBYL_PUBLIC_URL: "https://sibyl.example.com"
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

The chart default keeps local username/password login enabled for simple self-hosted installs while
leaving public signup, OIDC providers, silent refresh, extra OAuth providers, and break-glass access
off. Enterprise SSO values should set `auth.localAuthEnabled=false` only after the corporate OIDC
provider is configured and an owner has successfully signed in through it.

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

### Transactional Email

Password reset and invitation emails need a delivery provider. Configure either Resend or SMTP, plus
a from address:

```text
# Option A: Resend
SIBYL_RESEND_API_KEY
SIBYL_EMAIL_FROM

# Option B: SMTP
SIBYL_SMTP_HOST
SIBYL_SMTP_PORT
SIBYL_SMTP_USERNAME
SIBYL_SMTP_PASSWORD
SIBYL_EMAIL_FROM
```

Without one of these configured, Sibyl logs those emails to its JSONL outbox and skips live
delivery, so invited users and password-reset requests never receive a link. When setting SMTP
passwords in a Compose `.env` file, escape literal `$` characters as `$$`.

## First Owner

The default Sibyl install is local-first. The first setup signup creates the owner/admin user. After
setup completes, account creation is invite-based unless `SIBYL_PUBLIC_SIGNUPS_ENABLED=true` or
`auth.publicSignupsEnabled=true` is set.

Enterprise SSO installs can opt into OIDC and then disable local password login after the corporate
provider is working. Do not disable local auth on a fresh install before completing either local
setup or a verified OIDC owner login, or the instance has no normal owner path.

Break-glass remains an explicit emergency path:

```yaml
breakGlass:
  enabled: true
  allowedIPs:
    - 203.0.113.0/24
  expiresAt: "<UTC timestamp no more than four hours out>"
  existingSecret: sibyl-break-glass
```

After the OIDC owner signs in successfully, enterprise values may set `auth.localAuthEnabled=false`
unless you are actively running the break-glass path. Set `expiresAt` no more than four hours out
for an emergency window and keep `allowedIPs` scoped to the operator network. When break-glass is
enabled, Sibyl denies login if either field is missing, if the expiry has passed, or if the expiry
is more than four hours out.
