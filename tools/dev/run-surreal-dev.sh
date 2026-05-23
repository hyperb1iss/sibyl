#!/usr/bin/env bash

set -euo pipefail
set -m

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
  --ignore-legacy   Start SurrealDB dev even if local legacy data is detected
  --lan             Bind the API for access from other devices on the local network
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

docker_is_podman_emulation() {
  local version=""
  local normalized_version=""

  command -v docker >/dev/null 2>&1 || return 1
  version="$(docker --version 2>&1 || true)"
  normalized_version="$(printf '%s' "$version" | tr '[:upper:]' '[:lower:]')"
  [[ "$normalized_version" == *podman* ]]
}

docker_compose_provider() {
  local candidate=""

  for candidate in \
    "${SIBYL_DOCKER_COMPOSE_PROVIDER:-}" \
    "$(command -v docker-compose 2>/dev/null || true)" \
    /usr/lib/docker/cli-plugins/docker-compose \
    /usr/local/lib/docker/cli-plugins/docker-compose \
    /usr/libexec/docker/cli-plugins/docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-compose; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

compose_command() {
  if docker_is_podman_emulation && command -v podman >/dev/null 2>&1; then
    local provider=""

    if provider="$(docker_compose_provider)"; then
      printf 'env\nPODMAN_COMPOSE_WARNING_LOGS=false\nPODMAN_COMPOSE_PROVIDER=%s\npodman\ncompose\n' \
        "$provider"
      return 0
    fi

    if command -v podman-compose >/dev/null 2>&1; then
      printf 'env\nPODMAN_COMPOSE_WARNING_LOGS=false\nPODMAN_COMPOSE_PROVIDER=%s\npodman\ncompose\n' \
        "$(command -v podman-compose)"
      return 0
    fi

    printf 'env\nPODMAN_COMPOSE_WARNING_LOGS=false\npodman\ncompose\n'
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    printf 'docker\ncompose\n'
    return 0
  fi

  if command -v podman >/dev/null 2>&1; then
    printf 'env\nPODMAN_COMPOSE_WARNING_LOGS=false\npodman\ncompose\n'
    return 0
  fi

  return 1
}

run_compose() {
  local -a command=()
  local command_part=""

  while IFS= read -r command_part; do
    command+=("$command_part")
  done < <(compose_command)

  if ((${#command[@]} == 0)); then
    echo "Docker or Podman compose is required for local services" >&2
    return 1
  fi

  "${command[@]}" "$@"
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

   Import a previously exported archive with:
     uv run --directory apps/api sibyld migrate import <archive> \
       --source-type legacy-archive \
       --target-mode surreal \
       --yes --clean

   Start a fresh SurrealDB dev runtime:
     moon run dev -- --ignore-legacy
EOF
  return 1
}

resolve_coordination_backend() {
  local configured="${SIBYL_COORDINATION_BACKEND:-auto}"

  if [[ "$configured" == "auto" ]]; then
    printf 'local\n'
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

wait_for_api_ready() {
  local pid="${1:-}"
  local ready_host="$SIBYL_SERVER_HOST"
  if [[ "$ready_host" == "0.0.0.0" || "$ready_host" == "::" ]]; then
    ready_host="127.0.0.1"
  fi
  local url="http://${ready_host}:${SIBYL_SERVER_PORT}/api/health"
  local deadline=$((SECONDS + ${SIBYL_DEV_API_READY_TIMEOUT:-30}))

  while ((SECONDS < deadline)); do
    if [[ -n "$pid" ]] && ! process_tree_alive "$pid"; then
      echo "API process exited before becoming ready" >&2
      return 1
    fi

    if curl --fail --silent --show-error --max-time 1 "$url" >/dev/null 2>&1; then
      return 0
    fi

    sleep 0.2
  done

  echo "Timed out waiting for API readiness at $url" >&2
  return 1
}

resolve_lan_host() {
  local candidate=""
  local iface=""

  if [[ -n "${SIBYL_LAN_HOST:-}" ]]; then
    printf '%s\n' "$SIBYL_LAN_HOST"
    return
  fi

  if command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
    iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)"
    if [[ -n "$iface" ]]; then
      candidate="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
      if [[ -n "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return
      fi
    fi
  fi

  if command -v ipconfig >/dev/null 2>&1; then
    for iface in en0 en1 bridge100; do
      candidate="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
      if [[ -n "$candidate" ]]; then
        printf '%s\n' "$candidate"
        return
      fi
    done
  fi

  if command -v hostname >/dev/null 2>&1; then
    candidate="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    candidate="$(
      python3 - <<'PY' 2>/dev/null || true
import socket

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.connect(("8.8.8.8", 80))
    print(sock.getsockname()[0])
PY
    )"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  fi

  printf 'localhost\n'
}

append_unique_csv_value() {
  local current="${1:-}"
  local value="${2:-}"

  if [[ -z "$value" ]]; then
    printf '%s\n' "$current"
    return
  fi

  case ",$current," in
    *",$value,"*)
      printf '%s\n' "$current"
      ;;
    *)
      if [[ -z "$current" ]]; then
        printf '%s\n' "$value"
      else
        printf '%s,%s\n' "$current" "$value"
      fi
      ;;
  esac
}

resolve_lan_dev_origins() {
  local lan_host="${1:-}"
  local origins="${SIBYL_ALLOWED_DEV_ORIGINS:-}"
  local candidate=""

  origins="$(append_unique_csv_value "$origins" "$lan_host")"
  origins="$(append_unique_csv_value "$origins" "${lan_host}:${SIBYL_WEB_PORT}")"

  if command -v hostname >/dev/null 2>&1; then
    candidate="$(hostname -s 2>/dev/null || true)"
    origins="$(append_unique_csv_value "$origins" "$candidate")"
    origins="$(append_unique_csv_value "$origins" "${candidate}:${SIBYL_WEB_PORT}")"
    if [[ -n "$candidate" ]]; then
      origins="$(append_unique_csv_value "$origins" "${candidate}.local")"
      origins="$(append_unique_csv_value "$origins" "${candidate}.local:${SIBYL_WEB_PORT}")"
    fi

    candidate="$(hostname 2>/dev/null || true)"
    origins="$(append_unique_csv_value "$origins" "$candidate")"
    origins="$(append_unique_csv_value "$origins" "${candidate}:${SIBYL_WEB_PORT}")"
  fi

  if command -v scutil >/dev/null 2>&1; then
    candidate="$(scutil --get LocalHostName 2>/dev/null || true)"
    origins="$(append_unique_csv_value "$origins" "$candidate")"
    origins="$(append_unique_csv_value "$origins" "${candidate}:${SIBYL_WEB_PORT}")"
    if [[ -n "$candidate" ]]; then
      origins="$(append_unique_csv_value "$origins" "${candidate}.local")"
      origins="$(append_unique_csv_value "$origins" "${candidate}.local:${SIBYL_WEB_PORT}")"
    fi
  fi

  printf '%s\n' "$origins"
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
  local lan_mode=false

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
      --lan)
        lan_mode=true
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
  if [[ "$SIBYL_STORE" != "surreal" ]]; then
    echo "⚠️  SIBYL_STORE=$SIBYL_STORE is no longer supported by dev; using SurrealDB"
    export SIBYL_STORE="surreal"
    export SIBYL_AUTH_STORE="surreal"
  fi
  export SIBYL_COORDINATION_BACKEND="${SIBYL_COORDINATION_BACKEND:-auto}"
  export SIBYL_SERVER_PORT="${SIBYL_SERVER_PORT:-3334}"
  export SIBYL_WEB_PORT="${SIBYL_WEB_PORT:-3337}"
  local lan_host=""
  if [[ "$lan_mode" == true ]]; then
    lan_host="$(resolve_lan_host)"
    export SIBYL_SERVER_HOST="${SIBYL_SERVER_HOST:-0.0.0.0}"
    export SIBYL_PUBLIC_URL="${SIBYL_PUBLIC_URL:-http://${lan_host}:${SIBYL_WEB_PORT}}"
    export SIBYL_SERVER_URL="${SIBYL_SERVER_URL:-http://localhost:${SIBYL_SERVER_PORT}}"
    export SIBYL_FRONTEND_URL="${SIBYL_FRONTEND_URL:-http://${lan_host}:${SIBYL_WEB_PORT}/}"
    export NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://${lan_host}:${SIBYL_SERVER_PORT}}"
    export SIBYL_ALLOWED_DEV_ORIGINS="$(resolve_lan_dev_origins "$lan_host")"
  else
    export SIBYL_SERVER_HOST="${SIBYL_SERVER_HOST:-127.0.0.1}"
  fi
  export SIBYL_BACKEND_URL="${SIBYL_BACKEND_URL:-http://127.0.0.1:3334}"
  export SIBYL_API_URL="${SIBYL_API_URL:-http://127.0.0.1:3334/api}"
  export SIBYL_EMAIL_OUTBOX_PATH="${SIBYL_EMAIL_OUTBOX_PATH:-$repo_root/.moon/cache/auth-flow-email-outbox.jsonl}"

  local api_reload_dir=""
  local default_api_command=""
  local surreal_url="${SIBYL_SURREAL_URL:-}"
  local coordination_backend=""
  local surreal_volume_dir=""
  local uses_surreal=false
  local services=()
  local web_command="${SIBYL_DEV_WEB_COMMAND:-moon run web:dev}"
  local worker_command="${SIBYL_DEV_WORKER_COMMAND:-uv run --directory apps/api arq sibyl.jobs.worker.WorkerSettings --watch src}"
  local extra_commands=()

  printf -v api_reload_dir "%q" "$repo_root/apps/api/src"
  default_api_command="uv run --directory apps/api python -m uvicorn sibyl.main:create_dev_app --factory --host ${SIBYL_SERVER_HOST} --port ${SIBYL_SERVER_PORT} --reload --reload-dir $api_reload_dir --timeout-graceful-shutdown 5 --log-level warning"
  local api_command="${SIBYL_DEV_API_COMMAND:-$default_api_command}"

  coordination_backend="$(resolve_coordination_backend)"

  if [[ "$SIBYL_STORE" == "surreal" || "$SIBYL_AUTH_STORE" == "surreal" ]]; then
    uses_surreal=true
  fi
  trap 'cleanup 130' INT TERM
  trap 'cleanup $?' EXIT

  rm -f "$pid_file"

  if [[ "$uses_surreal" == true ]] && is_local_service "$surreal_url"; then
    if [[ -n "${SIBYL_SURREAL_DATA_DIR:-}" ]]; then
      echo "⚠️  Ignoring SIBYL_SURREAL_DATA_DIR for server mode; use SURREAL_DATA_DIR instead"
      unset SIBYL_SURREAL_DATA_DIR
    fi

    surreal_volume_dir="$(resolve_surreal_volume_dir)"
    mkdir -p "$surreal_volume_dir"
    # The compose `surrealdb` service mounts this dir with the podman `:U` flag,
    # which should chown it to the container UID. But `docker compose` (the
    # plugin podman delegates to by default) silently drops `:U`, leaving the
    # SurrealDB image's non-root user (uid 65532) unable to write a host-owned
    # bind mount and exiting with `Failed to create RocksDB directory`. Open
    # the directory so RocksDB initialisation succeeds regardless of mapping.
    chmod 0777 "$surreal_volume_dir"
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

    extra_commands+=("$worker_command")
  else
    unset SIBYL_REDIS_HOST
    unset SIBYL_REDIS_PORT
    unset SIBYL_REDIS_PASSWORD
  fi

  if [[ "$print_env" == true ]]; then
    printf 'SIBYL_STORE=%s\n' "$SIBYL_STORE"
    printf 'SIBYL_AUTH_STORE=%s\n' "$SIBYL_AUTH_STORE"
    printf 'SIBYL_COORDINATION_BACKEND=%s\n' "$coordination_backend"
    printf 'SIBYL_SERVER_HOST=%s\n' "$SIBYL_SERVER_HOST"
    printf 'SIBYL_SERVER_PORT=%s\n' "$SIBYL_SERVER_PORT"
    printf 'SIBYL_WEB_PORT=%s\n' "$SIBYL_WEB_PORT"
    if [[ -n "$lan_host" ]]; then
      printf 'SIBYL_LAN_HOST=%s\n' "$lan_host"
    fi
    if [[ -n "${SIBYL_PUBLIC_URL:-}" ]]; then
      printf 'SIBYL_PUBLIC_URL=%s\n' "$SIBYL_PUBLIC_URL"
    fi
    if [[ -n "${SIBYL_SERVER_URL:-}" ]]; then
      printf 'SIBYL_SERVER_URL=%s\n' "$SIBYL_SERVER_URL"
    fi
    if [[ -n "${SIBYL_FRONTEND_URL:-}" ]]; then
      printf 'SIBYL_FRONTEND_URL=%s\n' "$SIBYL_FRONTEND_URL"
    fi
    if [[ -n "${NEXT_PUBLIC_API_URL:-}" ]]; then
      printf 'NEXT_PUBLIC_API_URL=%s\n' "$NEXT_PUBLIC_API_URL"
    fi
    if [[ -n "${SIBYL_ALLOWED_DEV_ORIGINS:-}" ]]; then
      printf 'SIBYL_ALLOWED_DEV_ORIGINS=%s\n' "$SIBYL_ALLOWED_DEV_ORIGINS"
    fi
    if [[ -n "${SIBYL_SURREAL_URL:-}" ]]; then
      printf 'SIBYL_SURREAL_URL=%s\n' "$SIBYL_SURREAL_URL"
    fi
    if [[ -n "${SURREAL_DATA_DIR:-}" ]]; then
      printf 'SURREAL_DATA_DIR=%s\n' "$SURREAL_DATA_DIR"
    fi
    printf 'SIBYL_EMAIL_OUTBOX_PATH=%s\n' "$SIBYL_EMAIL_OUTBOX_PATH"
    if [[ "$coordination_backend" == "redis" ]]; then
      printf 'SIBYL_REDIS_HOST=%s\n' "$SIBYL_REDIS_HOST"
      printf 'SIBYL_REDIS_PORT=%s\n' "$SIBYL_REDIS_PORT"
    fi
    return 0
  fi

  if [[ "$ignore_legacy" != true ]] && ! warn_if_legacy_setup_detected; then
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
  echo "💎 Email outbox: $SIBYL_EMAIL_OUTBOX_PATH"
  if [[ "$coordination_backend" == "redis" ]]; then
    echo "🛠️  Redis: ${SIBYL_REDIS_HOST}:${SIBYL_REDIS_PORT}"
  fi
  if [[ "$lan_mode" == true ]]; then
    echo "🌐 LAN Web: http://${lan_host}:${SIBYL_WEB_PORT}"
    echo "🌐 LAN API: http://${lan_host}:${SIBYL_SERVER_PORT}/api"
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
    run_compose up -d --remove-orphans "${services[@]}"
  fi

  sleep 1

  launch_command "$api_command"
  local api_pid="${child_pids[$((${#child_pids[@]} - 1))]}"
  if ! wait_for_api_ready "$api_pid"; then
    return 1
  fi

  launch_command "$web_command"
  if ((${#extra_commands[@]} > 0)); then
    for command in "${extra_commands[@]}"; do
      launch_command "$command"
    done
  fi

  if ! wait_for_commands; then
    return $?
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
