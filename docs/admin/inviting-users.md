---
title: Inviting Users
description: Local invitations, OIDC JIT provisioning, admin role changes, and deprovisioning
---

# Inviting Users

How users get into Sibyl depends on the install shape. The default local install uses admin-issued
invitations. Enterprise SSO installs use just-in-time provisioning driven by the identity provider's
role claim.

## Inviting Users (Local / Default Install)

On a default local install, the first setup signup creates the owner, and everyone else joins by
invitation (unless `SIBYL_PUBLIC_SIGNUPS_ENABLED=true`). An owner or admin creates the invitation,
and the invitee accepts through an emailed link.

To invite someone:

- **Web UI:** open Settings, organization members, and add the person's email with a role.
- **API:** `POST /api/orgs/{slug}/invitations` with a JSON body of `email`, `role`, and optional
  `expires_days` (defaults to 7, max 30).

```bash
curl -X POST https://sibyl.example.com/api/orgs/acme/invitations \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"email": "newuser@example.com", "role": "member", "expires_days": 7}'
```

`role` is one of the Sibyl roles `owner`, `admin`, `member`, or `viewer` (read-only).

The invitee receives an email with a link and accepts it, which provisions their account:

```text
POST /api/invitations/{token}/accept
```

Invitation and password-reset emails require a configured email provider (Resend or SMTP). Without
one, Sibyl writes the message to its JSONL outbox and skips delivery, so the invitee never gets the
link. See [Installing Sibyl](installing.md#transactional-email).

## JIT Provisioning (Enterprise SSO)

Enterprise Sibyl uses just-in-time provisioning. Each OIDC provider is bound to one exact
non-personal organization by `organization_slug`. Users do not need pre-created Sibyl accounts when
the identity provider sends a valid stable subject and a Sibyl role claim.

On first OIDC login, Sibyl:

1. Verifies the provider ID token.
2. Reads the stable subject key.
3. Reads the configured role claim.
4. Creates a User record if the subject has not been seen before.
5. Creates or updates membership in the provider-bound organization from the role claim.
6. Records an audit event for the login.

Email is not used to find the user. If an email address collides with another user, Sibyl keeps
identity binding on the provider subject and only stores safe profile data.

Sibyl never chooses the oldest or first organization membership and never changes the user's other
organization memberships during OIDC login.

The role claim is authoritative for the bound organization. Unlike in-app role management, an OIDC
login carrying a lower role can demote the organization's last owner; grant the owner role in the
identity provider and ownership is restored on that user's next login.

## Giving Access

Grant access in the identity provider:

- Assign `Sibyl.Member` for ordinary users.
- Assign `Sibyl.Admin` for people who manage settings, audit, users, and API keys.
- Assign `Sibyl.Owner` only to platform owners and break-glass accounts.

`Sibyl.Member`, `Sibyl.Admin`, and `Sibyl.Owner` are IdP claim **strings**, not Sibyl roles. Sibyl
maps them onto its own organization roles: `Sibyl.Owner` to `owner`, `Sibyl.Admin` to `admin`, and
`Sibyl.Member` to `member` (lowercase `owner`, `admin`, and `member` claim values are accepted too).
The read-only `viewer` role exists for local invitations; it has no dedicated OIDC claim string.

The next successful login creates the user. For existing users, the next login or silent refresh
updates the organization membership role.

## Bumping Someone To Admin

1. Add or change the user's assignment in the identity provider to `Sibyl.Admin`.
2. Ask the user to sign out and sign back in, or wait for the next OIDC refresh.
3. Confirm the user can access Settings and audit surfaces.
4. Review the audit log for the role-change/login event.

For urgent changes, revoke the user's active sessions after updating the IdP so the next request has
to return through the provider.

## Deprovisioning

Remove the Sibyl role assignment in the identity provider. On the next OIDC refresh, Sibyl denies
the session because the role claim is gone. For immediate lockout:

1. Remove the role or disable the account in the IdP.
2. Revoke active Sibyl sessions from the admin security surface.
3. Revoke API keys owned by that user.
4. Review the audit log for any follow-up access attempts.

Do not rely on email-domain blocking for deprovisioning. The role claim is the authorization
boundary.

## Local Accounts

Local username/password accounts are the default single-user and small-team path, provisioned by the
[invitation flow above](#inviting-users-local--default-install), and they back the break-glass path.
Enterprise SSO values should set `SIBYL_LOCAL_AUTH_ENABLED=false` once a corporate OIDC owner can
sign in, so local login is not the normal enterprise path.
