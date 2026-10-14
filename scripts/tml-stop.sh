#!/bin/bash
###############################################################################
# Text My Lights - stop the plugin daemon
###############################################################################
# Stops the sms_plugin.py daemon using the PID file it writes at startup, rather
# than `pkill -f sms_plugin.py`, which can match unrelated processes (an editor
# or a backup that happens to have the filename on its command line). The PID
# path mirrors PID_FILE in sms_plugin.py.
#
# Shared by preStop.sh, postStart.sh (stop-before-start), the installer and the
# uninstaller so there is exactly one stop implementation.
#
# The PID path mirrors PID_FILE in sms_plugin.py.
PID_FILE="/home/fpp/media/plugindata/fpp-plugin-textmylights/sms_plugin.pid"

_wait_gone() {  # $1 = pid; returns 0 once the process is gone, 1 on timeout (~3s)
    for _i in $(seq 1 30); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.1; done
    return 1
}

if [ -f "$PID_FILE" ]; then
    PID="$(cat "$PID_FILE" 2>/dev/null)"
    # Only kill if the PID is live AND is actually our daemon — guards against a
    # stale PID file whose number has since been recycled by an unrelated process.
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null \
        && grep -qa sms_plugin.py "/proc/$PID/cmdline" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true          # SIGTERM: clean shutdown
        _wait_gone "$PID" || kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
else
    # No PID file: a daemon started by a version that predates it. Fall back to a
    # precise match (the python3 interpreter running THIS script), not a broad
    # `-f sms_plugin.py`. This path only matters during the one-time upgrade.
    pkill -f "python3.*sms_plugin\.py" 2>/dev/null || true
fi

exit 0
