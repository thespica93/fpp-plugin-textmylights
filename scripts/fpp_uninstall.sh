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

# Remove ALL plugin data, including STORED CREDENTIALS, so a later reinstall
# starts clean and never remembers the old account. This directory holds:
#   • secrets/credentials.json  — Twilio auth token + Gmail app password
#   • plugin.json               — Gmail address, Twilio account SID / phone number
#   • blocked_phones.json, queue, message history logs (phone-number PII)
log_and_show "Removing plugin data and stored credentials..."
rm -rf /home/fpp/media/plugin.fpp-textmylights
rm -rf /home/fpp/media/plugin.fpp-sms-twilio          # pre-rename data dir (older installs)

# Legacy scattered config paths from versions before the data-dir migration.
rm -f /home/fpp/media/config/plugin.fpp-textmylights.json
rm -f /home/fpp/media/config/plugin.fpp-sms-twilio.json
rm -f /home/fpp/media/config/blacklist.txt
rm -f /home/fpp/media/config/whitelist.txt
rm -f /home/fpp/media/config/blocked_phones.json
rm -f /home/fpp/media/config/received_messages.json
rm -f /home/fpp/media/config/last_message_sid.txt

if [ -e /home/fpp/media/plugin.fpp-textmylights ]; then
    log_and_show "⚠ Could NOT fully remove the plugin data dir — check permissions/ownership"
else
    log_and_show "✓ Plugin data and stored credentials removed"
fi

# Remove the system integration this plugin added outside its own directory: the
# sudoers rule and the root-owned shared-memory permission helper. Leaving the
# sudoers rule behind would keep granting the fpp user a root command after the
# plugin is gone.
log_and_show "Removing sudoers rule and privileged helper..."
rm -f /etc/sudoers.d/90-fpp-sms-shm
rm -f /usr/local/bin/tml-fix-shm-perms
log_and_show "✓ Sudoers rule and helper removed"

# Remove log files
log_and_show "Removing log files..."
rm -f /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log
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
