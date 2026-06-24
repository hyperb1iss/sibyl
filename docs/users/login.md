---
title: Enterprise Sign-In
description: Signing in to Sibyl with a corporate OIDC identity provider
---

# Enterprise Sign-In

This page applies when an operator has enabled enterprise OIDC. The default Sibyl install is
local-first: the first setup signup creates the owner/admin user, and later users join by invitation
unless public signups are enabled.

In enterprise SSO mode, Sibyl signs users in through your organization's OpenID Connect provider.
The production shape is intentionally boring: the identity provider proves who you are, Sibyl reads
a role claim, then Sibyl issues its own short-lived session for the web app.

## What Happens At Login

1. Open the Sibyl web app and choose the organization sign-in provider.
2. The browser redirects to the identity provider, such as Microsoft Entra ID.
3. The provider enforces its policies: MFA, device posture, sign-in frequency, and conditional
   access.
4. The browser returns to Sibyl with an ID token.
5. Sibyl verifies the token signature, issuer, audience, expiry, nonce, and role claim.
6. If the role claim contains a Sibyl role, Sibyl creates or updates your user record and signs you
   in.

Sibyl does not use your email address as the identity key. Email is only a profile field for display
and audit readability.

## Role Claims

Your identity provider must put one of these claim strings in the configured role claim. Each maps
to a Sibyl organization role:

| Claim string   | Maps to Sibyl role | Meaning                                                                       |
| -------------- | ------------------ | ----------------------------------------------------------------------------- |
| `Sibyl.Member` | `member`           | Standard user. Can use memory, projects, CLI, and MCP within assigned scopes. |
| `Sibyl.Admin`  | `admin`            | Organization admin. Manages users, settings, audit exports, and controls.     |
| `Sibyl.Owner`  | `owner`            | Full owner. Use sparingly for break-glass and platform ownership.             |

Lowercase `member`, `admin`, and `owner` claim values are accepted as well. The read-only `viewer`
role is available for local invitations but has no dedicated OIDC claim string.

If the role claim is missing, Sibyl denies the login. If the claim is removed later, the next OIDC
refresh fails and the session must go through full login again.

## Microsoft Entra Example

The reference enterprise setup uses Microsoft Entra ID app roles:

1. Create an App Registration for Sibyl.
2. Add a web redirect URI:

   ```text
   https://sibyl.example.com/api/auth/oidc/entra/callback
   ```

3. Define App Roles named `Sibyl.Member`, `Sibyl.Admin`, and `Sibyl.Owner`.
4. Assign a security group, such as `Sibyl Users`, to `Sibyl.Member`.
5. Assign admin roles only to people who should administer Sibyl.
6. Require phishing-resistant MFA or your organization's standard MFA policy for the Sibyl app
   through Conditional Access.

For Entra, Sibyl binds identity with the tenant ID plus object ID (`tid` and `oid`). That survives
email changes and avoids pairwise-subject surprises across applications.

## Other Providers

| Provider           | Stable identity | Common role claim shape                                  |
| ------------------ | --------------- | -------------------------------------------------------- |
| Microsoft Entra ID | `tid` + `oid`   | App Roles in the `roles` claim                           |
| Okta               | `iss` + `sub`   | Groups or custom claim mapped to `roles`                 |
| Auth0              | `iss` + `sub`   | Namespaced custom claim such as `urn:sibyl:roles`        |
| Keycloak           | `iss` + `sub`   | `realm_access.roles` or `resource_access.<client>.roles` |
| Generic OIDC       | `iss` + `sub`   | Configured claim path, default `roles`                   |

Sibyl supports dotted claim paths. For example, a Keycloak deployment can use
`resource_access.sibyl.roles` without flattening the claim.

## Session Renewal

OIDC web sessions do not get a long-lived Sibyl refresh cookie. Instead, the web app renews with a
best-effort OIDC `prompt=none` request:

- If the provider returns a fresh ID token with a Sibyl role, Sibyl issues a new access cookie.
- If the provider returns `login_required`, `interaction_required`, `consent_required`, or
  `account_selection_required`, the browser returns to full sign-in.
- If the browser blocks the silent flow, full sign-in is the fallback.

That keeps deprovisioning real: removing the role in the identity provider stops new Sibyl sessions
without depending on a local refresh token.

## Forgot Your Password

This applies to local username/password installs, not OIDC sign-in. From the web login screen,
choose **Forgot password?**, enter your email, and submit. When Sibyl has an email provider
configured (SMTP or Resend), it sends a reset link; follow it to set a new password and sign in. The
response is intentionally the same whether or not an account exists, so it never reveals which
emails are registered.

If no reset email arrives, the instance may not have email configured yet; ask your admin to set up
[transactional email](../admin/installing.md#transactional-email). OIDC users do not have a Sibyl
password and reset credentials at their identity provider instead.

## Troubleshooting

| Symptom                             | What to check                                                                      |
| ----------------------------------- | ---------------------------------------------------------------------------------- |
| The provider button is missing      | Ask an admin to confirm `oidc.providers` is configured.                            |
| Login returns to the sign-in page   | The provider may have returned a soft OIDC error. Try full sign-in.                |
| "Missing role" or access denied     | Confirm your IdP assignment emits `Sibyl.Member`, `Sibyl.Admin`, or `Sibyl.Owner`. |
| You changed email and still sign in | Expected. Sibyl identity is the provider subject, not email.                       |
