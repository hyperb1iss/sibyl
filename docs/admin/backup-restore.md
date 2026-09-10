---
title: Backup And Restore
description: SurrealDB snapshots, logical exports, and restore drills
---

# Backup And Restore

Enterprise Sibyl uses two backup lanes for SurrealDB:

- PVC snapshots for fast storage-level recovery.
- Logical `surreal export` jobs for portable restore and drill validation.

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

Use your deployment overlay for object storage credentials, encryption commands, and notification
hooks.

## Restore Drill

The restore drill imports the latest export into an ephemeral runtime and checks fixture table
counts. It writes a structured receipt to disk and emits the same JSON between
`SIBYL_RESTORE_RECEIPT_JSON_BEGIN` / `SIBYL_RESTORE_RECEIPT_JSON_END` markers in the job logs:

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
2. Choose a snapshot or export timestamp.
3. Restore storage from a PVC snapshot, or start a fresh SurrealDB pod and import the selected
   export.
4. Run fixture checks and a sampled recall query.
5. Point Sibyl at the restored endpoint.
6. Unfreeze writes after validation.

Record the restore receipt: export name, snapshot name, fixture counts, sampled query, operator, and
timestamp.

## What To Keep

- Daily logical exports.
- Daily PVC snapshots when the storage driver supports them.
- Weekly restore-drill receipts.
- The exact chart and image versions used for the backup and restore.
- The secret-store version or sealed secret revision needed to decrypt settings.

## Restore an API backup

A backup downloaded from the web settings or `/api/backups/{id}/download` contains
`metadata.json` and the enabled auth, content and graph JSON payloads. The migration
CLI accepts this version 2.0 backup format directly, as well as its own manifest
archives. Do not unpack or rename the metadata file.

Stop writers and configure `sibyld` for the intended destination, then check the
bundle before restoring it:

```bash
sibyld migrate check ./sibyl_backup.tar.gz
sibyld migrate import ./sibyl_backup.tar.gz \
  --source-type surreal-archive --target-mode surreal \
  --org-id YOUR_ORGANIZATION_UUID --clean
```

The loader verifies the declared file inventory, checksums and organization
before using the existing restore owners. The organization must match the backup;
this command does not move protected memory into another organization.

An organization backup excludes reusable authentication secrets. Restoring one
does not restore passwords, sessions, API keys, device authorizations or API key
scope grants. The command reports credential-dependent rows as skipped, including
rows from older backups. Invitation metadata is restored without acceptance tokens.
Use the destination's supported account recovery or authentication setup. Source tombstones and newer destination
revocations remain authoritative, so an older backup cannot make purged or hidden
memory readable again. The command reports retained history and quarantined
records instead of claiming every archived row was written.

Auth, content and graph restore in separate stages. A failure does not imply a
cross-namespace rollback. Preserve the failed archive and destination, inspect the
reported stage, and retry only after correcting the cause. Database dumps and
legacy PostgreSQL bundles are not accepted by this logical backup adapter.
