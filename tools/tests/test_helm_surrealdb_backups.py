"""Run the surrealdb chart's rendered backup jobs against a fake HTTP API.

The export and restore-drill CronJobs are shell scripts embedded in the
chart. These tests render them with helm and execute them unchanged, with
real jq and sha256sum, while a fake curl (fixtures/fake_surreal_curl.py)
answers with SurrealDB 3.x response shapes. That proves the script logic
in CI without a database; the shapes themselves were taken from a live
v3.2.4 server.
"""

from __future__ import annotations

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
