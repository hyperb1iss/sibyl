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
