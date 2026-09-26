#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Comprehensive Uninstall Script
###############################################################################

LOG="/home/fpp/media/logs/sms_plugin_uninstall.log"

# Function to log and display
log_and_show() {
    echo "$1" | tee -a "$LOG"
}

log_and_show "========================================"
log_and_show "FPP SMS Twilio Plugin Uninstaller"
log_and_show "$(date)"
log_and_show "========================================"

# Stop the service
log_and_show "Stopping SMS Twilio service..."
pkill -f sms_plugin.py 2>/dev/null || true
sleep 2
log_and_show "✓ Service stopped"

# Remove Python packages
log_and_show "Removing Python packages..."
pip3 uninstall -y --break-system-packages twilio >> "$LOG" 2>&1 && log_and_show "✓ Twilio removed"
pip3 uninstall -y --break-system-packages flask >> "$LOG" 2>&1 && log_and_show "✓ Flask removed"
pip3 uninstall -y --break-system-packages requests >> "$LOG" 2>&1 && log_and_show "✓ Requests removed"

# Remove configuration files
log_and_show "Removing configuration files..."
rm -f /home/fpp/media/config/plugin.fpp-textmylights.json
rm -f /home/fpp/media/config/blacklist.txt
rm -f /home/fpp/media/config/whitelist.txt
rm -f /home/fpp/media/config/blocked_phones.json
rm -f /home/fpp/media/config/received_messages.json
log_and_show "✓ Configuration files removed"

# Remove log files
log_and_show "Removing log files..."
rm -f /home/fpp/media/logs/sms_plugin.log
rm -f /home/fpp/media/logs/sms_plugin_install.log
rm -f /home/fpp/media/logs/received_messages.json
log_and_show "✓ Log files removed"

log_and_show "========================================"
log_and_show "✅ Uninstall complete!"
log_and_show "All plugin files and dependencies removed"
log_and_show "========================================"

# No errors — remove the uninstall log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
