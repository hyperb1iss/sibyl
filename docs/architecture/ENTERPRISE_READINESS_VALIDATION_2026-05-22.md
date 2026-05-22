# Enterprise Readiness Validation Packet

- Date: 2026-05-22
- Scope: implementation evidence for
  [`ENTERPRISE_READINESS_PLAN_2026-05-21.md`](ENTERPRISE_READINESS_PLAN_2026-05-21.md)
- Status: automated local validation is passing; external/manual validation is still required before
  claiming the full enterprise gate complete.

This packet is the review artifact for the OSS-side work. It records what the current repository
proves, which commands produced the proof, and which plan gates still need evidence from a real
identity provider, real MCP clients, or a Kubernetes cluster.

## Implemented Workstreams

| Workstream                         | Current evidence                                                                                                                                                                                                                                                                                                                  | Status                                     |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| W1 native OIDC authentication      | `apps/api/src/sibyl/auth/oidc.py`, `jit.py`, `silent_refresh.py`; OIDC routes in `apps/api/src/sibyl/api/routes/auth.py`; config in `apps/api/src/sibyl/config.py`; schema tables in `packages/python/sibyl-core/src/sibyl_core/backends/surreal/auth_schema.py`; API-key Argon2id in `apps/api/src/sibyl/auth/api_key_common.py` | Automated tests passing                    |
| W2 Sibyl Helm enterprise templates | `charts/sibyl/templates/httproute.yaml`, `networkpolicy.yaml`, `podsecurity.yaml`, `bootstrap-job.yaml`; OIDC and break-glass values in `charts/sibyl/values.yaml`; extra-provider fail check in `charts/sibyl/templates/_helpers.tpl`                                                                                            | Helm lint and template checks passing      |
| W3 SurrealDB wrapper               | `charts/surrealdb` with pinned chart dependency, RocksDB defaults, bootstrap job, snapshot CronJob, export CronJob, restore-drill CronJob                                                                                                                                                                                         | Helm lint/template and backup gate passing |
| W4 audit log UI/API                | `/api/admin/audit` and `/api/admin/audit/export` in `apps/api/src/sibyl/api/routes/admin.py`; page at `apps/web/src/app/(main)/settings/admin/audit/page.tsx`; tests for API list, CSV, JSON, member denial, and web UI rendering/export                                                                                          | Automated tests passing                    |
| W5 docs                            | `docs/users/login.md`, `cli-setup.md`, `mcp-setup.md`, `sharing-memory.md`; `docs/admin/installing.md`, `inviting-users.md`, `audit-log.md`, `backup-restore.md`, `break-glass.md`; VitePress nav links in `docs/.vitepress/config.mts`                                                                                           | Docs lint and build passing                |
| W6 hardening                       | Pod Security template, Trivy SBOM/CVE gate and Cosign signing in `.github/workflows/publish.yml`, LLM budget enforcement, personal-memory deletion purge, recall concurrency limiter, backup restore-to-scratch workflow                                                                                                          | Automated tests passing                    |

## Automated Receipts

These commands were run from `/Users/bliss/dev/sibyl`.

| Gate                     | Command                                                                                                                                                                                                                                                                                     | Receipt                                                                                                     |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Full monorepo check      | `moon run :check`                                                                                                                                                                                                                                                                           | `48 completed (28 cached)`; `api:test` included `1667 passed, 1 skipped, 13 deselected in 19.00s`           |
| Backup restore gate      | `moon run backup-restore-gate`                                                                                                                                                                                                                                                              | `20 passed` core, `17 passed` API, receipt `.moon/cache/backup-restore-gate/receipt.json` status `PASS`     |
| Chart lint               | `helm lint charts/sibyl charts/surrealdb`                                                                                                                                                                                                                                                   | `2 chart(s) linted, 0 chart(s) failed`                                                                      |
| Sibyl chart render       | `helm template enterprise charts/sibyl ...` with Gateway API, classic Ingress, NetworkPolicy, Pod Security, bootstrap, and Entra OIDC enabled                                                                                                                                               | rendered `Namespace`, five `NetworkPolicy` resources, `Ingress`, `HTTPRoute`, and bootstrap `Job`           |
| SurrealDB chart render   | `helm template enterprise-db charts/surrealdb ...` with snapshot, export, and restore-drill enabled                                                                                                                                                                                         | rendered `ServiceAccount`, `Role`, `RoleBinding`, `Service`, three `CronJob` resources, and bootstrap `Job` |
| Extra-provider guard     | `helm template bad charts/sibyl --set 'oidc.providers[0].name=github'`                                                                                                                                                                                                                      | failed with `oidc.extra_providers_enabled must be true before enabling GitHub or Google OIDC providers`     |
| Provider-neutral ingress | `helm template default charts/sibyl \| rg -i 'kong\|nginx\|traefik\|alb\|gce'`                                                                                                                                                                                                              | no provider-specific ingress defaults matched                                                               |
| Audit API focused tests  | `moon run --force api:test -- tests/test_routes_admin.py::test_admin_audit_lists_filtered_events tests/test_routes_admin.py::test_admin_audit_exports_csv tests/test_routes_admin.py::test_admin_audit_exports_json tests/test_routes_admin.py::test_admin_audit_route_rejects_member_role` | `4 passed in 2.43s`                                                                                         |
| Web audit UI coverage    | `moon run --force web:test`                                                                                                                                                                                                                                                                 | `34 passed` test files, `129 passed` tests                                                                  |
| External evidence gate   | `moon run enterprise-readiness-evidence-test`                                                                                                                                                                                                                                               | `23 passed in 0.80s`                                                                                        |
| Evidence item listing    | `moon run enterprise-readiness-evidence -- --list`                                                                                                                                                                                                                                          | listed 13 required external evidence items                                                                  |
| Rendered Helm capture    | `moon run enterprise-readiness-evidence -- --capture-rendered-helm-manifests`                                                                                                                                                                                                               | captured `rendered_helm_manifests` artifacts under `.moon/cache/enterprise-readiness-evidence`              |
| Package diff capture     | `moon run enterprise-readiness-evidence -- --capture-package-lock-diff '5dcdc2a0^'`                                                                                                                                                                                                         | captured `package_lock_diff` artifacts under `.moon/cache/enterprise-readiness-evidence`                    |
| Evidence status          | `moon run enterprise-readiness-evidence -- --status`                                                                                                                                                                                                                                        | expected exit `1`; `2 PASS, 11 INCOMPLETE`; `rendered_helm_manifests` and `package_lock_diff`: `PASS`       |
| Docs lint                | `moon run docs:lint`                                                                                                                                                                                                                                                                        | `All matched files use Prettier code style!`                                                                |
| Docs build               | `moon run --force docs:build`                                                                                                                                                                                                                                                               | `build complete`                                                                                            |

