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
instance so the server can reject a changed destination. Cached identity is used to record who owns
an offline draft, never as sufficient proof to send it.

A write buffered while the server is unreachable still records an owner, taken from the identity the
stored login already proved for that server and credential scope. No request is made to read it.
Signing in fresh clears that cache and asks the server for the identity once while the connection is
up, so the first offline write after a login is not stranded. A write buffered with no login at all
for its destination records why nobody owns it, and it is never reported as retrying.

Servers predating this contract support replay only under the original credential lineage. Upgrade
the server to recover writes across new logins.

## Inspect the queue

```bash
sibyl pending-writes list
sibyl pending-writes list --json
```

The list shows the target, operation, class, attempts, and the failure that parked the entry. A
rejection also shows the HTTP status, the error code, and the server's own message, bounded to 200
characters. Request bodies are never printed, and a message is kept only for a rejection or a
conflict, where the server is describing the request rather than itself.

Each entry falls in exactly one class, and only the first moves on its own:

- **retrying:** this login owns it and the destination is the current server. Replay is attempted
  after a successful API request, with backoff between attempts.
- **needs_attention:** the server rejected it or left a conflict unresolved. Automatic replay skips
  it until the cause is fixed and `retry` re-enables it.
- **unowned:** no credential this login can prove owns it. A rotated login, an older CLI, or a write
  buffered with nobody signed in all land here. Use `adopt` or `discard`.
- **foreign_server:** the write targets another server URL, so no command against this one will ever
  send it. Select that server's context, or discard it.
- **read_like:** a search buffered by an older CLI that treated it as a mutation. Re-run the command
  instead.
- **corrupt:** the file must be repaired or explicitly discarded before it can be used.

Commands report the queue at completion, and only when a person has to act. Nothing prints while
every buffered write is young and owned. Writes that are failing or past a short grace period get
one line. Everything parked gets one line naming the count per class.

Within a verified owner or original credential lineage, an unresolved write holds later operations
on the same entity. Independent writes can continue. Bulk operations form an ordering barrier
because they can touch multiple entities. Explicitly selecting a later write does not bypass its
unresolved predecessor. Outside task, entity, and project routes, ordering follows the first two URL
path segments; different action paths do not share an ordering lane.

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
sibyl -C <context> pending-writes adopt <write-id>...
```

`claim` is the same command under its original name. Before it does anything, it prints one line
naming how many writes it will replay, to which server, as which user and organization, and which
requests they are.

For entries tied to an older login that is no longer available:

```bash
sibyl -C <context> pending-writes adopt --unverified <write-id>...
```

The command shows the authenticated user and organization plus each selected operation and server.
Confirm only when those entries belong to that account and organization. The CLI cannot prove
historical ownership that was never recorded. Explicit IDs are required with `--unverified`; `--yes`
supplies confirmation for operator-controlled automation.

Adoption never transfers an entry that already has a verified owner. It preserves the old credential
scope as recovery provenance and retries eligible claimed writes. Another server URL must be handled
through its own context.

## Discard unwanted entries

```bash
sibyl pending-writes discard <write-id>...
sibyl pending-writes discard --read-like
sibyl pending-writes discard --foreign
sibyl pending-writes discard --rejected
```

Offline drafts retain their last verified owner. Replacing the database does not transfer those
drafts to the new instance. Keep their payloads for inspection before deciding how to recover them.

Discard permanently removes the named local copies without replay. Export or copy any payload you
need before discarding it. Nothing is ever discarded on the CLI's own initiative.

Each selector is explicit, and only one can be used per run. `--read-like` removes requests buffered
by older CLI versions that incorrectly treated some searches as mutations. `--foreign` removes
writes whose destination is not the current server. `--rejected` removes writes the server refused
with a 4xx that the payload cannot pass, which a replay of the same request can never fix.
