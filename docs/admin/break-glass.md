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
  existingSecret: sibyl-break-glass
  ownerEmailKey: owner-email
  ownerPasswordKey: owner-password
```

Keep production `SIBYL_LOCAL_AUTH_ENABLED=false` for normal operation. Temporarily enable local auth
only for a documented break-glass window, or restrict it with network and ingress controls in your
deployment overlay.

## Storage

Store the credentials in your organization's emergency secret system, not in Git, chat, or a normal
password note. Require at least two authorized people for retrieval when your process supports it.

Rotate the credentials after every use and after any staff change that affects the break-glass
roster.

## Runbook

1. Declare the break-glass event in the incident channel.
2. Restrict access at ingress or firewall level if possible.
3. Enable the break-glass values or local-auth override.
4. Sign in with the break-glass owner.
5. Fix the IdP, OIDC secret, role assignment, or admin membership issue.
6. Confirm normal OIDC admin login works.
7. Disable the break-glass override.
8. Rotate the break-glass password.
9. Export the relevant audit log window and attach it to the incident record.

## Audit Expectations

Break-glass sign-ins should be visible in the audit log. Treat every use as an incident, even when
it is planned maintenance. The evidence packet should include who approved the access, when it
started, when it ended, and what changed.
