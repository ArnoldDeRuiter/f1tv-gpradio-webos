#!/bin/sh
# Starts gpradio_overlay.py in the background, unless it's already running.
# Kept as its own file for the same reason start-loginfill.sh is: avoids
# shell-quoting mangling through the exec-bridge chain.
DIR="$(dirname "$0")"
PIDFILE=/tmp/f1tvgpradio-overlay.pid

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0
fi

nohup python3 "$DIR/gpradio_overlay.py" >/tmp/f1tvgpradio-overlay.log 2>&1 &
echo $! > "$PIDFILE"
