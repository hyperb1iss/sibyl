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
import itertools
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
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
        self._values_counter = itertools.count()

    def serve(
        self,
        *,
        source: dict[str, dict[str, dict[str, int]]] | None = None,
        restore: dict[str, dict[str, dict[str, int]]] | None = None,
        empty_exports: tuple[str, ...] = (),
        drop_on_import: tuple[str, ...] = (),
    ) -> None:
        """Describe the fake servers. ``empty_exports`` and ``drop_on_import``
        name ``ns/db`` pairs whose export comes back empty or whose import
        silently loses every row."""
        servers: dict[str, object] = {
            "http://restore": {"user": "drill:drill", "namespaces": restore or {}},
        }
        if source is not None:
            servers["http://source"] = {
                "user": "root:secret",
                "version": "surrealdb-3.2.4",
                "namespaces": source,
            }
        self.state_path.write_text(
            json.dumps(
                {
                    "servers": servers,
                    "empty_exports": list(empty_exports),
                    "drop_on_import": list(drop_on_import),
                }
            )
        )

    def values(self, values: dict[str, Any]) -> tuple[str, str]:
        """Write chart values to a file and return the helm arguments for it.
        Multi-line shell hooks survive a values file intact, where --set
        would split them on commas and braces."""
        path = self.root / f"values-{next(self._values_counter)}.yaml"
        path.write_text(yaml.safe_dump(values))
        return "-f", str(path)

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
DEFAULT_MAX_AGE_HOURS = 36
FRESH_SECONDS = 600


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


# A reversible stand-in for a real cipher: a header line plus rot13. The
# output is not JSON, so a drill that skips decryption cannot read it.
CIPHER_HEADER = "SIBYL-TEST-CIPHER"
ENCRYPT = (
    f'( echo {CIPHER_HEADER}; tr "A-Za-z" "N-ZA-Mn-za-m" < "$SIBYL_EXPORT_FILE" )'
    ' > "$SIBYL_EXPORT_ENCRYPTED_FILE"'
)
DECRYPT = (
    'tail -n +2 "$SIBYL_RESTORE_ENCRYPTED_FILE" | tr "A-Za-z" "N-ZA-Mn-za-m"'
    ' > "$SIBYL_RESTORE_FILE"'
)
NOTIFY = 'echo notified >> "$HOME/notified.log"'


def _encrypted_export_values() -> dict[str, Any]:
    return {"export": {"encryption": {"enabled": True, "command": ENCRYPT}}}


def _decrypting_drill_values(command: str = DECRYPT) -> dict[str, Any]:
    return {
        "restoreDrill": {
            "decryption": {"enabled": True, "command": command},
            "failureNotification": {"command": NOTIFY},
        }
    }


def _stored_files(run_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(run_dir.iterdir())}


def test_helm_export_hooks_cover_every_file_and_the_manifest(harness: OpsHarness) -> None:
    harness.serve(source=_source())
    result = harness.run(
        "export",
        *harness.values(
            {
                "export": {
                    "encryption": {
                        "enabled": True,
                        "command": f'echo "$SIBYL_EXPORT_FILE" >> "$HOME/encrypted.log"\n{ENCRYPT}',
                    },
                    "syncCommand": 'echo "$SIBYL_EXPORT_RUN_DIR" >> "$HOME/synced.log"',
                }
            }
        ),
    )
    assert result.returncode == 0, result.stderr

    (run_dir,) = _run_dirs(harness)
    stored = _stored_files(run_dir)
    files = sorted(name for name in stored if name.endswith(".surql"))
    assert set(stored) == {*files, "manifest.json"}
    assert len(files) == SOURCE_DATABASES
    encrypted = (harness.root / "encrypted.log").read_text().splitlines()
    assert sorted(Path(line).name for line in encrypted[:-1]) == files
    assert encrypted[-1] == str(run_dir / "manifest.json")
    for name, data in stored.items():
        assert data.startswith(CIPHER_HEADER.encode()), name
    assert (harness.root / "synced.log").read_text().splitlines() == [str(run_dir)]


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("true", "wrote nothing to $SIBYL_EXPORT_ENCRYPTED_FILE"),
        ('mv "$SIBYL_EXPORT_FILE" "$SIBYL_EXPORT_FILE.enc"', "wrote nothing"),
        ('cp "$SIBYL_EXPORT_FILE" "$SIBYL_EXPORT_ENCRYPTED_FILE"', "without encrypting it"),
        ("echo cipher-tool-missing >&2; false", "cipher-tool-missing"),
    ],
    ids=["writes-nothing", "renames", "copies-plaintext", "fails"],
)
def test_helm_export_refuses_encryption_that_does_not_encrypt(
    harness: OpsHarness, command: str, message: str
) -> None:
    harness.serve(source=_source())
    result = harness.run(
        "export", *harness.values({"export": {"encryption": {"enabled": True, "command": command}}})
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not list(harness.backups.glob("*/manifest.json"))


@pytest.mark.parametrize(
    ("source", "empty_exports", "message"),
    [
        ({}, (), "discovery found no databases"),
        ({"ns_only": {}}, (), "discovery found no databases"),
        (_source(), (f"{ORG_B}/graph",), f"empty export for {ORG_B}/graph"),
        ({"bad name": {"db": {"t": 1}}}, (), "not a plain SurrealDB identifier"),
        ({"ok": {"bad-db": {"t": 1}}}, (), "not a plain SurrealDB identifier"),
    ],
    ids=["no-namespaces", "no-databases", "empty-file", "unsafe-namespace", "unsafe-database"],
)
def test_helm_export_fails_loudly_instead_of_recording_a_partial_backup(
    harness: OpsHarness,
    source: dict[str, dict[str, dict[str, int]]],
    empty_exports: tuple[str, ...],
    message: str,
) -> None:
    harness.serve(source=source, empty_exports=empty_exports)
    result = harness.run("export")

    assert result.returncode != 0
    assert message in result.stderr
    assert not list(harness.backups.glob("*/manifest.json"))


def _export_then_reset(harness: OpsHarness, drop_on_import: tuple[str, ...] = ()) -> Path:
    harness.serve(source=_source())
    exported = harness.run("export")
    assert exported.returncode == 0, exported.stderr
    harness.serve(drop_on_import=drop_on_import)
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
    assert 0 <= receipt["manifest"]["age_seconds"] < FRESH_SECONDS
    assert receipt["manifest"]["max_age_hours"] == DEFAULT_MAX_AGE_HOURS
    assert receipt["manifest"]["decrypted"] is False
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
    ("damage", "drop_on_import", "message"),
    [
        ("tamper", (), "does not match the manifest's size and sha256"),
        ("delete", (), "is missing"),
        (None, (f"{ORG_A}/graph",), "restored no rows; the export counted 14"),
    ],
    ids=["checksum", "missing-file", "empty-restore"],
)
def test_helm_restore_drill_fails_and_notifies_on_a_bad_database(
    harness: OpsHarness,
    damage: str | None,
    drop_on_import: tuple[str, ...],
    message: str,
) -> None:
    run_dir = _export_then_reset(harness, drop_on_import)
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


