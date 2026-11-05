---
title: SurrealDB 4.0 Upgrade
description: Move a store a SurrealDB 3.x server wrote to SurrealDB 4.0, once and for good
---

# SurrealDB 4.0 Upgrade

Sibyl supports one SurrealDB server release, `surrealdb/surrealdb:v4.0.0`. A fresh install starts on
it and needs nothing from this page. A store a 3.x server wrote has to be converted once before 4.0
will open it, and that conversion is one-way. This page is the runbook for that move on every
surface Sibyl ships: the compose files, the Ansible role, the Helm chart, and the CLI runtimes.

## What happens

- **4.0 converts the store in place.** 4.0 has no RocksDB engine. Started once with
  `SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true` on the same directory, it converts the RocksDB files
  to SurrealKV, keeps the originals in `_rocksdb_backup/` inside the directory, and records version
  4.0.0. Without that setting it refuses the store ("holds a RocksDB database written by SurrealDB
  3.x"), leaves every file untouched, and a container restarts in a loop until you act.
- **The value log stays off.** Every SurrealDB service Sibyl ships sets
  `SURREAL_SURREALKV_ENABLE_VLOG=false`, the converting start included, so the converted store
  writes no value log (the conversion's log line reports `values_in_value_log: 0`) and
  `_rocksdb_backup/` holds the original RocksDB files byte for byte. Keep that setting on every
  start: a directory that ever writes value-log files keeps the value log, and nothing reclaims
  them.
- **Nothing listens while it converts.** The port stays closed until the conversion ends, about 23 s
  per GB of RocksDB data. Allow 30 s per GB: the compose healthchecks' ten-minute `start_period` and
  the Helm chart's ten-minute liveness delay cover a 20 GB store.
- **Sibyl rebuilds the indexes itself.** Converted indexes are wrong or ignored until rebuilt, so
  each database rebuilds its own indexes the first time Sibyl bootstraps it on 4.0, under that
  database's schema lease, then marks itself done. The API bootstraps the auth and content databases
  before it serves, so its first start takes about 60 s per GB of those two; allow that much: the
  backend healthchecks' ten-minute `start_period` and the Helm chart's default startup probe cover
  10 GB. Each organization's graph database rebuilds on that organization's first access, from the
  API or the worker, and that access waits for it, so the first requests for each organization are
  slow. The lease makes extra API replicas and workers safe; one replica for the first start is only
  faster. Each database logs `surreal_index_rebuild_started` and `surreal_index_rebuild_finished`.
- **A rebuild that fails, fails closed.** The database stays unmarked and nothing serves it: the API
  does not report ready while auth or content is unrebuilt, and an organization's requests fail
  until its graph rebuilds. A restart tries again, and databases already marked are skipped.
- **The first 4.0 start needs a WebSocket URL.** A rebuild can outlast the 30 s limit of the HTTP
  transport, so a database due a rebuild refuses while `SIBYL_SURREAL_URL` is `http://` or
  `https://`. Every configuration Sibyl ships uses `ws://`; if yours points at HTTP, start once on
  the server's `ws://` (or `wss://`) `/rpc` endpoint.
- **The way back is a copy.** A 3.x server pointed at the converted directory starts on an empty
  database and writes files that then stop 4.0 from opening the store. Roll back only from
  `_rocksdb_backup/` or the cold copy you take first.
- **The image runs as uid and gid 65532.** The compose files and the Ansible role hand the data
  directory to that user with a one-shot `surreal-init`, and the Helm chart sets `fsGroup: 65532`.

## The steps

Every surface below follows the same sequence:

1. **Upgrade Sibyl first, while SurrealDB is still on 3.x.** Releases from this one on talk to both
   3.x and 4.0. Earlier releases cannot sign in or write on 4.0 at all
   (`CBOR tag 0 (a datetime as text) is not accepted`).
2. **Stop the API and the worker,** then SurrealDB.
3. **Take a cold copy** of the stopped data directory, volume, or PVC. It is your rollback point.
4. **Check free disk:** at least twice the RocksDB directory's size free on its filesystem, on top
   of the directory itself. The directory peaks at 2.4 times its size while it converts and at 2.75
   times while Sibyl rebuilds indexes with `_rocksdb_backup/` still in it.
5. **Start 4.0 once with the opt-in** on the `surrealkv://` path, same directory, and wait for it to
   answer `/ready`. Its log shows `Datastore version recorded version=4.0.0`.
6. **Remove the opt-in and start Sibyl** on a `ws://` URL. The API rebuilds the auth and content
   indexes before it answers `/api/health/ready`; each organization's graph rebuilds on its first
   access.
7. **Verify,** then delete `_rocksdb_backup/` to reclaim about one store's worth of disk, and the
   cold copy when you no longer want it.

## Compose

This covers `docker-compose.quickstart.yml`, `docker-compose.prod.yml`, the root
`docker-compose.yml`, and compose files of your own built from them. Add your usual `-f` and
`--env-file` flags to each `docker compose` command, and use your file's service and volume names:
the quickstart runs `api`, `worker` and `web` on the `sibyl_surreal` volume, the prod file
`backend`, `worker` and `frontend` on `<project>_surreal_data`.

```bash
# 2. Stop the writers, then SurrealDB.
docker compose stop api worker web surrealdb

# 3. Cold copy of the volume.
docker volume create sibyl_surreal_3x_backup
docker run --rm -v sibyl_surreal:/data:ro -v sibyl_surreal_3x_backup:/backup busybox:1.37 \
  cp -a /data/. /backup/

# 4. Used and available KiB; available must be at least twice used.
docker run --rm -v sibyl_surreal:/data:ro busybox:1.37 sh -c 'du -sk /data; df -Pk /data'

# 5. Convert: one start with the opt-in, then wait until it answers.
SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true docker compose up -d --no-deps --force-recreate surrealdb
until docker compose exec -T surrealdb /surreal is-ready --endpoint http://localhost:8000; do
  sleep 10
done

# 6. Recreate SurrealDB without the opt-in and start Sibyl. The API turns healthy once its
#    auth and content indexes are rebuilt; each organization's graph rebuilds on first access.
docker compose up -d
docker compose ps api   # healthy once /api/health/ready answers
```

The compose services pass `SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X` through from the shell, so leaving
it unset is all step 6 needs. Then clean up (step 7):

```bash
docker run --rm -v sibyl_surreal:/data busybox:1.37 rm -rf /data/sibyl.db/_rocksdb_backup
docker volume rm sibyl_surreal_3x_backup
```

For `moon run dev`, stop it, copy `.moon/cache/surreal-dev`, and run
`SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true moon run dev` once; the dev API rebuilds its indexes as
it bootstraps.

To stay on 3.x for now, set both `SIBYL_SURREAL_IMAGE` to the 3.x image you ran and
`SIBYL_SURREAL_PATH=rocksdb:///data/sibyl.db`. Each compose file's `surreal-init` checks the image
and path before the server starts, and exits with the reason instead of starting one that would open
the data wrong. First the pairing alone, whatever the directory holds: the image must name its
version (a tag such as `v3.2.4`, not `latest` or a bare digest), the path must be absolute, a 3.x
image runs only on `rocksdb://`, and 4.x runs only on `surrealkv://`. A 3.x server opened with
`surrealkv://` serves an empty database beside the data, so no directory check can make that pairing
safe. Then the directory: the one-shot refuses a directory it cannot list, a 3.x image on a store
SurrealDB 4 wrote, a directory holding only the `_rocksdb_backup/` copy, and 4.0 on a RocksDB store
without the opt-in.

## Ansible

1. Stop `sibyl.service`, take the cold copy of the `sibyl_surreal_data` volume, and check free disk
   as in the compose steps.
2. Run the role once with `sibyl_surreal_upgrade_from_3x: true` and the default
   `sibyl_surreal_image` and `sibyl_surreal_path`. The stack starts: SurrealDB converts within its
   healthcheck's ten-minute `start_period`, then the backend rebuilds the auth and content indexes
   within its own, and the frontend and Caddy wait for it. Raise the start periods first for a store
   over 10 GB.
3. Set `sibyl_surreal_upgrade_from_3x` back to `false` and run the role again.
4. Verify, then remove `/data/sibyl.db/_rocksdb_backup` from the volume as in the compose cleanup.

To stay on 3.x, set `sibyl_surreal_image` to the 3.x image and `sibyl_surreal_path` to
`rocksdb:///data/sibyl.db` together. The role refuses a path that does not match the image's major.

## Helm

The `charts/surrealdb` wrapper sets `SURREAL_SURREALKV_ENABLE_VLOG=false`, `fsGroup: 65532` with
`fsGroupChangePolicy: OnRootMismatch`, readiness on `/ready`, and a ten-minute liveness delay. The
upstream chart it wraps has no startup probe, so the liveness delay is what keeps the kubelet from
killing the pod while it converts.

The commands below use `<release>` for the surrealdb chart's release name, which names its
Deployment and PVC `<release>-surrealdb`, and `<ns>` for its namespace.

1. Scale the Sibyl backend and worker to zero.
2. Stop SurrealDB and wait until its pod is gone, so the snapshot is a cold copy:

   ```bash
   kubectl -n <ns> scale deployment/<release>-surrealdb --replicas=0
   kubectl -n <ns> wait --for=delete pod --timeout=10m \
     -l app.kubernetes.io/instance=<release>,app.kubernetes.io/name=surrealdb
   ```

3. Take a VolumeSnapshot of the `<release>-surrealdb` PVC and wait until it is ready to use. It is
   your rollback point, so do not go on before it is:

   ```bash
   kubectl -n <ns> wait --for=jsonpath='{.status.readyToUse}'=true \
     volumesnapshot/<snapshot> --timeout=30m
   ```

4. Make sure the PVC has twice the store's size free. For a store over 20 GB, raise
   `surrealdb.livenessProbe.initialDelaySeconds` to 30 s per GB for this rollout.
5. Add the opt-in to `surrealdb.podExtraEnv` for one rollout. Helm replaces lists, so restate the
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

6. Run `helm upgrade` on the surrealdb chart with `--no-hooks`, then start SurrealDB and wait for it
   to convert. The strict-bootstrap hook would otherwise wait on a server that is not running yet.
   Readiness is `/ready`, so the rollout finishes only once the conversion has:

   ```bash
   kubectl -n <ns> scale deployment/<release>-surrealdb --replicas=1
   kubectl -n <ns> rollout status deployment/<release>-surrealdb --timeout=30m
   ```

7. Remove the opt-in entry and run `helm upgrade` again, this time with its hooks. The
   strict-bootstrap hook waits for `/ready` for up to `bootstrap.readyTimeoutSeconds` (1800 by
   default) before it defines the databases.
8. Scale the backend and worker back up. The backend's startup probe (10 s period) must allow 6
   failures per GB of the auth and content databases while it rebuilds their indexes: the default
   `backend.startupProbe.failureThreshold: 60` covers 10 GB. Replicas are safe, since a lease
   serializes the rebuild, but one replica for this start finishes sooner.
9. Verify, then remove `/data/db/_rocksdb_backup` from the PVC. The image has no shell, so scale
   SurrealDB to zero and run a one-off `busybox:1.37` pod that mounts the PVC, or wait for the next
   maintenance window.

The chart refuses to render a store path that does not pair with `surrealdb.image.tag`: the tag must
name its version (such as `v3.2.4`, not `latest`), the path must be absolute, a 3.x tag runs only on
`rocksdb:`, and 4.x runs only on `surrealkv:`. To stay on 3.x, set
`surrealdb.surrealdb.path: rocksdb:/data/db` with the 3.x tag.

## CLI runtimes

`sibyl update`, `sibyl local upgrade`, `sibyl docker upgrade`, and `sibyl up` never move SurrealDB
to a new major version. A runtime on 3.x keeps its whole SurrealDB service while the Sibyl images
move, and each command says so. `sibyl local upgrade --surreal-major` and
`sibyl docker upgrade --surreal-major` run the steps above for their runtime after asking first:
they copy the `sibyl_surreal` volume into `sibyl_surreal_3x_backup`, check free disk, convert, start
the stack and wait for the API to finish rebuilding its auth and content indexes, then print the
cleanup and rollback commands. Upgrade the runtime's Sibyl images first; the command refuses while
the API image is older than the CLI. The free-disk check runs after the copy, so with Docker's
volumes on one disk the move needs about three times the store's size free when it starts. A failure
before the conversion starts the stack again on 3.x with nothing changed and leaves the copy behind;
remove `sibyl_surreal_3x_backup` before you try again. See
[`sibyl local`](../cli/local.md#local-upgrade) and
[`sibyl docker`](../cli/docker.md#docker-upgrade). Both runtimes keep their data in the same
`sibyl_surreal` volume, so the command refuses while the other runtime's compose file still runs a
3.x SurrealDB.

## Memory

Cap SurrealDB's cache with `SURREAL_SURREALKV_BLOCK_CACHE_CAPACITY` (bytes); the 3.x
`SURREAL_ROCKSDB_*` settings are ignored. If you also set `SURREAL_MEMORY_THRESHOLD`, it must exceed
the block cache.

## Roll back

Never point a 3.x server at the converted directory. Start the 3.x image you ran before, with a
`rocksdb://` path, on the cold copy, or on a fresh directory holding a copy of `_rocksdb_backup/`,
and keep running the Sibyl release you upgraded to (it still talks to 3.x). Anything written on 4.0
after the conversion is lost.
