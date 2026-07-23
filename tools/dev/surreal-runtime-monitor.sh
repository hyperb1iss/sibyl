#!/usr/bin/env bash
set -uo pipefail

command="${1:-}"
if [[ "$command" == "-h" || "$command" == "--help" ]]; then
  usage_requested=true
else
  usage_requested=false
fi
if [[ -n "$command" ]]; then
  shift
fi

container=""
output_dir=""
interval_seconds=5

usage() {
  cat <<'EOF'
Usage: tools/dev/surreal-runtime-monitor.sh COMMAND [options]

Continuously sample SurrealDB process and cgroup memory, or gate a completed run.

Commands:
  monitor                    Sample until terminated
  gate                       Summarize samples and fail on runtime integrity loss

Options:
  -c, --container ID         Docker container ID or name
  -o, --output-dir PATH      Telemetry directory
  -i, --interval SECONDS     Sample interval (default: 5)
  -h, --help                 Show this help
EOF
}

if [[ "$usage_requested" == "true" ]]; then
  usage
  exit 0
fi

while (($#)); do
  case "$1" in
    -c | --container)
      container="${2:?missing container}"
      shift 2
      ;;
    -o | --output-dir)
      output_dir="${2:?missing output directory}"
      shift 2
      ;;
    -i | --interval)
      interval_seconds="${2:?missing interval}"
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

if [[ "$command" != "monitor" && "$command" != "gate" ]]; then
  usage >&2
  exit 2
fi
if [[ -z "$container" || -z "$output_dir" ]]; then
  usage >&2
  exit 2
fi
if [[ ! "$interval_seconds" =~ ^[1-9][0-9]*$ ]]; then
  printf 'interval must be a positive integer\n' >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  printf 'docker not found on PATH\n' >&2
  exit 127
fi

samples="$output_dir/samples.tsv"
events="$output_dir/docker-events.jsonl"
events_pid=""

read_proc_status() {
  local pid="$1"
  local status="/proc/$pid/status"

  if [[ ! -r "$status" ]]; then
    printf '%s\t%s\t%s\t%s\t%s\t%s' - - - - - -
    return
  fi

  awk '
    BEGIN {
      rss = hwm = anon = file = swap = threads = "-"
    }
    $1 == "VmRSS:" { rss = $2 }
    $1 == "VmHWM:" { hwm = $2 }
    $1 == "RssAnon:" { anon = $2 }
    $1 == "RssFile:" { file = $2 }
    $1 == "VmSwap:" { swap = $2 }
    $1 == "Threads:" { threads = $2 }
    END {
      printf "%s\t%s\t%s\t%s\t%s\t%s", rss, hwm, anon, file, swap, threads
    }
  ' "$status"
}

read_pressure_total() {
  local path="$1"
  local class="$2"

  if [[ ! -r "$path" ]]; then
    printf -
    return
  fi

  awk -v class="$class" '
    $1 == class {
      for (field_index = 2; field_index <= NF; field_index++) {
        split($field_index, field, "=")
        if (field[1] == "total") {
          print field[2]
          exit
        }
      }
    }
  ' "$path"
}

read_value() {
  local path="$1"
  if [[ -r "$path" ]]; then
    cat "$path"
  else
    printf -
  fi
}

read_event_value() {
  local path="$1"
  local key="$2"

  if [[ ! -r "$path" ]]; then
    printf -
    return
  fi

  awk -v key="$key" '$1 == key { print $2; found = 1 } END { if (!found) print "-" }' "$path"
}

cgroup_directory() {
  local pid="$1"
  local path

  path="$(awk -F: '$1 == "0" { print $3; exit }' "/proc/$pid/cgroup" 2>/dev/null)"
  if [[ -n "$path" && -d "/sys/fs/cgroup$path" ]]; then
    printf '%s' "/sys/fs/cgroup$path"
    return
  fi

  path="$(
    awk -F: '$2 ~ /(^|,)memory(,|$)/ { print $3; exit }' \
      "/proc/$pid/cgroup" 2>/dev/null
  )"
  if [[ -n "$path" && -d "/sys/fs/cgroup/memory$path" ]]; then
    printf '%s' "/sys/fs/cgroup/memory$path"
  fi
}

