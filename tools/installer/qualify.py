"""Run the actual installer against an isolated index inside an owned container."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import urllib.request
import zipfile
from functools import partial
from pathlib import Path


def require_owned_container(inputs: Path, output: Path, run_id: str, role: str) -> None:
    """Refuse host execution and writable aliases before bootstrap mutations."""
    if not Path("/.dockerenv").is_file():
        raise RuntimeError("owned Docker container required")
    if not re.fullmatch(r"sibyl14-installer-[0-9a-f]{32}", run_id):
        raise RuntimeError("explicit owned run identity required")
    if role not in {"remote", "daemon", "bootstrap"} or socket.gethostname() != f"{run_id}-{role}":
        raise RuntimeError("owned container hostname mismatch")
    if inputs != Path("/input") or output != Path("/output") / role:
        raise RuntimeError("canonical isolated paths required")
    mounts = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] in {"/input", "/output"}:
            mounts[fields[4]] = (Path(fields[3]), set(fields[5].split(",")))
    if set(mounts) != {"/input", "/output"}:
        raise RuntimeError("dedicated input and output mounts required")
    source, flags = mounts["/input"]
    results, _ = mounts["/output"]
    if "ro" not in flags or run_id not in source.parts or run_id not in results.parts:
        raise RuntimeError("owned read-only input mount required")
    if source.is_relative_to(results) or results.is_relative_to(source):
        raise RuntimeError("input and output mounts overlap")


def prepare_index(inputs: Path, output: Path):
    index = output / "index"
    index.mkdir()
    artifacts = {}
    for wheel in sorted((inputs / "wheels").glob("*.whl")):
        name = wheel.name.split("-")[0].replace("_", "-")
        if name in artifacts or name not in {"sibyl-core", "sibyl-dev", "sibyld"}:
            raise RuntimeError("unexpected or duplicate wheel: " + name)
        package = index / "simple" / name
        package.mkdir(parents=True)
        data = wheel.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        (package / wheel.name).write_bytes(data)
        (package / "index.html").write_text(
            f'<a href="{wheel.name}#sha256={digest}">{wheel.name}</a>\n'
        )
        artifacts[name] = {"wheel": wheel.name, "sha256": digest}
    if not {"sibyl-core", "sibyl-dev", "sibyld"} <= artifacts.keys():
        raise RuntimeError("all unpublished Sibyl wheels are mandatory")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(index))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return artifacts, server


def verify_installation(home, inputs, artifacts, env, mode):
    receipt = {}
    tools = ["sibyl-dev"] + (["sibyld"] if mode == "daemon" else [])
    verified = {}
    for tool in tools:
        venv = home / ".local/share/uv/tools" / tool
        for name in ["sibyl-core", tool]:
            wheel = inputs / "wheels" / artifacts[name]["wheel"]
            script = 'import sysconfig; print(sysconfig.get_paths()["purelib"])'
            site = Path(
                subprocess.check_output(  # noqa: S603 - interpreter under fresh owned tool environment
                    [str(venv / "bin/python"), "-c", script], env=env, text=True
                ).strip()
            )
            with zipfile.ZipFile(wheel) as archive:
                files = [
                    p
                    for p in archive.namelist()
                    if not p.endswith("/") and not p.endswith(".dist-info/RECORD")
                ]
                for path in files:
                    if (site / path).read_bytes() != archive.read(path):
                        raise RuntimeError(f"installed wheel mismatch: {name}:{path}")
                verified[f"{tool}:{name}"] = len(files)
    receipt["verified_wheel_files"] = verified
    inventory_script = 'import importlib.metadata,json; print(json.dumps({d.metadata["Name"]:d.version for d in importlib.metadata.distributions()},sort_keys=True))'
    receipt["installed_distributions"] = {
        tool: json.loads(
            subprocess.check_output(  # noqa: S603 - interpreter under fresh owned tool environment
                [
                    str(home / ".local/share/uv/tools" / tool / "bin/python"),
                    "-c",
                    inventory_script,
                ],
                env=env,
                text=True,
            )
        )
        for tool in tools
    }
    if mode == "daemon":
        with urllib.request.urlopen(
            "http://127.0.0.1:3334/api/health/ready", timeout=5
        ) as response:
            receipt["api_readiness"] = {
                "status": response.status,
                "body": json.loads(response.read()),
            }
        receipt["daemon_pid"] = int((home / ".sibyl/run/sibyld.pid").read_text())
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("remote", "daemon"))
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("qualification must not have a Docker socket")
    output = args.output.resolve()
    inputs = args.inputs.resolve()
    require_owned_container(
        inputs,
        output,
        os.environ.get("SIBYL_INSTALLER_RUN_ID", ""),
        os.environ.get("SIBYL_INSTALLER_ROLE", ""),
    )
    output.mkdir(parents=True, exist_ok=False)
    inputs = args.inputs.resolve()
    home = output / "home"
    home.mkdir()
    if args.bootstrap:
        Path("/usr/local/bin/uv").rename("/usr/local/bin/uv-unused")
    receipt = {
        "mode": args.mode,
        "provider_credentials": False,
        "dependency_resolution": "installer_default_unconstrained",
    }
    server = None
    try:
        artifacts, server = prepare_index(inputs, output)
        env = {
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "NO_COLOR": "1",
            "UV_INDEX": f"http://127.0.0.1:{server.server_port}/simple",
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_INDEX_STRATEGY": "first-index",
            "UV_CACHE_DIR": str(output / "cache"),
            "UV_NO_ENV_FILE": "1",
        }
        receipt["artifacts"] = artifacts
        with (output / "installer.log").open("wb") as log:
            result = subprocess.run(  # noqa: S603 - owned installer and fixed argument choices
                [
                    "/bin/sh",
                    str(inputs / "install.sh"),
                    "--" + args.mode,
                    "--version",
                    "1.3.2",
                    "--no-open",
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        receipt["installer_exit"] = result.returncode
        receipt["bootstrap_without_uv"] = args.bootstrap
        uv_binary = home / ".local/bin/uv" if args.bootstrap else Path("/usr/local/bin/uv")
        if uv_binary.exists():
            receipt["uv_binary_sha256"] = hashlib.sha256(uv_binary.read_bytes()).hexdigest()
        result.check_returncode()
        receipt.update(verify_installation(home, inputs, artifacts, env, args.mode))
        receipt["status"] = "passed"
    except Exception as exc:
        receipt.update(status="failed", error_category=type(exc).__name__, error=str(exc))
        raise
    finally:
        if server is not None:
            server.shutdown()
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
