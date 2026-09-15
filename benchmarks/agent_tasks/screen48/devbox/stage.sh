#!/usr/bin/env bash
# Stage a frozen screen48 runtime on the eval devbox.
#
# Usage (run ON the devbox):
#   bash stage.sh <git-commit> [runtime-root]
#
# Produces a root the checkpoint phase's owner qualification accepts:
#
#   $ROOT/git/                  bare-ish clone, the only thing that talks to the remote
#   $ROOT/source/               clean `git archive` export: no .git, no .venv, no caches
#   $ROOT/source-manifest.json  base_commit / files / symlinks / modes, outside the tree
#   $ROOT/runtime/python/       the base CPython, staged INSIDE the runtime root
#   $ROOT/runtime/.venv/        the dependency environment, built against that CPython
#   $ROOT/runtime/runtime-manifest.json   what runtime_pin.verify re-walks
#   $ROOT/stage.json            the receipt, carrying the dependency_runtime binding
#
# Three layout rules come straight from the verifiers and none of them are
# preference:
#
#   * CurrentOwners recomputes the whole recursive inventory of $ROOT/source and
#     compares it to the manifest, so a .git index mutating under a plain read,
#     or a 20k-file .venv, or one __pycache__ directory, is "the source moved".
#   * runtime_pin.verify refuses any symlink under the runtime root that
#     resolves outside it, and a plain `uv venv` points .venv/bin/python at a
#     user-level or system interpreter. The base CPython is therefore copied
#     into $ROOT/runtime/python first and the environment is built against it.
#   * Both manifests are recomputed at the end of qualify_originals and compared
#     to the values from the start of it, so bytecode writing stays off for the
#     whole run: every python here runs with PYTHONDONTWRITEBYTECODE=1, and
#     run_phase must be invoked with `python -B` (see the receipt's run_phase
#     hint and the run_phase module docstring).
#
# Read-only against the Docker socket, the owned database container and the
# tokenizer assets: it inspects them and starts nothing.
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
PYTHON_VERSION="${SCREEN48_PYTHON_VERSION:-3.13}"

export HOME="${HOME:-/home/dev}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
# Every interpreter this script starts, and every phase started later, must
# leave no bytecode behind: the owner qualification hashes both trees twice.
export PYTHONDONTWRITEBYTECODE=1
umask 077

if [[ -e "$ROOT" ]]; then
  echo "refusing to reuse existing runtime root: $ROOT" >&2
  exit 1
fi
mkdir -p "$ROOT/git" "$ROOT/source" "$ROOT/runtime"

# 1. The clone is the only remote conversation, and it never becomes the source.
git clone --quiet "$REPO_URL" "$ROOT/git"
HEAD="$(git -C "$ROOT/git" rev-parse "$COMMIT")"
[[ "$HEAD" == "$COMMIT"* ]] || { echo "resolved $HEAD, wanted $COMMIT" >&2; exit 1; }

# 2. The source is a clean export of that commit: tracked content only.
git -C "$ROOT/git" archive --format=tar "$HEAD" | tar -x -C "$ROOT/source"
for stray in .git .venv; do
  if [[ -e "$ROOT/source/$stray" ]]; then
    echo "export carries $stray" >&2
    exit 1
  fi
done

# 3. The base CPython is staged inside the runtime root so that .venv/bin/python
#    cannot resolve out of it. uv reports where its managed interpreter lives.
BASE_PYTHON="$(uv python find "$PYTHON_VERSION")"
BASE_PREFIX="$("$BASE_PYTHON" -B -c 'import sys; print(sys.base_prefix)')"
mkdir -p "$ROOT/runtime/python"
cp -a "$BASE_PREFIX/." "$ROOT/runtime/python/"
STAGED_PYTHON="$ROOT/runtime/python/bin/python${PYTHON_VERSION}"
[[ -x "$STAGED_PYTHON" ]] || { echo "staged interpreter missing: $STAGED_PYTHON" >&2; exit 1; }

# 4. The environment is built outside the source tree, against the staged
#    interpreter. --frozen keeps uv.lock from being rewritten inside the export.
( cd "$ROOT/source" && UV_PROJECT_ENVIRONMENT="$ROOT/runtime/.venv" \
    uv sync --all-groups --frozen --quiet --python "$STAGED_PYTHON" )
PYTHON="$ROOT/runtime/.venv/bin/python"
PY_VERSION="$("$PYTHON" -B --version)"

# 5. Both manifests, each written by the module that mirrors its verifier. The
#    runtime one is proved by handing it straight back to runtime_pin.verify.
SOURCE_RECEIPT="$(cd "$ROOT/source" && "$PYTHON" -B -m benchmarks.agent_tasks.screen48.devbox.source_manifest \
  write --source "$ROOT/source" --commit "$HEAD" --out "$ROOT/source-manifest.json")"
RUNTIME_RECEIPT="$(cd "$ROOT/source" && "$PYTHON" -B -m benchmarks.agent_tasks.screen48.devbox.runtime_manifest \
  write --root "$ROOT/runtime" --interpreter "$PYTHON")"

# 6. Host resources, inspected and never driven.
inspect="$(curl -s --unix-socket "$SOCKET" "http://localhost/containers/${OWNED_CONTAINER}/json")"
owned_name="$(printf '%s' "$inspect" | "$PYTHON" -B -c 'import json,sys; print(json.load(sys.stdin)["Name"])')"
owned_state="$(printf '%s' "$inspect" | "$PYTHON" -B -c 'import json,sys; print(json.load(sys.stdin)["State"]["Status"])')"
[[ "$owned_name" == "$OWNED_NAME_PREFIX"* ]] || { echo "container $OWNED_CONTAINER is $owned_name, not the owned restore" >&2; exit 1; }

tok_sha="$(sha256sum "$TOKENIZER_ASSETS/tokenizer.json" | cut -c1-16)"
[[ "$tok_sha" == "$TOKENIZER_SHA" ]] || { echo "tokenizer.json sha $tok_sha != $TOKENIZER_SHA" >&2; exit 1; }

"$PYTHON" -B - "$ROOT" "$HEAD" "$PY_VERSION" "$owned_name" "$owned_state" "$TOKENIZER_ASSETS" "$SOCKET" "$SOURCE_RECEIPT" "$RUNTIME_RECEIPT" <<'PY'
import json, sys, datetime
root, head, py, name, state, assets, socket, source_receipt, runtime_receipt = sys.argv[1:]
source = json.loads(source_receipt)
runtime = json.loads(runtime_receipt)
receipt = {
    "schema": "sibyl-screen48-stage-v2",
    "staged_at": datetime.datetime.now(datetime.UTC).isoformat(),
    "runtime_root": root,
    "source_commit": head,
    "source": f"{root}/source",
    "source_manifest": f"{root}/source-manifest.json",
    "source_manifest_sha256": source["manifest_sha256"],
    "source_files": source["files"],
    "source_symlinks": source["symlinks"],
    "dependency_runtime": runtime["dependency_runtime"],
    "dependency_runtime_files": runtime["files"],
    "python": py,
    "docker_socket": socket,
    "owned_container": {"name": name, "state": state},
    "tokenizer_assets": assets,
    "run_phase": (
        f"cd {root}/source && PYTHONDONTWRITEBYTECODE=1 "
        f"{root}/runtime/.venv/bin/python -B "
        "-m benchmarks.agent_tasks.screen48.devbox.run_phase <phase> --output <dir>"
    ),
}
with open(f"{root}/stage.json", "x") as stream:
    json.dump(receipt, stream, indent=2)
    stream.write("\n")
print(json.dumps(receipt))
PY