sample_container() {
  local timestamp inspect
  local container_id status restart_count oom_killed exit_code pid started_at finished_at
  local proc_status rss hwm anon file swap threads
  local cgroup_dir cgroup_current cgroup_peak cgroup_swap
  local cgroup_oom cgroup_oom_kill pressure_some pressure_full host_available

  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if ! inspect="$(
    docker inspect --format \
      '{{.Id}}|{{.State.Status}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.State.Pid}}|{{.State.StartedAt}}|{{.State.FinishedAt}}' \
      "$container" 2>/dev/null
  )"; then
    printf '%s\t%s\tmissing\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\n' \
      "$timestamp" "$container" >> "$samples"
    return
  fi

  IFS='|' read -r container_id status restart_count oom_killed exit_code pid \
    started_at finished_at <<< "$inspect"
  proc_status="$(read_proc_status "$pid")"
  IFS=$'\t' read -r rss hwm anon file swap threads <<< "$proc_status"
  cgroup_dir="$(cgroup_directory "$pid")"

  if [[ -n "$cgroup_dir" && -e "$cgroup_dir/memory.current" ]]; then
    cgroup_current="$(read_value "$cgroup_dir/memory.current")"
    cgroup_peak="$(read_value "$cgroup_dir/memory.peak")"
    cgroup_swap="$(read_value "$cgroup_dir/memory.swap.current")"
    cgroup_oom="$(read_event_value "$cgroup_dir/memory.events" oom)"
    cgroup_oom_kill="$(read_event_value "$cgroup_dir/memory.events" oom_kill)"
    pressure_some="$(read_pressure_total "$cgroup_dir/memory.pressure" some)"
    pressure_full="$(read_pressure_total "$cgroup_dir/memory.pressure" full)"
  elif [[ -n "$cgroup_dir" ]]; then
    cgroup_current="$(read_value "$cgroup_dir/memory.usage_in_bytes")"
    cgroup_peak="$(read_value "$cgroup_dir/memory.max_usage_in_bytes")"
    cgroup_swap="$(read_value "$cgroup_dir/memory.memsw.usage_in_bytes")"
    cgroup_oom="$(read_value "$cgroup_dir/memory.failcnt")"
    cgroup_oom_kill=-
    pressure_some=-
    pressure_full=-
  else
    cgroup_current=-
    cgroup_peak=-
    cgroup_swap=-
    cgroup_oom=-
    cgroup_oom_kill=-
    pressure_some=-
    pressure_full=-
  fi

  host_available="$(
    awk '$1 == "MemAvailable:" { print $2; exit }' /proc/meminfo 2>/dev/null
  )"
  host_available="${host_available:--}"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$timestamp" "$container_id" "$status" "$restart_count" "$oom_killed" \
    "$exit_code" "$pid" "$started_at" "$finished_at" "$rss" "$hwm" \
    "$anon" "$file" "$swap" "$threads" \
    "$cgroup_current" "$cgroup_peak" "$cgroup_swap" "$cgroup_oom" \
    "$cgroup_oom_kill" "$pressure_some" "$pressure_full" "$host_available" \
    >> "$samples"
}

