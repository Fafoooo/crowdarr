#!/bin/sh
set -eu

fail() {
  echo "crowdarr entrypoint: $*" >&2
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
DATA_DIR="${CROWDARR_DATA_DIR-${CROWDARRR_DATA_DIR-/config}}"
HOST="${CROWDARR_HOST-${CROWDARRR_HOST-0.0.0.0}}"
PORT="${CROWDARR_PORT-${CROWDARRR_PORT-8000}}"
LOG_LEVEL="${CROWDARR_LOG_LEVEL-${CROWDARRR_LOG_LEVEL-info}}"

is_uint "$PUID" || fail "PUID must be a non-negative integer"
is_uint "$PGID" || fail "PGID must be a non-negative integer"
is_uint "$PORT" || fail "CROWDARR_PORT must be an integer"
[ "$PUID" -gt 0 ] || fail "PUID 0 is not supported"
[ "$PGID" -gt 0 ] || fail "PGID 0 is not supported"
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || fail "CROWDARR_PORT must be between 1 and 65535"
case "$UMASK" in
  0[0-7][0-7][0-7]|[0-7][0-7][0-7]) ;;
  *) fail "UMASK must be a three- or four-digit octal value" ;;
esac
[ -n "$DATA_DIR" ] || fail "CROWDARR_DATA_DIR cannot be empty"
[ "$DATA_DIR" = "/config" ] \
  || fail "CROWDARR_DATA_DIR is fixed at /config in the container; change the host bind source instead"

export CROWDARR_DATA_DIR="$DATA_DIR"
export CROWDARR_HOST="$HOST"
export CROWDARR_PORT="$PORT"
export CROWDARR_LOG_LEVEL="$LOG_LEVEL"
export CROWDARRR_DATA_DIR="$DATA_DIR"
export CROWDARRR_HOST="$HOST"
export CROWDARRR_PORT="$PORT"
export CROWDARRR_LOG_LEVEL="$LOG_LEVEL"

umask "$UMASK"

if [ "$(id -u)" -eq 0 ]; then
  if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    printf '%s\n' "$TZ" > /etc/timezone
  fi

  groupmod --non-unique --gid "$PGID" crowdarr
  usermod --non-unique --uid "$PUID" --gid "$PGID" --home "$DATA_DIR" crowdarr
  mkdir -p "$DATA_DIR"
  [ ! -L "$DATA_DIR" ] || fail "CROWDARR_DATA_DIR must not be a symlink"
  if [ -d "$DATA_DIR/etc" ] && [ -d "$DATA_DIR/usr" ]; then
    fail "CROWDARR_DATA_DIR appears to contain a mounted host root"
  fi
  chown --no-dereference "$PUID:$PGID" "$DATA_DIR"
  for state_name in \
    crowdarr.sqlite3 \
    crowdarr.sqlite3-journal \
    crowdarr.sqlite3-shm \
    crowdarr.sqlite3-wal \
    crowdarr.sqlite3.key \
    crowdarrr.sqlite3 \
    crowdarrr.sqlite3-journal \
    crowdarrr.sqlite3-shm \
    crowdarrr.sqlite3-wal \
    crowdarrr.sqlite3.key
  do
    state_path="$DATA_DIR/$state_name"
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
  export HOME="$DATA_DIR"
else
  mkdir -p "$DATA_DIR" 2>/dev/null \
    || fail "cannot create $DATA_DIR as uid $(id -u)"
  export HOME="${HOME:-$DATA_DIR}"
fi

if [ "$#" -eq 0 ] || [ "$1" = "serve" ]; then
  set -- uvicorn backend.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level "$LOG_LEVEL"
fi

if [ "$(id -u)" -eq 0 ]; then
  exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
