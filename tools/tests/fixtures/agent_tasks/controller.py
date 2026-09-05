"""Deterministic process fixture; never calls a model or network service."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

request = json.load(sys.stdin)
mode = sys.argv[1] if len(sys.argv) > 1 else "success"
assert "SIBYL_TEST_SECRET" not in os.environ
assert "checker" not in request
assert not Path("checker.py").exists()
assert Path.cwd().name == "controller-workspace"
if mode == "fail":
    sys.stderr.write("intentional controller failure\n")
    raise SystemExit(7)
if mode == "timeout":
    time.sleep(30)
if mode in {"child", "orphan"}:
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; from pathlib import Path; time.sleep(0.8); Path('late-child.txt').write_text('bad')",
        ],
    )
    if mode == "child":
        time.sleep(30)
if mode == "directories":
    Path("empty/subdir").mkdir(parents=True)
    Path("empty").chmod(0o700)
    Path(".").chmod(0o700)
if mode == "unreadable":
    Path("private").mkdir()
    Path("private/retained.txt").write_text("must not disappear during snapshot")
    Path("private").chmod(0)
if mode == "tamper-checker":
    checker = Path("../inputs/checker.py")
    checker.chmod(0o600)
    checker.write_text("raise SystemExit(0)")
if mode == "symlink":
    Path("unsafe").symlink_to("answer.txt")
Path("answer.txt").write_text("wrong" if mode == "wrong" else request["memory_pack"])
if mode == "malformed":
    sys.stdout.write("not json")
elif mode == "badusage":
    sys.stdout.write('{"synthetic":true,"cost_usd":NaN}')
elif mode == "duplicate":
    sys.stdout.write(
        '{"synthetic":true,"input_tokens":0,"output_tokens":0,"tool_calls":0,'
        '"cost_usd":100,"cost_usd":0}'
    )
elif mode == "unknownusage":
    sys.stdout.write('{"synthetic":true}')
else:
    sys.stdout.write(
        json.dumps(
            {
                "synthetic": True,
                "model": "deterministic-fixture",
                "tool_calls": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }
        )
    )
