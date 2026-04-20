#!/usr/bin/env bash

set -euo pipefail

resolve_surreal_data_dir() {
  local surreal_data_dir="${SIBYL_SURREAL_DATA_DIR:-}"

  if [[ -z "$surreal_data_dir" && -d .moon/cache ]]; then
    surreal_data_dir="$(
      find .moon/cache -maxdepth 1 -type d \( -name 'surreal-rehearsal' -o -name 'surreal-rehearsal-cli-*' \) \
        | LC_ALL=C sort \
        | tail -n 1
    )"
  fi

  if [[ -z "$surreal_data_dir" ]]; then
    surreal_data_dir=".moon/cache/surreal-dev"
  fi

  printf '%s\n' "$surreal_data_dir"
}

main() {
  local surreal_data_dir
  surreal_data_dir="$(resolve_surreal_data_dir)"

  export SIBYL_STORE="${SIBYL_STORE:-surreal}"
  export SIBYL_SURREAL_DATA_DIR="$surreal_data_dir"

  if [[ "${1:-}" == "--print-env" ]]; then
    printf 'SIBYL_STORE=%s\n' "$SIBYL_STORE"
    printf 'SIBYL_SURREAL_DATA_DIR=%s\n' "$SIBYL_SURREAL_DATA_DIR"
    return 0
  fi

  echo "🔮 Surreal data dir: $SIBYL_SURREAL_DATA_DIR"
  docker compose up -d
  sleep 1
  npx concurrently --raw --kill-others-on-fail \
    "uv run --directory apps/api sibyld serve --reload" \
    "uv run --directory apps/api arq sibyl.jobs.worker.WorkerSettings --watch src" \
    "moon run web:dev"
}

main "$@"
