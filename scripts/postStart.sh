#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Post Start Script
# This runs automatically after FPPD starts
###############################################################################

PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-textmylights"

# Stop any existing service, then wait (bounded) for it to actually exit instead of a
# flat sleep that would block fppd startup for a fixed time on every run.
pkill -f sms_plugin.py 2>/dev/null || true
for _i in $(seq 1 20); do pgrep -f sms_plugin.py >/dev/null 2>&1 || break; sleep 0.1; done

# NOTE: shared-memory permissions are fixed at RUNTIME (per overlay model, scoped to the
# fpp user/group) by the root helper tml-fix-shm-perms - see render_to_shm() in sms_plugin.py.
# FPP creates /dev/shm/FPP-Model-Data-* as root AFTER this script runs, so there is nothing
# to chmod here anyway.

# Start the service as fpp user
cd "$PLUGIN_DIR"
su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /dev/null 2>/home/fpp/media/logs/plugin-fpp-plugin-textmylights.log &"

exit 0
