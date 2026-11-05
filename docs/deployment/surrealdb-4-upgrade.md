---
title: SurrealDB 4.0 Upgrade
description: Move a store a SurrealDB 3.x server wrote to SurrealDB 4.0, once and for good
---

# SurrealDB 4.0 Upgrade

Sibyl supports one SurrealDB server release, `surrealdb/surrealdb:v4.0.0`. A fresh install starts on
it and needs nothing from this page. A store a 3.x server wrote has to be converted once before 4.0
will open it, and that conversion is one-way. This page is the runbook for that move on every
surface Sibyl ships: the compose files, the Ansible role, the Helm chart, and the CLI runtimes.

## What changes

- **No RocksDB.** 4.0 stores data with SurrealKV, so every start path names `surrealkv://`. The old
  `SURREAL_ROCKSDB_*` tuning settings are ignored.
- **4.0 refuses a 3.x store by default.** The server exits with "holds a RocksDB database written by
  SurrealDB 3.x", leaves the files untouched, and a container restarts in a loop until you act.
  Setting `SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true` for one start converts the store in place.
- **Nothing listens while it converts.** The port stays closed until the conversion ends, measured
  at about 10 s per 1.4 GB. Health checks and probes have room for that: compose healthchecks have a
  ten-minute `start_period`, and the Helm chart delays liveness by ten minutes.
- **Converted indexes are wrong until rebuilt.** Datetime indexes keep the 3.x ordering, which gives
  wrong range counts, and the 4.0 planner ignores some converted indexes. Rebuild every index after
  the conversion, not only the full-text and vector ones.
- **There is no way back except a copy.** A 3.x server pointed at the converted directory starts on
  an empty database and writes files that then stop 4.0 from opening the store. Roll back only from
  the copy you take first.
- **The image runs as uid and gid 65532.** The compose files and the Ansible role hand the data
  directory to that user with a one-shot `surreal-init`, and the Helm chart sets `fsGroup: 65532`.

<!-- PENDING-UPGRADE-PATH-DECISION: the 4.0 disk lane picks in-place conversion or export/import into
a fresh store. The steps below describe in-place conversion, the path measured so far; replace them
if the decision differs. -->

<!-- PENDING-VLOG-DECISION: stores 4.0 creates run with SURREAL_SURREALKV_ENABLE_VLOG=false. A store
converted in place keeps a value log whatever that setting says; the disk lane's decision says
whether and how to rewrite it. -->

## Before you start

1. **Upgrade Sibyl first, while SurrealDB is still on 3.x.** Releases from this one on talk to both
   3.x and 4.0. Earlier releases cannot sign in or write on 4.0 at all
   (`CBOR tag 0 (a datetime as text) is not accepted`), so the API and worker must already run this
   release before SurrealDB moves.
2. **Stop the API and the worker.** Nothing may write while the store converts.
3. **Take two copies.** A logical export of every database (see
   [Backup And Restore](../admin/backup-restore.md)), and a cold copy of the stopped data directory,
   volume, or PVC snapshot. The cold copy is your rollback.
4. **Check free disk.** The conversion peaks at about 2.4 times the store's size, and about 4 times
   if you rebuild indexes before deleting the backup directory the conversion leaves.

## Compose

This covers `docker-compose.yml`, `docker-compose.prod.yml`, `docker-compose.quickstart.yml`, and
compose files of your own built from them. Add your usual `-f` and `--env-file` flags to each
command.

```bash
# 1. Stop the writers, then SurrealDB.
docker compose stop api worker web
docker compose stop surrealdb

# 2. Start SurrealDB 4.0 once with the opt-in and wait for it.
SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true docker compose up -d --no-deps --force-recreate surrealdb
until curl -fsS -o /dev/null http://localhost:8000/ready; do sleep 5; done

# 3. Rebuild every index (the script is below).
SURREAL_URL=http://localhost:8000 SURREAL_USER=root SURREAL_PASS=<password> ./rebuild-indexes.sh

# 4. Recreate SurrealDB without the opt-in and start everything.
docker compose up -d --force-recreate
```

The compose services pass `SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X` through from the shell, so leaving
it unset is all step 4 needs. For `moon run dev`, stop it, copy `.moon/cache/surreal-dev`, run
`SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true moon run dev` once, then rebuild the indexes against
`http://127.0.0.1:8000` with the `root`/`root` dev credentials.

To stay on 3.x for now, set both `SIBYL_SURREAL_IMAGE` to the 3.x image you ran and
`SIBYL_SURREAL_PATH=rocksdb:///data/sibyl.db`. Never set one without the other: a 3.x server opened
with `surrealkv://` serves an empty database beside the data.

## Ansible

1. Run the role once with `sibyl_surreal_upgrade_from_3x: true` and the default
   `sibyl_surreal_image` and `sibyl_surreal_path`. `sibyl.service` restarts the stack, and the
   backend waits on SurrealDB's healthcheck, whose ten-minute `start_period` covers the conversion.
   Stop `sibyl.service` first if you want to rebuild before the backend starts writing.
2. Rebuild every index against the host's SurrealDB (below).
3. Set `sibyl_surreal_upgrade_from_3x` back to `false` and run the role again.

