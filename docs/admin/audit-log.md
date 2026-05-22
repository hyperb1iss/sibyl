---
title: Audit Log
description: Reading and exporting Sibyl audit events
---

# Audit Log

The audit log is the admin view of security-relevant activity in Sibyl. It is restricted to
`Sibyl.Admin` and `Sibyl.Owner`.

## Open The Audit Log

In the web UI, open Settings, Admin, Audit Log. The table supports filtering by:

- User.
- Action.
- Resource type and resource ID.
- Time range.

The API surface is `/api/admin/audit` and supports paginated JSON responses. Exports are available
as JSON and CSV for incident review or SIEM ingestion.

## Events To Expect

The audit surface records security and data-governance events such as:

| Event family   | Examples                                                                |
| -------------- | ----------------------------------------------------------------------- |
| Authentication | OIDC login, silent refresh, local login, logout, break-glass sign-in    |
| API keys       | API key create, revoke, and scoped access decisions                     |
| Memory         | Memory create, recall/context receipts, reflection, promotion, deletion |
| Access control | Organization role changes, invitations, session revocation              |
| Operations     | Backup actions, restore drills, settings updates                        |

Event details should be useful for investigation without exposing secrets. Exported rows should be
treated as sensitive operational data.

## Retention

Set retention in the deployment overlay to match your organization's policy. The default operational
expectation is to keep enough history to investigate account compromise, data deletion, and backup
events. Forward exports or logs to your SIEM or log warehouse if centralized retention is required.

## Incident Review

For a suspected account issue:

1. Filter by user and the suspected time window.
2. Check login events and IP/user-agent metadata.
3. Review API key creation and revoke events.
4. Review memory and project actions after the suspicious login.
5. Export JSON for a lossless record, then CSV for spreadsheet review if needed.

For denied member access to the admin audit surface, expect a forbidden response. That denial is
intentional and should be covered by admin-access tests.
