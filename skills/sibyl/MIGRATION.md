# Migrating a Local Sibyl Install to SurrealDB

Agent playbook for moving an existing FalkorDB + PostgreSQL Sibyl install onto SurrealDB. Complements the user-facing `docs/guide/migrating-from-falkor.md` with the operational reality — which version to anchor on, which gotchas you'll hit, and which "failures" are actually false positives.

## When to use this

- `moon run dev` aborts with `⚠️  Local legacy data detected` (the `run-surreal-dev.sh` legacy guard fires).
- The user has data in podman volumes named `sibyl_falkordb*` / `sibyl_postgres*` but `.moon/cache/surreal-dev` is empty.
- The Sibyl server is unreachable and the CLI is buffering writes to `~/.config/sibyl/pending_writes/`.

If none of these apply, the regular Sibyl skill is the right one.

**Sacred Boundary:** Do not auto-start `moon run dev` at any point. Propose it; let the user run it.

---

## The anchor: commit `290b824b`

`docs/guide/migrating-from-falkor.md` references "the v0.6 compatibility release" for the export step. That release was version-bumped in commit `290b824b` (`🔖 v0.6.0`, 2026-05-10) but **never git-tagged** — `git tag` stops at `v0.4.1`. The FalkorDB client was removed the next morning in `efbd8de8` ("remove falkor client path"), so `290b824b` is the last commit that can read a FalkorDB `dump.rdb`.

Use a worktree at `290b824b` for the export side; the current branch for the import. The two halves use different `migrate` CLI shapes — that's intentional.

---

## Phase 1 — Snapshot the legacy volumes

Cheap insurance and the migration guide insists on it.

```bash
BACKUP=~/sibyl-legacy-backup
mkdir -p "$BACKUP"
for v in sibyl_falkordb sibyl_falkordb_data sibyl_postgres sibyl_postgres_data; do
  podman volume export "$v" > "$BACKUP/$v.tar"
done
ls -lh "$BACKUP"
```

If the user has a different volume layout, adapt the names but keep the principle: snapshot first, then touch anything.

---

## Phase 2 — Identify which volumes hold real data

The `_data`-suffixed volumes are typically the **empty** post-Apr-5 compose-rename targets. The real data lives in:

- `sibyl_falkordb` — FalkorDB `dump.rdb` (single-digit MB+).
- `sibyl_postgres` — Postgres 18 PGDATA (tens of MB+).

Inspect quickly:

```bash
for v in sibyl_falkordb sibyl_falkordb_data sibyl_postgres sibyl_postgres_data; do
  echo "--- $v ---"
  podman run --rm -v "$v":/v:ro alpine sh -c 'du -sh /v; ls /v'
done
```

If the layout is inverted on this install, swap the volume names in Phase 3.

---

## Phase 3 — Stand up the legacy stack

Throwaway containers named `sibyl-mig-*` so they don't collide with anything `moon run dev` will create.

```bash
podman run -d --name sibyl-mig-falkordb \
  -v sibyl_falkordb:/var/lib/falkordb/data \
  -p 16379:6379 \
  -e 'FALKORDB_ARGS=--requirepass sibyl_dev' \
  docker.io/falkordb/falkordb:latest

podman run -d --name sibyl-mig-postgres \
  -v sibyl_postgres:/var/lib/postgresql \
  -p 15432:5432 \
  -e POSTGRES_USER=sibyl -e POSTGRES_PASSWORD=sibyl_dev -e POSTGRES_DB=sibyl \
  -e PGDATA=/var/lib/postgresql/18/docker \
  docker.io/pgvector/pgvector:pg18
```

The PGDATA subpath may be `/18/docker` or `/18/data` — verify:

```bash
podman run --rm -v sibyl_postgres:/v:ro alpine find /v -maxdepth 4 -name PG_VERSION
```

Wait ~8 seconds for Postgres recovery. Check FalkorDB loaded the graph (`GRAPH.LIST` lists org UUIDs):

```bash
podman exec sibyl-mig-falkordb redis-cli -a sibyl_dev --no-auth-warning GRAPH.LIST
```

### Gotcha: `POSTGRES_PASSWORD` is init-only

The env var only applies on first init of an empty PGDATA. Against an existing cluster, the original password is preserved. If TCP auth fails later with `asyncpg.exceptions.InvalidPasswordError`, reset over the container's Unix socket (peer/trust auth — passwordless):

```bash
podman exec sibyl-mig-postgres psql -U sibyl -d sibyl -c "ALTER USER sibyl PASSWORD 'sibyl_dev';"
```

Verify TCP works:

```bash
podman exec sibyl-mig-postgres psql "postgresql://sibyl:sibyl_dev@127.0.0.1:5432/sibyl" -tAc "SELECT 1;"
```

---

## Phase 4 — Build the v0.6.0 worktree

