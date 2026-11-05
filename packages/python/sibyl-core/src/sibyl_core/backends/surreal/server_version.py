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
# conversion opt-in, it refuses what would open existing data wrong. First the
# pairing alone: a versioned image, an absolute path, 3.x only on rocksdb://
# and 4.x only on surrealkv://, since a 3.x server on surrealkv:// serves an
# empty database beside its data. Then the directory: it must be readable, a
# 3.x image refuses anything SurrealDB 4 wrote, a lone _rocksdb_backup/ is
# refused, and 4.x on RocksDB files needs the opt-in, because converting is
# one-way. Every compose file and CLI runtime runs it before the server
# starts; a compose file escapes each $.
SURREAL_STORE_GUARD = """\
# Refuse a SurrealDB image and store path that would open the data wrong.
refuse() {
  echo "surreal-init: $1" >&2
  echo "surreal-init: nothing was started; see docs/deployment/surrealdb-4-upgrade.md" >&2
  exit 1
}
# First, the pairing itself, whatever the directory holds: Sibyl runs 3.x
# only on rocksdb:// and 4.x only on surrealkv://, at an absolute path.
ref="${SURREAL_IMAGE%%@*}"
tag="${ref##*/}"
case "$tag" in *:*) tag="${tag##*:}" ;; *) tag="" ;; esac
tag="${tag#v}"
major="${tag%%.*}"
case "$major" in "" | *[!0-9]*) refuse "the image $SURREAL_IMAGE names no version; pin a tag such as v4.0.0" ;; esac
case "$SURREAL_STORE" in
  rocksdb:///*) scheme=rocksdb ;;
  surrealkv:///*) scheme=surrealkv ;;
  *) refuse "the store path $SURREAL_STORE is not an absolute rocksdb:// or surrealkv:// path" ;;
esac
store="${SURREAL_STORE#*://}"
if [ "$major" -lt 4 ] && [ "$scheme" != rocksdb ]; then
  refuse "a $major.x server runs only on a rocksdb:// path; on $SURREAL_STORE it serves an empty database beside the data"
fi
if [ "$major" -ge 4 ] && [ "$scheme" != surrealkv ]; then
  refuse "a $major.x server runs on a surrealkv:// path, not $SURREAL_STORE"
fi
# Then what the directory holds.
readable() { (cd "$1" && ls . > /dev/null) 2> /dev/null; }
if ! readable "${store%/*}/"; then
  refuse "cannot read ${store%/*}/ to check which store it holds"
fi
if [ -e "$store" ] && ! readable "$store"; then
  refuse "cannot read $store to check which store it holds"
fi
if [ "$major" -lt 4 ] && { [ -e "$store/manifest" ] || [ -e "$store/_rocksdb_backup" ]; }; then
  refuse "$store holds a store SurrealDB 4 wrote, which a 3.x server opens as an empty database"
fi
if [ -e "$store/_rocksdb_backup" ] && [ ! -e "$store/manifest" ]; then
  refuse "$store holds only the 3.x copy a conversion keeps in _rocksdb_backup/, so any server would start an empty database beside it; restore or remove it first"
fi
if [ "$major" -ge 4 ] && [ -e "$store/CURRENT" ] && [ "${SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X:-}" != true ]; then
  refuse "$store holds a store a SurrealDB 3.x server wrote; 4.0 converts it once and for good: back it up, then start once with SURREAL_EXPERIMENTAL_UPGRADE_FROM_3X=true"
fi
"""


def is_supported_server_version(reported: str) -> bool:
    """Whether a server's reported version is exactly the supported release."""
    return reported.split("+", 1)[0] == SURREAL_SERVER_VERSION_STRING
