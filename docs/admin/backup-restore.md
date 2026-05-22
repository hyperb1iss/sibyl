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
counts:

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
```

A restore process that has not been rehearsed is not a backup strategy. Keep the weekly drill
enabled for production and page on failure.

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
