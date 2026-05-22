#!/usr/bin/env bash
set -euo pipefail

: "${PHOTOME_RUN_UID:=1000}"
: "${PHOTOME_RUN_GID:=1000}"
: "${PHOTOME_RUN_USER:=photome}"
: "${PHOTOME_HOME:=/var/lib/photome}"

export HOME="${HOME:-$PHOTOME_HOME}"

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
  mkdir -p "$PHOTOME_HOME" /var/lib/photome/data /var/lib/photome/derived /var/lib/photome/models
  ensure_runtime_identity "$PHOTOME_RUN_UID" "$PHOTOME_RUN_GID" "$PHOTOME_RUN_USER" "$PHOTOME_HOME"
  chown -R "$PHOTOME_RUN_UID:$PHOTOME_RUN_GID" "$PHOTOME_HOME" /var/lib/photome/data /var/lib/photome/derived /var/lib/photome/models 2>/dev/null || true
  exec gosu "$PHOTOME_RUN_UID:$PHOTOME_RUN_GID" "$@"
fi

# If an operator still runs the container with Docker's numeric --user flag,
# keep HOME explicit so libraries do not need pwd.getpwuid() just to locate a home dir.
export HOME="${HOME:-$PHOTOME_HOME}"
exec "$@"
