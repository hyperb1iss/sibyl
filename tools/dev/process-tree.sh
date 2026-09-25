#!/usr/bin/env bash

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

process_pgid() {
  local pid="${1:-}"

  if [[ -z "$pid" ]]; then
    return 1
  fi

  ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]'
}

process_state() {
  local pid="${1:-}"

  if [[ -z "$pid" ]]; then
    return 1
  fi

  ps -o stat= -p "$pid" 2>/dev/null | tr -d '[:space:]'
}

process_is_zombie() {
  local pid="${1:-}"
  local state=""

  if [[ -z "$pid" ]]; then
    return 1
  fi

  state="$(process_state "$pid")"
  [[ "$state" == Z* ]]
}

process_is_group_leader() {
  local pid="${1:-}"
  local pgid=""

  if [[ -z "$pid" ]]; then
    return 1
  fi

  pgid="$(process_pgid "$pid")"
  [[ -n "$pgid" && "$pgid" == "$pid" ]]
}

process_tree_alive() {
  local pid="${1:-}"
  local child=""

  if [[ -z "$pid" ]]; then
    return 1
  fi

  if process_is_zombie "$pid"; then
    return 1
  fi

  if process_is_group_leader "$pid" && kill -0 -- "-$pid" 2>/dev/null; then
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

  if process_is_group_leader "$pid"; then
    kill "-$signal" -- "-$pid" 2>/dev/null || true
  else
    kill "-$signal" "$pid" 2>/dev/null || true
  fi

  if ((${#descendants[@]} > 0)); then
    for child in "${descendants[@]}"; do
      if process_is_group_leader "$child"; then
        kill "-$signal" -- "-$child" 2>/dev/null || true
      fi
      kill "-$signal" "$child" 2>/dev/null || true
    done
  fi
}

process_cwd() {
  local pid="${1:-}"

  if [[ -z "$pid" ]]; then
    return 1
  fi

  if [[ -r "/proc/$pid/cwd" ]]; then
    readlink "/proc/$pid/cwd" 2>/dev/null
    return
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
    return
  fi

  return 1
}

# True when the process runs from inside the given workspace root. Pattern
# matches on command lines are machine wide, and several checkouts of this
# repo run the same commands, so a stop from one workspace must leave the
# others alone.
process_in_workspace() {
  local pid="${1:-}"
  local root="${2:-}"
  local cwd=""

  if [[ -z "$pid" || -z "$root" ]]; then
    return 1
  fi

  cwd="$(process_cwd "$pid")"
  if [[ -z "$cwd" ]]; then
    return 1
  fi

  [[ "$cwd" == "$root" || "$cwd" == "$root"/* ]]
}
