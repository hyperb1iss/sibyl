#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

child_pids=()
cleanup_started=0

resolve_surreal_volume_dir() {
  local surreal_volume_dir="${SURREAL_DATA_DIR:-.moon/cache/surreal-dev}"

  if [[ "$surreal_volume_dir" != /* ]]; then
    surreal_volume_dir="$repo_root/${surreal_volume_dir#./}"
  fi

  printf '%s\n' "$surreal_volume_dir"
}

is_local_service() {
  local value="${1:-}"
  [[ -z "$value" ]]
}

resolve_coordination_backend() {
  local configured="${SIBYL_COORDINATION_BACKEND:-local}"

  if [[ "$configured" == "auto" ]]; then
    if [[ "${SIBYL_STORE:-legacy}" == "legacy" ]]; then
      printf 'redis\n'
    else
      printf 'local\n'
    fi
    return
  fi

  printf '%s\n' "$configured"
}

process_tree_alive() {
  local pid="${1:-}"
  local child=""

  if [[ -z "$pid" ]]; then
    return 1
  fi

  if kill -0 -- "-$pid" 2>/dev/null; then
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi

  while IFS= read -r child; do
    if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
      return 0
    fi
  done < <(collect_descendants "$pid")

  return 1
}

collect_descendants() {
  local pid="${1:-}"
  local child=""

  if [[ -z "$pid" ]]; then
    return 0
  fi

  while IFS= read -r child; do
    [[ -z "$child" ]] && continue
    printf '%s\n' "$child"
    collect_descendants "$child"
  done < <(pgrep -P "$pid" || true)
}

collect_process_targets() {
  local pid="${1:-}"

  if [[ -z "$pid" ]]; then
    return 0
  fi

  printf '%s\n' "$pid"
  collect_descendants "$pid"
}

signal_process_tree() {
  local signal="${1:-TERM}"
  local pid="${2:-}"
  local -a descendants=()
  local child=""

  if [[ -z "$pid" ]]; then
    return
  fi

  while IFS= read -r child; do
    [[ -n "$child" ]] && descendants+=("$child")
  done < <(collect_descendants "$pid")

  kill "-$signal" -- "-$pid" 2>/dev/null || true

  if ((${#descendants[@]} > 0)); then
    local index=0
    for ((index=${#descendants[@]}-1; index>=0; index--)); do
      kill "-$signal" -- "-${descendants[index]}" 2>/dev/null || true
      kill "-$signal" "${descendants[index]}" 2>/dev/null || true
    done
  fi

  kill "-$signal" "$pid" 2>/dev/null || true
}

launch_command() {
  local command="${1:-}"

  python3 -c '
import os
import sys

repo_root, command = sys.argv[1], sys.argv[2]
os.chdir(repo_root)
os.setsid()
os.execvp("bash", ["bash", "-lc", f"exec {command}"])
' "$repo_root" "$command" &
  child_pids+=("$!")
}

wait_for_commands() {
  local exit_code=0

  while ((${#child_pids[@]} > 0)); do
    local -a finished=()
    local -a remaining=()

    for pid in "${child_pids[@]}"; do
      if process_tree_alive "$pid"; then
        remaining+=("$pid")
      else
        finished+=("$pid")
      fi
    done

    if ((${#finished[@]} > 0)); then
      for pid in "${finished[@]}"; do
        local status=0
        if wait "$pid"; then
          status=0
        else
          status=$?
        fi
        if ((status != 0)); then
          exit_code=$status
        fi
      done

      if ((${#remaining[@]} > 0)); then
        child_pids=("${remaining[@]}")
      else
        child_pids=()
      fi
      return "$exit_code"
    fi

    sleep 0.2
  done

  return "$exit_code"
}

cleanup() {
  local exit_code="${1:-0}"

  if ((cleanup_started)); then
    exit "$exit_code"
  fi

  cleanup_started=1
  trap - INT TERM EXIT

  if ((${#child_pids[@]} > 0)); then
    printf '\n🛑 Stopping dev processes...\n'

    local -a shutdown_targets=()

    for pid in "${child_pids[@]}"; do
      while IFS= read -r child; do
        [[ -n "$child" ]] && shutdown_targets+=("$child")
      done < <(collect_process_targets "$pid")
      signal_process_tree TERM "$pid"
    done

    if ((${#shutdown_targets[@]} > 0)); then
      child_pids=("${shutdown_targets[@]}")
    fi

    local deadline=$((SECONDS + 10))

    while ((${#child_pids[@]} > 0)); do
      local -a remaining=()

      for pid in "${child_pids[@]}"; do
        if process_tree_alive "$pid"; then
          remaining+=("$pid")
        fi
      done

      if ((${#remaining[@]} > 0)); then
        child_pids=("${remaining[@]}")
      else
        child_pids=()
      fi

      if ((${#child_pids[@]} == 0)); then
        break
      fi

      if ((SECONDS >= deadline)); then
        printf '⚠️  Forcing stubborn dev processes to stop\n'
        for pid in "${child_pids[@]}"; do
          signal_process_tree KILL "$pid"
        done
        break
      fi

      sleep 0.2
    done
  fi

  exit "$exit_code"
}

main() {
  export SIBYL_STORE="${SIBYL_STORE:-surreal}"
  export SIBYL_COORDINATION_BACKEND="${SIBYL_COORDINATION_BACKEND:-local}"

  local surreal_url="${SIBYL_SURREAL_URL:-}"
  local coordination_backend=""
  local surreal_volume_dir=""
  local services=()
  local api_command="${SIBYL_DEV_API_COMMAND:-uv run --directory apps/api sibyld serve --reload}"
  local web_command="${SIBYL_DEV_WEB_COMMAND:-moon run web:dev}"
  local worker_command="${SIBYL_DEV_WORKER_COMMAND:-uv run --directory apps/api arq sibyl.jobs.worker.WorkerSettings --watch src}"
  local commands=("$api_command" "$web_command")

  coordination_backend="$(resolve_coordination_backend)"

  trap 'cleanup 130' INT TERM
  trap 'cleanup $?' EXIT

  if is_local_service "$surreal_url"; then
    if [[ -n "${SIBYL_SURREAL_DATA_DIR:-}" ]]; then
      echo "⚠️  Ignoring SIBYL_SURREAL_DATA_DIR for server mode; use SURREAL_DATA_DIR instead"
      unset SIBYL_SURREAL_DATA_DIR
    fi

    surreal_volume_dir="$(resolve_surreal_volume_dir)"
    mkdir -p "$surreal_volume_dir"
    export SURREAL_DATA_DIR="$surreal_volume_dir"
    export SIBYL_SURREAL_URL="ws://127.0.0.1:${SIBYL_SURREAL_PORT:-8000}/rpc"
    export SIBYL_SURREAL_USERNAME="${SIBYL_SURREAL_USERNAME:-root}"
    export SIBYL_SURREAL_PASSWORD="${SIBYL_SURREAL_PASSWORD:-root}"
    services+=(surrealdb)
  else
    unset SIBYL_SURREAL_DATA_DIR
    unset SURREAL_DATA_DIR
  fi

  if [[ "$coordination_backend" == "redis" ]]; then
    local redis_host="${SIBYL_REDIS_HOST:-}"

    if is_local_service "$redis_host"; then
      export SIBYL_REDIS_HOST="127.0.0.1"
      export SIBYL_REDIS_PORT="${SIBYL_REDIS_PORT:-6381}"
      export SIBYL_REDIS_PASSWORD="${SIBYL_REDIS_PASSWORD:-}"
      services+=(redis)
    else
      export SIBYL_REDIS_HOST="$redis_host"
      export SIBYL_REDIS_PORT="${SIBYL_REDIS_PORT:-6381}"
      export SIBYL_REDIS_PASSWORD="${SIBYL_REDIS_PASSWORD:-}"
    fi

    commands+=("$worker_command")
  else
    unset SIBYL_REDIS_HOST
    unset SIBYL_REDIS_PORT
    unset SIBYL_REDIS_PASSWORD
  fi

  if [[ "${1:-}" == "--print-env" ]]; then
    printf 'SIBYL_STORE=%s\n' "$SIBYL_STORE"
    printf 'SIBYL_COORDINATION_BACKEND=%s\n' "$coordination_backend"
    printf 'SIBYL_SURREAL_URL=%s\n' "$SIBYL_SURREAL_URL"
    if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
      printf 'SURREAL_DATA_DIR=%s\n' "$SURREAL_DATA_DIR"
    fi
    if [[ "$coordination_backend" == "redis" ]]; then
      printf 'SIBYL_REDIS_HOST=%s\n' "$SIBYL_REDIS_HOST"
      printf 'SIBYL_REDIS_PORT=%s\n' "$SIBYL_REDIS_PORT"
    fi
    return 0
  fi

  echo "🔮 Surreal URL: $SIBYL_SURREAL_URL"
  echo "🪄 Coordination: $coordination_backend"
  if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
    echo "💎 Surreal data dir: $SURREAL_DATA_DIR"
  fi
  if [[ "$coordination_backend" == "redis" ]]; then
    echo "🛠️  Redis: ${SIBYL_REDIS_HOST}:${SIBYL_REDIS_PORT}"
  fi

  if ((${#services[@]} > 0)); then
    docker compose up -d "${services[@]}"
  fi

  sleep 1

  for command in "${commands[@]}"; do
    launch_command "$command"
  done

  if ! wait_for_commands; then
    return $?
  fi
}

main "$@"
