#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage: moon run migrate-local-surreal -- [--org-id <org-uuid>] [options]

Exports a legacy Falkor/Postgres org, imports it into the local Surreal server,
then verifies the restored graph.

Options:
  --org-id <uuid>          Organization UUID to migrate (auto-detected when only one exists)
  --archive <path>         Archive path to write (default: /tmp/sibyl-migrate-<timestamp>.tar.gz)
  --restore-database-dump  Replay the database dump sidecar before graph import
  --restore-postgres       Alias for --restore-database-dump
  --help                   Show this help
EOF
}

main() {
  local org_id=""
  local archive=""
  local restore_postgres=false

  while (($# > 0)); do
    case "$1" in
      --org-id)
        org_id="${2:-}"
        shift 2
        ;;
      --archive)
        archive="${2:-}"
        shift 2
        ;;
      --restore-database-dump|--restore-postgres)
        restore_postgres=true
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

  if [[ -z "$archive" ]]; then
    archive="/tmp/sibyl-migrate-$(date +%Y%m%d-%H%M%S).tar.gz"
  fi

  export SURREAL_DATA_DIR="${SURREAL_DATA_DIR:-$repo_root/.moon/cache/surreal-dev}"
  mkdir -p "$SURREAL_DATA_DIR"

  local surreal_url="ws://127.0.0.1:${SIBYL_SURREAL_PORT:-8000}/rpc"
  local redis_host="${SIBYL_REDIS_HOST:-127.0.0.1}"
  local redis_port="${SIBYL_REDIS_PORT:-6381}"
  local export_args=(migrate export --output "$archive" --include-content)
  if [[ -n "$org_id" ]]; then
    export_args+=(--org-id "$org_id")
  fi

  if [[ -n "$org_id" ]]; then
    echo "🔮 Migrating org: $org_id"
  else
    echo "🔮 Migrating the only legacy org"
  fi
  echo "📦 Archive: $archive"
  echo "💎 Surreal data dir: $SURREAL_DATA_DIR"

  docker compose up -d falkordb postgres surrealdb redis

  SIBYL_STORE=legacy SIBYL_AUTH_STORE=postgres uv run --directory apps/api sibyld "${export_args[@]}"

  if [[ "$restore_postgres" == true ]]; then
    env \
      SIBYL_STORE=surreal \
      SIBYL_AUTH_STORE=surreal \
      SIBYL_SURREAL_URL="$surreal_url" \
      SIBYL_SURREAL_USERNAME="${SIBYL_SURREAL_USERNAME:-root}" \
      SIBYL_SURREAL_PASSWORD="${SIBYL_SURREAL_PASSWORD:-root}" \
      SIBYL_REDIS_HOST="$redis_host" \
      SIBYL_REDIS_PORT="$redis_port" \
      SIBYL_REDIS_PASSWORD="${SIBYL_REDIS_PASSWORD:-}" \
      uv run --directory apps/api sibyld migrate import "$archive" --yes --clean --restore-database-dump
  else
    env \
      SIBYL_STORE=surreal \
      SIBYL_AUTH_STORE=surreal \
      SIBYL_SURREAL_URL="$surreal_url" \
      SIBYL_SURREAL_USERNAME="${SIBYL_SURREAL_USERNAME:-root}" \
      SIBYL_SURREAL_PASSWORD="${SIBYL_SURREAL_PASSWORD:-root}" \
      SIBYL_REDIS_HOST="$redis_host" \
      SIBYL_REDIS_PORT="$redis_port" \
      SIBYL_REDIS_PASSWORD="${SIBYL_REDIS_PASSWORD:-}" \
      uv run --directory apps/api sibyld migrate import "$archive" --yes --clean
  fi

  env \
    SIBYL_STORE=surreal \
    SIBYL_AUTH_STORE=surreal \
    SIBYL_SURREAL_URL="$surreal_url" \
    SIBYL_SURREAL_USERNAME="${SIBYL_SURREAL_USERNAME:-root}" \
    SIBYL_SURREAL_PASSWORD="${SIBYL_SURREAL_PASSWORD:-root}" \
    SIBYL_REDIS_HOST="$redis_host" \
    SIBYL_REDIS_PORT="$redis_port" \
    SIBYL_REDIS_PASSWORD="${SIBYL_REDIS_PASSWORD:-}" \
    uv run --directory apps/api sibyld migrate verify "$archive"

  printf 'archive=%s\nmigrated_at=%s\n' "$archive" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$SURREAL_DATA_DIR/.sibyl-migrated"

  echo "✓ Local Surreal migration complete"
}

main "$@"
