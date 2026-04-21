#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

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

main() {
  export SIBYL_STORE="${SIBYL_STORE:-surreal}"

  local surreal_url="${SIBYL_SURREAL_URL:-}"
  local redis_host="${SIBYL_REDIS_HOST:-}"
  local surreal_volume_dir=""
  local services=()

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

  if [[ "${1:-}" == "--print-env" ]]; then
    printf 'SIBYL_STORE=%s\n' "$SIBYL_STORE"
    printf 'SIBYL_SURREAL_URL=%s\n' "$SIBYL_SURREAL_URL"
    if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
      printf 'SURREAL_DATA_DIR=%s\n' "$SURREAL_DATA_DIR"
    fi
    printf 'SIBYL_REDIS_HOST=%s\n' "$SIBYL_REDIS_HOST"
    printf 'SIBYL_REDIS_PORT=%s\n' "$SIBYL_REDIS_PORT"
    return 0
  fi

  echo "🔮 Surreal URL: $SIBYL_SURREAL_URL"
  if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
    echo "💎 Surreal data dir: $SURREAL_DATA_DIR"
  fi
  echo "🛠️  Redis: ${SIBYL_REDIS_HOST}:${SIBYL_REDIS_PORT}"

  if ((${#services[@]} > 0)); then
    docker compose up -d "${services[@]}"
  fi
  sleep 1
  npx concurrently --raw --kill-others-on-fail \
    "uv run --directory apps/api sibyld serve --reload" \
    "uv run --directory apps/api arq sibyl.jobs.worker.WorkerSettings --watch src" \
    "moon run web:dev"
}

main "$@"
