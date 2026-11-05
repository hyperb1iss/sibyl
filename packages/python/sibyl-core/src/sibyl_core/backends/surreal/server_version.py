"""The SurrealDB server release Sibyl supports.

Sibyl supports one server release at a time. Code that must name it imports
it from here; the deployment surfaces that cannot import (compose files, the
Helm chart, the Ansible role, CI workflows) carry it as a literal image tag,
and `tools/tests/test_surreal_server_pins.py` holds every one of them to this
value.
"""

SURREAL_SERVER_VERSION = "4.0.0"
SURREAL_SERVER_IMAGE = f"surrealdb/surrealdb:v{SURREAL_SERVER_VERSION}"
# What the server's version RPC answers for that release, before any
# `+build` suffix.
SURREAL_SERVER_VERSION_STRING = f"surrealdb-{SURREAL_SERVER_VERSION}"


# Storage settings every SurrealDB server Sibyl starts carries, the start that
# converts a 3.x store included. Value-log separation stays off: every rewrite
# of a full-text term bitmap would land in value-log files that nothing ever
# reclaims, and a directory that once holds them keeps the value log on
# whatever this says. The same parity test holds each deployment surface's
# server environment to this.
SURREAL_STORAGE_ENV: dict[str, str] = {"SURREAL_SURREALKV_ENABLE_VLOG": "false"}


# A POSIX sh prelude for the one-shot that prepares a server's data directory.
# Given SURREAL_IMAGE, SURREAL_STORE (the path the server opens) and the
# conversion opt-in, it refuses the pairs that would open existing data wrong:
# a 3.x image with surrealkv:// on a RocksDB store or rocksdb:// on a SurrealKV
# store serves an empty database beside the data, and a 4.x image on a RocksDB
# store converts it one way, so that needs the opt-in. Every compose file and
# CLI runtime runs it before the server starts; a compose file escapes each $.
SURREAL_STORE_GUARD = """\
# Refuse a SurrealDB image and store path that would open the data wrong.
store="${SURREAL_STORE#*://}"
case "$store" in /*) ;; *) store="" ;; esac
ref="${SURREAL_IMAGE%%@*}"
tag="${ref##*/}"
case "$tag" in *:*) tag="${tag##*:}" ;; *) tag="" ;; esac
tag="${tag#v}"
major="${tag%%.*}"
case "$major" in "" | *[!0-9]*) major="" ;; esac
convert="${SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X:-}"
refuse() {
  echo "surreal-init: $1" >&2
  echo "surreal-init: nothing was started; see docs/deployment/surrealdb-4-upgrade.md" >&2
  exit 1
}
if [ -n "$store" ] && [ -e "$store" ] && ! ls "$store" > /dev/null 2>&1; then
  refuse "cannot read $store to check which store it holds"
fi
if [ -n "$store" ] && [ -d "$store/manifest" ]; then
  case "$SURREAL_STORE" in
    rocksdb://*)
      if [ "$major" = 3 ]; then
        refuse "$store holds a SurrealKV store; a 3.x server opened with rocksdb:// on it serves an empty database and leaves files that stop 4.0 from opening the store"
      fi
      ;;
  esac
elif [ -n "$store" ] && [ -f "$store/CURRENT" ]; then
  case "$SURREAL_STORE" in
    surrealkv://*)
      if [ "$major" = 3 ]; then
        refuse "a 3.x server opened with surrealkv:// serves an empty database beside the 3.x store in $store; a 3.x image keeps the rocksdb:// store path"
      fi
      if [ "$convert" != true ]; then
        refuse "$store holds a store a SurrealDB 3.x server wrote; 4.0 converts it once and for good: back it up, then start once with SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true"
      fi
      ;;
    *)
      if [ -n "$major" ] && [ "$major" -ge 4 ] && [ "$convert" != true ]; then
        refuse "$store holds a store a SurrealDB 3.x server wrote; 4.0 converts it once and for good: back it up, then start once with SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true"
      fi
      ;;
  esac
fi
"""


def is_supported_server_version(reported: str) -> bool:
    """Whether a server's reported version is exactly the supported release."""
    return reported.split("+", 1)[0] == SURREAL_SERVER_VERSION_STRING
