---
title: Backup And Restore
description: SurrealDB snapshots, logical exports, and restore drills
---

# Backup And Restore

Enterprise Sibyl uses two backup lanes for SurrealDB:

- PVC snapshots for fast storage-level recovery.
- Logical exports of every namespace and database on the server, for portable restore and drill
  validation.

The `charts/surrealdb` wrapper chart owns the reference CronJobs.

## Snapshot CronJob

Enable snapshots when your cluster has a `VolumeSnapshotClass` for the SurrealDB PVC:

```yaml
snapshot:
  enabled: true
  persistentVolumeClaimName: sibyl-surrealdb-data
  volumeSnapshotClassName: premium-block-snapshots
  retention:
    enabled: true
    keep: 7
```

Snapshots are useful for fast rollback, but they are tied to the storage provider and do not replace
logical exports.

## Export CronJob

Enable logical export and mount or sync the destination in your overlay:

```yaml
export:
  enabled: true
  destination:
    path: /backups
    uri: s3://example-sibyl-backups/prod
  syncCommand: "aws s3 sync /backups s3://example-sibyl-backups/prod"
```

Each run asks the server what exists (`INFO FOR ROOT`, then `INFO FOR NS` for every namespace) and
exports every database it finds, so nobody maintains a list. An organization created yesterday is in
tonight's export. Discovery needs root credentials, which is the chart default
(`connection.authLevel: root`).

### What the export captures

Every database in every namespace that exists when the job runs:

- `sibyl_auth/auth`: users, organizations, memberships, API keys and the rest of the auth plane.
- `sibyl_content/content`: crawled documents and chunks, raw captures, validation executions and the
  other content tables.
- `org_<uuid>/graph` for each organization: that organization's knowledge graph.
- Any other namespace on the server, including SurrealDB's empty `main/main` default.

Each file holds the database's tables, fields, indexes, analyzers and events along with its records.

The export does not capture:

