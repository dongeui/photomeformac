#!/usr/bin/env bash
set -euo pipefail

: "${TROVE_RUN_UID:=1000}"
: "${TROVE_RUN_GID:=1000}"
: "${TROVE_RUN_USER:=trove}"
: "${TROVE_HOME:=/var/lib/trove}"

export HOME="${HOME:-$TROVE_HOME}"

ensure_runtime_identity() {
  local uid="$1"
  local gid="$2"
  local user="$3"
  local home="$4"

  if ! getent group "$gid" >/dev/null 2>&1; then
    groupadd --gid "$gid" "$user" >/dev/null 2>&1 || groupadd --gid "$gid" "${user}-${gid}"
  fi

  if ! getent passwd "$uid" >/dev/null 2>&1; then
    local group_name
    group_name="$(getent group "$gid" | cut -d: -f1)"
    useradd --uid "$uid" --gid "$gid" --home-dir "$home" --shell /usr/sbin/nologin --no-create-home "$user" \
      >/dev/null 2>&1 || true
  fi
}

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$TROVE_HOME" /var/lib/trove/data /var/lib/trove/derived /var/lib/trove/models
  ensure_runtime_identity "$TROVE_RUN_UID" "$TROVE_RUN_GID" "$TROVE_RUN_USER" "$TROVE_HOME"
  chown -R "$TROVE_RUN_UID:$TROVE_RUN_GID" "$TROVE_HOME" /var/lib/trove/data /var/lib/trove/derived /var/lib/trove/models 2>/dev/null || true
  exec gosu "$TROVE_RUN_UID:$TROVE_RUN_GID" "$@"
fi

# If an operator still runs the container with Docker's numeric --user flag,
# keep HOME explicit so libraries do not need pwd.getpwuid() just to locate a home dir.
export HOME="${HOME:-$TROVE_HOME}"
exec "$@"