def test_helm_restore_drill_scratch_server_keeps_data_on_its_tmp_volume() -> None:
    """The drill now restores every org's graph, so the scratch sidecar's
    default store must be disk on its own /tmp emptyDir, not RAM."""
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [_HELM, "template", "drill", "charts/surrealdb", "--set", "restoreDrill.enabled=true"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    for document in yaml.safe_load_all(rendered.stdout):
        if document and document.get("kind") == "CronJob":
            pod = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            (server,) = (c for c in pod["initContainers"] if c["name"] == "restore-server")
            env = {item["name"]: item.get("value") for item in server["env"]}
            assert env["SURREAL_RESTORE_PATH"] == "rocksdb:/tmp/restore-db"
            store_path = env["SURREAL_RESTORE_PATH"].removeprefix("rocksdb:")
            tmp_mount = next(
                m for m in server["volumeMounts"] if store_path.startswith(m["mountPath"] + "/")
            )
            tmp_volume = next(v for v in pod["volumes"] if v["name"] == tmp_mount["name"])
            assert "emptyDir" in tmp_volume
            return
    pytest.fail("restore-drill CronJob not rendered")


def test_helm_restore_drill_decrypts_into_scratch_and_checks_the_plaintext(
    harness: OpsHarness,
) -> None:
    harness.serve(source=_source())
    exported = harness.run("export", *harness.values(_encrypted_export_values()))
    assert exported.returncode == 0, exported.stderr
    harness.serve()
    (run_dir,) = _run_dirs(harness)
    before = _stored_files(run_dir)

    result = harness.run("restore-drill", *harness.values(_decrypting_drill_values()))
    assert result.returncode == 0, result.stderr

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["status"] == "PASS"
    assert receipt["manifest"]["decrypted"] is True
    assert {item["status"] for item in receipt["databases"]} == {"PASS"}
    assert harness.server("http://restore")["namespaces"] == _source()
    assert _stored_files(run_dir) == before, "the drill must never rewrite the backup it reads"


@pytest.mark.parametrize(
    ("drill_values", "message"),
    [
        ({}, "is not JSON; encrypted exports need restoreDrill.decryption"),
        (
            _decrypting_drill_values('cp "$SIBYL_RESTORE_ENCRYPTED_FILE" "$SIBYL_RESTORE_FILE"'),
            "is not JSON",
        ),
        (
            _decrypting_drill_values(
                DECRYPT
                + '\ncase "$SIBYL_RESTORE_FILE" in *.surql) echo tampered >> "$SIBYL_RESTORE_FILE";; esac'
            ),
            f"restore failed for {ORG_A}/graph: export file",
        ),
        (
            _decrypting_drill_values(
                'case "$SIBYL_RESTORE_FILE" in *.surql) exit 4;; esac\n' + DECRYPT
            ),
            f"restore failed for {ORG_A}/graph: decryption failed",
        ),
    ],
    ids=["no-decryption", "wrong-key", "wrong-plaintext", "decrypt-error"],
)
def test_helm_restore_drill_fails_when_decryption_is_missing_or_wrong(
    harness: OpsHarness, drill_values: dict[str, Any], message: str
) -> None:
    harness.serve(source=_source())
    exported = harness.run("export", *harness.values(_encrypted_export_values()))
    assert exported.returncode == 0, exported.stderr
    harness.serve()

    values = {"restoreDrill": {"failureNotification": {"command": NOTIFY}}}
    values["restoreDrill"].update(drill_values.get("restoreDrill", {}))
    result = harness.run("restore-drill", *harness.values(values))

    assert result.returncode != 0
    assert message in result.stderr
    assert (harness.root / "notified.log").read_text() == "notified\n"
    assert not harness.receipt_path.exists()


# Mirrors the documented S3 fetch with a directory standing in for the
# bucket: newest run whose manifest exists, files first, manifest last.
# The `exit 0` also proves the hook cannot end the drill early.
FETCH = """\
remote="$SIBYL_EXPORT_DESTINATION_URI"
for run in $(ls -1 "$remote" | grep "^$SIBYL_EXPORT_FILE_PREFIX-" | sort -r); do
  if [ -f "$remote/$run/manifest.json" ]; then
    mkdir -p "$SIBYL_RESTORE_SOURCE_PATH/$run"
    for file in "$remote/$run"/*.surql; do cp "$file" "$SIBYL_RESTORE_SOURCE_PATH/$run/"; done
    cp "$remote/$run/manifest.json" "$SIBYL_RESTORE_SOURCE_PATH/$run/manifest.json"
    exit 0
  fi
done
echo "no complete run under $remote" >&2
exit 1
"""


def test_helm_restore_drill_fetches_the_newest_complete_run(harness: OpsHarness) -> None:
    run_dir = _export_then_reset(harness)
    remote = harness.root / "bucket"
    remote.mkdir()
    shutil.move(run_dir, remote / run_dir.name)
    unfinished = remote / "sibyl-99991231235959"
    unfinished.mkdir()
    (unfinished / f"{ORG_A}.graph.surql").write_text("-- upload still running\n")

    result = harness.run(
        "restore-drill",
        *harness.values(
            {
                "export": {"destination": {"uri": str(remote)}},
                "restoreDrill": {"fetchCommand": FETCH},
            }
        ),
    )
    assert result.returncode == 0, result.stderr

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["status"] == "PASS"
    assert receipt["manifest"]["path"] == str(harness.backups / run_dir.name / "manifest.json")
    assert [path.name for path in _run_dirs(harness)] == [run_dir.name]
    assert harness.server("http://restore")["namespaces"] == _source()


def _age_manifest(run_dir: Path, hours: float) -> None:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    created = datetime.now(UTC) - timedelta(hours=hours)
    manifest["created_at"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    ("max_age_hours", "passes"),
    [(None, False), (48, True), (0, True)],
    ids=["default-36h", "raised-to-48h", "disabled"],
)
def test_helm_restore_drill_pages_when_the_newest_export_is_stale(
    harness: OpsHarness, max_age_hours: int | None, *, passes: bool
) -> None:
    run_dir = _export_then_reset(harness)
    _age_manifest(run_dir, hours=40)
    drill: dict[str, Any] = {"failureNotification": {"command": NOTIFY}}
    if max_age_hours is not None:
        drill["maxExportAgeHours"] = max_age_hours

    result = harness.run("restore-drill", *harness.values({"restoreDrill": drill}))

    if passes:
        assert result.returncode == 0, result.stderr
        manifest = json.loads(harness.receipt_path.read_text())["manifest"]
        assert abs(manifest["age_seconds"] - 40 * 3600) < FRESH_SECONDS
        assert manifest["max_age_hours"] == max_age_hours
    else:
        assert result.returncode != 0
        assert "40h ago; restoreDrill.maxExportAgeHours is 36" in result.stderr
        assert (harness.root / "notified.log").read_text() == "notified\n"
        assert not harness.receipt_path.exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            ("--set", "restoreDrill.decryption.enabled=true"),
            "restoreDrill.decryption.command is required",
        ),
        (("--set-string", "restoreDrill.maxExportAgeHours=abc"), "whole number of hours"),
        (("--set", "restoreDrill.maxExportAgeHours=-1"), "whole number of hours"),
        (("--set", "restoreDrill.maxExportAgeHours=1.5"), "whole number of hours"),
    ],
    ids=["decryption-without-command", "age-text", "age-negative", "age-fraction"],
)
def test_helm_restore_drill_rejects_unusable_hook_values(
    override: tuple[str, str], message: str
) -> None:
    assert _HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "drill",
            "charts/surrealdb",
            "--set",
            "restoreDrill.enabled=true",
            *override,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode != 0
    assert message in rendered.stderr
