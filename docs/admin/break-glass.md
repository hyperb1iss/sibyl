---
title: Break-Glass Access
description: Emergency owner access when OIDC is unavailable
---

# Break-Glass Access

Break-glass access is the emergency path for OIDC outages, IdP misconfiguration, or locked-out admin
roles. It should exist, it should be tested, and it should be boring.

## Account Shape

Use a local owner account stored in a dedicated secret:

```yaml
breakGlass:
  enabled: true
  allowedIPs:
    - 203.0.113.0/24
  expiresAt: "<UTC timestamp no more than four hours out>"
  existingSecret: sibyl-break-glass
  ownerEmailKey: owner-email
  ownerPasswordKey: owner-password
```

Keep production `SIBYL_LOCAL_AUTH_ENABLED=false` for normal operation. Temporarily enable local auth
only for a documented break-glass window. `breakGlass.enabled=true` sets
`SIBYL_BREAK_GLASS_ENABLED=true`, and Sibyl denies break-glass login after `breakGlass.expiresAt` or
from a source address outside `breakGlass.allowedIPs`.

When break-glass is enabled, `expiresAt` and at least one `allowedIPs` CIDR are required. Sibyl also
denies login if the expiry is more than four hours out, so the emergency window stays bounded even
if the chart override is left in place.

Every break-glass sign-in must include an incident reason. The web login form, device approval page,
and `sibyl auth login --break-glass-reason` all send that reason into the audit event.

The CIDR allowlist is an app-level backstop. Keep the same restriction at ingress or firewall level
when possible, especially if the app only sees proxy addresses.

## Storage

Store the credentials in your organization's emergency secret system, not in Git, chat, or a normal
password note. Require at least two authorized people for retrieval when your process supports it.

Rotate the credentials after every use and after any staff change that affects the break-glass
roster.

## Runbook

1. Declare the break-glass event in the incident channel.
2. Restrict access at ingress or firewall level if possible.
3. Enable the break-glass values with CIDRs and an expiry no more than four hours out.
4. Sign in with the break-glass owner and enter the incident or change record as the reason.
5. Fix the IdP, OIDC secret, role assignment, or admin membership issue.
6. Confirm normal OIDC admin login works.
7. Disable the break-glass override.
8. Rotate the break-glass password.
9. Export the relevant audit log window and attach it to the incident record.

## Audit Expectations

Break-glass sign-ins are recorded as `auth.break_glass.login` audit events. The details include the
actor name, email, incident reason, break-glass start timestamp, and configured expiry. Treat every
use as an incident, even when it is planned maintenance. The evidence packet should include who
approved the access, when it started, when it ended, and what changed.
