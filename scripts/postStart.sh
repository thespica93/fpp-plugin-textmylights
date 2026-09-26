#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Post Start Script
# This runs automatically after FPPD starts
###############################################################################

PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-textmylights"

# Stop any existing service
pkill -f sms_plugin.py 2>/dev/null || true
sleep 1

# Grant fpp user write access to FPP shared memory files (created by root on FPPD start)
chmod 666 /dev/shm/FPP-Model-Data-* 2>/dev/null || true

# Start the service as fpp user
cd "$PLUGIN_DIR"
su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /dev/null 2>/home/fpp/media/logs/sms_plugin.log &"

exit 0
