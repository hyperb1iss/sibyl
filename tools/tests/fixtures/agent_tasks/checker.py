"""Independent fixture oracle, intentionally absent from controller input."""

import json
import sys
import time
from pathlib import Path

SHA256_HEX_LENGTH = 64

request = json.load(sys.stdin)
assert Path.cwd().name == "checker-workspace"
assert len(request["snapshot_sha256"]) == SHA256_HEX_LENGTH
mode = sys.argv[1] if len(sys.argv) > 1 else "check"
if mode == "timeout":
    time.sleep(30)
if mode == "fail":
    sys.stderr.write("intentional checker failure\n")
    raise SystemExit(9)
if mode == "malformed":
    sys.stdout.write("not json")
elif mode == "duplicate":
    sys.stdout.write('{"passed":false,"passed":true,"detail":"conflicting verdict"}')
elif mode == "deleted-private":
    Path("private").chmod(0o700)
    passed = not Path("private/retained.txt").exists()
    sys.stdout.write(
        json.dumps({"passed": passed, "detail": "private file must have been deleted"})
    )
else:
    passed = Path("answer.txt").read_text() == "42"
    sys.stdout.write(json.dumps({"passed": passed, "detail": "answer must be 42"}))
