# Moving Sibyl Data Between Instances

Agent playbook for moving memory between current SurrealDB-backed Sibyl instances. There are two
migration lanes:

- **Consolidation** (operator lane): export several instances, merge their archives into one target
  org, and import the result with `sibyld migrate consolidate`.
- **To-team replay** (self-service lane): replay one project's raw captures into a team server
  through its authenticated API with `sibyl migrate to-team`.

Restore accepts only Sibyl's own archives and API backups. Archives from pre-1.0 installs or other
memory systems are not supported.

## When to use this

- The user has several personal instances and wants one canonical org: use consolidation.
- The user wants their local project knowledge in a shared team server they do not operate: use
  to-team replay.

If neither applies, the regular Sibyl skill is the right one.

**Sacred Boundary:** Do not auto-start `moon run dev` at any point. Propose it; let the user run it.

---

## Consolidating Personal Surreal Instances into One Target

Use this when the user has multiple current Sibyl instances and wants to merge their graph/content
into one hosted canonical org.

The safe default is content consolidation only:

- Export each source with auth skipped.
- Merge all archives into the target org ID.
- Run the target import dry run first.
- Import with `--skip-auth` and without `--clean`.
- Configure the local CLI to the hosted URL after the import succeeds.

The one-shot helper is:

```bash
uv run --directory apps/api sibyld migrate consolidate \
  --source local=<local-org-id> \
  --source laptop=<laptop-org-id> \
  --source desktop=<desktop-org-id> \
  --canonical-org-id <target-org-id> \
  --canonical-org-name "<owner>" \
  --canonical-org-slug <owner-slug> \
  --target-host <your-host> \
  --target-sudo \
  --server-url https://sibyl.example.com \
  --context-name <your-context> \
  --setup-cli
```

Run without `--apply` first. That exports, checks, merges, copies the archive to the target, and
runs `sibyld migrate import ... --dry-run` inside the target backend container. Add `--apply` only
after the dry run is clean:

```bash
uv run --directory apps/api sibyld migrate consolidate \
  --source local=<local-org-id> \
  --canonical-org-id <target-org-id> \
  --canonical-org-name "<owner>" \
  --canonical-org-slug <owner-slug> \
  --target-host <your-host> \
  --target-sudo \
  --server-url https://sibyl.example.com \
  --context-name <your-context> \
  --setup-cli \
  --apply
```

When exporting from a local `sibyld up`/`moon run dev` instance, set the same SurrealDB runtime env
that the dev server uses. A standalone `uv run --directory apps/api sibyld ...` with an empty
`SIBYL_SURREAL_URL` defaults to `memory://` and will export an empty graph:

```bash
SIBYL_STORE=surreal \
SIBYL_AUTH_STORE=surreal \
SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc \
SIBYL_SURREAL_USERNAME=root \
SIBYL_SURREAL_PASSWORD=root \
uv run --directory apps/api sibyld migrate consolidate \
  --source local=<local-org-id> \
  --canonical-org-id <target-org-id> \
  --target-host <your-host> \
  --target-sudo \
  --apply
```

Defaults assume the target host is reachable by SSH and runs the self-hosted Docker Compose deploy:

- Compose project directory: `/opt/sibyl`
- Backend service: `backend`
- Backend container: `sibyl-backend`
- Target archive path: `/tmp/sibyl-consolidated.tar.gz`

Override those with `--target-compose-dir`, `--target-service`, `--target-container`, or
`--target-archive-path` when the deploy shape differs. Pass `--target-sudo` when the SSH user needs
`sudo -n docker ...` for Docker access.

After the live import, verify both stores:

```bash
# Graph parity: expected/actual entities, relationships, episodes, mentions.
ssh <your-host> 'sudo -n docker compose --project-directory /opt/sibyl exec -T backend \
  sibyld migrate verify /tmp/sibyl-consolidated.tar.gz --org-id <target-org-id>'

# Content parity: compare row_counts from content.json with an org-scoped content export.
ssh <your-host> 'sudo -n docker compose --project-directory /opt/sibyl exec -T backend \
  python - <<'"'"'PY'"'"'
from sibyl.cli.common import run_async
from sibyl.persistence.content_archive import export_content_archive_payload

@run_async
async def main():
    payload = await export_content_archive_payload("<target-org-id>")
    print(payload["total_rows"])
    print(payload["row_counts"])

main()
PY'
```

`--setup-cli` creates/activates the local context and starts normal browser/device auth. Do not pass
passwords in shell history or process args; run `sibyl auth login https://sibyl.example.com --context <your-context>`
interactively if setup needs to be finished by hand.

For already-collected archives, skip SSH exports and pass them directly:

```bash
uv run --directory apps/api sibyld migrate consolidate \
  --archive ~/sibyl-exports/laptop.tar.gz \
  --archive ~/sibyl-exports/desktop.tar.gz \
  --canonical-org-id <target-org-id> \
  --target-host <your-host>
```

Keep `--skip-auth` semantics. The helper intentionally preserves the target's working users,
sessions, SMTP settings, and API keys. Importing auth from personal machines can duplicate the owner
or clobber a live login surface.

---

## Migrating a Personal Instance into a Team Server (to-team replay)

Use this when a user wants their local project knowledge in a shared team server (an
enterprise deployment with SSO) and they are not an operator of that server. This is the
self-service lane; several teammates can each run it with their own login.

**Do not reach for the archive/consolidate lane here.** It is operator-shaped (SSH or
kubectl to the target, root store access), and it migrates whole orgs rather than one
project. The replay lane goes through the target's ordinary authenticated API instead:
ownership lands on the caller's own server identity by construction, and the target
re-projects and re-embeds from the verbatim raw records.

One-time setup per person:

```bash
sibyl config context create <team> --server https://<team-server> --use
sibyl auth login --server https://<team-server>   # their own SSO login
```

The target project must exist on the team server first (any member with project-create
rights, once for the whole team):

```bash
sibyl project create "<Project Name>" --description "..."
```

Then, from the person's normal local context:

```bash
sibyl migrate to-team --target-context <team> --project project_<source-scope-key> --dry-run
sibyl migrate to-team --target-context <team> --project project_<source-scope-key>
```

What the verb guarantees, and what to check:

- Scope: only raw captures with `memory_scope=project` and the given scope key move.
  Private memories stay local. Find the scope key distribution first when unsure:
  the source content store's `raw_captures` table, grouped by `scope_key`.
- Idempotence: a per-route ledger under `~/.sibyl/migrations` (keyed and
  manifest-pinned to source org + target context + target project) persists after every
  successful post, so interrupts and re-runs never duplicate.
- Provenance: `provenance.migration` on each replayed record carries the origin org,
  raw id, original `created_at`, capture surface, and any truncated original title.
- Failures are per-row and reported at the end; empty and oversize bodies are named
  individually rather than aborting the run.
- Replayed records carry today's `created_at` (the original lives in provenance);
  decay-aware backdating is a known follow-up, not a bug to chase.
- The target needs a working embedding key before the run, or ingestion will queue
  projection failures.
- Server/client version skew is enforced by the version contract: the server stamps
  `X-Sibyl-Server-Version` and may publish a `minimum_client_version` floor that fails
  old clients closed, so run the migration with a CLI at least as new as the target
  server's release.

Verification after a run: a few `sibyl context "<known topic>"` recalls against the
team context that the user can grade themselves, plus the run's own migrated/skipped/
failed counts.
