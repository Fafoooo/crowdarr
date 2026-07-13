#!/bin/sh
set -eu

fail() {
  echo "crowdarrr entrypoint: $*" >&2
  exit 1
}

is_uint() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-0022}"
CROWDARRR_DATA_DIR="${CROWDARRR_DATA_DIR:-/config}"
CROWDARRR_HOST="${CROWDARRR_HOST:-0.0.0.0}"
CROWDARRR_PORT="${CROWDARRR_PORT:-8000}"
CROWDARRR_LOG_LEVEL="${CROWDARRR_LOG_LEVEL:-info}"

is_uint "$PUID" || fail "PUID must be a non-negative integer"
is_uint "$PGID" || fail "PGID must be a non-negative integer"
is_uint "$CROWDARRR_PORT" || fail "CROWDARRR_PORT must be an integer"
[ "$PUID" -gt 0 ] || fail "PUID 0 is not supported"
[ "$PGID" -gt 0 ] || fail "PGID 0 is not supported"
[ "$CROWDARRR_PORT" -ge 1 ] && [ "$CROWDARRR_PORT" -le 65535 ] \
  || fail "CROWDARRR_PORT must be between 1 and 65535"
case "$UMASK" in
  0[0-7][0-7][0-7]|[0-7][0-7][0-7]) ;;
  *) fail "UMASK must be a three- or four-digit octal value" ;;
esac
[ -n "$CROWDARRR_DATA_DIR" ] || fail "CROWDARRR_DATA_DIR cannot be empty"
[ "$CROWDARRR_DATA_DIR" = "/config" ] \
  || fail "CROWDARRR_DATA_DIR is fixed at /config in the container; change the host bind source instead"

export CROWDARRR_DATA_DIR CROWDARRR_HOST CROWDARRR_PORT CROWDARRR_LOG_LEVEL

umask "$UMASK"

if [ "$(id -u)" -eq 0 ]; then
  if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    printf '%s\n' "$TZ" > /etc/timezone
  fi

  groupmod --non-unique --gid "$PGID" crowdarrr
  usermod --non-unique --uid "$PUID" --gid "$PGID" --home "$CROWDARRR_DATA_DIR" crowdarrr
  mkdir -p "$CROWDARRR_DATA_DIR"
  [ ! -L "$CROWDARRR_DATA_DIR" ] || fail "CROWDARRR_DATA_DIR must not be a symlink"
  if [ -d "$CROWDARRR_DATA_DIR/etc" ] && [ -d "$CROWDARRR_DATA_DIR/usr" ]; then
    fail "CROWDARRR_DATA_DIR appears to contain a mounted host root"
  fi
  chown --no-dereference "$PUID:$PGID" "$CROWDARRR_DATA_DIR"
  for state_name in \
    crowdarrr.sqlite3 \
    crowdarrr.sqlite3-journal \
    crowdarrr.sqlite3-shm \
    crowdarrr.sqlite3-wal \
    crowdarrr.sqlite3.key
  do
    state_path="$CROWDARRR_DATA_DIR/$state_name"
    [ ! -L "$state_path" ] \
      || fail "$state_path must not be a symlink"
    if [ -e "$state_path" ]; then
      [ -f "$state_path" ] \
        || fail "$state_path must be a regular file"
      [ "$(stat --format='%h' "$state_path")" -eq 1 ] \
        || fail "$state_path must not be hard-linked"
      chown --no-dereference "$PUID:$PGID" "$state_path"
    fi
  done
  export HOME="$CROWDARRR_DATA_DIR"
else
  mkdir -p "$CROWDARRR_DATA_DIR" 2>/dev/null \
    || fail "cannot create $CROWDARRR_DATA_DIR as uid $(id -u)"
  export HOME="${HOME:-$CROWDARRR_DATA_DIR}"
fi

if [ "$#" -eq 0 ] || [ "$1" = "serve" ]; then
  set -- uvicorn backend.main:app \
    --host "$CROWDARRR_HOST" \
    --port "$CROWDARRR_PORT" \
    --log-level "$CROWDARRR_LOG_LEVEL"
fi

if [ "$(id -u)" -eq 0 ]; then
  exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
