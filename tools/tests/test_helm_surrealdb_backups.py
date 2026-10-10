"""Run the surrealdb chart's rendered backup jobs against a fake HTTP API.

The export and restore-drill CronJobs are shell scripts embedded in the
chart. These tests render them with helm and execute them unchanged, with
real jq and sha256sum, while a fake curl (fixtures/fake_surreal_curl.py)
answers with SurrealDB 3.x response shapes. That proves the script logic
in CI without a database; the shapes themselves were taken from a live
v3.2.4 server.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_CURL = Path(__file__).resolve().parent / "fixtures" / "fake_surreal_curl.py"

_HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(
    _HELM is None or shutil.which("jq") is None or shutil.which("sha256sum") is None,
    reason="helm, jq and sha256sum are required to run the chart's ops scripts",
)


def _render_job(job: str, *overrides: str) -> tuple[str, dict[str, str]]:
    """Return one ops CronJob's shell script and its literal env values."""
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "export.enabled=true",
            "--set",
            "restoreDrill.enabled=true",
            *overrides,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for document in yaml.safe_load_all(rendered.stdout):
        if not document or document.get("kind") != "CronJob":
            continue
        if not document["metadata"]["name"].endswith(f"-{job}"):
            continue
        container = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item["value"] for item in container["env"] if "value" in item}
        return container["args"][0], env
    pytest.fail(f"{job} CronJob not rendered")


class OpsHarness:
    """A scratch root with a backups directory and a fake SurrealDB API."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.backups = root / "backups"
        self.backups.mkdir()
        (root / "tmp").mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        shim = self.bin / "curl"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CURL}" "$@"\n')
        shim.chmod(0o755)
        self.state_path = root / "surreal-state.json"
        self.receipt_path = root / "receipt.json"

    def serve(
        self,
        *,
        source: dict[str, dict[str, dict[str, int]]] | None = None,
        restore: dict[str, dict[str, dict[str, int]]] | None = None,
        **behaviour: list[str],
    ) -> None:
        servers: dict[str, object] = {
            "http://restore": {"user": "drill:drill", "namespaces": restore or {}},
        }
        if source is not None:
            servers["http://source"] = {
                "user": "root:secret",
                "version": "surrealdb-3.2.4",
                "namespaces": source,
            }
        self.state_path.write_text(json.dumps({"servers": servers, **behaviour}))

    def server(self, endpoint: str) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())["servers"][endpoint]

    def run(self, job: str, *overrides: str) -> subprocess.CompletedProcess[str]:
        script, rendered_env = _render_job(job, *overrides)
        env = {
            **rendered_env,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "TMPDIR": str(self.root / "tmp"),
            "FAKE_SURREAL_STATE": str(self.state_path),
            "SURREAL_ENDPOINT": "http://source",
            "SURREAL_PASS": "secret",
            "SURREAL_RESTORE_ENDPOINT": "http://restore",
            "SIBYL_EXPORT_DESTINATION_PATH": str(self.backups),
            "SIBYL_RESTORE_SOURCE_PATH": str(self.backups),
            "SIBYL_RESTORE_RECEIPT_PATH": str(self.receipt_path),
            "POD_NAMESPACE": "sibyl",
        }
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-ec", script],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


@pytest.fixture
def harness(tmp_path: Path) -> OpsHarness:
    return OpsHarness(tmp_path)


def test_helm_restore_drill_notifies_on_every_failure(harness: OpsHarness) -> None:
    """A drill that finds nothing to restore fails through an explicit
    exit, which busybox ash never reports to an ERR trap."""
    harness.serve()
    result = harness.run(
        "restore-drill",
        "--set-string",
        'restoreDrill.failureNotification.command=echo notified >> "$HOME/notified.log"',
    )

    assert result.returncode != 0
    assert (harness.root / "notified.log").read_text() == "notified\n"
    assert not harness.receipt_path.exists()


ORG_A = "org_0b8e5a3c1f2d4e6a9b7c8d9e0f1a2b3c"
ORG_B = "org_5d4c3b2a1f0e4d9c8b7a6f5e4d3c2b1a"


def _source() -> dict[str, dict[str, dict[str, int]]]:
    """A server shaped like a live Sibyl: auth, content, two org graphs,
    and SurrealDB's own empty default namespace."""
    return {
        "main": {"main": {}},
        "sibyl_auth": {"auth": {"users": 2, "organizations": 2, "api_keys": 0}},
        "sibyl_content": {"content": {"documents": 3, "raw_captures": 0}},
        ORG_A: {"graph": {"entity": 7, "relates_to": 6, "schema_version": 1}},
        ORG_B: {"graph": {"entity": 4, "relates_to": 3, "schema_version": 1}},
    }


