#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
source "$repo_root/tools/dev/process-tree.sh"

child_pids=()
cleanup_started=0
pid_file="$repo_root/.moon/cache/dev/processes.pid"

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

usage() {
  cat <<'EOF'
Usage: moon run dev -- [options]

Options:
  --migrate-legacy  Migrate the local legacy setup to SurrealDB, then start dev
  --ignore-legacy   Start SurrealDB dev even if local legacy data is detected
  --print-env       Print resolved runtime environment and exit
  --help            Show this help
EOF
}

docker_legacy_setup_detected() {
  local project_name="${COMPOSE_PROJECT_NAME:-sibyl}"
  local compose_ps=""
  local volumes=""

  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi

  compose_ps="$(docker compose ps -a --format json 2>/dev/null || true)"
  if [[ "$compose_ps" == *'"Service":"falkordb"'* || "$compose_ps" == *'"Service":"postgres"'* ]]; then
    return 0
  fi

  volumes="$(docker volume ls --format '{{.Name}}' 2>/dev/null || true)"
  if grep -qx "${project_name}_falkordb_data" <<<"$volumes"; then
    return 0
  fi
  if grep -qx "${project_name}_postgres_data" <<<"$volumes"; then
    return 0
  fi

  return 1
}

surreal_runtime_data_detected() {
  local surreal_data_dir="${1:-}"

  if [[ -z "$surreal_data_dir" ]]; then
    surreal_data_dir="$(resolve_surreal_volume_dir)"
  elif [[ "$surreal_data_dir" != /* ]]; then
    surreal_data_dir="$repo_root/${surreal_data_dir#./}"
  fi

  [[ -d "$surreal_data_dir" ]] || return 1
  [[ -e "$surreal_data_dir/.sibyl-migrated" ]] && return 0
  [[ -e "$surreal_data_dir/sibyl.db/CURRENT" ]] && return 0
  [[ -e "$surreal_data_dir/sibyl.db/IDENTITY" ]] && return 0
  [[ -d "$surreal_data_dir/sibyl.db" ]] || return 1
  find "$surreal_data_dir/sibyl.db" -mindepth 1 -maxdepth 1 -type f -print -quit 2>/dev/null | grep -q .
}

warn_if_legacy_setup_detected() {
  if [[ "$SIBYL_STORE" != "surreal" || "${SIBYL_DEV_SKIP_LEGACY_CHECK:-}" == "1" ]]; then
    return 0
  fi
  if ! docker_legacy_setup_detected; then
    return 0
  fi
  if surreal_runtime_data_detected; then
    return 0
  fi

  cat <<'EOF'
⚠️  Local legacy data detected.
   `moon run dev` now starts the SurrealDB runtime by default.

   Migrate the common single-org setup:
     moon run dev -- --migrate-legacy

   Keep using FalkorDB + PostgreSQL:
     moon run dev-legacy

   Start a fresh SurrealDB dev runtime:
     moon run dev -- --ignore-legacy
EOF
  return 1
}

resolve_coordination_backend() {
  local configured="${SIBYL_COORDINATION_BACKEND:-auto}"

  if [[ "$configured" == "auto" ]]; then
    if [[ "${SIBYL_STORE:-surreal}" == "legacy" ]]; then
      printf 'redis\n'
    else
      printf 'local\n'
    fi
    return
  fi

  printf '%s\n' "$configured"
}

launch_command() {
  local command="${1:-}"
  local pid=""
  local quoted_repo_root=""

  printf -v quoted_repo_root "%q" "$repo_root"
  bash -c "cd $quoted_repo_root && exec $command" &
  pid="$!"
  child_pids+=("$pid")
  disown "$pid" 2>/dev/null || true
  mkdir -p "$(dirname "$pid_file")"
  printf '%s\t%s\n' "$pid" "$command" >> "$pid_file"
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
    local -a wait_targets=("${child_pids[@]}")

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

    for pid in "${wait_targets[@]}"; do
      wait "$pid" 2>/dev/null || true
    done
  fi

  rm -f "$pid_file"
  exit "$exit_code"
}

main() {
  local print_env=false
  local ignore_legacy=false
  local migrate_legacy=false

  while (($# > 0)); do
    case "$1" in
      --print-env)
        print_env=true
        shift
        ;;
      --ignore-legacy)
        ignore_legacy=true
        shift
        ;;
      --migrate-legacy)
        migrate_legacy=true
        shift
        ;;
      --help|-h)
        usage
        return 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage >&2
        return 1
        ;;
    esac
  done

  export SIBYL_STORE="${SIBYL_STORE:-surreal}"
  if [[ -z "${SIBYL_AUTH_STORE:-}" ]]; then
    if [[ "$SIBYL_STORE" == "surreal" ]]; then
      export SIBYL_AUTH_STORE="surreal"
    else
      export SIBYL_AUTH_STORE="postgres"
    fi
  fi
  export SIBYL_COORDINATION_BACKEND="${SIBYL_COORDINATION_BACKEND:-auto}"
  export SIBYL_SERVER_HOST="${SIBYL_SERVER_HOST:-127.0.0.1}"
  export SIBYL_SERVER_PORT="${SIBYL_SERVER_PORT:-3334}"
  export SIBYL_BACKEND_URL="${SIBYL_BACKEND_URL:-http://127.0.0.1:3334}"
  export SIBYL_API_URL="${SIBYL_API_URL:-http://127.0.0.1:3334/api}"

  local api_reload_dir=""
  local default_api_command=""
  local surreal_url="${SIBYL_SURREAL_URL:-}"
  local coordination_backend=""
  local surreal_volume_dir=""
  local uses_surreal=false
  local uses_postgres=false
  local services=()
  local web_command="${SIBYL_DEV_WEB_COMMAND:-moon run web:dev}"
  local worker_command="${SIBYL_DEV_WORKER_COMMAND:-uv run --directory apps/api arq sibyl.jobs.worker.WorkerSettings --watch src}"
  local commands=()

  printf -v api_reload_dir "%q" "$repo_root/apps/api/src"
  default_api_command="uv run --directory apps/api python -m uvicorn sibyl.main:create_dev_app --factory --host ${SIBYL_SERVER_HOST} --port ${SIBYL_SERVER_PORT} --reload --reload-dir $api_reload_dir --timeout-graceful-shutdown 5 --log-level warning"
  local api_command="${SIBYL_DEV_API_COMMAND:-$default_api_command}"
  commands=("$api_command" "$web_command")

  coordination_backend="$(resolve_coordination_backend)"

  if [[ "$SIBYL_STORE" == "surreal" || "$SIBYL_AUTH_STORE" == "surreal" ]]; then
    uses_surreal=true
  fi
  if [[ "$SIBYL_STORE" == "legacy" || "$SIBYL_AUTH_STORE" == "postgres" ]]; then
    uses_postgres=true
  fi

  trap 'cleanup 130' INT TERM
  trap 'cleanup $?' EXIT

  rm -f "$pid_file"

  if [[ "$SIBYL_STORE" == "legacy" ]]; then
    services+=(falkordb)
  fi

  if [[ "$uses_surreal" == true ]] && is_local_service "$surreal_url"; then
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

  if [[ "$uses_postgres" == true ]]; then
    services+=(postgres)
  fi

  if [[ "$coordination_backend" == "redis" ]]; then
    local redis_host="${SIBYL_REDIS_HOST:-}"

    if [[ "$SIBYL_STORE" == "legacy" ]]; then
      export SIBYL_REDIS_HOST="${redis_host:-127.0.0.1}"
      export SIBYL_REDIS_PORT="${SIBYL_REDIS_PORT:-6380}"
      export SIBYL_REDIS_PASSWORD="${SIBYL_REDIS_PASSWORD:-${SIBYL_FALKORDB_PASSWORD:-sibyl_dev}}"
    elif is_local_service "$redis_host"; then
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

  if [[ "$print_env" == true ]]; then
    printf 'SIBYL_STORE=%s\n' "$SIBYL_STORE"
    printf 'SIBYL_AUTH_STORE=%s\n' "$SIBYL_AUTH_STORE"
    printf 'SIBYL_COORDINATION_BACKEND=%s\n' "$coordination_backend"
    if [[ -n "${SIBYL_SURREAL_URL:-}" ]]; then
      printf 'SIBYL_SURREAL_URL=%s\n' "$SIBYL_SURREAL_URL"
    fi
    if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
      printf 'SURREAL_DATA_DIR=%s\n' "$SURREAL_DATA_DIR"
    fi
    if [[ "$coordination_backend" == "redis" ]]; then
      printf 'SIBYL_REDIS_HOST=%s\n' "$SIBYL_REDIS_HOST"
      printf 'SIBYL_REDIS_PORT=%s\n' "$SIBYL_REDIS_PORT"
    fi
    return 0
  fi

  if [[ "$migrate_legacy" == true ]]; then
    bash "$repo_root/tools/dev/migrate-local-surreal.sh"
  elif [[ "$ignore_legacy" != true ]] && ! warn_if_legacy_setup_detected; then
    return 1
  fi

  echo "🔮 Store: $SIBYL_STORE"
  echo "🔮 Auth store: $SIBYL_AUTH_STORE"
  if [[ -n "${SIBYL_SURREAL_URL:-}" ]]; then
    echo "🔮 Surreal URL: $SIBYL_SURREAL_URL"
  fi
  echo "🪄 Coordination: $coordination_backend"
  if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
    echo "💎 Surreal data dir: $SURREAL_DATA_DIR"
  fi
  if [[ "$coordination_backend" == "redis" ]]; then
    echo "🛠️  Redis: ${SIBYL_REDIS_HOST}:${SIBYL_REDIS_PORT}"
  fi

  if [[ -z "${SIBYL_DEV_API_COMMAND:-}" ]]; then
    echo "Starting Sibyl in dev mode..."
    echo "Hot reload enabled - watching for changes"
    echo "API: http://${SIBYL_SERVER_HOST}:${SIBYL_SERVER_PORT}/api"
    echo "MCP: http://${SIBYL_SERVER_HOST}:${SIBYL_SERVER_PORT}/mcp"
    echo "Docs: http://${SIBYL_SERVER_HOST}:${SIBYL_SERVER_PORT}/api/docs"
    echo "Debug stacks: kill -USR1 <api-child-pid>"
    echo
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

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
