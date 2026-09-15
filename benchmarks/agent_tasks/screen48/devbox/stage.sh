#!/usr/bin/env bash
# Stage a frozen screen48 runtime on the eval devbox.
#
# Usage (run ON the devbox):
#   bash stage.sh <git-commit> [runtime-root]
#
# Clones the repository at the exact commit into an owned directory, syncs the
# Python environment with uv, checks the Docker engine socket, the owned
# restored database container, and the Qwen tokenizer assets, then writes a
# stage.json receipt. Read-only against every resource it checks.
set -euo pipefail

COMMIT="${1:?git commit required}"
ROOT="${2:-/home/dev/dev/eval-runs/sibyl14-screen48-runtime-${COMMIT:0:12}}"
REPO_URL="${SCREEN48_REPO_URL:-https://github.com/hyperb1iss/sibyl.git}"
SOCKET="${SCREEN48_DOCKER_SOCKET:-/run/devbox-docker/docker.sock}"
OWNED_CONTAINER="${SCREEN48_OWNED_CONTAINER:-4e76d720d420}"
# The id is interpolated into a Docker API path below, so it may only ever be
# a container id: anything else addresses an endpoint this script does not read
# as written.
[[ "$OWNED_CONTAINER" =~ ^[0-9a-f]{12,64}$ ]] || { echo "container id $OWNED_CONTAINER is not a hex container id" >&2; exit 1; }
OWNED_NAME_PREFIX="/sibyl14-full-cohort-restore-5c53857b"
TOKENIZER_ASSETS="${SCREEN48_TOKENIZER_ASSETS:-/home/dev/dev/eval-runs/sibyl14-current324-preparation-inputs-ef7304c30a374ede81c0605db117aca3/assets}"
TOKENIZER_SHA="19564a48c4f71a2a"

export HOME="${HOME:-/home/dev}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
umask 077

if [[ -e "$ROOT" ]]; then
  echo "refusing to reuse existing runtime root: $ROOT" >&2
  exit 1
fi
mkdir -p "$ROOT"
git clone --quiet "$REPO_URL" "$ROOT/source"
git -C "$ROOT/source" checkout --quiet --detach "$COMMIT"
HEAD="$(git -C "$ROOT/source" rev-parse HEAD)"
[[ "$HEAD" == "$COMMIT"* ]] || { echo "checked out $HEAD, wanted $COMMIT" >&2; exit 1; }

( cd "$ROOT/source" && uv sync --all-groups --python 3.13 --quiet )
PYTHON="$ROOT/source/.venv/bin/python"
PY_VERSION="$("$PYTHON" --version)"

inspect="$(curl -s --unix-socket "$SOCKET" "http://localhost/containers/${OWNED_CONTAINER}/json")"
owned_name="$(printf '%s' "$inspect" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["Name"])')"
owned_state="$(printf '%s' "$inspect" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["State"]["Status"])')"
[[ "$owned_name" == "$OWNED_NAME_PREFIX"* ]] || { echo "container $OWNED_CONTAINER is $owned_name, not the owned restore" >&2; exit 1; }

tok_sha="$(sha256sum "$TOKENIZER_ASSETS/tokenizer.json" | cut -c1-16)"
[[ "$tok_sha" == "$TOKENIZER_SHA" ]] || { echo "tokenizer.json sha $tok_sha != $TOKENIZER_SHA" >&2; exit 1; }

"$PYTHON" - "$ROOT" "$HEAD" "$PY_VERSION" "$owned_name" "$owned_state" "$TOKENIZER_ASSETS" "$SOCKET" <<'PY'
import json, sys, datetime
root, head, py, name, state, assets, socket = sys.argv[1:]
receipt = {
    "schema": "sibyl-screen48-stage-v1",
    "staged_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "runtime_root": root,
    "source_commit": head,
    "python": py,
    "docker_socket": socket,
    "owned_container": {"name": name, "state": state},
    "tokenizer_assets": assets,
}
with open(f"{root}/stage.json", "x") as stream:
    json.dump(receipt, stream, indent=2)
    stream.write("\n")
print(json.dumps(receipt))
PY