SOURCE_DATABASES = sum(len(databases) for databases in _source().values())


def _run_dirs(harness: OpsHarness) -> list[Path]:
    return sorted(path for path in harness.backups.iterdir() if path.is_dir())


def _manifest(harness: OpsHarness) -> dict[str, Any]:
    (run_dir,) = _run_dirs(harness)
    return json.loads((run_dir / "manifest.json").read_text())


def test_helm_export_discovers_every_namespace_without_a_list(harness: OpsHarness) -> None:
    """Org graphs live in org_<uuid> namespaces created at run time; the
    static `databases` list (bootstrap only) must not limit the export."""
    harness.serve(source=_source())
    result = harness.run(
        "export",
        "--set",
        "databases[0].namespace=sibyl_auth",
        "--set",
        "databases[0].database=auth",
    )
    assert result.returncode == 0, result.stderr

    manifest = _manifest(harness)
    (run_dir,) = _run_dirs(harness)
    assert run_dir.name.startswith("sibyl-")
    assert manifest["format"] == "sibyl-surrealdb-export"
    assert manifest["version"] == 1
    assert manifest["server_version"] == "surrealdb-3.2.4"
    assert manifest["created_at"].endswith("Z")
    exported = {(entry["namespace"], entry["database"]): entry for entry in manifest["databases"]}
    assert set(exported) == {
        ("main", "main"),
        ("sibyl_auth", "auth"),
        ("sibyl_content", "content"),
        (ORG_A, "graph"),
        (ORG_B, "graph"),
    }
    for (namespace, database), entry in exported.items():
        data = (run_dir / entry["file"]).read_bytes()
        assert entry["file"] == f"{namespace}.{database}.surql"
        assert entry["bytes"] == len(data) > 0
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["tables"] == _source()[namespace][database]
        assert entry["rows"] == sum(_source()[namespace][database].values())


def test_helm_export_and_drill_scripts_ignore_the_bootstrap_list() -> None:
    default_export, _ = _render_job("export")
    default_drill, _ = _render_job("restore-drill")
    custom = ("--set", "databases[0].namespace=only_this", "--set", "databases[0].database=db")

    assert _render_job("export", *custom)[0] == default_export
    assert _render_job("restore-drill", *custom)[0] == default_drill


def test_helm_export_hooks_cover_every_file_and_the_manifest(harness: OpsHarness) -> None:
    harness.serve(source=_source())
    result = harness.run(
        "export",
        "--set",
        "export.encryption.enabled=true",
        "--set-string",
        'export.encryption.command=echo "$SIBYL_EXPORT_FILE" >> "$HOME/encrypted.log"',
        "--set-string",
        'export.syncCommand=echo "$SIBYL_EXPORT_RUN_DIR" >> "$HOME/synced.log"',
    )
    assert result.returncode == 0, result.stderr

    (run_dir,) = _run_dirs(harness)
    encrypted = (harness.root / "encrypted.log").read_text().splitlines()
    files = sorted(path.name for path in run_dir.glob("*.surql"))
    assert len(files) == len(_manifest(harness)["databases"]) == SOURCE_DATABASES
    assert sorted(Path(line).name for line in encrypted[:-1]) == files
    assert encrypted[-1] == str(run_dir / "manifest.json")
    assert (harness.root / "synced.log").read_text().splitlines() == [str(run_dir)]