monitor_runtime() {
  mkdir -p "$output_dir"
  printf '%s\n' \
    $'timestamp\tcontainer_id\tstatus\trestart_count\toom_killed\texit_code\tpid\tstarted_at\tfinished_at\trss_kib\thwm_kib\tanon_kib\tfile_kib\tswap_kib\tthreads\tcgroup_current_bytes\tcgroup_peak_bytes\tcgroup_swap_bytes\tcgroup_oom\tcgroup_oom_kill\tpressure_some_total\tpressure_full_total\thost_available_kib' \
    > "$samples"
  : > "$events"

  docker inspect --format \
    'container={{.Id}} image={{.Config.Image}} started_at={{.State.StartedAt}}' \
    "$container" > "$output_dir/container.txt"

  docker events \
    --since "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --filter "container=$container" \
    --format '{{json .}}' \
    > "$events" 2> "$output_dir/docker-events.stderr" &
  events_pid=$!
  printf '%s\n' "$events_pid" > "$output_dir/docker-events.pid"

  cleanup() {
    kill "$events_pid" 2>/dev/null || true
    wait "$events_pid" 2>/dev/null || true
  }
  trap cleanup EXIT
  trap 'exit 0' INT TERM

  while true; do
    sample_container
    if ! kill -0 "$events_pid" 2>/dev/null; then
      wait "$events_pid" 2>/dev/null || true
      : > "$output_dir/docker-events.failed"
      printf 'docker events collector exited unexpectedly\n' >&2
      return 1
    fi
    : > "$output_dir/monitor-ready"
    sleep "$interval_seconds"
  done
}

numeric_summary() {
  awk -F '\t' '
    NR == 1 {
      for (field_index = 1; field_index <= NF; field_index++) {
        column[$field_index] = field_index
      }
      next
    }
    {
      rows++
      if (NF != 23) {
        invalid_samples++
        next
      }
      status_value = $(column["status"])
      oom_killed_value = $(column["oom_killed"])
      rss_value = $(column["rss_kib"])
      hwm_value = $(column["hwm_kib"])
      anon_value = $(column["anon_kib"])
      file_value = $(column["file_kib"])
      swap_value = $(column["swap_kib"])
      threads_value = $(column["threads"])
      current_value = $(column["cgroup_current_bytes"])
      peak_value = $(column["cgroup_peak_bytes"])
      cgroup_swap_value = $(column["cgroup_swap_bytes"])
      available_value = $(column["host_available_kib"])
      restart_value = $(column["restart_count"])
      oom_value = $(column["cgroup_oom"])
      oom_kill_value = $(column["cgroup_oom_kill"])
      pressure_some_value = $(column["pressure_some_total"])
      pressure_full_value = $(column["pressure_full_total"])

      sample_valid = 1
      if (status_value != "running") sample_valid = 0
      if (oom_killed_value != "false") sample_valid = 0
      if (rss_value !~ /^[0-9]+$/) sample_valid = 0
      if (hwm_value !~ /^[0-9]+$/) sample_valid = 0
      if (anon_value !~ /^[0-9]+$/) sample_valid = 0
      if (file_value !~ /^[0-9]+$/) sample_valid = 0
      if (swap_value !~ /^[0-9]+$/) sample_valid = 0
      if (threads_value !~ /^[0-9]+$/) sample_valid = 0
      if (current_value !~ /^[0-9]+$/) sample_valid = 0
      if (peak_value !~ /^[0-9]+$/) sample_valid = 0
      if (cgroup_swap_value !~ /^[0-9]+$/) sample_valid = 0
      if (available_value !~ /^[0-9]+$/) sample_valid = 0
      if (restart_value !~ /^[0-9]+$/) sample_valid = 0
      if (oom_value !~ /^[0-9]+$/) sample_valid = 0
      if (oom_kill_value !~ /^[0-9]+$/) sample_valid = 0
      if (pressure_some_value !~ /^[0-9]+$/) sample_valid = 0
      if (pressure_full_value !~ /^[0-9]+$/) sample_valid = 0
      if (!sample_valid) {
        invalid_samples++
        next
      }

      valid_samples++
      if (first_rss == "") first_rss = rss_value
      last_rss = rss_value
      if (rss_value > max_rss || max_rss == "") {
        max_rss = rss_value
      }
      if (hwm_value > max_hwm || max_hwm == "") {
        max_hwm = hwm_value
      }
      if (first_current == "") first_current = current_value
      last_current = current_value
      if (current_value > max_current || max_current == "") {
        max_current = current_value
      }
      if (peak_value > max_peak || max_peak == "") {
        max_peak = peak_value
      }
      if (min_available == "" || available_value < min_available) {
        min_available = available_value
      }
      if (first_restart == "") first_restart = restart_value
      last_restart = restart_value
      if (restart_value > max_restart || max_restart == "") max_restart = restart_value
      if (first_oom == "") first_oom = oom_value
      last_oom = oom_value
      if (oom_value > max_oom || max_oom == "") max_oom = oom_value
      if (first_oom_kill == "") first_oom_kill = oom_kill_value
      last_oom_kill = oom_kill_value
      if (oom_kill_value > max_oom_kill || max_oom_kill == "") {
        max_oom_kill = oom_kill_value
      }
    }
    END {
      printf "samples=%d\n", rows
      printf "valid_samples=%d\n", valid_samples
      printf "invalid_samples=%d\n", invalid_samples
      printf "rss_first_kib=%s\n", (first_rss == "" ? "-" : first_rss)
      printf "rss_last_kib=%s\n", (last_rss == "" ? "-" : last_rss)
      printf "rss_delta_kib=%s\n", \
        (first_rss == "" ? "-" : last_rss - first_rss)
      printf "rss_peak_kib=%s\n", (max_rss == "" ? "-" : max_rss)
      printf "hwm_peak_kib=%s\n", (max_hwm == "" ? "-" : max_hwm)
      printf "cgroup_current_first_bytes=%s\n", \
        (first_current == "" ? "-" : first_current)
      printf "cgroup_current_last_bytes=%s\n", \
        (last_current == "" ? "-" : last_current)
      printf "cgroup_current_delta_bytes=%s\n", \
        (first_current == "" ? "-" : last_current - first_current)
      printf "cgroup_current_peak_bytes=%s\n", (max_current == "" ? "-" : max_current)
      printf "cgroup_reported_peak_bytes=%s\n", (max_peak == "" ? "-" : max_peak)
      printf "host_available_min_kib=%s\n", (min_available == "" ? "-" : min_available)
      printf "restart_max=%s\n", (max_restart == "" ? "-" : max_restart)
      printf "restart_delta=%d\n", \
        (first_restart == "" ? 0 : last_restart - first_restart)
      printf "cgroup_oom_first=%s\n", (first_oom == "" ? "-" : first_oom)
      printf "cgroup_oom_last=%s\n", (last_oom == "" ? "-" : last_oom)
      printf "cgroup_oom_max=%s\n", (max_oom == "" ? "-" : max_oom)
      printf "cgroup_oom_delta=%d\n", \
        (first_oom == "" ? 0 : last_oom - first_oom)
      printf "cgroup_oom_kill_first=%s\n", \
        (first_oom_kill == "" ? "-" : first_oom_kill)
      printf "cgroup_oom_kill_last=%s\n", \
        (last_oom_kill == "" ? "-" : last_oom_kill)
      printf "cgroup_oom_kill_max=%s\n", \
        (max_oom_kill == "" ? "-" : max_oom_kill)
      printf "cgroup_oom_kill_delta=%d\n", \
        (first_oom_kill == "" ? 0 : last_oom_kill - first_oom_kill)
    }
  ' "$samples"
}

