#!/bin/sh
# Starts loginfill.py in the background, unless it's already running.
# Kept as its own file (rather than inlined in index.html's exec-bridge
# call) to avoid multiple layers of shell-quoting mangling a one-liner --
# same lesson learned building family7-webos's start-scrollfix.sh.
DIR="$(dirname "$0")"
PIDFILE=/tmp/f1tvgpradio-loginfill.pid

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0
fi

nohup python3 "$DIR/loginfill.py" >/tmp/f1tvgpradio-loginfill.log 2>&1 &
echo $! > "$PIDFILE"