```bash
cd <user's sibyl checkout>     # e.g. ~/dev/sibyl
mkdir -p ~/.sibyl-worktrees
git worktree add --detach ~/.sibyl-worktrees/v0.6.0-export 290b824b
cd ~/.sibyl-worktrees/v0.6.0-export
uv sync
```

Expect ~160 packages including `falkordb`, `graphiti-core`, `asyncpg`, `surrealdb`, `alembic`. Takes 1-3 minutes the first time.

---

## Phase 5 — Upgrade the Postgres schema

The legacy Postgres is typically a few migrations behind v0.6.0's head. Run alembic from the worktree's `apps/api`:

```bash
cd ~/.sibyl-worktrees/v0.6.0-export/apps/api
export SIBYL_STORE=legacy SIBYL_AUTH_STORE=postgres
export SIBYL_POSTGRES_HOST=localhost SIBYL_POSTGRES_PORT=15432
export SIBYL_POSTGRES_USER=sibyl SIBYL_POSTGRES_PASSWORD=sibyl_dev SIBYL_POSTGRES_DB=sibyl

uv run alembic current   # show current revision
uv run alembic heads     # show target revision
uv run alembic upgrade head
```

v0.6.0's head is `0017_drop_agent_runner_tables`. The typical path is `0013 → 0017`: adds `raw_captures`, `brainstorm_*`, `planning_sessions`, then drops the agent-runner scratch tables. No data-bearing tables are touched.

---

## Phase 6 — Export

Get the org UUID — Sibyl FalkorDB graph names are the org UUID:

```bash
podman exec sibyl-mig-falkordb redis-cli -a sibyl_dev --no-auth-warning GRAPH.LIST
# returns: <org-uuid>  (plus a 'default' graph, usually empty)
```

Run the export from the worktree:

```bash
cd ~/.sibyl-worktrees/v0.6.0-export
export SIBYL_STORE=legacy SIBYL_AUTH_STORE=postgres
export SIBYL_FALKORDB_HOST=localhost SIBYL_FALKORDB_PORT=16379 SIBYL_FALKORDB_PASSWORD=sibyl_dev
export SIBYL_POSTGRES_HOST=localhost SIBYL_POSTGRES_PORT=15432
export SIBYL_POSTGRES_USER=sibyl SIBYL_POSTGRES_PASSWORD=sibyl_dev SIBYL_POSTGRES_DB=sibyl

uv run --directory apps/api sibyld migrate export \
  --no-include-database-dump \
  --org-id <uuid> \
  --output /tmp/sibyl-migration.tar.gz
```

`--no-include-database-dump` skips the `pg_dump` sidecar — avoids host-binary version-match dependency, and the volume snapshots already cover rollback. The flag is honored by `resolve_backup_runtime_options`.

Inspect the archive (validates checksums + prints counts):

```bash
uv run --directory apps/api sibyld migrate check /tmp/sibyl-migration.tar.gz
```

Cross-check the graph counts against the raw FalkorDB baseline — `Entity`-label node count should equal the archive's `entity_count`; `Episodic`-label count should equal `episode_count`.

---

## Phase 7 — Bring up SurrealDB

Use the same compose service `moon run dev` would use, so the imported data lands where dev expects it.

```bash
cd <user's sibyl checkout>
export SURREAL_DATA_DIR="$PWD/.moon/cache/surreal-dev"
podman compose up -d --force-recreate surrealdb
```

### Gotcha: `:U` bind-mount flag dropped under the docker-compose plugin

`docker-compose.yml`'s `surrealdb` service uses `:U` on its bind mount to auto-chown the host dir to the container UID. When `podman compose` routes through the `docker-compose` plugin (Ubuntu default), `:U` is **silently dropped**. The SurrealDB image runs as non-root uid 65532; the bliss-owned bind mount at mode 0775 gives "other" only `r-x` → `Failed to create RocksDB directory: PermissionDenied` → container exits(1).

Fix once, survives across restarts:

```bash
chmod 0777 .moon/cache/surreal-dev
podman start sibyl-surrealdb
```

Verify:

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:8000/health   # expect 200
ls -la .moon/cache/surreal-dev/sibyl.db/                                     # expect CURRENT, IDENTITY, *.log
```

---

## Phase 8 — Import (current-branch CLI)

The current-branch importer requires `--source-type` and `--target-mode`. The v0.6.0 importer doesn't — but the current schema is what `moon run dev` will expect, so use the current branch:

```bash
cd <user's sibyl checkout>
export SIBYL_STORE=surreal SIBYL_AUTH_STORE=surreal
export SIBYL_SURREAL_URL=ws://127.0.0.1:8000/rpc
export SIBYL_SURREAL_USERNAME=root SIBYL_SURREAL_PASSWORD=root
export SIBYL_LOG_LEVEL=WARNING        # suppresses the surreal_query_complete debug spam