gate_runtime() {
  local inspect status restart_count oom_killed exit_code
  local summary event_oom_count event_die_count event_restart_count
  local samples_count valid_samples invalid_samples rss_peak cgroup_peak
  local restart_max restart_delta oom_max oom_delta oom_kill_max oom_kill_delta
  local failed=0

  mkdir -p "$output_dir"
  if [[ ! -s "$samples" ]]; then
    printf 'telemetry samples are missing\n' >&2
    return 1
  fi

  inspect="$(
    docker inspect --format \
      '{{.State.Status}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.ExitCode}}' \
      "$container"
  )"
  IFS='|' read -r status restart_count oom_killed exit_code <<< "$inspect"

  summary="$(numeric_summary)"
  samples_count="$(awk -F= '$1 == "samples" { print $2 }' <<< "$summary")"
  valid_samples="$(awk -F= '$1 == "valid_samples" { print $2 }' <<< "$summary")"
  invalid_samples="$(awk -F= '$1 == "invalid_samples" { print $2 }' <<< "$summary")"
  rss_peak="$(awk -F= '$1 == "rss_peak_kib" { print $2 }' <<< "$summary")"
  cgroup_peak="$(awk -F= '$1 == "cgroup_reported_peak_bytes" { print $2 }' <<< "$summary")"
  restart_max="$(awk -F= '$1 == "restart_max" { print $2 }' <<< "$summary")"
  restart_delta="$(awk -F= '$1 == "restart_delta" { print $2 }' <<< "$summary")"
  oom_max="$(awk -F= '$1 == "cgroup_oom_max" { print $2 }' <<< "$summary")"
  oom_delta="$(awk -F= '$1 == "cgroup_oom_delta" { print $2 }' <<< "$summary")"
  oom_kill_max="$(awk -F= '$1 == "cgroup_oom_kill_max" { print $2 }' <<< "$summary")"
  oom_kill_delta="$(awk -F= '$1 == "cgroup_oom_kill_delta" { print $2 }' <<< "$summary")"
  event_oom_count="$(grep -Ec '"(Action|status)":"oom"' "$events" 2>/dev/null || true)"
  event_die_count="$(grep -Ec '"(Action|status)":"die"' "$events" 2>/dev/null || true)"
  event_restart_count="$(grep -Ec '"(Action|status)":"restart"' "$events" 2>/dev/null || true)"

  {
    printf 'status=%s\n' "$status"
    printf 'restart_count=%s\n' "$restart_count"
    printf 'oom_killed=%s\n' "$oom_killed"
    printf 'exit_code=%s\n' "$exit_code"
    printf '%s\n' "$summary"
    printf 'docker_oom_events=%s\n' "$event_oom_count"
    printf 'docker_die_events=%s\n' "$event_die_count"
    printf 'docker_restart_events=%s\n' "$event_restart_count"
  } > "$output_dir/runtime-summary.txt"

  if [[ "$status" != "running" ]]; then
    printf 'SurrealDB is not running: %s\n' "$status" >&2
    failed=1
  fi
  if [[ "$restart_count" != "0" || "$restart_delta" != "0" ]]; then
    printf 'SurrealDB restarted: count=%s delta=%s\n' "$restart_count" "$restart_delta" >&2
    failed=1
  fi
  if [[ "$restart_max" != "0" ]]; then
    printf 'SurrealDB telemetry observed a nonzero restart count\n' >&2
    failed=1
  fi
  if [[ "$oom_killed" == "true" || "$oom_max" != "0" || "$oom_delta" != "0" ||
    "$oom_kill_max" != "0" || "$oom_kill_delta" != "0" ]]; then
    printf 'SurrealDB cgroup recorded OOM activity\n' >&2
    failed=1
  fi
  if [[ "$event_oom_count" != "0" || "$event_die_count" != "0" || "$event_restart_count" != "0" ]]; then
    printf 'SurrealDB lifecycle events recorded runtime integrity loss\n' >&2
    failed=1
  fi
  if ((samples_count < 2 || valid_samples < 2 || invalid_samples != 0)) ||
    [[ "$rss_peak" == "-" || "$cgroup_peak" == "-" ]]; then
    printf 'SurrealDB telemetry is incomplete\n' >&2
    failed=1
  fi
  if [[ ! -e "$events" || ! -s "$output_dir/docker-events.pid" ||
    ! -e "$output_dir/monitor-ready" ]]; then
    printf 'SurrealDB lifecycle telemetry did not initialize\n' >&2
    failed=1
  fi
  for marker in \
    docker-events.failed \
    docker-events-orphaned \
    monitor-force-killed \
    monitor-unexpected-exit; do
    if [[ -e "$output_dir/$marker" ]]; then
      printf 'SurrealDB telemetry failure marker: %s\n' "$marker" >&2
      failed=1
    fi
  done

  if ((failed)); then
    printf 'result=fail\n' >> "$output_dir/runtime-summary.txt"
    return 1
  fi
  printf 'result=pass\n' >> "$output_dir/runtime-summary.txt"
  cat "$output_dir/runtime-summary.txt"
}

case "$command" in
  monitor)
    monitor_runtime
    ;;
  gate)
    gate_runtime
    ;;
esac
