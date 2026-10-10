#!/bin/sh
# linuxserver-style startup: run as PUID/PGID, honour TZ and UMASK.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/downloads}"
PORT="${PORT:-7341}"

if [ -n "$TZ" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

[ -n "$UMASK" ] && umask "$UMASK"

mkdir -p "$CONFIG_DIR" "$DOWNLOAD_DIR"

if [ "$(id -u)" = "0" ]; then
    echo "webui-dl: running as uid=$PUID gid=$PGID, TZ=${TZ:-UTC}"
    # Fix ownership inside the config folder only (never touches your downloads).
    find "$CONFIG_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} + 2>/dev/null || true
    # Make the top of the downloads folder writable if it is a fresh, root-owned mount.
    if [ "$(stat -c %u "$DOWNLOAD_DIR")" = "0" ] && [ -z "$(ls -A "$DOWNLOAD_DIR" 2>/dev/null)" ]; then
        chown "$PUID:$PGID" "$DOWNLOAD_DIR"
    fi
    export HOME="$CONFIG_DIR"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
        uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
fi

export HOME="${HOME:-$CONFIG_DIR}"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
