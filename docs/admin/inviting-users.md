---
title: Inviting Users
description: JIT provisioning, admin role changes, and deprovisioning
---

# Inviting Users

Enterprise Sibyl uses just-in-time provisioning. Users do not need pre-created Sibyl accounts when
the identity provider sends a valid stable subject and a Sibyl role claim.

## JIT Provisioning

On first OIDC login, Sibyl:

1. Verifies the provider ID token.
2. Reads the stable subject key.
3. Reads the configured role claim.
4. Creates a User record if the subject has not been seen before.
5. Creates or updates the Organization membership from the role claim.
6. Records an audit event for the login.

Email is not used to find the user. If an email address collides with another user, Sibyl keeps
identity binding on the provider subject and only stores safe profile data.

## Giving Access

Grant access in the identity provider:

- Assign `Sibyl.Member` for ordinary users.
- Assign `Sibyl.Admin` for people who manage settings, audit, users, and API keys.
- Assign `Sibyl.Owner` only to platform owners and break-glass accounts.

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

Production values should set `SIBYL_LOCAL_AUTH_ENABLED=false`. Local username/password accounts
remain for development and break-glass workflows, but they should not be the normal enterprise path.
