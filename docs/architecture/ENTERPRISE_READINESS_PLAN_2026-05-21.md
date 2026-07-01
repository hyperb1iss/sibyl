# Sibyl Enterprise Readiness Plan

> ⚠️ **HISTORICAL (as of 2026-07-01).** This plan's work has shipped (Sibyl is at v1.0.2). Kept as a
> historical receipt; it does not describe current status. See
> [`SIBYL_NORTHSTAR.md`](SIBYL_NORTHSTAR.md) for current product truth.

- **Date:** 2026-05-21
- **Status:** Validated proposal — drives the OSS work needed to make Sibyl enterprise-deployable
- **Scope:** What Sibyl itself needs to support enterprise deployment behind a corporate IdP, on a
  Kubernetes cluster, with audit, encrypted secrets, MFA enforcement, tested backups, and a
  single-tenant data plane that ships clean. No cloud or deployment specifics — those belong in a
  deployment-overlay doc per organization.
- **Related:** [`SIBYL_NORTHSTAR.md`](SIBYL_NORTHSTAR.md),
  [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)

### Revision history

- **2026-05-21 v1** — initial OSS-only plan extracted from the original deployment proposal. Two
  Codex review iterations completed against the predecessor; this file inherits those resolutions.
- **2026-05-21 v2** — applied Codex iteration-3 fixes against the split doc. Removed
  deployment-specific terms (Kong, ClickHouse, Azure Key Vault, etc.) from generic guidance, now
  using "Gateway API-compatible ingress controller", "cloud KMS-backed secret manager", "SIEM/log
  warehouse". Keycloak role-claim row corrected to reflect default-mapper nesting
  (`resource_access.<client>.roles`) with note that `oidc.role_claim` supports dotted paths. Added
  per-provider `scopes` config for IdPs (notably Okta) where the role claim requires an additional
  scope. Silent refresh contract reframed as best-effort with explicit OIDC error surface
  (`login_required` / `interaction_required` / `consent_required` / `account_selection_required`)
  and third-party-cookie fallback. Added `SIBYL_OIDC_EXTRA_PROVIDERS_ENABLED` /
  `oidc.extra_providers_enabled` to W1/W2 config with Helm-time assertion.
- **2026-05-22 v3** — validation pass against the current worktree and current upstream sources.
  Updated PyJWT to `>=2.13.0,<3`, corrected the Authlib advisory wording (`1.7.1+` fixed, `1.7.2`
  latest), corrected the `python-jose` rationale, made the Authlib/MCP OAuth consolidation a
  feasibility-gated refactor because Authlib documents Flask/Django provider integrations but
  FastAPI/Starlette client integrations, and added implementation acceptance gates plus
  plan-validation receipts.
- **2026-05-22 v4** — added the current implementation validation packet:
  [`ENTERPRISE_READINESS_VALIDATION_2026-05-22.md`](ENTERPRISE_READINESS_VALIDATION_2026-05-22.md).
  The packet separates automated local proof from external/manual gates that still require a real
  IdP tenant, MCP clients, or Kubernetes cluster.
- **2026-05-22 v5** — clarified that enterprise SSO is opt-in. The default Sibyl install remains
  local-first: first owner/admin setup is allowed, post-setup account creation is invite-based
  unless public signups are explicitly enabled, and OIDC/extra providers/silent refresh/break-glass
  are off until configured.

---

## Executive summary

This plan describes the OSS-side work that lets a team install Sibyl into a production Kubernetes
environment behind a corporate identity provider, with the security and operational properties an
internal security review will check: MFA-enforced sessions, audit logging, encrypted secrets,
restricted Pod Security, tested backup, and a documented break-glass path. The single net-new Sibyl
product feature is **application-layer OIDC** implemented with Authlib and PyJWT plus ~one focused
module of Sibyl-owned glue — Sibyl owns identity end-to-end, the ingress controller does not.
Authorization is driven by **IdP App Roles** (or equivalent role-claim mapping per IdP), not by
email domain. Identity is keyed on stable identifiers from the IdP, never email. The data plane is
**single-node SurrealDB on RocksDB** with PVC snapshots, tested logical-export backups, and an
explicit promotion gate to TiKV clustering when measurable load forces the issue.

The plan stays IdP-agnostic in its contract but uses Microsoft Entra ID as the concrete worked
example throughout (it's the most-documented enterprise OIDC story and the one we have hands-on
validation against). A "Mapping to other IdPs" section translates the pattern to Okta, Auth0,
Keycloak, and generic OIDC. Per-deployment specifics (cloud provider, ingress controller flavor,
secrets-injector choice, audit-log sink, backup target) belong in a deployment-overlay doc
maintained separately by the deploying organization — they're explicitly out of scope here.

The enterprise path does not change Sibyl's default product mode. A fresh install remains
local-first and single-user friendly: local auth is enabled, the first setup signup creates the
owner/admin user, and later account creation is invite-based unless public signups are explicitly
enabled. OIDC, silent refresh, non-corporate OAuth providers, break-glass, and disabled local auth
are enterprise/operator opt-ins.

---

## Goals

1. A team can install Sibyl into Kubernetes and have users sign in with their corporate IdP,
   JIT-provision into an Organization, create personal and project MemorySpaces, and use Sibyl from
   web, CLI, and MCP clients.
2. The OSS Helm chart and config surface support an internal security review without requiring
   downstream forks: MFA-enforced sessions, encrypted secrets, restricted Pod Security, audit log
   surface, default-deny network policy, tested backup.
3. Use libraries instead of writing OAuth/OIDC from scratch. No bespoke crypto.
4. The chart is composable: deploys cleanly with any Gateway-API-compatible ingress, any secrets
   injector, any block-storage CSI driver. Sibyl does not assume a specific cloud.

## Non-goals

1. **No SaaS auth vendor lock-in.** No Auth0 SDK, Clerk, WorkOS, or similar unless they become
   uniquely best-fit later.
2. **No separately-deployed identity broker.** Authentik/Keycloak/Zitadel buy admin UIs Sibyl
   doesn't need — the IdP (Entra/Okta/Auth0/Keycloak/etc.) is the IdP, Sibyl is the relying party.
3. **No multi-tenant IdP app by default.** The reference deployment shape is single-tenant against
   the deploying organization's IdP. Multi-tenant operation across IdPs is future work.
4. **No per-Organization SSO config in v1.** The data model leaves room for per-Organization OIDC
   provider config (for multi-org deployments), but the UI and admin flow are out of scope here. A
   single deployment talks to one IdP.
5. **No SCIM provisioning by default.** At the team-deployment scale this plan targets (≤
   low-hundreds of users), JIT-on-first-login plus role-claim-driven deprovisioning is the realistic
   shape. SCIM is documented as a future option.
6. **No re-architecture of Sibyl's auth model.** The existing User/Organization/Membership/Role
   records in SurrealDB stay; OIDC plugs into them.
7. **No ingress-side OIDC enforcement as the primary auth boundary.** Sibyl owns OIDC. Ingress is
   routing/TLS/rate-limits. Running ingress-OIDC in addition to app-OIDC is two callback paths and
   two session models fighting each other.
8. **No TiKV clustering in the default shape.** Single-node SurrealDB on RocksDB is the starting
   point; clustering is a promotion gate with explicit criteria, not the v1 starting shape.

---

## Architecture

### Identity and authentication

