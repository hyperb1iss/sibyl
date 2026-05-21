#!/usr/bin/env bash
set -euo pipefail

container="${SIBYL_SURREAL_CONTAINER:-sibyl-surrealdb}"
sample_seconds="${SIBYL_SURREAL_SAMPLE_SECONDS:-3}"
toolbox_image="${SIBYL_SURREAL_TOOLBOX_IMAGE:-alpine:3.20}"

usage() {
  cat <<'EOF'
Usage: tools/dev/surreal-container-snapshot.sh [options]

Capture read-only diagnostics for the local SurrealDB Docker container.

Options:
  -c, --container NAME   Container name (default: sibyl-surrealdb)
  -s, --seconds SECONDS  Thread CPU sample window (default: 3)
      --image IMAGE      Toolbox image for distroless containers (default: alpine:3.20)
  -h, --help             Show this help

The official SurrealDB image is distroless, so the script joins its PID namespace
with a short-lived toolbox container instead of requiring a shell inside SurrealDB.
EOF
}

while (($#)); do
  case "$1" in
    -c | --container)
      container="${2:?missing container name}"
      shift 2
      ;;
    -s | --seconds)
      sample_seconds="${2:?missing sample seconds}"
      shift 2
      ;;
    --image)
      toolbox_image="${2:?missing toolbox image}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  printf 'docker not found on PATH\n' >&2
  exit 127
fi

section() {
  printf '\n== %s ==\n' "$1"
}

section "container"
docker inspect "$container" --format \
  'name={{.Name}} pid={{.State.Pid}} running={{.State.Running}} oom={{.State.OOMKilled}} restarting={{.State.Restarting}} started={{.State.StartedAt}} image={{.Config.Image}}'

section "stats"
docker stats --no-stream --format \
  'name={{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}} net={{.NetIO}} block={{.BlockIO}} pids={{.PIDs}}' \
  "$container"

if command -v curl >/dev/null 2>&1; then
  section "surreal health"
  curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' \
    --max-time 5 \
    "${SIBYL_SURREAL_HTTP_URL:-http://127.0.0.1:8000/health}" || true

  section "surreal tiny sql"
  curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' \
    --max-time 5 \
    -u "${SURREAL_USER:-root}:${SURREAL_PASS:-root}" \
    -H "Surreal-NS: ${SIBYL_SURREAL_PROBE_NS:-sibyl_auth}" \
    -H "Surreal-DB: ${SIBYL_SURREAL_PROBE_DB:-auth}" \
    -H 'Accept: application/json' \
    --data 'RETURN 1;' \
    "${SIBYL_SURREAL_SQL_URL:-http://127.0.0.1:8000/sql}" || true

  section "surreal metrics"
  metrics_file="$(mktemp)"
  curl -sS -o "$metrics_file" -w 'HTTP %{http_code} in %{time_total}s bytes=%{size_download}\n' \
    --max-time 5 \
    -u "${SURREAL_USER:-root}:${SURREAL_PASS:-root}" \
    "${SIBYL_SURREAL_METRICS_URL:-http://127.0.0.1:8000/metrics}" || true
  sed -n '1,80p' "$metrics_file"
  rm -f "$metrics_file"
fi

section "surreal logs"
docker logs --tail="${SIBYL_SURREAL_LOG_TAIL:-80}" "$container" 2>&1 || true

section "pid namespace"
docker run --rm \
  --pid="container:$container" \
  -e SAMPLE_SECONDS="$sample_seconds" \
  "$toolbox_image" \
  sh -lc '
set -eu

printf "processes:\n"
ps -o pid,ppid,stat,time,comm,args 2>/dev/null || ps

sample_threads() {
  for task in /proc/1/task/*; do
    [ -r "$task/stat" ] || continue
    tid=${task##*/}
    set -- $(cat "$task/stat")
    comm=$(cat "$task/comm" 2>/dev/null || printf "?")
    wchan=$(cat "$task/wchan" 2>/dev/null || printf "?")
    printf "%s %s %s %s %s\n" "$tid" "$14" "$15" "$comm" "$wchan"
  done
}

printf "\nthread cpu sample over %ss:\n" "$SAMPLE_SECONDS"
sample_threads > /tmp/thread-a
sleep "$SAMPLE_SECONDS"
sample_threads > /tmp/thread-b
awk '"'"'
  NR == FNR {
    user[$1] = $2
    sys[$1] = $3
    next
  }
  {
    ticks = ($2 - user[$1]) + ($3 - sys[$1])
    if (ticks > 0) {
      printf "%d tid=%s comm=%s wchan=%s\n", ticks, $1, $4, $5
    }
  }
'"'"' /tmp/thread-a /tmp/thread-b | sort -nr | head -20

printf "\nprocess status:\n"
sed -n "1,90p" /proc/1/status

printf "\nopen tcp sockets:\n"
netstat -tan 2>/dev/null | sed -n "1,120p" || true
'