To stay on 3.x, set `sibyl_surreal_image` to the 3.x image and `sibyl_surreal_path` to
`rocksdb:///data/sibyl.db` together. The role refuses a path that does not match the image's major.

## Helm

The `charts/surrealdb` wrapper already sets `fsGroup: 65532` with
`fsGroupChangePolicy: OnRootMismatch`, readiness on `/ready`, and a ten-minute liveness delay. The
upstream chart it wraps has no startup probe, so the liveness delay is what keeps the kubelet from
killing the pod while it converts.

1. Scale the Sibyl backend and worker to zero, and take a VolumeSnapshot of the SurrealDB PVC.
2. Size the PVC for about 2.4 times the store. If the conversion may take longer than ten minutes,
   raise `surrealdb.livenessProbe.initialDelaySeconds` above it for this rollout.
3. Add the opt-in to `surrealdb.podExtraEnv` for one rollout. Helm replaces lists, so restate the
   chart's default entries with it:

   ```yaml
   surrealdb:
     podExtraEnv:
       - name: OTEL_EXPORTER_OTLP_ENDPOINT
         value: ""
       - name: OTEL_SERVICE_NAME
         value: sibyl-surrealdb
       - name: SURREAL_SURREALKV_ENABLE_VLOG
         value: "false"
       - name: SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X
         value: "true"
   ```

4. Run `helm upgrade` with a `--timeout` longer than the conversion, such as `--timeout 30m`. The
   strict-bootstrap hook waits for `/ready` for up to `bootstrap.readyTimeoutSeconds` (1800 by
   default) before it defines the databases.
5. Rebuild every index from a pod that can reach the service, such as the chart's `alpine/k8s` ops
   image, or through `kubectl port-forward`.
6. Remove the opt-in entry, run `helm upgrade` again, and scale the backend and worker back up.

The chart refuses to render a 3.x `surrealdb.image.tag` with a `surrealkv:` path. To stay on 3.x,
set `surrealdb.surrealdb.path: rocksdb:/data/db` with the 3.x tag.

## CLI runtimes

`sibyl update`, `sibyl local upgrade`, `sibyl docker upgrade`, and `sibyl up` never move SurrealDB
to a new major version. A runtime on 3.x keeps its whole SurrealDB service while the Sibyl images
move, and each command says so. `sibyl local upgrade --surreal-major` and
`sibyl docker upgrade --surreal-major` do the move for their runtime after asking first; see
[`sibyl local`](../cli/local.md#local-upgrade) and
[`sibyl docker`](../cli/docker.md#docker-upgrade). Both runtimes keep their data in the same
`sibyl_surreal` Docker volume, so move only the one you run, and never start the other runtime's 3.x
service on the converted volume.

## Rebuild every index

Run each rebuild as its own request so none runs inside a transaction. On a store holding about 1.4
GB, all 522 indexes rebuilt in about 98 s; a small store with Sibyl's schema took 18 s for 283.

```bash
#!/usr/bin/env bash
# Rebuild every index on a SurrealDB 4 server, one statement per request so
# none runs inside a transaction. Needs SURREAL_URL, SURREAL_USER, SURREAL_PASS.
set -euo pipefail
sql() {
  local headers=(-H "Accept: application/json")
  [ -n "$1" ] && headers+=(-H "surreal-ns: $1")
  [ -n "$2" ] && headers+=(-H "surreal-db: $2")
  curl -fsS -u "$SURREAL_USER:$SURREAL_PASS" "${headers[@]}" --data-binary "$3" "$SURREAL_URL/sql"
}
ident() { printf '`%s`' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/`/\\`/g')"; }
keys() { jq -r --arg field "$1" '.[0].result[$field] // {} | keys[]'; }
rebuilt=0
while read -r ns; do
  while read -r db; do
    while read -r table; do
      while read -r index; do
        echo "rebuilding $ns/$db $table.$index"
        sql "$ns" "$db" "REBUILD INDEX $(ident "$index") ON TABLE $(ident "$table");" |
          jq -e 'all(.[]; .status == "OK")' >/dev/null
        rebuilt=$((rebuilt + 1))
      done < <(sql "$ns" "$db" "INFO FOR TABLE $(ident "$table");" | keys indexes)
    done < <(sql "$ns" "$db" "INFO FOR DB;" | keys tables)
  done < <(sql "$ns" "" "INFO FOR NS;" | keys databases)
done < <(sql "" "" "INFO FOR ROOT;" | keys namespaces)
echo "rebuilt $rebuilt indexes"
```

## Verify and clean up

1. Compare per-table counts with the export's manifest, then sign in, search, remember something,
   and list tasks.
2. Keep the `_rocksdb_backup/` directory the conversion leaves inside the store until you trust the
   result, then delete it to reclaim about one store's worth of disk.
3. Make sure the opt-in is gone from the shell, the role's variables, or the chart's values.

## Roll back

Never point a 3.x server at the converted directory. Start the 3.x image you ran before, with a
`rocksdb://` path, on the cold copy or on a copy of `_rocksdb_backup/`, and run the Sibyl release
you upgraded to (it still talks to 3.x). Anything written on 4.0 after the conversion is lost.
