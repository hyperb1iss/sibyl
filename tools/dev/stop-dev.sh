#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$repo_root/tools/dev/process-tree.sh"

pid_file="$repo_root/.moon/cache/dev/processes.pid"
targets=()

add_target() {
  local pid="${1:-}"
  local existing=""

  if [[ -z "$pid" || "$pid" == "$$" ]]; then
    return
  fi

  for existing in "${targets[@]:-}"; do
    if [[ "$existing" == "$pid" ]]; then
      return
    fi
  done

  if process_tree_alive "$pid"; then
    targets+=("$pid")
  fi
}

add_pattern_targets() {
  local pattern="${1:-}"
  local pid=""

  while IFS= read -r pid; do
    add_target "$pid"
  done < <(pgrep -f "$pattern" || true)
}

if [[ -f "$pid_file" ]]; then
  while IFS=$'\t' read -r pid _command; do
    add_target "$pid"
  done < "$pid_file"
fi

add_pattern_targets "uv run --directory apps/api sibyld serve --reload"
add_pattern_targets "uvicorn .*sibyl.main:create_dev_app"
add_pattern_targets "arq sibyl.jobs.worker.WorkerSettings"
add_pattern_targets "concurrently .*sibyld serve --reload"
add_pattern_targets "next dev --port 3337"
add_pattern_targets "next dev -p 3337"
add_pattern_targets "tools/dev/run-surreal-dev.sh"

printf '🛑 Stopping Sibyl dev services...\n'

if ((${#targets[@]} > 0)); then
  for pid in "${targets[@]}"; do
    signal_process_tree TERM "$pid"
  done

  deadline=$((SECONDS + 10))
  while :; do
    remaining=()
    for pid in "${targets[@]}"; do
      if process_tree_alive "$pid"; then
        remaining+=("$pid")
      fi
    done

    if ((${#remaining[@]} == 0)); then
      break
    fi

    if ((SECONDS >= deadline)); then
      printf '⚠️  Forcing stubborn Sibyl dev processes to stop\n'
      for pid in "${remaining[@]}"; do
        signal_process_tree KILL "$pid"
      done
      break
    fi

    targets=("${remaining[@]}")
    sleep 0.2
  done
else
  printf 'No matching Sibyl dev processes found.\n'
fi

rm -f "$pid_file"
docker compose --env-file /dev/null down
printf '✓ All services stopped\n'
