# Sibyl SurrealDB Phase 2 Live Gates

This is the execution runbook for the remaining Phase 2 release gates. Run it only in an approved
maintenance or rehearsal window with both legacy and Surreal runtimes available.

The goal is to collect enough evidence to decide whether Sibyl can ship the SurrealDB-first release
while keeping the legacy PostgreSQL auth store as a one-release escape hatch.

---

## Evidence Directory

Create one directory per rehearsal and keep command output, archive manifests, and notes together:

```bash
export SIBYL_GATE_DIR=.moon/cache/surreal-live-gates/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$SIBYL_GATE_DIR"
```

Record:

- source environment and commit SHA
- legacy archive path and manifest summary
- Surreal target URL and data directory or volume
- private legacy API base URL
- private Surreal API base URL
- every command exit code
- any deviations copied back into `docs/guide/surrealdb-migration-release-notes.md`

---

## Gate 1 - Production-Like Archive Rehearsal

Export from the legacy runtime:

```bash
SIBYL_STORE=legacy \
SIBYL_AUTH_STORE=postgres \
uv run --directory apps/api sibyld migrate export \
  --org-id <org-uuid> \
  --output "$SIBYL_GATE_DIR/legacy-archive.tar.gz" \
  --include-content \
  2>&1 | tee "$SIBYL_GATE_DIR/export.log"
```

Rehearse against a fresh Surreal runtime and a private Surreal-backed API:

```bash
SIBYL_STORE=surreal \
SIBYL_AUTH_STORE=surreal \
SIBYL_SURREAL_URL=<fresh-surreal-url> \
moon run migrate-rehearse -- \
  "$SIBYL_GATE_DIR/legacy-archive.tar.gz" \
  --yes \
  --base-url <surreal-api-base-url> \
  2>&1 | tee "$SIBYL_GATE_DIR/rehearse.log"
```

Pass criteria:

- export exits 0 and produces an archive with `graph.json`, `auth.json`, `content.json`, and
  `manifest.json` when those payloads exist in the source
- rehearsal exits 0
- archive verification passes
- baseline replay passes or any skipped baseline is explicitly justified
- auth-flow replay passes against the Surreal API

---

## Gate 2 - Live Auth-Flow Compare

Run one PostgreSQL-auth API and one Surreal-auth API against equivalent rehearsed data, then compare
observable behavior:

```bash
moon run auth-flow-compare -- \
  --postgres-base-url <legacy-api-base-url> \
  --surreal-base-url <surreal-api-base-url> \
  --postgres-email-outbox-path "$SIBYL_GATE_DIR/postgres-email-outbox.jsonl" \
  --surreal-email-outbox-path "$SIBYL_GATE_DIR/surreal-email-outbox.jsonl" \
  2>&1 | tee "$SIBYL_GATE_DIR/auth-flow-compare.log"
```

Pass criteria:

- compare exits 0
- base URLs are distinct unless intentionally debugging the harness with `--allow-same-base-url`
- JWT claim shapes match
- semantic observations match, including API-key rejection, device authorization pending errors,
  session listing behavior, and logout revocation

---

## Gate 3 - Local Single-Org Migration

Validate the default local path for existing single-org installs:

```bash
SURREAL_DATA_DIR="$SIBYL_GATE_DIR/single-org-surreal" \
moon run migrate-local-surreal -- \
  --archive "$SIBYL_GATE_DIR/single-org.tar.gz" \
  2>&1 | tee "$SIBYL_GATE_DIR/local-single-org.log"
```

Pass criteria:

- command auto-selects the only organization
- import and verify exit 0
- `$SIBYL_GATE_DIR/single-org-surreal/.sibyl-migrated` exists
- a later `moon run dev` does not re-warn for the migrated legacy setup

---

## Gate 4 - Local Multi-Org Migration

Validate the explicit-org path for local installs with more than one organization:

```bash
SURREAL_DATA_DIR="$SIBYL_GATE_DIR/multi-org-surreal" \
moon run migrate-local-surreal -- \
  --org-id <org-uuid> \
  --archive "$SIBYL_GATE_DIR/multi-org.tar.gz" \
  2>&1 | tee "$SIBYL_GATE_DIR/local-multi-org.log"
```

Pass criteria:

- command uses the requested organization
- import and verify exit 0
- `$SIBYL_GATE_DIR/multi-org-surreal/.sibyl-migrated` exists
- if `moon run dev -- --migrate-legacy` is run without `--org-id` on the same multi-org source, it
  prints the available org IDs and refuses to guess

---

## Release Decision

Ship the SurrealDB-first release only after all four gates pass or every deviation has an explicit
release-owner decision.

Before tagging:

- paste the rehearsal summary into `docs/guide/surrealdb-migration-release-notes.md`
- leave `SIBYL_AUTH_STORE=postgres` available for one compatibility release
- keep `docs/architecture/SURREALDB_PHASE3_BURNDOWN.md` as the Phase 3 deletion tracker