- Validation receipt journal files. Completed validation receipts live outside SurrealDB, in the
  private receipt directory (`SIBYL_VALIDATION_RECEIPT_DIR`, the `validationReceipts` claim in the
  Sibyl chart) or in an S3 receipt store where the deployment uses one. A receipt the database could
  not retain exists only there. The API backup archive carries those receipts (see
  [Completed validation receipts](#completed-validation-receipts)), as does the bucket of an S3
  receipt store. The `.surql` export never contains them.
- Root and namespace definitions, such as root users and namespace-level users or accesses.
  Credentials come from your secret store.
- One consistent moment across databases. Each database is exported on its own, seconds apart, so a
  write that touches auth and a graph during the run can land in one file and not the other. PVC
  snapshots are the crash-consistent lane.

The API backup archive is written to `SIBYL_BACKUP_DIR` (default `./backups`). The Sibyl chart runs
the API and worker with a read-only root filesystem and mounts no volume for that directory, so set
it to a durable mount before relying on API archives in a Kubernetes deployment.

### Layout and manifest

Each run writes one directory:

```text
/backups/sibyl-20261010023000/
  manifest.json
  sibyl_auth.auth.surql
  sibyl_content.content.surql
  org_0b8e5a3c1f2d4e6a9b7c8d9e0f1a2b3c.graph.surql
```

`manifest.json` lists every database with its file name, byte size, sha256 and per-table row counts,
plus the SurrealDB server version and the UTC time of the run. The job writes it after every file,
so a directory without one belongs to a failed or interrupted run, and the restore drill ignores it.
Sizes and checksums describe the plaintext files.

The job fails without writing a manifest when discovery finds no database, when any export comes
back empty, or when a namespace or database name is not a plain identifier
(`^[A-Za-z_][A-Za-z0-9_]*$`).

### Encryption and sync hooks

`export.encryption.command` runs once per exported file, after the file's checksum is recorded, with
the path in `$SIBYL_EXPORT_FILE`. It runs for `manifest.json` last. `export.syncCommand` runs once
after that, with the run directory in `$SIBYL_EXPORT_RUN_DIR`. The ops image ships the AWS CLI but
no encryption tool, so bring one through `opsImage` or `export.extraVolumes`. Use your deployment
overlay for object storage credentials.

## Restore Drill

The restore drill picks the newest run directory under `source.path` that has a `manifest.json`,
checks every file's size and sha256 against it, and imports every database into a scratch SurrealDB
sidecar. It then counts the rows in every table and compares them with the counts the export
recorded. A database fails when its file is missing or altered, when the import is refused, when a
table the export saw is missing, or when it restores no rows although the export counted some. The
drill reports every failing database, runs `failureNotification.command`, and exits non-zero. Small
count differences are reported but do not fail the drill: the export counts rows just before it
exports, in a separate read, so writes in between can move them.

After the databases pass, the drill runs the configured fixture checks and the optional recall
check. It writes a structured receipt to disk and emits the same JSON between
`SIBYL_RESTORE_RECEIPT_JSON_BEGIN` / `SIBYL_RESTORE_RECEIPT_JSON_END` markers in the job logs. The
receipt names the manifest it restored and lists every database with its exported and restored row
counts per table.

```yaml
restoreDrill:
  enabled: true
  source:
    path: /backups
  fixtureChecks:
    - namespace: sibyl_auth
      database: auth
      table: users
      minRows: 1
  receipt:
    path: /tmp/restore-drill-receipt.json
```

The drill reads `source.path`, which defaults to an empty `emptyDir`. Mount the export destination
there: set `restoreDrill.workspace.emptyDir: false` and add the volume through
`restoreDrill.extraVolumes` and `restoreDrill.extraVolumeMounts`. The drill has no decryption step,
so a deployment that encrypts exports must stage decrypted files under `source.path` before the
drill runs.

The scratch server keeps its copy with RocksDB under the pod's `/tmp` emptyDir
(`restoreDrill.restore.path`), so the node needs ephemeral disk for a full copy of the data.

For the enterprise evidence gate, enable a sampled recall check and have the command write a JSON
sample to `$SIBYL_RESTORE_RECALL_SAMPLE_PATH`:

```yaml
restoreDrill:
  recallCheck:
    enabled: true
    command: |
      pack="$(sibyl search "restore drill fixture memory" --json)"
      count="$(printf '%s\n' "$pack" | jq '.total_items')"
      printf '{"query":"restore drill fixture memory","result_count":%s}\n' "$count" \
        > "$SIBYL_RESTORE_RECALL_SAMPLE_PATH"
```

The sampled command can use your deployment's preferred helper image or mounted tools; the only
contract is that it writes a non-empty `query` and `result_count > 0`. A restore process that has
not been rehearsed is not a backup strategy. Keep the weekly drill enabled for production and page
on failure.

Capture the enterprise evidence bundle from a completed Kubernetes Job with:

```bash
moon run enterprise-readiness-evidence -- \
  --capture-kubernetes-restore-drill sibyl-surrealdb-restore-drill-manual \
  --kubernetes-context kind-sibyl-enterprise \
  --kubernetes-namespace sibyl \
  --manual-captured-by "$(whoami)"
```

## Manual Restore Shape

1. Freeze writes or take the service offline.
2. Choose a snapshot, or an export run directory that has a `manifest.json`.
3. Restore storage from a PVC snapshot, or start a fresh SurrealDB server and import every database
   the manifest lists (below).
4. Run fixture checks and a sampled recall query.
5. Point Sibyl at the restored endpoint.
6. Unfreeze writes after validation.

To import an export run into a fresh server, check the files against the manifest, then define each
namespace and database before importing its file. SurrealDB answers both calls with HTTP 200 and a
status per statement, so the `jq` checks are what catch a refused import:

```bash
set -euo pipefail
run=/backups/sibyl-20261010023000
(cd "$run" && jq -r '.databases[] | "\(.sha256)  \(.file)"' manifest.json | sha256sum -c -)
for entry in $(jq -r '.databases[] | "\(.namespace)/\(.database)"' "$run/manifest.json"); do
  ns="${entry%/*}" db="${entry#*/}"
  curl -fsS -u "root:$SURREAL_PASS" -H "Accept: application/json" \
    --data-binary "DEFINE NAMESPACE IF NOT EXISTS $ns; USE NS $ns; DEFINE DATABASE IF NOT EXISTS $db;" \
    "$SURREAL_URL/sql" | jq -e 'all(.[]; .status == "OK")' >/dev/null
  curl -fsS -u "root:$SURREAL_PASS" -H "Accept: application/json" \
    -H "surreal-ns: $ns" -H "surreal-db: $db" -X POST -T "$run/$ns.$db.surql" \
    "$SURREAL_URL/import" | jq -e 'all(.[]; .status == "OK")' >/dev/null
done
```

Record the restore receipt: export run, snapshot name, fixture counts, sampled query, operator, and
timestamp.

## What To Keep

- Daily logical exports, each a complete run directory with its manifest.
- Daily PVC snapshots when the storage driver supports them.
- Weekly restore-drill receipts.
- API backup archives, which carry validation receipts the export cannot.
- The exact chart and image versions used for the backup and restore.
- The secret-store version or sealed secret revision needed to decrypt settings.

## Restore an API backup

A backup downloaded from the web settings or `/api/backups/{id}/download` contains `metadata.json`
and the enabled auth, content and graph JSON payloads. The migration CLI accepts this version 2.0
backup format directly, as well as its own manifest archives. Do not unpack or rename the metadata
file.

Stop writers and configure `sibyld` for the intended destination, then check the bundle before
restoring it:

```bash
sibyld migrate check ./sibyl_backup.tar.gz
sibyld migrate import ./sibyl_backup.tar.gz \
  --source-type surreal-archive --target-mode surreal \
  --org-id YOUR_ORGANIZATION_UUID --clean
```

The loader verifies the declared file inventory, checksums and organization before using the
existing restore owners. The organization must match the backup; this command does not move
protected memory into another organization.

An organization backup excludes reusable authentication secrets. Restoring one does not restore
passwords, sessions, API keys, device authorizations or API key scope grants. The command reports
credential-dependent rows as skipped, including rows from older backups. Invitation metadata is
restored without acceptance tokens. Use the destination's supported account recovery or
authentication setup. Source tombstones and newer destination revocations remain authoritative, so
an older backup cannot make purged or hidden memory readable again. The command reports retained
history and quarantined records instead of claiming every archived row was written.

Auth, content and graph restore in separate stages. A failure does not imply a cross-namespace
rollback. Preserve the failed archive and destination, inspect the reported stage, and retry only
after correcting the cause. The loader accepts only Sibyl archives and API backups; it does not read
raw database dumps or archives from other memory systems.

### Completed validation receipts

Content archive 2.3 includes encrypted completion receipts for the archived validation executions. A
completed validation can remain in its private journal when the database cannot retain its result.
Public backup and import preserve those ciphertext bytes, so recovery on a fresh host does not
require the old receipt volume or another provider request.

The `validation_receipts.executions` inventory identifies each execution as `journal`, `database`,
`purged`, or `unresolved`. An unresolved execution has no retained completed result in that
snapshot. Its provider outcome and cost may remain unknown; restoring the archive does not authorize
redispatch.

Export fails if execution history changes while receipts are captured. Retry the backup after the
concurrent completion or purge settles. Import authenticates receipt bytes against the archived
request and key before any content writes. Existing destination history, source revocation, and
purge rules still apply. Older content archives remain supported but cannot supply omitted journal
files.

Receipt files are published privately before the content transaction. If that transaction fails,
ciphertext may remain without an authorized execution row. Preserve the failed archive and receipt
directory, then retry the same import after resolving the reported conflict. Import never replaces
conflicting receipt bytes or restores an erased recovery key over current destination history.

Protect the complete backup as secret material: the content payload includes the recovery keys
needed to decrypt its receipts. The archive is not encrypted as a whole. Continue using encrypted
backup storage and the configured private receipt directory. Logical backup still does not claim
cross-namespace crash atomicity.
