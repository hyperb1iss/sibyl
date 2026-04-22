"""Operator-facing migration archive and rehearsal commands."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from sibyl.cli.common import console, error, info, run_async, success, warn
from sibyl.cli.db import (
    _find_pg_tool,
    _get_pg_connection_args,
    _get_pg_env,
    _restore_graph_payload,
    _restore_pg_sql,
)
from sibyl.config import settings
from sibyl.persistence.auth_archive import export_auth_archive_payload, restore_auth_archive_payload
from sibyl.persistence.content_archive import (
    export_content_archive_payload,
    restore_content_archive_payload,
)
from sibyl_core.migrate import (
    AUTH_FILENAME,
    CONTENT_FILENAME,
    GRAPH_FILENAME,
    POSTGRES_FILENAME,
    auth_payload_from_archive,
    build_manifest,
    content_payload_from_archive,
    effective_graph_counts,
    graph_payload_from_archive,
    load_archive,
    validate_archive,
    verify_graph_archive,
    write_archive,
)

app = typer.Typer(
    name="migrate",
    help="Migration archives, verification, and rehearsal tooling",
    no_args_is_help=True,
)

DEFAULT_REHEARSAL_BASE_URL = "http://localhost:3334"
DEFAULT_REHEARSAL_BASELINES_DIR = Path("baselines")
DEFAULT_REHEARSAL_MANIFEST = Path(".moon/cache/baseline-runtime-manifest.json")
DEFAULT_REHEARSAL_EMAIL = "baseline-corpus@sibyl.dev"
DEFAULT_REHEARSAL_PASSWORD = "baseline-corpus-password-secure-123!"  # noqa: S105
DEFAULT_CUTOVER_BENCH_LABEL = "cutover-acceptance"


def _load_valid_archive(source: Path):
    try:
        archive = load_archive(source)
    except Exception as exc:
        error(f"Archive load failed: {exc}")
        raise typer.Exit(code=1) from exc

    errors = validate_archive(archive)
    if errors:
        for issue in errors:
            warn(issue)
        error("Archive validation failed")
        raise typer.Exit(code=1)

    return archive


def _resolve_org_id(requested_org_id: str, archive_org_id: str) -> str:
    effective_org_id = requested_org_id or archive_org_id
    if not effective_org_id:
        error("Operation requires --org-id or an archive manifest organization_id")
        raise typer.Exit(code=1)
    return effective_org_id


def _print_verify_summary(result: object) -> None:
    expected_entities = getattr(result, "expected_entities", 0)
    actual_entities = getattr(result, "actual_entities", 0)
    expected_relationships = getattr(result, "expected_relationships", 0)
    actual_relationships = getattr(result, "actual_relationships", 0)
    expected_episodes = getattr(result, "expected_episodes", 0)
    actual_episodes = getattr(result, "actual_episodes", 0)
    expected_mentions = getattr(result, "expected_mentions", 0)
    actual_mentions = getattr(result, "actual_mentions", 0)
    validated_entity_ids = list(getattr(result, "validated_entity_ids", []))
    validated_episode_ids = list(getattr(result, "validated_episode_ids", []))
    errors = list(getattr(result, "errors", []))

    info(f"Entities: expected {expected_entities}, actual {actual_entities}")
    info(f"Relationships: expected {expected_relationships}, actual {actual_relationships}")
    info(f"Episodes: expected {expected_episodes}, actual {actual_episodes}")
    info(f"Mentions: expected {expected_mentions}, actual {actual_mentions}")
    if validated_entity_ids:
        info(f"Sampled entities: {len(validated_entity_ids)}")
    if validated_episode_ids:
        info(f"Sampled episodes: {len(validated_episode_ids)}")
    if errors:
        warn(f"Verification failed with {len(errors)} issue(s)")
        for issue in errors:
            console.print(f"  [dim]{issue}[/dim]")
        raise typer.Exit(code=1)


def _warn_if_postgres_payload_skipped(*, archive: object, restore_postgres: bool) -> None:
    if restore_postgres:
        return
    archive_files = getattr(archive, "files", {})
    if POSTGRES_FILENAME not in archive_files:
        return
    warn("Archive includes postgres.sql, but PostgreSQL restore is disabled")
    info("Pass --restore-postgres when you want a full Falkor/Postgres migration rehearsal")


def _warn_if_auth_payload_skipped(*, archive: object, restore_auth: bool) -> None:
    archive_files = getattr(archive, "files", {})
    if AUTH_FILENAME not in archive_files:
        return
    if not restore_auth:
        warn("Archive includes auth.json, but auth restore is disabled")
        info("Pass --restore-auth when you want the Surreal auth snapshot imported")
        return
    if settings.auth_store != "surreal":
        warn("Archive includes auth.json, but SIBYL_AUTH_STORE is not surreal")
        info("PostgreSQL remains the active auth source; auth.json will be skipped")


def _warn_if_content_payload_skipped(*, archive: object, restore_content: bool) -> None:
    archive_files = getattr(archive, "files", {})
    if CONTENT_FILENAME not in archive_files:
        return
    if not restore_content:
        warn("Archive includes content.json, but content restore is disabled")
        info("Pass --restore-content when you want the Surreal content snapshot imported")
        return
    if settings.store != "surreal":
        warn("Archive includes content.json, but SIBYL_STORE is not surreal")
        info("The active runtime is not using Surreal; content.json will be skipped")


async def _replay_baseline(
    *,
    base_url: str,
    baselines_dir: Path,
    email: str,
    password: str,
    manifest_path: Path,
) -> None:
    from tools.baselines.replay import replay_all

    await replay_all(
        base_url=base_url,
        baselines_dir=baselines_dir,
        email=email,
        password=password,
        manifest_path=manifest_path,
    )


def _run_moon_task(task: list[str], *, label: str) -> None:
    moon = shutil.which("moon")
    if moon is None:
        error("moon executable not found in PATH")
        raise typer.Exit(code=1)

    result = subprocess.run(  # noqa: S603 - trusted moon task invocation
        [moon, "run", *task],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error(f"{label} failed")
        if result.stdout.strip():
            console.print(f"[dim]{result.stdout.strip()}[/dim]")
        if result.stderr.strip():
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        raise typer.Exit(code=1)

    success(f"{label} passed")


def _print_cutover_plan(
    *,
    run_baseline: bool,
    run_bench_live_smoke: bool,
    run_bench_live: bool,
    reopen_writes: bool,
    manifest_path: Path,
) -> None:
    info("Cutover plan:")
    info("  1. Confirm legacy writes are frozen")
    info("  2. Import archive into the Surreal runtime")
    info("  3. Verify imported counts and sample entities")
    if run_baseline:
        info("  4. Replay deterministic runtime baseline")
    if run_bench_live_smoke:
        info("  5. Run bench-live-smoke acceptance check")
    if run_bench_live:
        info("  6. Run bench-live artifact capture")
    if reopen_writes:
        info("  7. Reopen writes on SurrealDB after operator acknowledgment")
    if run_baseline and not manifest_path.exists():
        warn(f"Baseline manifest not found yet: {manifest_path}")


async def _run_cutover_acceptance(
    *,
    archive: object,
    organization_id: str,
    sample_size: int,
    run_baseline: bool,
    base_url: str,
    baselines_dir: Path,
    email: str,
    password: str,
    manifest_path: Path,
    run_bench_live_smoke: bool,
    run_bench_live: bool,
    bench_label: str,
) -> None:
    result = await verify_graph_archive(
        archive,
        organization_id=organization_id,
        sample_size=sample_size,
    )
    _print_verify_summary(result)
    success("Archive verification passed")

    if run_baseline:
        info(f"Replaying deterministic baseline against {base_url}...")
        await _replay_baseline(
            base_url=base_url,
            baselines_dir=baselines_dir,
            email=email,
            password=password,
            manifest_path=manifest_path,
        )
        success("Baseline replay passed")

    if run_bench_live_smoke:
        info("Running bench-live-smoke acceptance check...")
        _run_moon_task(["bench-live-smoke"], label="bench-live-smoke")

    if run_bench_live:
        info("Running bench-live acceptance capture...")
        _run_moon_task(
            [
                "bench-live",
                "--",
                "--label",
                bench_label,
                "--metadata",
                "store=surreal",
                "--metadata",
                "mode=cutover",
            ],
            label="bench-live",
        )

    success("Acceptance suite passed while writes remain frozen")


def _load_graph_export(org_id: str) -> tuple[dict[str, object], bytes]:
    from dataclasses import asdict

    from sibyl_core.tools.admin import create_backup

    @run_async
    async def _export() -> tuple[dict[str, object], bytes]:
        result = await create_backup(organization_id=org_id)
        if not result.success or result.backup_data is None:
            msg = result.message or "graph export failed"
            raise RuntimeError(msg)
        payload = asdict(result.backup_data)
        encoded = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return payload, encoded

    return _export()


def _load_auth_export() -> tuple[dict[str, object], bytes]:
    @run_async
    async def _export() -> tuple[dict[str, object], bytes]:
        payload = await export_auth_archive_payload()
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        return payload, encoded

    return _export()


def _load_content_export() -> tuple[dict[str, object], bytes]:
    @run_async
    async def _export() -> tuple[dict[str, object], bytes]:
        payload = await export_content_archive_payload()
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        return payload, encoded

    return _export()


def _load_runtime_exports(
    *,
    include_auth: bool,
    include_content: bool,
) -> tuple[
    tuple[dict[str, object], bytes] | None,
    tuple[dict[str, object], bytes] | None,
]:
    @run_async
    async def _export() -> tuple[
        tuple[dict[str, object], bytes] | None,
        tuple[dict[str, object], bytes] | None,
    ]:
        auth_export: tuple[dict[str, object], bytes] | None = None
        content_export: tuple[dict[str, object], bytes] | None = None

        if include_auth:
            payload = await export_auth_archive_payload()
            auth_export = (
                payload,
                json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            )

        if include_content:
            payload = await export_content_archive_payload()
            content_export = (
                payload,
                json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            )

        return auth_export, content_export

    return _export()


def _restore_auth_payload(payload: dict[str, object], *, clean: bool) -> bool:
    @run_async
    async def _restore() -> bool:
        try:
            result = await restore_auth_archive_payload(payload, clean=clean)
            if result.success:
                success(
                    f"  Auth restored: {result.rows_restored} rows across {result.tables_restored} tables"
                )
            else:
                warn(f"  Auth restore completed with errors: {len(result.errors)}")
                for issue in result.errors[:10]:
                    console.print(f"  [dim]{issue}[/dim]")
            return result.success
        except Exception as exc:
            error(f"  Auth restore failed: {exc}")
            return False

    return _restore()


def _restore_content_payload(payload: dict[str, object], *, clean: bool) -> bool:
    @run_async
    async def _restore() -> bool:
        try:
            result = await restore_content_archive_payload(payload, clean=clean)
            if result.success:
                success(
                    "  Content restored: "
                    f"{result.rows_restored} rows across {result.tables_restored} tables"
                )
            else:
                warn(f"  Content restore completed with errors: {len(result.errors)}")
                for issue in result.errors[:10]:
                    console.print(f"  [dim]{issue}[/dim]")
            return result.success
        except Exception as exc:
            error(f"  Content restore failed: {exc}")
            return False

    return _restore()


def _run_pg_dump() -> bytes:
    cmd = [
        _find_pg_tool("pg_dump"),
        *_get_pg_connection_args(),
        "--format=plain",
        "--no-owner",
        "--no-acl",
    ]
    result = subprocess.run(  # noqa: S603 - trusted pg_dump command
        cmd,
        env=_get_pg_env(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"pg_dump failed: {stderr}")
    return result.stdout


@app.command("check")
def check_archive(
    source: Annotated[Path, typer.Argument(help="Archive .tar.gz or directory to inspect")],
) -> None:
    """Validate an archive and print its manifest summary."""
    archive = _load_valid_archive(source)

    manifest = archive.manifest
    info(f"Archive: {source}")
    info(f"Version: {manifest.version}")
    info(f"Source store: {manifest.source_store}")
    info(f"Organization: {manifest.organization_id or 'unknown'}")
    info(f"Created: {manifest.created_at or 'unknown'}")

    for name, file_manifest in sorted(manifest.files.items()):
        info(
            f"  {name} ({file_manifest.kind}, {file_manifest.size_bytes} bytes, {file_manifest.sha256[:12]})"
        )

    graph_payload = graph_payload_from_archive(archive)
    if graph_payload is not None:
        effective_counts = effective_graph_counts(graph_payload)
        info(
            "Graph counts: "
            f"{graph_payload.get('entity_count', len(graph_payload.get('entities', [])))} entities, "
            f"{graph_payload.get('relationship_count', len(graph_payload.get('relationships', [])))} relationships, "
            f"{graph_payload.get('episode_count', len(graph_payload.get('episodes', [])))} episodes, "
            f"{graph_payload.get('mention_count', len(graph_payload.get('mentions', [])))} mentions"
        )
        if (
            effective_counts["relationship_count"]
            != int(graph_payload.get("relationship_count", len(graph_payload.get("relationships", []))))
            or effective_counts["mention_count"]
            != int(graph_payload.get("mention_count", len(graph_payload.get("mentions", []))))
        ):
            info(
                "Effective restore counts: "
                f"{effective_counts['entity_count']} entities, "
                f"{effective_counts['relationship_count']} relationships, "
                f"{effective_counts['episode_count']} episodes, "
                f"{effective_counts['mention_count']} mentions"
            )

    auth_payload = auth_payload_from_archive(archive)
    if auth_payload is not None:
        row_counts = auth_payload.get("row_counts", {})
        total_rows = int(auth_payload.get("total_rows") or sum(int(v) for v in row_counts.values()))
        info(f"Auth tables: {len(row_counts)} tables, {total_rows} rows")

    content_payload = content_payload_from_archive(archive)
    if content_payload is not None:
        row_counts = content_payload.get("row_counts", {})
        total_rows = int(
            content_payload.get("total_rows") or sum(int(v) for v in row_counts.values())
        )
        info(f"Content tables: {len(row_counts)} tables, {total_rows} rows")
    success("Archive validation passed")


@app.command("export")
def export_archive(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output archive path"),
    ] = Path("sibyl_migration.tar.gz"),
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID for graph export"),
    ] = "",
    include_postgres: Annotated[
        bool,
        typer.Option("--include-postgres/--no-include-postgres", help="Include PostgreSQL dump"),
    ] = True,
    include_graph: Annotated[
        bool,
        typer.Option("--include-graph/--skip-graph", help="Include graph runtime export"),
    ] = True,
    include_auth: Annotated[
        bool,
        typer.Option("--include-auth/--skip-auth", help="Include auth/RBAC snapshot"),
    ] = True,
    include_content: Annotated[
        bool,
        typer.Option("--include-content/--skip-content", help="Include content/operations snapshot"),
    ] = False,
) -> None:
    """Export a manifest-driven migration archive from the active store."""
    if not include_postgres and not include_graph and not include_auth and not include_content:
        error(
            "Select at least one payload: "
            "--include-postgres, --include-graph, --include-auth, or --include-content"
        )
        raise typer.Exit(code=1)

    if include_graph and not org_id:
        error("--org-id is required when exporting graph runtime data")
        raise typer.Exit(code=1)

    files: dict[str, bytes] = {}
    file_metadata: dict[str, dict[str, object]] = {}
    archive_metadata: dict[str, object] = {}

    if include_postgres:
        info("Exporting PostgreSQL...")
        files[POSTGRES_FILENAME] = _run_pg_dump()
        file_metadata[POSTGRES_FILENAME] = {"kind": "postgres"}

    if include_graph:
        info(f"Exporting graph runtime from {settings.store} store...")
        graph_payload, graph_bytes = _load_graph_export(org_id)
        effective_counts = effective_graph_counts(graph_payload)
        files[GRAPH_FILENAME] = graph_bytes
        file_metadata[GRAPH_FILENAME] = {
            "kind": "graph",
            "entity_count": int(graph_payload.get("entity_count", 0)),
            "relationship_count": int(graph_payload.get("relationship_count", 0)),
            "episode_count": int(graph_payload.get("episode_count", 0)),
            "mention_count": int(graph_payload.get("mention_count", 0)),
            "effective_relationship_count": effective_counts["relationship_count"],
            "effective_mention_count": effective_counts["mention_count"],
        }
        archive_metadata["graph_entity_count"] = int(graph_payload.get("entity_count", 0))
        archive_metadata["graph_relationship_count"] = int(
            graph_payload.get("relationship_count", 0)
        )
        archive_metadata["graph_episode_count"] = int(graph_payload.get("episode_count", 0))
        archive_metadata["graph_mention_count"] = int(graph_payload.get("mention_count", 0))
        archive_metadata["graph_effective_relationship_count"] = effective_counts[
            "relationship_count"
        ]
        archive_metadata["graph_effective_mention_count"] = effective_counts["mention_count"]

    auth_export: tuple[dict[str, object], bytes] | None = None
    content_export: tuple[dict[str, object], bytes] | None = None
    if include_auth and include_content:
        auth_export, content_export = _load_runtime_exports(
            include_auth=True,
            include_content=True,
        )

    if include_auth:
        info("Exporting auth/RBAC snapshot from PostgreSQL...")
        if auth_export is None:
            auth_export = _load_auth_export()
        auth_payload, auth_bytes = auth_export
        row_counts = dict(auth_payload.get("row_counts", {}))
        files[AUTH_FILENAME] = auth_bytes
        file_metadata[AUTH_FILENAME] = {
            "kind": "auth",
            "table_count": len(row_counts),
            "total_rows": int(auth_payload.get("total_rows", 0)),
        }
        archive_metadata["auth_table_count"] = len(row_counts)
        archive_metadata["auth_total_rows"] = int(auth_payload.get("total_rows", 0))

    if include_content:
        info("Exporting content/operations snapshot from PostgreSQL...")
        if content_export is None:
            content_export = _load_content_export()
        content_payload, content_bytes = content_export
        row_counts = dict(content_payload.get("row_counts", {}))
        files[CONTENT_FILENAME] = content_bytes
        file_metadata[CONTENT_FILENAME] = {
            "kind": "content",
            "table_count": len(row_counts),
            "total_rows": int(content_payload.get("total_rows", 0)),
        }
        archive_metadata["content_table_count"] = len(row_counts)
        archive_metadata["content_total_rows"] = int(content_payload.get("total_rows", 0))

    manifest = build_manifest(
        organization_id=org_id,
        source_store=settings.store,
        files=files,
        file_metadata=file_metadata,
        metadata=archive_metadata,
    )
    write_archive(output, manifest=manifest, files=files)

    success(f"Migration archive written to {output}")


@app.command("import")
def import_archive(
    source: Annotated[Path, typer.Argument(help="Archive .tar.gz or directory to import")],
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID override"),
    ] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Clear the target graph before import"),
    ] = False,
    restore_postgres: Annotated[
        bool,
        typer.Option("--restore-postgres", help="Restore postgres.sql before graph import"),
    ] = False,
    restore_graph: Annotated[
        bool,
        typer.Option("--restore-graph/--skip-graph", help="Restore graph payload"),
    ] = True,
    restore_auth: Annotated[
        bool,
        typer.Option("--restore-auth/--skip-auth", help="Restore auth payload into Surreal auth storage"),
    ] = True,
    restore_content: Annotated[
        bool,
        typer.Option(
            "--restore-content/--skip-content",
            help="Restore content payload into Surreal content storage",
        ),
    ] = True,
) -> None:
    """Import a manifest archive into the active store."""
    archive = _load_valid_archive(source)
    effective_org_id = _resolve_org_id(org_id, archive.manifest.organization_id) if restore_graph else ""

    if restore_postgres and POSTGRES_FILENAME not in archive.files:
        error("Archive does not contain postgres.sql")
        raise typer.Exit(code=1)

    if restore_graph and GRAPH_FILENAME not in archive.files:
        error("Archive does not contain graph.json")
        raise typer.Exit(code=1)
    _warn_if_postgres_payload_skipped(archive=archive, restore_postgres=restore_postgres)
    _warn_if_auth_payload_skipped(archive=archive, restore_auth=restore_auth)
    _warn_if_content_payload_skipped(archive=archive, restore_content=restore_content)

    if not yes:
        warn("This will import archive data into the active runtime.")
        if not typer.confirm("Continue?"):
            info("Cancelled")
            return

    if restore_postgres:
        info("Restoring PostgreSQL payload...")
        _restore_pg_sql(archive.files[POSTGRES_FILENAME].decode("utf-8"), clean)

    if restore_auth and AUTH_FILENAME in archive.files and settings.auth_store == "surreal":
        info("Restoring auth payload into Surreal auth storage...")
        payload = auth_payload_from_archive(archive)
        if payload is None or not _restore_auth_payload(payload, clean=clean):
            error("Auth import failed")
            raise typer.Exit(code=1)

    if restore_content and CONTENT_FILENAME in archive.files and settings.store == "surreal":
        info("Restoring content payload into Surreal content storage...")
        payload = content_payload_from_archive(archive)
        if payload is None or not _restore_content_payload(payload, clean=clean):
            error("Content import failed")
            raise typer.Exit(code=1)

    if restore_graph:
        info(f"Restoring graph payload into {settings.store} store...")
        payload = json.loads(archive.files[GRAPH_FILENAME].decode("utf-8"))
        if not _restore_graph_payload(payload, effective_org_id, clean=clean):
            error("Graph import failed")
            raise typer.Exit(code=1)

    success("Archive import complete")


@app.command("verify")
def verify_archive(
    source: Annotated[Path, typer.Argument(help="Archive .tar.gz or directory to verify")],
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID override"),
    ] = "",
    sample_size: Annotated[
        int,
        typer.Option("--sample-size", help="How many entity IDs to spot-check"),
    ] = 10,
) -> None:
    """Verify an archive against the active runtime."""
    archive = _load_valid_archive(source)
    effective_org_id = _resolve_org_id(org_id, archive.manifest.organization_id)

    @run_async
    async def _verify() -> None:
        result = await verify_graph_archive(
            archive,
            organization_id=effective_org_id,
            sample_size=sample_size,
        )
        _print_verify_summary(result)
        success("Archive verification passed")

    _verify()


@app.command("rehearse")
def rehearse_archive(
    source: Annotated[Path, typer.Argument(help="Archive .tar.gz or directory to rehearse")],
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID override"),
    ] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Clear the target graph before import"),
    ] = True,
    restore_postgres: Annotated[
        bool,
        typer.Option("--restore-postgres", help="Restore postgres.sql before graph import"),
    ] = False,
    restore_auth: Annotated[
        bool,
        typer.Option("--restore-auth/--skip-auth", help="Restore auth payload into Surreal auth storage"),
    ] = True,
    restore_content: Annotated[
        bool,
        typer.Option(
            "--restore-content/--skip-content",
            help="Restore content payload into Surreal content storage",
        ),
    ] = True,
    run_baseline: Annotated[
        bool,
        typer.Option("--run-baseline/--skip-baseline", help="Replay the deterministic runtime baseline"),
    ] = True,
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Base URL for baseline replay"),
    ] = DEFAULT_REHEARSAL_BASE_URL,
    baselines_dir: Annotated[
        Path,
        typer.Option("--baselines-dir", help="Directory containing baseline case files"),
    ] = DEFAULT_REHEARSAL_BASELINES_DIR,
    manifest_path: Annotated[
        Path,
        typer.Option("--manifest-path", help="Runtime baseline manifest from `moon run baseline-seed`"),
    ] = DEFAULT_REHEARSAL_MANIFEST,
    email: Annotated[
        str,
        typer.Option("--email", help="Baseline user email"),
    ] = DEFAULT_REHEARSAL_EMAIL,
    password: Annotated[
        str,
        typer.Option("--password", help="Baseline user password"),
    ] = DEFAULT_REHEARSAL_PASSWORD,
    sample_size: Annotated[
        int,
        typer.Option("--sample-size", help="How many entity IDs to spot-check during verify"),
    ] = 10,
) -> None:
    """Run an import + verify + baseline smoke rehearsal against the active store."""
    archive = _load_valid_archive(source)
    effective_org_id = _resolve_org_id(org_id, archive.manifest.organization_id)

    if restore_postgres and POSTGRES_FILENAME not in archive.files:
        error("Archive does not contain postgres.sql")
        raise typer.Exit(code=1)
    if GRAPH_FILENAME not in archive.files:
        error("Archive does not contain graph.json")
        raise typer.Exit(code=1)
    _warn_if_postgres_payload_skipped(archive=archive, restore_postgres=restore_postgres)
    _warn_if_auth_payload_skipped(archive=archive, restore_auth=restore_auth)
    _warn_if_content_payload_skipped(archive=archive, restore_content=restore_content)
    if run_baseline and not manifest_path.exists():
        error(f"Baseline manifest not found: {manifest_path}")
        raise typer.Exit(code=1)

    if not yes:
        warn("This will import archive data and run rehearsal checks against the active runtime.")
        if not typer.confirm("Continue?"):
            info("Cancelled")
            return

    if restore_postgres:
        info("Restoring PostgreSQL payload...")
        _restore_pg_sql(archive.files[POSTGRES_FILENAME].decode("utf-8"), clean)

    if restore_auth and AUTH_FILENAME in archive.files and settings.auth_store == "surreal":
        info("Restoring auth payload into Surreal auth storage...")
        payload = auth_payload_from_archive(archive)
        if payload is None or not _restore_auth_payload(payload, clean=clean):
            error("Auth import failed")
            raise typer.Exit(code=1)

    if restore_content and CONTENT_FILENAME in archive.files and settings.store == "surreal":
        info("Restoring content payload into Surreal content storage...")
        payload = content_payload_from_archive(archive)
        if payload is None or not _restore_content_payload(payload, clean=clean):
            error("Content import failed")
            raise typer.Exit(code=1)

    info(f"Restoring graph payload into {settings.store} store...")
    payload = json.loads(archive.files[GRAPH_FILENAME].decode("utf-8"))
    if not _restore_graph_payload(payload, effective_org_id, clean=clean):
        error("Graph import failed")
        raise typer.Exit(code=1)

    @run_async
    async def _rehearse() -> None:
        result = await verify_graph_archive(
            archive,
            organization_id=effective_org_id,
            sample_size=sample_size,
        )
        _print_verify_summary(result)
        success("Archive verification passed")

        if run_baseline:
            info(f"Replaying deterministic baseline against {base_url}...")
            await _replay_baseline(
                base_url=base_url,
                baselines_dir=baselines_dir,
                email=email,
                password=password,
                manifest_path=manifest_path,
            )
            success("Baseline replay passed")

        success("Migration rehearsal passed")

    _rehearse()


@app.command("cutover")
def cutover_archive(
    source: Annotated[Path, typer.Argument(help="Archive .tar.gz or directory to cut over from")],
    org_id: Annotated[
        str,
        typer.Option("--org-id", help="Organization UUID override"),
    ] = "",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the cutover plan without importing data"),
    ] = False,
    write_freeze_confirmed: Annotated[
        bool,
        typer.Option(
            "--write-freeze-confirmed",
            help="Acknowledge that legacy writes are frozen before cutover begins",
        ),
    ] = False,
    clean: Annotated[
        bool,
        typer.Option("--clean", help="Clear the target graph before import"),
    ] = True,
    restore_postgres: Annotated[
        bool,
        typer.Option("--restore-postgres", help="Restore postgres.sql before graph import"),
    ] = False,
    restore_auth: Annotated[
        bool,
        typer.Option("--restore-auth/--skip-auth", help="Restore auth payload into Surreal auth storage"),
    ] = True,
    restore_content: Annotated[
        bool,
        typer.Option(
            "--restore-content/--skip-content",
            help="Restore content payload into Surreal content storage",
        ),
    ] = True,
    run_baseline: Annotated[
        bool,
        typer.Option("--run-baseline/--skip-baseline", help="Replay the deterministic runtime baseline"),
    ] = True,
    run_bench_live_smoke: Annotated[
        bool,
        typer.Option("--run-bench-live-smoke", help="Run the live smoke bench after baseline replay"),
    ] = False,
    run_bench_live: Annotated[
        bool,
        typer.Option("--run-bench-live", help="Run the artifact-producing live bench after acceptance smoke"),
    ] = False,
    bench_label: Annotated[
        str,
        typer.Option("--bench-label", help="Label used when running bench-live"),
    ] = DEFAULT_CUTOVER_BENCH_LABEL,
    base_url: Annotated[
        str,
        typer.Option("--base-url", help="Base URL for baseline replay"),
    ] = DEFAULT_REHEARSAL_BASE_URL,
    baselines_dir: Annotated[
        Path,
        typer.Option("--baselines-dir", help="Directory containing baseline case files"),
    ] = DEFAULT_REHEARSAL_BASELINES_DIR,
    manifest_path: Annotated[
        Path,
        typer.Option("--manifest-path", help="Runtime baseline manifest from `moon run baseline-seed`"),
    ] = DEFAULT_REHEARSAL_MANIFEST,
    email: Annotated[
        str,
        typer.Option("--email", help="Baseline user email"),
    ] = DEFAULT_REHEARSAL_EMAIL,
    password: Annotated[
        str,
        typer.Option("--password", help="Baseline user password"),
    ] = DEFAULT_REHEARSAL_PASSWORD,
    sample_size: Annotated[
        int,
        typer.Option("--sample-size", help="How many entity IDs to spot-check during verify"),
    ] = 10,
    reopen_writes: Annotated[
        bool,
        typer.Option("--reopen-writes", help="Mark the acceptance gate complete and permit writes on SurrealDB"),
    ] = False,
    acknowledge_no_instant_rollback: Annotated[
        bool,
        typer.Option(
            "--acknowledge-no-instant-rollback",
            help="Acknowledge that rollback is no longer promised once writes reopen on SurrealDB",
        ),
    ] = False,
) -> None:
    """Run the explicit Surreal cutover acceptance gate on a validated archive."""
    if settings.store != "surreal":
        error("Cutover must run with SIBYL_STORE=surreal on the target runtime")
        raise typer.Exit(code=1)

    archive = _load_valid_archive(source)
    effective_org_id = _resolve_org_id(org_id, archive.manifest.organization_id)

    if restore_postgres and POSTGRES_FILENAME not in archive.files:
        error("Archive does not contain postgres.sql")
        raise typer.Exit(code=1)
    if GRAPH_FILENAME not in archive.files:
        error("Archive does not contain graph.json")
        raise typer.Exit(code=1)
    _warn_if_postgres_payload_skipped(archive=archive, restore_postgres=restore_postgres)
    _warn_if_auth_payload_skipped(archive=archive, restore_auth=restore_auth)
    _warn_if_content_payload_skipped(archive=archive, restore_content=restore_content)

    warn("Rollback is supported only until writes reopen on SurrealDB.")
    warn("This command does not unfreeze or freeze writes for you; it enforces the operator gate.")

    if dry_run:
        _print_cutover_plan(
            run_baseline=run_baseline,
            run_bench_live_smoke=run_bench_live_smoke,
            run_bench_live=run_bench_live,
            reopen_writes=reopen_writes,
            manifest_path=manifest_path,
        )
        success("Cutover dry run complete")
        return

    if not write_freeze_confirmed:
        error("Cutover requires --write-freeze-confirmed before import begins")
        raise typer.Exit(code=1)
    if run_baseline and not manifest_path.exists():
        error(f"Baseline manifest not found: {manifest_path}")
        raise typer.Exit(code=1)

    if not yes:
        warn("This will import the archive and run acceptance checks while writes remain frozen.")
        if not typer.confirm("Continue?"):
            info("Cancelled")
            return

    if restore_postgres:
        info("Restoring PostgreSQL payload...")
        _restore_pg_sql(archive.files[POSTGRES_FILENAME].decode("utf-8"), clean)

    if restore_auth and AUTH_FILENAME in archive.files and settings.auth_store == "surreal":
        info("Importing auth payload into the Surreal auth runtime...")
        payload = auth_payload_from_archive(archive)
        if payload is None or not _restore_auth_payload(payload, clean=clean):
            error("Auth import failed")
            raise typer.Exit(code=1)

    if restore_content and CONTENT_FILENAME in archive.files and settings.store == "surreal":
        info("Importing content payload into the Surreal content runtime...")
        payload = content_payload_from_archive(archive)
        if payload is None or not _restore_content_payload(payload, clean=clean):
            error("Content import failed")
            raise typer.Exit(code=1)

    info("Importing graph payload into the Surreal runtime...")
    payload = json.loads(archive.files[GRAPH_FILENAME].decode("utf-8"))
    if not _restore_graph_payload(payload, effective_org_id, clean=clean):
        error("Graph import failed")
        raise typer.Exit(code=1)
    run_async(_run_cutover_acceptance)(
        archive=archive,
        organization_id=effective_org_id,
        sample_size=sample_size,
        run_baseline=run_baseline,
        base_url=base_url,
        baselines_dir=baselines_dir,
        email=email,
        password=password,
        manifest_path=manifest_path,
        run_bench_live_smoke=run_bench_live_smoke,
        run_bench_live=run_bench_live,
        bench_label=bench_label,
    )

    if not reopen_writes:
        warn("Writes remain frozen. Rollback is still supported at this point.")
        info(
            "Rerun with --reopen-writes --acknowledge-no-instant-rollback "
            "after final operator sign-off."
        )
        return

    if not acknowledge_no_instant_rollback:
        error("Refusing to reopen writes without --acknowledge-no-instant-rollback")
        raise typer.Exit(code=1)

    warn("Rollback is no longer promised once writes reopen on SurrealDB.")
    success("Acceptance gate complete. Writes may now be reopened on the Surreal runtime.")