**Library choice:** Authlib (Starlette/FastAPI client, ~5.3k stars) for the OIDC dance, pinned
`authlib>=1.7.2,<1.8`. The May 2026 Authlib advisory
([Snyk SNYK-PYTHON-AUTHLIB-16643257](https://security.snyk.io/vuln/SNYK-PYTHON-AUTHLIB-16643257)) is
fixed in `1.7.1+` and `1.6.12+`; `1.7.2` is the current latest release as of 2026-05-22. PyJWT
pinned `pyjwt[crypto]>=2.13.0,<3` with `PyJWKClient` for ID-token verification. Sources:
[Authlib FastAPI docs](https://docs.authlib.org/en/v1.6.11/client/fastapi.html),
[PyJWT JWKS usage](https://pyjwt.readthedocs.io/en/latest/usage.html),
[PyJWT PyPI](https://pypi.org/project/PyJWT/). Argon2id (`argon2-cffi`) for API key hashing.
Rationale for choosing Authlib over alternatives:

- **fastapi-users is active** (15.0.5 released 2026-03-27) but wrong fit — owns too much
  user-management shape, only documents SQLAlchemy and Beanie backends, would force a custom
  SurrealDB backend on top of OIDC anyway. Source:
  [fastapi-users PyPI](https://pypi.org/project/fastapi-users/).
- **python-jose is the wrong fit for new Sibyl OIDC work.** It did resume releases (`3.5.0` on
  2025-05-28), so the objection is not project death. The issue is fit: it brings a broader JOSE
  stack and backend choices Sibyl does not need, while the repo already uses PyJWT for local session
  tokens and PyJWT's `PyJWKClient` covers JWKS lookup for ID-token verification cleanly. Source:
  [python-jose PyPI](https://pypi.org/project/python-jose/).
- **IdP-specific FastAPI integration libraries** (such as `fastapi-azure-auth` for Entra) are
  excellent at their narrow scope, but Authlib's `OAuth()` registry generalizes to any OIDC provider
  with the same async flow shape — otherwise we'd ship a different library per provider Sibyl ever
  wants to support.
- **fastapi-sso (0.21.x)** handles the redirect dance but doesn't issue sessions or persist users,
  so we'd still write everything downstream of the callback.

**Provider gating:** Sibyl supports any standard OIDC provider via the `IdentityProvider` table. A
deployment chooses which providers are enabled via `oidc.providers` Helm values. Each deployment
should enable only the providers whose MFA story is enforced by the corporate IdP — see "MFA
enforcement" below. The reference shape in this plan enables one corporate-IdP provider in prod;
non-corporate providers (Google, GitHub direct, etc.) are intended for dev environments only, gated
behind `SIBYL_OIDC_EXTRA_PROVIDERS_ENABLED=false` in prod values so they cannot be accidentally
enabled at the application layer, with the additional structural guarantee that the secrets and
network egress rules in prod also don't include them.

#### Token contract

Two distinct tokens live in two distinct lanes:

1. **IdP ID token** — issued by the corporate IdP, consumed by Sibyl exactly once, at the OIDC
   callback. Validation per the standard OIDC ID-token rules: signature against `jwks_uri` from the
   provider's OIDC discovery doc; `iss` matches the discovery-document issuer; `aud` matches Sibyl's
   registered client ID; `exp`, `nbf`, `iat`; `nonce` matches the request nonce; any
   provider-specific tenant/audience claims also verified. After validation, Sibyl reads the stable
   identity claims (`sub` always, plus provider-specific stable identifiers — see "Stable identity
   per IdP" below) along with `email`, `name`, and the role claim, then discards the IdP token.
   Sibyl does not store, refresh, or replay IdP tokens against any IdP API by default.
2. **Sibyl session JWT** — issued by Sibyl after a successful OIDC login, signed with Sibyl's own
   key (HS256 with a key from the secrets injector). This is the cookie/bearer token attached to
   every subsequent web request. Validation is local: signature against Sibyl's key, `iss == sibyl`,
   `aud == ` Sibyl's API audience constant, `exp`, `nbf`. **Short lifetime (default 60 minutes), no
   Sibyl-side refresh cookie that bypasses the IdP.** Session renewal goes through a best-effort
   silent OIDC `prompt=none` authorization request against the IdP. The renewal contract:
   - **Success**: the IdP returns a fresh ID token containing the current role claim. Sibyl
     validates and mints a new session JWT. If the role claim is absent or no longer contains a
     Sibyl role, the session is hard-denied — this is the "deprovisioning is real" semantic, and
     it's a deny even though the IdP technically returned a token.
   - **Soft-fail to full login**: the IdP returns any of `login_required`, `interaction_required`,
     `consent_required`, or `account_selection_required` (per
     [OIDC Core §3.1.2.6](https://openid.net/specs/openid-connect-core-1_0.html#AuthError)). The
     browser is bounced to full sign-in.
   - **Cookie-blocked failure**: modern browsers (Safari ITP, Chrome third-party cookie
     restrictions, Firefox ETP) increasingly break hidden-iframe silent auth because the IdP cookie
     is treated as third-party from the Sibyl origin. When the iframe fails to complete (timeout,
     navigation error, opaque response), Sibyl falls back to full sign-in. Auth0, Okta, and Keycloak
     all document this third-party-cookie breakage in their silent-auth guides; the deployment may
     need to host the IdP on a same-site domain or use the IdP's first-party redirect-based silent
     flow to preserve seamless renewal.

   Silent refresh is therefore best-effort UX — the security contract is that no role-less ID token
   mints a session, and any non-trivial failure bounces to full login. A local refresh cookie that
   doesn't round-trip the IdP would silently break this contract; that's why we don't ship one.

   Server-side immediate revocation lives in a database-level `token_version` field that bumps on
   logout-everywhere or admin-forced-logout, invalidating all outstanding session JWTs for that user
   without waiting for the next silent refresh.

API keys for CLI and MCP are a third lane: random 32-byte token, prefix + Argon2id hash stored,
scope claims for `read`/`write`/`admin`, verified by the same FastAPI dependency that handles
session JWTs. They never touch the IdP after issuance.

Sibyl does **not** accept IdP-issued access tokens as bearer credentials against Sibyl's API by
default. If a deployment wants that path (for first-party native apps holding IdP access tokens), it
registers an explicit App ID URI / API audience with the IdP and validates that audience server-side
— but the default design has no path that needs it.

#### Existing session migration contract

The current codebase has local username/password login, GitHub OAuth, server-tracked access
sessions, and a `sibyl_refresh_token` cookie. W1 does not pretend those paths vanish automatically.
It makes the production OIDC contract explicit:

- OIDC web login does **not** issue a Sibyl refresh cookie. Renewal goes through the IdP
  `prompt=none` flow described above.
- Existing refresh-token issuance remains only for local-auth/dev and for any MCP OAuth
  compatibility path that still requires it, and both are gated out of the default enterprise
  values.
- `SIBYL_LOCAL_AUTH_ENABLED=false` in production hides password login, and
  `SIBYL_OIDC_EXTRA_PROVIDERS_ENABLED=false` prevents GitHub/Google-style non-corporate providers
  from being registered in the live OAuth registry.
- W1 adds regression tests that OIDC login/callback/refresh never sets `sibyl_refresh_token`, while
  local-auth tests keep covering the legacy refresh-token path behind the flag.

#### Stable identity per IdP

User identity in Sibyl is keyed on stable IdP-provided identifiers, never on `email`. The OIDC spec
and individual IdP docs all warn against email as an identifier — it can change, be missing, or
collide. The mapping table per IdP:

| IdP                | Stable identity key | Notes                                                                                                                                                                  |
| ------------------ | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Microsoft Entra ID | `(tid, oid)`        | `oid` is the immutable user object ID within the tenant; `sub` is per-pairwise-app and also stable. Microsoft explicitly recommends `oid` for cross-app user identity. |
| Okta               | `(iss, sub)`        | `sub` is the Okta-stable user ID.                                                                                                                                      |
| Auth0              | `(iss, sub)`        | `sub` includes the connection prefix (for example Auth0 database or Google OAuth); use the full string.                                                                |
| Keycloak           | `(iss, sub)`        | `sub` is the Keycloak user UUID.                                                                                                                                       |
| Generic OIDC       | `(iss, sub)`        | OIDC core requires `sub` to be locally unique and never reassigned within `iss`.                                                                                       |

Sibyl stores this in `user_identity (provider, subject_key, user_id)`. Email is stored on the User
row as a display-only profile field that can change without affecting Sibyl identity.

#### Authorization model: IdP role claims, not email domain

Sibyl reads a role claim from the validated ID token and gates JIT provisioning + ongoing
authorization on it. The roles are application-defined (`Sibyl.Member`, `Sibyl.Admin`,
`Sibyl.Owner`); the IdP-side mechanism that gets them into the claim varies:

| IdP                | Mechanism                                                                 | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Microsoft Entra ID | App Roles defined on the app registration                                 | Assign a security group (e.g. `Sibyl Users`) to `Sibyl.Member`; assign Admin/Owner per user. `roles` claim contains the assigned App Roles.                                                                                                                                                                                                                                                                                                     |
| Okta               | Groups → `groups` claim with a filter, or custom claim mapping to `roles` | Either map Okta groups directly into a `roles` ID-token claim via Okta's claim-mapping UI (claim inclusion set to "Always" or matching the OIDC scope being requested), or set `oidc.role_claim: "groups"` and add `groups` to `oidc.providers[].scopes` so Okta emits the claim.                                                                                                                                                               |
| Auth0              | Rules / Actions populate a custom `urn:sibyl:roles` claim from Auth0 RBAC | Auth0 RBAC roles assigned via the Dashboard or Management API; a Post-Login Action copies them into the namespaced custom claim. Set `oidc.role_claim: "urn:sibyl:roles"`.                                                                                                                                                                                                                                                                      |
| Keycloak           | Realm or client roles surfaced via the appropriate role-mapper            | Keycloak's default tokens emit realm roles under `realm_access.roles` and client roles under `resource_access.<client>.roles`, **not** a flat top-level `roles` claim. ID-token inclusion is mapper-configurable: either add a Protocol Mapper that flattens Sibyl roles into a top-level `roles` claim, or configure `oidc.role_claim: "resource_access.sibyl.roles"` (nested dot-path) so Sibyl reads them where Keycloak natively puts them. |
| Generic OIDC       | A configurable role-claim name or dot-path (default `roles`)              | Sibyl reads `oidc.role_claim` from config and supports nested paths via dotted notation.                                                                                                                                                                                                                                                                                                                                                        |

Sibyl's contract: on OIDC callback, read the configured role-claim path from the validated ID token;
if no Sibyl role is present, deny sign-in with a friendly "ask an admin for access" page; if
`Sibyl.Member` or higher is present, JIT-provision and attach the role.

Deprovisioning is real because of the silent-refresh contract: removing a user from the IdP-side
role assignment means their next session renewal (≤60 minutes) gets an ID token without the role
claim, which terminates the session. Admins can also use server-side immediate revocation via the
`token_version` bump for emergency logout-everywhere.

Why role-claims over raw group GUIDs: Microsoft documents
[around 200 groups in JWT access tokens](https://learn.microsoft.com/en-us/security/zero-trust/develop/configure-tokens-group-claims-app-roles)
before hitting the overage indicator (SAML caps lower at 150). Overage indicators (`hasgroups` /
`groups:src1`) force a Microsoft Graph callback Sibyl doesn't want to wire. Equivalent IdPs have
similar bounds (Okta caps group claims per token, Keycloak emits all roles by default which is fine
for small role sets but unbounded for groups). Application-defined roles are bounded and travel
cleanly.

Email domain is **not** an auth boundary anywhere in the contract — not for sign-in, not for
provisioning, not as a sanity check. The role claim is the sole authorization decision. Email is
read from the ID token's optional `email` claim only for display in the UI ("logged in as ...") and
audit log readability.

#### MFA enforcement: at the IdP, not in the app

MFA enforcement happens at the IdP, by policy, not by claim inspection in Sibyl. Per IdP:

- **Entra ID**: tenant-level Conditional Access policy targeting "All resources" (or scoped to the
  Sibyl app) requires MFA for every sign-in. Session length via CA Sign-in Frequency. Sources:
  [Conditional Access target resources](https://learn.microsoft.com/entra/identity/conditional-access/concept-conditional-access-cloud-apps),
  [Mandatory MFA enforcement](https://learn.microsoft.com/entra/identity/authentication/concept-mandatory-multifactor-authentication).
  Phishing-resistant MFA (FIDO2 / Windows Hello) for `Sibyl.Admin` / `Sibyl.Owner` via CA
  authentication strength.
- **Okta**: Authentication policies on the OIDC app require MFA factor; assurance levels distinguish
  Member from Admin requirements.
- **Auth0**: Multi-factor policies on the Auth0 tenant; rules can force phishing-resistant factors
  for high-privilege roles.
- **Keycloak**: Required actions on the realm or browser flow conditions for the client.

Sibyl does **not** inspect the `amr` claim to determine MFA satisfaction. That pattern breaks the
moment a new client type appears with a different `amr` value, and the IdP is the authoritative
source. Sibyl's contract is: "if the IdP says they're authenticated, they're authenticated."
Configuring the IdP's MFA policy correctly is the deployment's responsibility.

Wire continuous-access-evaluation-aware revocation when the relying-party SDK supports it for the
deployed IdP. For Entra, this is on the roadmap once Microsoft's Python relying-party libraries
support CAE.

#### Requested OAuth scopes: minimum viable

`openid profile email` is the default — that's it. Sibyl does not call any IdP-side API (Microsoft
Graph, Okta Management API, etc.) by default, so resource-specific scopes (`User.Read`, etc.) are
unnecessary consent surface. Sibyl does not store refresh tokens, so `offline_access` is unnecessary
too. Adding scopes when they're not used is avoidable consent blast radius — a noticeable consent
screen will scare off enterprise security review.

**Per-provider scope overrides** are supported via `oidc.providers[].scopes`. Some role-claim
mechanisms require an extra scope to make the IdP emit the claim — most notably, Okta's groups claim
is only included in tokens when the `groups` scope is in the request (per Okta's groups-claim and
ID-token claim-inclusion docs). When a deployment configures `oidc.role_claim: "groups"` against
Okta, it should also set `oidc.providers[okta].scopes: ["openid", "profile", "email", "groups"]`.
The OSS default remains the minimum-viable triplet.

#### Ingress controller's role: TLS and routing only

The deployment's ingress controller (any Gateway API-compatible implementation or a classic Ingress
controller) handles TLS termination, hostname routing, and edge rate limits. It does **not** enforce
OIDC. Sibyl owns the entire auth boundary — there is one identity verification surface, in the
FastAPI dependency. Running ingress-OIDC plus app-OIDC creates two callback paths, two session
models, and breakage when MCP clients hit the API path with an API key (which the ingress wouldn't
validate). The OSS chart provides Gateway API HTTPRoute and classic Ingress templates, gated by Helm
values so the deployment can pick one or neither; both are routing-only.

#### CLI / MCP onboarding

Browser-based OIDC for the web UI. CLI and MCP clients (Cursor, Claude Code, Claude Desktop, custom
clients) get an API key by logging into the web UI and minting one in `/settings/api-keys`. Paste
into the MCP server config or `sibyl auth login`. Device-code OIDC flows from CLI directly into the
IdP are not v1 scope — MCP client OAuth UX is inconsistent enough that API-key-after-web-login is
the realistic shape.

#### Local fallback and break-glass

Username/password login behind a `SIBYL_LOCAL_AUTH_ENABLED` flag defaults on for the single-user
install path. Setup mode allows the first admin to be created; after that, public signup stays off
unless explicitly enabled and additional users join by invitation. Enterprise SSO deployments set
`SIBYL_LOCAL_AUTH_ENABLED=false` after the corporate OIDC provider is configured, and break-glass
remains a separate bounded emergency path.

The break-glass pattern follows the standard "emergency access account" shape
([Microsoft Entra guidance](https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/security-emergency-access)
is the most explicit reference but the pattern is generic):

- **Two** custodian-held break-glass accounts, not one, to survive a single-account compromise.
- Credentials stored in the deployment's secrets injector with quarterly rotation.
- Accounts gated behind a `SIBYL_BREAK_GLASS_ENABLED` flag that defaults `false` and TTLs back to
  `false` after 4 hours of enablement.
- IP allowlist restricted to a documented operator network range.
- A high-severity alert into the deployment's pager / incident system on any successful break-glass
  sign-in.
- Quarterly drill that exercises the credentials so they're never first-used during an incident.
- Every break-glass action audit-logged with the actor's name, TTL session start, and reason.

The Sibyl chart provides the flag, allowlist field, and audit hooks. Wiring the alert and credential
storage is deployment-specific.

### Data plane: SurrealDB (single-node default)

**Topology:** Single SurrealDB pod backed by RocksDB on a premium block-storage PVC. This is the
default starting shape for any Sibyl deployment under the team-scale this plan targets (≤
low-hundreds of users, ≤ ~100k entities, sub-GB working set). The TiKV-backed clustered topology is
**not** the v1 starting shape — it's a promotion gate documented below.

The choice is honest about scale-necessity rather than data safety. At Sibyl's target deployment
scale, single-node RocksDB is the right correctness/operational fit. TiKV brings TiDB Operator
upgrades, BR restore complexity, quorum math, zonal scheduling, and CRD ownership that aren't worth
paying for without measurable benefit. Operational rehearsal (e.g., in a Tilt local cluster) is
valuable as proof-of-concept for the promotion path, not as a justification for shipping the heavier
topology immediately.

**Reference values (the OSS chart provides these knobs):**

- **SurrealDB chart**: `helm.surrealdb.com/surrealdb 0.4.0` (the only published 0.4.x version, last
  commit 2025-09-02; `appVersion: 2.3.7` is stale and overridden via `image.tag: v3.0.5`).
  `replicaCount: 1`, `strategy.type: Recreate` (RWO PVC requires it),
  `surrealdb.path: rocksdb:/data/db`, `surrealdb.unauthenticated: false`,
  `persistence.enabled: true` with a premium-block storage class, size starting at `100Gi`.
- **`args: [start]`** — no `--strict` server flag. Strict mode moved out of the server CLI in 3.x
  and lives in `DEFINE DATABASE <db> STRICT;` per the
  [`start` command docs](https://surrealdb.com/docs/reference/cli/surrealdb-cli/commands/start).
  Strict-mode is set by the bootstrap Job below.
- **Sync writes default to on** in SurrealDB 3.0
  ([3.0 release notes](https://surrealdb.com/blog/introducing-surrealdb-3-0--the-future-of-ai-agent-memory)).
  No explicit `--sync` flag needed.
- **Bootstrap (Helm post-install Job):** runs once after first install to apply
  `DEFINE DATABASE <db> STRICT;` per database, then triggers Sibyl's existing idempotent schema
  bootstrap. Migration-safe to re-run.

**Reference sizing** for ~50 users / ~100k entities / ~1M raw memories headroom: SurrealDB pod with
4 vCPU / 32 GiB node, 2 vCPU / 4 GiB requests, 4 vCPU / 8 GiB limits, 100 GiB premium SSD PVC.
Single-writer is the correctness fence — no HPA, but a PodDisruptionBudget with `minAvailable: 1`
and `topologySpreadConstraints` keeps scheduler sanity during node drains.

**Known caveats:**

- **Official SurrealDB Helm chart is dormant.** Last release 2025-09-02 with `appVersion: 2.3.7`; no
  commits since. Sibyl pins to that chart version and overrides `image.tag` to ride the current OSS
  server. The chart only ships Deployment + HPA + Ingress + Service + PVC templates — no StatefulSet
  (Deployment is fine for the single-pod RWO PVC pattern with `strategy: Recreate`), no Gateway API
  HTTPRoute (Sibyl's wrapper chart adds it), no ServiceMonitor / OTel-collector wiring (Sibyl adds
  via `podExtraEnv`). The chart's env-var surface (`SURREAL_PATH`, `SURREAL_USER`, `SURREAL_PASS`,
  `SURREAL_AUTH`, `SURREAL_UNAUTHENTICATED`, `SURREAL_LOG`, `SURREAL_OBJECT_CACHE`,
  `SURREAL_OBJECT_STORE`) is sufficient for our config without template patches.
- Native Prometheus `/metrics` endpoint is not shipped
  ([issue #6258](https://github.com/surrealdb/surrealdb/issues/6258)). Sibyl uses OTel-only.
- Enterprise object-storage-backed tier (S3/Blob/GCS storage-compute separation) is not OSS in 3.0.
  Not relevant at the default scale.
- Single-node means **no HA on the data plane.** PVC snapshots + tested restore is the durability
  story. RTO target depends on the deployment's SLO; for an internal team tool, ≤ 4h is realistic.

**When to promote to TiKV (the explicit gate):** trip any of these and revisit the topology.

1. p95 query latency exceeds the deployment's SLO (Sibyl targets sub-1s `recall`, sub-500ms `wake`)
   with pod profiling showing CPU/IO saturation that a node-size bump can't fix.
2. Working set exceeds 50 GiB _and_ read concurrency starts queueing.
3. A real HA requirement appears (security or product, not aesthetic preference).
4. Multi-region or cross-zone read replicas are needed.

When promotion triggers, lift to the TiKV-backed shape: PD + TiKV via TiDB Operator (3-node
minimums), SurrealDB compute moves to `surrealdb.path: tikv://<pd-service>:2379` with
`replicaCount: 2+` and `persistence.enabled: false`. Backups upgrade from PVC snapshots +
`surreal export` to `tikv-br backup raw` per the
[TiKV RawKV BR docs](https://tikv.org/docs/dev/concepts/explore-tikv-features/backup-restore/). The
Sibyl chart supports both shapes; the deployment chooses via values.

### Tenancy

One Organization per deployment is the default reference shape — a single shared workspace where all
users live. Within it, MemorySpaces map to projects: `personal:<user_id>` (per-user private),
`<org>-shared` (org-wide default), `project:<slug>` (created on demand).

**JIT provisioning is role-claim-gated.** On OIDC callback Sibyl reads the configured role claim
from the validated ID token. The user is denied sign-in if no Sibyl role is present. If
`Sibyl.Member` (or higher) is present, the user JIT-provisions into the configured Organization with
the corresponding role. Identity keying uses the stable IdP identifier (see "Stable identity per
IdP"). The link is recorded in `user_identity`; email is stored as a display-only profile field.

**Bootstrap (Helm post-install Job):** creates the configured Organization if absent, seeds the
default MemorySpace, runs `DEFINE DATABASE <db> STRICT;` per database. Idempotent. No domain-mapping
rules — role assignment lives in the IdP, not in Helm values.

For multi-Organization deployments (deferred), per-Organization OIDC provider config lives in the
`IdentityProvider` table and the role-claim is namespaced or distinguished per-Organization. The
data model supports this; the admin UI does not yet.

### Ingress and routing

The chart provides two ingress templates, both gated by Helm values so the deployment picks one:

- `templates/httproute.yaml` — Gateway API-compatible HTTPRoute. Gated by
  `ingress.gatewayApi.enabled`. Routes:
  - `<host>/` → frontend service
  - `<host>/api/` → backend service
  - `<host>/mcp/` → backend service with session affinity for stateful MCP connections
- `templates/ingress.yaml` — standard `networking.k8s.io/v1` Ingress for clusters not yet on Gateway
  API. Gated by `ingress.classic.enabled`.

Both default to disabled so the deployment must opt in.

The chart does not bundle cert-manager or external-dns annotations directly — those go in the
per-deployment values overlay. The chart accepts arbitrary annotations via `ingress.annotations`.

Edge rate limits: 60 req/min per-IP on `/api/auth/*` to slow credential stuffing. Per-API-key
budgets are enforced inside Sibyl, not at the ingress.

### Secrets

Sibyl expects a single Kubernetes Secret named per the chart values (default `sibyl-secrets`)
containing:

- `SIBYL_JWT_SECRET`: 32-byte hex, Sibyl session signing key
- `SIBYL_OIDC_<PROVIDER>_CLIENT_SECRET`: one per enabled provider (e.g.
  `SIBYL_OIDC_ENTRA_CLIENT_SECRET`)
- `SIBYL_ANTHROPIC_API_KEY`: shared LLM provider key
- `SIBYL_OPENAI_API_KEY`: embedding provider key (or skip if using a different embedding provider)
- `SIBYL_SURREAL_PASSWORD`: root password
- `SIBYL_VALKEY_PASSWORD`

How that Secret gets populated is the deployment's choice:

- **External Secrets Operator** (or equivalent) pulling from a cloud KMS-backed secret manager.
  Recommended where the deployment's secret store has its own audit log, rotation, and
  workload-identity auth.
- **Encrypted-secrets-in-Git tooling** (Sealed Secrets, SOPS-driven operators, etc.) with a
  cluster-side decryption controller. Lower operational overhead, fine if the secret store doesn't
  need its own audit trail.
- **Manual `kubectl create secret`** for development or air-gapped environments.

The chart does not bundle ExternalSecret or SealedSecret resources by default; the deployment
overlay adds them. The chart accepts `secret.existingSecret` to consume a pre-populated Secret
regardless of how it got there.

Provider secrets: only include secrets for OIDC providers actually enabled in the deployment.
Keeping the prod secret list to the providers in active use makes "accidentally enable a non-IdP
provider in prod" structurally impossible at the secrets layer — there's nothing to enable against.

API key storage: hashed with Argon2id at rest, never encrypted (encryption implies you'll display
them again, which is the wrong UX for secrets).

### Observability

**OpenTelemetry-first.** The OSS chart exports OTLP via `OTEL_EXPORTER_OTLP_ENDPOINT` to whatever
collector the deployment runs. No app-side Prometheus required.

**Required instrumentation:**

- FastAPI auto-instrumentation via `opentelemetry-instrumentation-fastapi` (HTTP spans).
- Custom spans on `recall`, `remember`, `wake`, `synthesize`, plus job-queue worker entry points.
- Span attributes always include `user.id`, `org.id`, `tool.name`, `memory_space`.
- For LLM calls: token counts, model, prompt-hash. Sources:
  [OTel for LLMs in 2026](https://openobserve.ai/blog/opentelemetry-for-llms/).

**Golden signals** (per
[Google SRE Book monitoring guidance](https://sre.google/sre-book/monitoring-distributed-systems/)):
request rate, error rate, p50/p95/p99 latency, saturation. Suggested informal SLOs: 99%
availability, p95 < 2s for search/recall, p95 < 500ms for `wake`. Deployments can tune.

**SurrealDB observability** ships via OTel only
([Surreal observability docs](https://surrealdb.com/docs/surrealdb/reference-guide/observability)).
Wire its OTLP exporter to the same collector.

### Audit logging

Two layers, both required for a credible enterprise deployment:

**Application-level (Sibyl).** `AuditEvent` records in SurrealDB cover login, role assignment
changes inside Sibyl, `remember`/`recall`/`wake`/`synthesize` invocations, admin actions, API key
create/revoke, and config changes. The chart surfaces them via `/admin/audit` in the web UI with
filters by user, action, time, resource; CSV/JSON export; restricted to `Sibyl.Admin` /
`Sibyl.Owner`.

**Infrastructure-level.** Sibyl's `AuditEvent` does not see Kubernetes Secret reads, pod actions, or
IdP sign-ins — those need the deployment's infrastructure-level audit configured. Internal security
review will ask for all three:

- **Secret store**: enable the secret store's own audit / access log (KMS audit logs, Vault audit
  devices, etc.). Captures secret get/list/set operations including the caller identity.
- **Kubernetes audit**: enable `kube-audit` / API server audit logs at the cluster level
  (cluster-provider-specific).
- **IdP sign-in logs**: configure the IdP to ship sign-in and audit logs to the deployment's log
  sink (Entra sign-in logs, Okta system log, Auth0 logs, Keycloak event listener).

All three flow to the deployment's log sink (whatever SIEM or log warehouse the org runs). Sibyl
`AuditEvent` ships to OTel as structured log records using `event.name` / `event.domain` attributes
per [OTel general log attributes](https://opentelemetry.io/docs/specs/semconv/general/logs/).

**Retention:** 1 year as the defensible internal floor — NIST SP 800-92 punts retention to
organizational policy and the
[SOC 2 logging norm](https://www.konfirmity.com/blog/soc-2-logging-pipelines-for-soc-2) is what most
teams default to. Deployments can override.

### Network policy

The chart provides a `templates/networkpolicy.yaml` (gated by `networkPolicy.enabled`) implementing
default-deny ingress + egress at the Sibyl namespace, with explicit allows for:

- API → SurrealDB (intra-namespace, port 8000)
- API → Valkey (intra-namespace, port 6379)
- API → OTel collector (configurable)
- API → LLM provider egress (configurable host:port; FQDN filtering recommended where the CNI
  supports it, since `ipBlock` can't reliably target managed-cloud LLM endpoints)
- API → enabled OIDC provider discovery + JWKS endpoints (configurable per enabled provider)
- Frontend → backend (intra-namespace)
- Worker → LLM providers, SurrealDB, Valkey
- Ingress controller → frontend + backend ingress

Deployments without a CNI that supports egress NetworkPolicy can disable this template and rely on
cluster-level egress controls.

### Pod Security

`restricted` Pod Security Standards at the Sibyl namespace level via labels. Current chart values
already set `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, and drop `ALL` capabilities for
backend, frontend, and worker; backend and worker set `readOnlyRootFilesystem: true`, while frontend
leaves it `false` because Next.js needs write access. Adding `seccompProfile: RuntimeDefault` to all
pods and namespace-level Pod Security labels is a v1 chart change. Source:
[Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/).

**Image signing and scanning:** Cosign-sign all Sibyl images in CI; Trivy SBOM + CVE scan with
HIGH/CRITICAL as a CI gate. Cosign-verify at admission is nice-to-have for the deployment but the CI
gate is load-bearing on the OSS side. Source:
[Trivy SBOM attestation](https://trivy.dev/docs/latest/supply-chain/attestation/sbom/).

### Backup and disaster recovery

**Reference target:** RPO ≤ 24h, RTO ≤ 4h. Proportionate for an internal team tool. Deployments with
stricter SLOs adjust.

**Mechanism (two complementary tracks, both shipped as CronJob templates in the chart):**

1. **PVC VolumeSnapshot CronJob** via the cluster's CSI snapshot driver. Daily, configurable
   retention. Fast restore (re-attach snapshot), captures consistent on-disk state if SurrealDB
   writes are briefly quiesced before snapshot (the CronJob waits for write idle, then triggers the
   snapshot).
2. **Logical export CronJob** via `surreal export`. Daily, output encrypted with a
   deployment-provided key, shipped to the deployment's blob storage. Slower restore than PVC
   snapshot but portable across SurrealDB versions and storage backends; the escape hatch if an
   upgrade misbehaves.

**Restore drill CronJob:** weekly automated job that pulls the latest export, spins an ephemeral
SurrealDB pod, runs `surreal import`, and verifies a known fixture row count. Failing this job pages
on-call. Untested backups are folklore.

When a deployment promotes to TiKV, the backup story upgrades to `tikv-br backup raw` per the
[TiKV RawKV BR docs](https://tikv.org/docs/dev/concepts/explore-tikv-features/backup-restore/) with
the actual `--storage`, `--ratelimit`, `--checksum`, `--gcttl` spec defined in the deployment
overlay.

### LLM cost management

**Per-user and per-org monthly token budgets** enforced in the LLM provider layer (Sibyl already has
a provider abstraction). Default values are conservative — 1M tokens/user/month, 30M
tokens/org/month — and deployments override per their LLM provider quota and budget posture.

Per-user rate limit on bulk recall (max N concurrent recall calls) to prevent a stuck agent from
burning the team's quota in an afternoon. Bypass for `Sibyl.Owner` role.

LLM API key strategy is a deployment choice:

- **Central key** with per-user attribution via audit events. Simplest, lowest friction, matches
  "shared team subscription" billing.
- **Per-user BYO keys** stored encrypted in the user profile. Cost isolation is automatic but breaks
  shared agent flows where the user isn't present.
- **Hybrid** with central default + per-user override.

The chart exposes the central-key path by default; the BYO path is a future feature.

### Image pipeline

The OSS chart consumes images from a single registry coordinate per workload
(`backend.image.repository`, `frontend.image.repository`, etc.). Default values point at the
official Sibyl GHCR images. Deployments that mirror to a private registry override these.

CI builds and pushes Sibyl images to GHCR on tag with SBOM + Cosign signature. Multi-registry
mirroring (e.g., GHCR + cloud-specific registry) is configured in the deployment's CI overlay, not
in the Sibyl OSS workflow.

---

## Workstreams

### W1. Native OIDC authentication (the only real Sibyl product change)

**Files to add/modify in the Sibyl repo:**

- `apps/api/src/sibyl/auth/oidc.py` — new. Authlib `OAuth()` registry consuming the
  `IdentityProvider` table; provider classes for Entra, Okta, Auth0, Keycloak, and generic-OIDC;
  async `login(provider)` and `callback(provider)` handlers with `state` + `nonce` validation,
  provider-specific stable-identity extraction (`(tid, oid)` for Entra, `(iss, sub)` for others),
  and ID-token claim verification per the token-contract section above.
- `apps/api/src/sibyl/auth/silent_refresh.py` — new. The `prompt=none` round-trip implementation:
  build the silent authorization URL, mount the iframe response handler, parse the IdP response,
  mint a new session JWT or bounce to full login.
- `apps/api/src/sibyl/auth/jit.py` — new. `provision_user_from_oidc(claims, provider)` → resolve
  User by stable identifier; never by email; JIT-provision into the configured Organization gated on
  the role claim (must contain `Sibyl.Member` or higher); record the link in `user_identity`; store
  email as a display-only field.
- `apps/api/src/sibyl/auth/dependencies.py` — modify. The dependency already accepts session JWTs
  and API keys; preserve that dual-credential contract while normalizing API-key scope claims so
  OIDC sessions and API keys resolve through the same authorization shape.
- `apps/api/src/sibyl/api/routes/auth.py` — add `/api/auth/oidc/{provider}/{login,callback,refresh}`
  routes. Keep `/api/auth/login` (password) under the `SIBYL_LOCAL_AUTH_ENABLED` flag.
- `apps/api/src/sibyl/config.py` — new fields: `oidc.providers` (list of provider configs from the
  chart, each with `name`, `issuer`, `client_id`, `client_secret_env`, `scopes` defaulting to
  `["openid","profile","email"]`, `role_claim_override` (optional, falls back to the top-level
  `oidc.role_claim`)), `oidc.role_claim` (default `roles`, supports dotted paths like
  `resource_access.sibyl.roles`), `oidc.redirect_uri_base`, `oidc.session_minutes` (default 60),
  `oidc.silent_refresh_enabled` (default false), `oidc.extra_providers_enabled` (default false;
  gates Google/GitHub-style non-corporate providers from being registered in the live OAuth
  registry, enforced at app startup with an exit-on-mismatch validation), `local_auth_enabled`
  (default true for the single-user install path; enterprise SSO values opt out explicitly).
- SurrealDB schema: new tables `identity_provider` (config per provider), `user_identity`
  (`(provider, subject_key) → user_id`). Migration via the existing idempotent schema bootstrap.
- `apps/web/src/app/login/page.tsx` — modify. Show provider buttons enumerated from the server's
  enabled-provider list. Hide username/password if `local_auth_enabled` is false.
- `apps/web/src/app/settings/api-keys/page.tsx` — new (or extend existing settings). UI for
  creating, viewing, and revoking API keys.
- `packages/python/sibyl-core/src/sibyl_core/models/users.py` — add `UserIdentity` model.
- Tests: `apps/api/tests/test_oidc.py` (Authlib mocked + ID-token claim verification fixtures for
  each provider), `apps/api/tests/test_jit_provisioning.py` (role-claim gating: missing role → deny,
  `Sibyl.Member` → provision, role removed → deny on next refresh),
  `apps/api/tests/test_api_keys.py` (Argon2id roundtrip), `apps/api/tests/test_silent_refresh.py`
  (mocked `prompt=none` flow — success path + `login_required` bounce path).

**Library additions in `apps/api/pyproject.toml`:**

```
authlib>=1.7.2,<1.8       # May 2026 advisory fixed in 1.7.1+; pin to current 1.7.x
pyjwt[crypto]>=2.13.0,<3  # current latest as of 2026-05-22
argon2-cffi
```

Enable Dependabot or equivalent security-update PRs on `authlib`/`pyjwt`/`argon2-cffi`. The
reference repo uses `.github/dependabot.yml` with the `uv` ecosystem at the workspace root.

**Library consolidation (in scope, part of W1):** Since Authlib is becoming a load-bearing
dependency, reduce bespoke OAuth-shaped code where Authlib fits cleanly. This is intentionally
feasibility-gated: Authlib documents FastAPI/Starlette client support, and Flask/Django provider
integrations. It exposes RFC primitives for OAuth metadata, dynamic client registration, PKCE, and
token validation, but there is no documented FastAPI/Starlette authorization-server adapter to drop
in wholesale. W1 starts with a short spike before deleting working MCP OAuth code.

- `apps/api/src/sibyl/auth/mcp_oauth.py` (~741 lines, custom FastMCP OAuth Authorization Server with
  `.well-known/oauth-authorization-server`, `/authorize`, `/token`, `/register`, dynamic client
  registration, authorization-code grant, refresh-token grant) → spike an Authlib-backed
  replacement. If an ASGI adapter is small and readable, use Authlib RFC/provider primitives for RFC
  6749, RFC 7591, RFC 7636, and RFC 8414, with Sibyl-owned client/user lookup callbacks. If the
  adapter is larger than the code it replaces, keep the FastMCP provider and extract only the
  Authlib validators/primitives that reduce risk.
- `apps/api/src/sibyl/auth/oauth_state.py` (~81 lines, HMAC-signed state-cookie module for CSRF
  protection) → delete for the OIDC app-login path. Authlib's
  `OAuth().register(...).authorize_redirect()` flow handles `state` and `nonce` internally via the
  configured session storage. Keep this module only if the MCP provider still needs a separate
  state-cookie path after the spike.
- `apps/api/src/sibyl/auth/mcp_auth.py` (~81 lines, FastMCP Bearer token verifier accepting Sibyl
  JWTs and API keys) → spike Authlib resource-server validators with a custom `BearerTokenValidator`
  that preserves dual-credential acceptance. If the available integration remains Flask-shaped only,
  keep the current verifier and add conformance tests around JWT/API-key scope handling instead of
  forcing a framework mismatch.

Keep as-is: `jwt.py` (already PyJWT-based, which the plan endorses), `passwords.py` for local-auth
compatibility until the separate password-hash migration lands, `primitives.py`
(`secrets.token_urlsafe` wrappers; correct). Change in W1: `api_key_common.py` currently hashes API
keys with PBKDF2-HMAC-SHA256; migrate API keys to Argon2id while preserving verification for
existing PBKDF2 records until they rotate.

Target consolidation: reduce roughly 900 lines of bespoke OAuth-shaped code when Authlib lowers
risk. Tests against the existing MCP client surface (Cursor, Claude Code, Claude Desktop) become a
v1 regression contract — the consolidation must not break existing MCP OAuth flows, and a smaller
custom provider is better than a brittle framework adapter.

**Out of scope for W1:** SCIM, per-Organization OIDC config (the schema supports it; the admin UI
doesn't), device-code CLI flow into the IdP, CAE-aware revocation, IdP-side API calls (Graph, Okta
Management API, etc.). Passwords migration from PBKDF2 to Argon2id is tracked separately in
"Decision defaults and deferred choices", not folded into W1.

### W2. Helm chart enterprise-readiness templates

**Files to add in the Sibyl repo:**

- `charts/sibyl/templates/httproute.yaml` — Gateway API-compatible HTTPRoute, gated by
  `ingress.gatewayApi.enabled` (default false).
- `charts/sibyl/templates/networkpolicy.yaml` — Default-deny + explicit allows, gated by
  `networkPolicy.enabled` (default false).
- `charts/sibyl/templates/bootstrap-job.yaml` — Post-install Helm hook Job that calls Sibyl's
  existing org-bootstrap CLI to seed the configured Organization, default MemorySpace, and any
  required `DEFINE DATABASE STRICT` calls. Idempotent. No domain-mapping rules — role assignment
  lives in the IdP.
- `charts/sibyl/values.yaml` — Add `oidc` config block (`providers` list with per-provider
  `scopes` + optional `role_claim_override`, `role_claim` global default with dotted-path support,
  `session_minutes`, `silent_refresh_enabled`, `extra_providers_enabled` defaulting to false),
  `ingress.gatewayApi.enabled`, `ingress.classic.enabled`, `networkPolicy.enabled`,
  `bootstrap.enabled`, `bootstrap.organization.name`, `breakGlass.enabled`, `breakGlass.allowedIPs`.
  Migrate the current single `ingress.enabled` / `className: "kong"` shape to provider-neutral
  `classic` and `gatewayApi` blocks. No `bootstrap.domains` — domain is not an auth axis. A Helm
  `lint`-time check (or chart `_helpers.tpl` assertion) fails the install if
  `extra_providers_enabled: false` but a Google/GitHub provider appears in the `providers` list —
  keeps the structural guarantee that non-corporate providers can't be enabled in prod.
- `charts/sibyl/templates/podsecurity.yaml` — Namespace `PodSecurity` labels (`restricted` enforce +
  audit) when `podSecurity.enforceRestricted` is true.

### W3. SurrealDB single-node chart wrapper (referenced by Sibyl)

**Files to add in the Sibyl repo (as a sub-chart or sibling chart):**

- `charts/surrealdb/Chart.yaml` — Sibyl-owned umbrella depending on `surrealdb/surrealdb 0.4.0`
  (pinned exactly).
- `charts/surrealdb/values.yaml` — Sibyl's recommended defaults (`replicaCount: 1`,
  `strategy.type: Recreate`, `image.tag: v3.0.5`, `surrealdb.path: rocksdb:/data/db`,
  `surrealdb.unauthenticated: false`, `persistence.enabled: true` with a generic premium-block
  storage class placeholder, `args: [start]`, OTel `podExtraEnv` stubs).
- `charts/surrealdb/templates/bootstrap-job.yaml` — Post-install/post-upgrade Job that runs
  `DEFINE DATABASE <db> STRICT;` per database.
- `charts/surrealdb/templates/snapshot-cronjob.yaml` — Daily CSI VolumeSnapshot CronJob with
  configurable retention.
- `charts/surrealdb/templates/export-cronjob.yaml` — Daily `surreal export` CronJob with
  configurable destination (blob/object-storage URI in deployment overlay).
- `charts/surrealdb/templates/restore-drill-cronjob.yaml` — Weekly CI-like Job that imports the
  latest export to an ephemeral pod, verifies fixture row counts, writes a structured restore
  receipt, supports a deployment-provided sampled recall check, and pages on failure.

**Deferred (the TiKV promotion path):** When a deployment trips the promotion gate, the chart
switches via `surrealdb.path: tikv://<pd>:2379` + `replicaCount: 2+` + `persistence.enabled: false`,
plus per-deployment TiDB Operator / TiKV cluster manifests in the overlay.

### W4. Audit log UI

**Files to add/modify:**

- `apps/api/src/sibyl/api/routes/admin.py` — Add `/api/admin/audit` endpoint with filters (user,
  action, resource, time range), pagination, CSV/JSON export. Restrict to `Sibyl.Admin` /
  `Sibyl.Owner`.
- `apps/web/src/app/admin/audit/page.tsx` — New page. Filterable table, time-range picker, export
  buttons.
- `packages/python/sibyl-core/src/sibyl_core/audit/` — If query patterns don't exist yet, add them.
- Tests: integration test that an `admin` can list events, a `member` cannot.

### W5. End-user docs

**Files to add in `docs/`:**

- `docs/users/login.md` — How sign-in works against an OIDC IdP (worked example: Microsoft Entra;
  mapping table for Okta/Auth0/Keycloak/generic OIDC).
- `docs/users/cli-setup.md` — Install Sibyl CLI (Homebrew or pip), `sibyl auth login` flow using an
  API key from the web UI.
- `docs/users/mcp-setup.md` — Configure Sibyl MCP in Cursor, Claude Code, Claude Desktop. Per-client
  config snippets.
- `docs/users/sharing-memory.md` — How MemorySpaces work, when to use personal vs project vs shared.
- `docs/admin/installing.md` — How to install Sibyl into a Kubernetes cluster: prerequisites,
  secrets injector choice, ingress controller choice, IdP setup (per-IdP).
- `docs/admin/inviting-users.md` — How JIT provisioning works, how to bump someone to admin, how to
  deprovision.
- `docs/admin/audit-log.md` — How to read the audit log, what events are recorded, retention.
- `docs/admin/backup-restore.md` — How the snapshot + export CronJobs work, how to restore.
- `docs/admin/break-glass.md` — How to set up the break-glass accounts and runbook.

### W6. Enterprise readiness hardening

Smaller items that don't deserve their own workstream but must land before any enterprise deploy is
credible:

- Pod Security `restricted` namespace label.
- Cosign-sign all Sibyl images, Trivy SBOM + CVE gate in CI.
- LLM provider budget enforcement (per-user, per-org monthly limits).
- User deletion: soft-delete-then-purge for personal memories within 30 days. Audit event for
  deletion.
- Backup restore-to-scratch CI job runs weekly against the OSS reference deployment.

---

## Implementation acceptance gates

The plan is implementation-ready only if each phase ships with receipts, not vibes:

1. **Auth contract gate:** `moon run api:test` passes for OIDC callback validation, JIT
   provisioning, missing-role denial, role-removed silent refresh denial, API-key Argon2id
   verification, and legacy PBKDF2 API-key compatibility. A real Entra dev-tenant smoke test proves
   the happy path and the missing-role denial.
2. **Session gate:** tests prove OIDC routes never set `sibyl_refresh_token`; local-auth
   refresh-token behavior remains covered behind `SIBYL_LOCAL_AUTH_ENABLED=true`.
3. **MCP compatibility gate:** Cursor, Claude Code, and Claude Desktop can still authenticate
   against Sibyl after the MCP OAuth/Authlib spike. If the Authlib adapter is not smaller and
   clearer, keep the current FastMCP provider and land only validator-level improvements.
4. **Chart gate:** `helm lint charts/sibyl charts/surrealdb`, `helm template` with
   `ingress.gatewayApi.enabled`, `ingress.classic.enabled`, `networkPolicy.enabled`,
   `podSecurity.enforceRestricted`, `bootstrap.enabled`, and backup CronJobs enabled. Rendered
   manifests must contain no provider-specific ingress defaults and must fail when
   `extra_providers_enabled=false` plus a GitHub/Google provider is configured.
5. **Data durability gate:** backup export, PVC snapshot, and restore-drill jobs run in a local
   cluster; restore proves fixture row counts and a sampled `recall` query.
6. **Audit gate:** admin audit endpoint/UI list login, API-key create/revoke, memory actions, role
   changes, and break-glass sign-ins; member access is denied; CSV and JSON export both work.
7. **Docs gate:** `moon run docs:lint` and `moon run docs:build` pass after W5 lands, with
   user/admin docs linked from the VitePress nav.
8. **Security review packet gate:** publish a short evidence packet with rendered Helm manifests,
   IdP role-claim screenshots or config export, backup drill receipt, audit export sample, image
   SBOM/signature receipt, and the exact package lock diff for Authlib/PyJWT/argon2-cffi.

---

## Phasing

**Phase 1 — OIDC and auth contract (1–2 weeks).** Land W1 in a feature branch. Tests against a real
Entra dev tenant for the prod-shaped flow; mocked providers for the other IdPs. Merges to `main`
when JIT provisioning, session JWTs, API keys, role-claim-gated authorization, silent refresh, and
the bootstrap Job all work end-to-end in local Tilt.

**Phase 2 — Chart enterprise-readiness (1 week).** W2 + W3 (HTTPRoute, NetworkPolicy, PodSecurity,
bootstrap Job, SurrealDB wrapper chart with backup/restore CronJobs). Reviewable as one
chart-focused PR. Validate by deploying into a local Tilt cluster with all the templates enabled.

**Phase 3 — Audit, docs, hardening (1 week).** W4, W5, W6. These can land in parallel with
downstream deployment teams already using earlier phases.

Codex (or equivalent cross-model) review happens before each phase's first PR merges, not just at
plan time.

---

## Risks

1. **Sibyl's Trust And Control Plane workstream (W3 in `SIBYL_1_0_ROADMAP.md`) is still in flight.**
   Any deployment is the first real multi-user instance under load. Things will break. Mitigation:
   maintain a fast Helm rollback path, pre-stage known-good image tags, and document the break-glass
   admin path for OIDC outages.
2. **Single-node SurrealDB means no data-plane HA in the default shape.** A node failure or PVC
   corruption is a real outage. Mitigation: weekly tested restore drill, PVC snapshots independent
   of `surreal export`, PodDisruptionBudget so voluntary disruptions are predictable, monitoring on
   PVC IOPS and free space, explicit promotion criteria for moving to TiKV.
3. **Official SurrealDB Helm chart is dormant.** Last release 2025-09-02, no commits since,
   `appVersion` 8 months behind current OSS. Sibyl pins `image.tag` to stay current but loses the
   safety net of upstream chart updates. Mitigation: pin both chart version and image tag
   explicitly; subscribe to the chart repo for movement; if dormancy persists 6+ months, fork the
   templates Sibyl needs into the Sibyl repo directly.
4. **Prompt injection via stored memories.** Per
   [OWASP LLM Top 10 2025](https://owasp.org/www-project-top-10-for-large-language-model-applications/assets/PDF/OWASP-Top-10-for-LLMs-v2025.pdf),
   indirect prompt injection is the realistic enterprise threat. Mitigation: treat all stored
   content as untrusted, no tool calls from recall context, structured output schemas, sanitization
   of recalled markdown before rendering in agent prompts.
5. **MCP client OAuth UX is inconsistent.** Cursor and Claude Code don't have a uniform OIDC flow.
   Mitigation: API-key-after-web-login is the v1 onboarding shape. Revisit when MCP clients converge
   on auth.
6. **Cross-Organization memory leak via missing scope.** At single-Organization deployments this is
   mostly a non-risk, but multi-Organization deployments will magnify it. Mitigation: code-review
   checklist for `group_id` in every Surreal query; integration test that fails if any query lacks
   namespace scope; SurrealDB namespace-per-org isolation is the structural backstop.
7. **Compromised IdP account → full memory exfil.** Single MFA bypass leaks everything that user
   could see. Mitigation: phishing-resistant MFA for `Sibyl.Admin` / `Sibyl.Owner` enforced at the
   IdP, per-user rate limits on bulk recall, audit alerting on anomalous bulk-recall patterns.

---

## Decision defaults and deferred choices

1. **Secrets injector default:** recommend External Secrets Operator backed by the deployment's
   cloud KMS/secret manager when available. Document Sealed Secrets/SOPS-style
   encrypted-secrets-in-Git as the lower-ops fallback, and manual Secrets for dev/air-gapped
   installs only. The chart stays neutral through `secret.existingSecret`.
2. **Embedding dependency:** keep OpenAI `text-embedding-3-small` as the documented default because
   it matches the current runtime, but make the provider configurable in docs and values.
   First-class self-hosted embeddings are a separate roadmap item, not a blocker for enterprise
   readiness.
3. **Per-IdP docs depth:** ship a full Entra walkthrough and concise Okta/Auth0/Keycloak recipes
   that show exact claim names, scopes, redirect URI shape, and where MFA is enforced. Do not
   duplicate every IdP console click when the vendor docs own that UX.
4. **Multi-Organization deployment:** document v1 as single-Organization per deployment. Keep
   `IdentityProvider` schema room for per-Organization OIDC, but treat multi-org admin UI and
   per-org SSO setup as a separate roadmap item.
5. **CAE-aware revocation:** defer until the deployed IdP's Python relying-party tooling supports it
   cleanly. It is not part of the v1 enterprise readiness gate because short sessions, silent
   refresh, and `token_version` revocation already give bounded exposure.
6. **Password hashing:** migrate local password hashes from PBKDF2-HMAC-SHA256 to Argon2id in a
   separate hardening task with dual-verify and rehash-on-login. W1 migrates API-key hashing to
   Argon2id first because API keys are the automation credential exposed by enterprise CLI/MCP
   onboarding.

---

## Mapping to other IdPs

Quick lookup for deployments using IdPs other than Entra. Each row maps the Entra worked example to
the equivalent in the other IdP.

| Concept                           | Entra ID                                  | Okta                                                                          | Auth0                                                | Keycloak                                                                                                                              | Generic OIDC                        |
| --------------------------------- | ----------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| App registration                  | App registration in tenant                | OIDC application                                                              | Application (Regular Web App)                        | Client in realm                                                                                                                       | OIDC client registration            |
| Stable user ID                    | `oid` (also `sub`)                        | `sub`                                                                         | `sub` (full namespaced form)                         | `sub`                                                                                                                                 | `sub`                               |
| Role claim emission               | App Roles → `roles`                       | Groups → `groups` or custom `roles` mapping                                   | Post-Login Action sets `urn:sibyl:roles`             | Realm roles → `realm_access.roles`, client roles → `resource_access.<client>.roles` (default mappers); or flatten via Protocol Mapper | Configurable claim, default `roles` |
| Role-claim path config            | `oidc.role_claim: "roles"`                | `oidc.role_claim: "groups"` + `scopes: ["openid","profile","email","groups"]` | `oidc.role_claim: "urn:sibyl:roles"`                 | `oidc.role_claim: "resource_access.sibyl.roles"` (nested) or `"roles"` if flattened via mapper                                        | `oidc.role_claim: "<dotted path>"`  |
| MFA enforcement                   | Conditional Access policy                 | Authentication Policy with MFA factor                                         | Multi-factor policy                                  | Required Action `Configure OTP` / browser flow                                                                                        | IdP's own MFA policy                |
| Phishing-resistant MFA for admins | CA authentication strength → FIDO2        | Authenticator policy → WebAuthn                                               | MFA policy → WebAuthn                                | Browser flow → WebAuthn                                                                                                               | IdP-specific                        |
| Silent refresh signal             | `login_required` / `interaction_required` | Same OIDC error                                                               | Same OIDC error                                      | Same OIDC error                                                                                                                       | Standard OIDC behavior              |
| Session length control            | CA Sign-in Frequency                      | Sign-on Policy max session lifetime                                           | Tenant settings → Inactivity / Maximum login session | Realm SSO Session Idle / Max                                                                                                          | IdP-specific                        |
| Group → role mapping              | Assign security group to App Role         | Map group to a `roles` claim value                                            | Auth0 Action assigns role on group membership        | Group → realm role composite                                                                                                          | IdP-specific                        |

---

## Pre-implementation validation evidence

Validated on 2026-05-22 before implementation against the then-current worktree and upstream
sources. For current implementation evidence, see
[`ENTERPRISE_READINESS_VALIDATION_2026-05-22.md`](ENTERPRISE_READINESS_VALIDATION_2026-05-22.md).

- Current chart surface: `charts/sibyl` exists with backend, frontend, worker, classic Ingress, PDB,
  service, secret, configmap, and HPA templates. It does **not** yet include HTTPRoute,
  NetworkPolicy, PodSecurity labels, bootstrap Job, or backup CronJobs, so W2/W3 are real chart
  work, not documentation-only work.
- Current chart security context: backend and worker set `readOnlyRootFilesystem: true`; frontend
  sets it `false`. All three set `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, and drop
  `ALL` capabilities in values. No `seccompProfile` is present yet.
- Current auth surface: `apps/api/src/sibyl/auth/dependencies.py` already accepts JWTs and API keys;
  `mcp_auth.py`, `mcp_oauth.py`, and `oauth_state.py` are bespoke OAuth-shaped code;
  `api_key_common.py` currently hashes API keys with PBKDF2-HMAC-SHA256; local passwords also use
  PBKDF2-HMAC-SHA256.
- Current production config: `.env.example` exposes JWT, GitHub OAuth, public signup, cookie, and
  MCP auth settings, but no generic OIDC provider block, local-auth production gate, or
  extra-provider gate yet.
- External package check: Authlib PyPI shows `1.7.2` released 2026-05-06; Snyk says the May 2026
  advisory is fixed in `1.6.12`, `1.7.1`, or higher; PyJWT PyPI shows `2.13.0` released 2026-05-21.
- External platform check: the SurrealDB Helm index exposes `surrealdb` chart `0.4.0` created
  2025-09-02 with `appVersion: 2.3.7`; the `surrealdb/helm-charts` main commit is dated 2025-09-02;
  SurrealDB stable `3.0.5` is the current 3.0 line while `3.1.0-beta.*` is beta.
- IdP contract check: Microsoft documents App Roles producing a `roles` claim, Okta documents
  `groups` scope for groups in ID tokens, and Keycloak documents default role claims under
  `realm_access` and `resource_access`.

---

## Sources

- Authlib FastAPI client: https://docs.authlib.org/en/v1.6.11/client/fastapi.html
- Authlib RFC 7591 / Dynamic Client Registration:
  https://docs.authlib.org/en/v1.6.11/specs/rfc7591.html
- Authlib PyPI: https://pypi.org/project/Authlib/
- Authlib May 2026 security advisory: https://security.snyk.io/vuln/SNYK-PYTHON-AUTHLIB-16643257
- fastapi-users PyPI: https://pypi.org/project/fastapi-users/
- python-jose PyPI: https://pypi.org/project/python-jose/
- PyJWT JWKS usage: https://pyjwt.readthedocs.io/en/latest/usage.html
- PyJWT PyPI: https://pypi.org/project/PyJWT/
- Microsoft Entra ID token claims reference:
  https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
- Microsoft Entra access token validation:
  https://learn.microsoft.com/entra/identity-platform/access-tokens#validate-tokens
- Microsoft Entra claims validation:
  https://learn.microsoft.com/entra/identity-platform/claims-validation
- Microsoft Entra App Roles and group claims:
  https://learn.microsoft.com/en-us/security/zero-trust/develop/configure-tokens-group-claims-app-roles
- Microsoft Entra App Roles vs groups:
  https://learn.microsoft.com/entra/identity-platform/howto-add-app-roles-in-apps#app-roles-vs-groups
- Microsoft Entra scopes / OIDC permissions:
  https://learn.microsoft.com/en-us/entra/identity-platform/scopes-oidc
- Microsoft Entra Conditional Access target resources:
  https://learn.microsoft.com/entra/identity/conditional-access/concept-conditional-access-cloud-apps
- Microsoft Entra Mandatory MFA:
  https://learn.microsoft.com/entra/identity/authentication/concept-mandatory-multifactor-authentication
- Microsoft Entra Continuous Access Evaluation:
  https://learn.microsoft.com/entra/identity/conditional-access/concept-continuous-access-evaluation
- Microsoft Entra Configurable Token Lifetimes:
  https://learn.microsoft.com/entra/identity-platform/configurable-token-lifetimes
- Microsoft emergency access account guidance:
  https://learn.microsoft.com/en-us/entra/identity/role-based-access-control/security-emergency-access
- SurrealDB CLI `start` command:
  https://surrealdb.com/docs/reference/cli/surrealdb-cli/commands/start
- SurrealDB 3.0 announcement:
  https://surrealdb.com/blog/introducing-surrealdb-3-0--the-future-of-ai-agent-memory
- SurrealDB releases: https://surrealdb.com/releases
- SurrealDB Helm chart:
  https://github.com/surrealdb/helm-charts/blob/main/charts/surrealdb/README.md
- SurrealDB Helm repo index: https://helm.surrealdb.com/index.yaml
- SurrealDB storage engines: https://surrealdb.com/docs/build/embedding/storage-engines
- SurrealDB TiKV setup: https://surrealdb.com/docs/surrealdb/installation/running/tikv
- SurrealDB observability: https://surrealdb.com/docs/surrealdb/reference-guide/observability
- SurrealDB Prometheus feature request: https://github.com/surrealdb/surrealdb/issues/6258
- TiKV RawKV BR: https://tikv.org/docs/dev/concepts/explore-tikv-features/backup-restore/
- External Secrets Operator: https://external-secrets.io/
- Kubernetes Pod Security Standards:
  https://kubernetes.io/docs/concepts/security/pod-security-standards/
- Trivy SBOM attestation: https://trivy.dev/docs/latest/supply-chain/attestation/sbom/
- OWASP LLM Top 10 2025:
  https://owasp.org/www-project-top-10-for-large-language-model-applications/assets/PDF/OWASP-Top-10-for-LLMs-v2025.pdf
- OpenTelemetry log attributes: https://opentelemetry.io/docs/specs/semconv/general/logs/
- OpenTelemetry for LLMs 2026: https://openobserve.ai/blog/opentelemetry-for-llms/
- Google SRE Book monitoring: https://sre.google/sre-book/monitoring-distributed-systems/
- NIST SP 800-92 Log Management: https://csrc.nist.gov/pubs/sp/800/92/final
- SOC 2 logging pipelines 2026: https://www.konfirmity.com/blog/soc-2-logging-pipelines-for-soc-2