@pytest.mark.parametrize(
    ("source", "behaviour", "message"),
    [
        ({}, {}, "discovery found no databases"),
        ({"ns_only": {}}, {}, "discovery found no databases"),
        (_source(), {"empty_exports": [f"{ORG_B}/graph"]}, f"empty export for {ORG_B}/graph"),
        ({"bad name": {"db": {"t": 1}}}, {}, "not a plain SurrealDB identifier"),
        ({"ok": {"bad-db": {"t": 1}}}, {}, "not a plain SurrealDB identifier"),
    ],
    ids=["no-namespaces", "no-databases", "empty-file", "unsafe-namespace", "unsafe-database"],
)
def test_helm_export_fails_loudly_instead_of_recording_a_partial_backup(
    harness: OpsHarness,
    source: dict[str, dict[str, dict[str, int]]],
    behaviour: dict[str, list[str]],
    message: str,
) -> None:
    harness.serve(source=source, **behaviour)
    result = harness.run("export")

    assert result.returncode != 0
    assert message in result.stderr
    assert not list(harness.backups.glob("*/manifest.json"))


def _export_then_reset(harness: OpsHarness, **behaviour: list[str]) -> Path:
    harness.serve(source=_source())
    exported = harness.run("export")
    assert exported.returncode == 0, exported.stderr
    harness.serve(**behaviour)
    (run_dir,) = _run_dirs(harness)
    return run_dir


def test_helm_restore_drill_restores_every_database_in_the_newest_manifest(
    harness: OpsHarness,
) -> None:
    run_dir = _export_then_reset(harness)
    stale = harness.backups / "sibyl-20000101000000"
    stale.mkdir()
    (stale / "manifest.json").write_text("not json: an older run the drill must not read")
    (harness.backups / "sibyl-99991231235959").mkdir()  # newer, but never completed

    result = harness.run("restore-drill")
    assert result.returncode == 0, result.stderr

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["status"] == "PASS"
    assert receipt["manifest"]["path"] == str(run_dir / "manifest.json")
    assert receipt["manifest"]["databases"] == SOURCE_DATABASES
    by_name = {(item["namespace"], item["database"]): item for item in receipt["databases"]}
    assert set(by_name) == {(ns, db) for ns, dbs in _source().items() for db in dbs}
    for (namespace, database), item in by_name.items():
        expected = _source()[namespace][database]
        assert item["status"] == "PASS"
        assert item["rows_exported"] == item["rows_restored"] == sum(expected.values())
        assert item["tables"] == {
            table: {"exported": rows, "restored": rows} for table, rows in expected.items()
        }
    assert receipt["row_counts"] == {"sibyl_auth.auth.users": {"expected": 2, "actual": 2}}
    assert harness.server("http://restore")["namespaces"] == _source()


@pytest.mark.parametrize(
    ("damage", "behaviour", "message"),
    [
        ("tamper", {}, "does not match the manifest's size and sha256"),
        ("delete", {}, "is missing"),
        (None, {"drop_on_import": [f"{ORG_A}/graph"]}, "restored no rows; the export counted 14"),
    ],
    ids=["checksum", "missing-file", "empty-restore"],
)
def test_helm_restore_drill_fails_and_notifies_on_a_bad_database(
    harness: OpsHarness,
    damage: str | None,
    behaviour: dict[str, list[str]],
    message: str,
) -> None:
    run_dir = _export_then_reset(harness, **behaviour)
    target = run_dir / f"{ORG_A}.graph.surql"
    if damage == "tamper":
        target.write_text(target.read_text() + "\n-- tampered\n")
    elif damage == "delete":
        target.unlink()

    result = harness.run(
        "restore-drill",
        "--set-string",
        'restoreDrill.failureNotification.command=echo notified >> "$HOME/notified.log"',
    )

    assert result.returncode != 0
    assert f"restore failed for {ORG_A}/graph: " in result.stderr
    assert message in result.stderr
    assert f"restore failed for {ORG_B}/graph" not in result.stderr
    assert (harness.root / "notified.log").read_text() == "notified\n"
    assert not harness.receipt_path.exists()
