# pending-writes

Inspect and recover writes whose server outcome is not confirmed. The CLI keeps the request and its
original idempotency key in a local directory restricted to your user. A timeout can happen after
the server applies a write, so a buffered entry does not prove that the server saved nothing.

## Ownership and reauthentication

New writes record a server-confirmed identity: the database instance, user, organization, and
credential restrictions. Signing in again as the same user in the same organization recovers that
identity. Replacing a database at the same URL, switching accounts, or selecting a different
organization does not authorize replay. API keys retain their own identity and restrictions.

The CLI verifies the current identity before replay. Mutations also carry the expected database
instance so the server can reject a changed destination. Cached identity is used to preserve an
offline draft, never as sufficient proof to send it. An offline draft created before any identity
was verified remains under legacy ownership until its owner can be established.

Servers predating this contract support replay only under the original credential lineage. Upgrade
the server to recover writes across new logins.

## Inspect the queue

```bash
sibyl pending-writes list
sibyl pending-writes list --json
```

The list shows the target, operation, ownership status, attempts, and last failure category and HTTP
status. Request bodies and response bodies are not printed. Older entries have no recorded failure
reason; the CLI cannot reconstruct one.

- **Pending:** authentication, transport, temporary server failures, or an unresolved predecessor
  prevent confirmation. Matching retryable writes are attempted after a successful API request.
- **Attention:** a rejection or unresolved conflict requires inspection. Automatic replay skips it.
- **Legacy ownership:** the entry predates verified identities. The original credential can still
  prove its lineage, but a new login cannot infer its historical owner.
- **Corrupt:** the file must be repaired or explicitly discarded before it can be used.

Within a verified owner or original credential lineage, an unresolved write holds later operations
on the same entity. Independent writes can continue. Bulk operations form an ordering barrier
because they can touch multiple entities. Explicitly selecting a later write does not bypass its
unresolved predecessor.

## Flush retryable writes

```bash
sibyl pending-writes flush
sibyl pending-writes flush <write-id>...
```

Flush verifies ownership and reuses the original idempotency key. The server can return its stored
receipt when an earlier attempt already completed. Only a confirmed successful response removes the
local entry. Rejected and uncertain outcomes preserve the payload for inspection.

After resolving an attention entry's cause, explicitly re-enable it:

```bash
sibyl pending-writes retry <write-id>...
```

Retry preserves the original payload, key, and failure evidence. Ownership and ordering checks still
apply. A permanent rejection stays in attention if the server rejects it again.

## Recover legacy entries

For old entries with no credential owner, select the original server context and run:

```bash
sibyl -C <context> pending-writes claim <write-id>...
```

For entries tied to an older login that is no longer available:

```bash
sibyl -C <context> pending-writes claim --unverified <write-id>...
```

The command shows the authenticated user and organization plus each selected operation and server.
Confirm only when those entries belong to that account and organization. The CLI cannot prove
historical ownership that was never recorded. Explicit IDs are required with `--unverified`; `--yes`
supplies confirmation for operator-controlled automation.

Claim never transfers an entry that already has a verified owner. It preserves the old credential
scope as recovery provenance and retries eligible claimed writes. Another server URL must be handled
through its own context.

## Discard unwanted entries

```bash
sibyl pending-writes discard <write-id>...
sibyl pending-writes discard --read-like
```

Discard permanently removes the named local copies without replay. Export or copy any payload you
need before discarding it. The `--read-like` option removes requests buffered by older CLI versions
that incorrectly treated some searches as mutations.
