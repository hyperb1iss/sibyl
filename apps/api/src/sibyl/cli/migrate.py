"""Operator-facing migration archive and rehearsal commands."""

from __future__ import annotations

import json
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
from sibyl_core.migrate import (
    GRAPH_FILENAME,
    POSTGRES_FILENAME,
    build_manifest,
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
    validated_entity_ids = list(getattr(result, "validated_entity_ids", []))
    errors = list(getattr(result, "errors", []))

    info(f"Entities: expected {expected_entities}, actual {actual_entities}")
    info(f"Relationships: expected {expected_relationships}, actual {actual_relationships}")
    if validated_entity_ids:
        info(f"Sampled entities: {len(validated_entity_ids)}")
    if errors:
        warn(f"Verification failed with {len(errors)} issue(s)")
        for issue in errors:
            console.print(f"  [dim]{issue}[/dim]")
        raise typer.Exit(code=1)


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
        info(
            "Graph counts: "
            f"{graph_payload.get('entity_count', len(graph_payload.get('entities', [])))} entities, "
            f"{graph_payload.get('relationship_count', len(graph_payload.get('relationships', [])))} relationships"
        )
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
) -> None:
    """Export a manifest-driven migration archive from the active store."""
    if not include_postgres and not include_graph:
        error("Select at least one payload: --include-postgres or --include-graph")
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
        files[GRAPH_FILENAME] = graph_bytes
        file_metadata[GRAPH_FILENAME] = {
            "kind": "graph",
            "entity_count": int(graph_payload.get("entity_count", 0)),
            "relationship_count": int(graph_payload.get("relationship_count", 0)),
        }
        archive_metadata["graph_entity_count"] = int(graph_payload.get("entity_count", 0))
        archive_metadata["graph_relationship_count"] = int(
            graph_payload.get("relationship_count", 0)
        )

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

    if not yes:
        warn("This will import archive data into the active runtime.")
        if not typer.confirm("Continue?"):
            info("Cancelled")
            return

    if restore_postgres:
        info("Restoring PostgreSQL payload...")
        _restore_pg_sql(archive.files[POSTGRES_FILENAME].decode("utf-8"), clean)

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