# 1. Dry-run rehearsal (no writes)
uv run --directory apps/api sibyld migrate import /tmp/sibyl-migration.tar.gz \
  --source-type legacy-archive --target-mode surreal --dry-run

# 2. Real import
uv run --directory apps/api sibyld migrate import /tmp/sibyl-migration.tar.gz \
  --source-type legacy-archive --target-mode surreal --yes --clean
```

Expect 3 SurrealDB namespaces afterwards:

- `sibyl_auth/auth` — users, organizations, sessions, audit_logs, projects, etc.
- `sibyl_content/content` — crawl_sources, crawled_documents, document_chunks.
- `org_<uuid_hex>/graph` — entity, episode, mentions, community, has_episode, has_member, next_episode tables.

The import reports `Auth restored: N rows across K tables`, `Content restored: N rows across K tables`, `Graph restored: N entities, N relationships`. Cross-check those against the archive's manifest counts (`migrate check` output).

---

## Phase 9 — Verify (carefully)

**Do not trust `sibyld migrate verify` alone on legacy archives.** It reports false-positive `missing imported episode: <legacy_id>` errors because the importer rekeys episodes to native Surreal record IDs (`episode:<random>`) while preserving the legacy ID in the `uuid` field — and the verifier looks them up by record id. The aggregate counts in its output ARE accurate; the per-episode spot-check is broken.

Verify directly against SurrealDB:

```bash
ORG_NS=org_$(printf '%s' '<uuid>' | tr -d -)   # strip dashes from uuid

Q() {
  curl -s -X POST http://localhost:8000/sql -u root:root \
    -H "surreal-ns: $1" -H "surreal-db: $2" -H 'Accept: application/json' -d "$3"
}

# Graph
Q "$ORG_NS"     graph   'SELECT count() FROM entity GROUP ALL;'
Q "$ORG_NS"     graph   'SELECT count() FROM episode GROUP ALL;'
Q "$ORG_NS"     graph   'SELECT count() FROM mentions GROUP ALL;'

# Auth
Q sibyl_auth    auth    'SELECT email, name FROM users;'
Q sibyl_auth    auth    'SELECT count() FROM user_sessions GROUP ALL;'

# Content
Q sibyl_content content 'SELECT count() FROM crawled_documents GROUP ALL;'
Q sibyl_content content 'SELECT count() FROM document_chunks GROUP ALL;'
```

If `migrate verify` reports specific "missing" episode IDs, prove they exist by `uuid` field:

```bash
Q "$ORG_NS" graph "SELECT id, uuid FROM episode WHERE uuid IN ['<legacy_id>'];"
```

When the aggregate counts match the baseline **and** the legacy IDs are findable by `uuid`, the migration is sound regardless of what `migrate verify`'s exit code says.

---

## Phase 10 — Unblock dev

`run-surreal-dev.sh`'s `surreal_runtime_data_detected()` checks for `.moon/cache/surreal-dev/sibyl.db/CURRENT` or `IDENTITY`. After a successful import both files exist, so the legacy guard passes on the next `moon run dev`.

**Tell the user `moon run dev` will now start cleanly. Do not run it yourself.**

The CLI's buffered writes in `~/.config/sibyl/pending_writes/` flush on next CLI activity once the API is back up.

---

## Cleanup

Safe to remove immediately:

- `sibyl-mig-falkordb`, `sibyl-mig-postgres` containers (`podman rm -f`).
- The v0.6.0 worktree (`git worktree remove --force ~/.sibyl-worktrees/v0.6.0-export` from the main checkout).
- Any `0001-fix-…FalkorDB-volume…patch` file in the working dir if it predates the FalkorDB removal commit (`efbd8de8`).

Keep for at least a few days as rollback:

- `~/sibyl-legacy-backup/*.tar` — volume snapshots.
- `/tmp/sibyl-migration.tar.gz` — the archive.
- The four legacy podman volumes (`sibyl_falkordb`, `sibyl_falkordb_data`, `sibyl_postgres`, `sibyl_postgres_data`).

Propose volume removal only after the user has run `moon run dev` and confirmed the new SurrealDB feels right.

---

## Reference

- Canonical user-facing playbook: `docs/guide/migrating-from-falkor.md`
- Release notes: `docs/guide/surrealdb-migration-release-notes.md`
- Legacy guard implementation: `tools/dev/run-surreal-dev.sh` (`warn_if_legacy_setup_detected`, `surreal_runtime_data_detected`, `docker_legacy_setup_detected`)
- Export command source: `apps/api/src/sibyl/cli/migrate.py` (at commit `290b824b`)
- Import command source: `apps/api/src/sibyl/cli/migrate.py` (current branch — has `--source-type` / `--target-mode`)