## Acceptance Gate Audit

| Gate                   | Evidence                                                                                                                                                                           | Result                                                                                                                                                                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auth contract          | Mocked OIDC, JIT provisioning, missing-role denial, silent refresh, API-key Argon2id, and PBKDF2 compatibility tests are present and passing through `api:test`.                   | Automated proof exists. Real Entra dev-tenant smoke is still missing.                                                                                                                                                             |
| Session                | OIDC callback and silent refresh tests assert the access cookie is set and `sibyl_refresh_token` is not set. Local-auth refresh-token tests remain in `tests/test_routes_auth.py`. | Automated proof exists.                                                                                                                                                                                                           |
| MCP compatibility      | `test_auth_mcp_token_verifier.py`, `test_mcp_oauth_session_refresh.py`, and MCP auth route tests cover JWT/API-key verifier and refresh-token compatibility.                       | Automated proof exists. Manual Cursor, Claude Code, and Claude Desktop smoke tests are still missing.                                                                                                                             |
| Chart                  | Helm lint, enterprise render, SurrealDB render, extra-provider failure, and provider-neutral ingress check are passing.                                                            | Automated proof exists.                                                                                                                                                                                                           |
| Data durability        | `moon run backup-restore-gate` passes and writes a PASS receipt. The SurrealDB chart renders snapshot, export, and restore-drill CronJobs.                                         | Automated proof exists. A live local-cluster restore drill with sampled `recall` is still missing.                                                                                                                                |
| Audit                  | API tests prove filtered list, CSV export, JSON export, and member denial. Web tests prove the audit page renders filters, rows, pagination, and JSON export.                      | Automated proof exists.                                                                                                                                                                                                           |
| Docs                   | User/admin docs exist, are linked from VitePress nav, and pass `docs:lint` plus `docs:build`.                                                                                      | Automated proof exists.                                                                                                                                                                                                           |
| Security review packet | This file is the packet shell with local receipts and explicit missing external evidence. `enterprise-readiness-evidence` validates the final external artifact bundle.            | Partial. Rendered Helm manifests and package lock diff are captured locally; still needs IdP role-claim evidence, backup drill receipt from a cluster, audit export sample, and image SBOM/signature receipts from a release run. |

## External Evidence Still Required

The following cannot be proven from this local repository alone:

1. Real Entra dev-tenant smoke for happy-path login and missing-role denial.
2. Manual MCP authentication smoke against Cursor, Claude Code, and Claude Desktop.
3. Local Kubernetes restore drill that imports an export into an ephemeral SurrealDB instance,
   verifies fixture row counts, and samples a `recall` query.
4. Security-review attachments from live systems: IdP role-claim screenshot or config export,
   cluster backup-drill receipt, audit export sample, release SBOM artifact, and Cosign signature
   receipt.

The local rendered Helm manifest evidence is captured in
`.moon/cache/enterprise-readiness-evidence/rendered_helm_manifests/`. The local package-lock diff
evidence is captured in `.moon/cache/enterprise-readiness-evidence/package_lock_diff/` from
`5dcdc2a0^..HEAD`; rerun the capture command if the auth dependency commits are rebased.

The final external evidence bundle is validated by:

```bash
moon run enterprise-readiness-evidence
```

Use
`moon run enterprise-readiness-evidence -- --init-template .moon/cache/enterprise-readiness-evidence`
to create the manifest skeleton and per-item `receipt.md` stubs. The template command preserves
existing evidence files by default; use `--force-template` only when intentionally regenerating the
bundle. After real artifacts are captured, run
`moon run enterprise-readiness-evidence -- --sync-hashes` to refresh the manifest checksums. The
`--status` mode reports every incomplete item at once before the final gate runs. The local rendered
Helm receipt can be captured with
`moon run enterprise-readiness-evidence -- --capture-rendered-helm-manifests`. The local package
receipt can be captured with
`moon run enterprise-readiness-evidence -- --capture-package-lock-diff <base-ref>`. The gate
requires every external item to be marked `PASS`, every artifact to exist under the evidence
directory, and every artifact hash to match the manifest.

Until those artifacts are captured, the code-side implementation is locally validated but the full
enterprise acceptance gate is not fully closed.
