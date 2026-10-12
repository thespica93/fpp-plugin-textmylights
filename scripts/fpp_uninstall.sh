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

# Stop the service via the PID file (shared helper), which waits for it to exit
# and won't match unrelated processes the way `pkill -f sms_plugin.py` could.
log_and_show "Stopping Text My Lights service..."
/home/fpp/media/plugins/fpp-plugin-textmylights/scripts/tml-stop.sh 2>/dev/null || true
log_and_show "✓ Service stopped"

# NOTE: we deliberately do NOT remove the Python packages (flask, twilio,
# requests, pillow, zstandard). They are installed via apt as declared
# dependencies in pluginInfo.json; FPP reference-counts those and removes them on
# uninstall only when no other plugin needs them, and never touches packages that
# were already on the image. Running `pip3 uninstall` here would rip shared
# libraries out from under other plugins that import them.

# Remove ALL plugin data, including STORED CREDENTIALS, so a later reinstall
# starts clean and never remembers the old account. This directory holds:
#   • secrets/credentials.json  — Twilio auth token + Gmail app password
#   • plugin.json               — Gmail address, Twilio account SID / phone number
#   • blocked_phones.json, queue, message history logs (phone-number PII)
log_and_show "Removing plugin data and stored credentials..."
rm -rf /home/fpp/media/plugindata/fpp-plugin-textmylights   # current (guideline) data dir
rm -rf /home/fpp/media/plugin.fpp-textmylights             # pre-migration data dir
rm -rf /home/fpp/media/plugin.fpp-sms-twilio               # pre-rename data dir (older installs)

# Legacy scattered config paths from versions before the data-dir migration.
rm -f /home/fpp/media/config/plugin.fpp-textmylights.json
rm -f /home/fpp/media/config/plugin.fpp-sms-twilio.json
rm -f /home/fpp/media/config/blacklist.txt
rm -f /home/fpp/media/config/whitelist.txt
rm -f /home/fpp/media/config/blocked_phones.json
rm -f /home/fpp/media/config/received_messages.json
rm -f /home/fpp/media/config/last_message_sid.txt

if [ -e /home/fpp/media/plugindata/fpp-plugin-textmylights ] || [ -e /home/fpp/media/plugin.fpp-textmylights ]; then
    log_and_show "⚠ Could NOT fully remove the plugin data dir — check permissions/ownership"
else
    log_and_show "✓ Plugin data and stored credentials removed"
fi

# Remove the plugin's bundled theme fonts. These live in our OWN subdirectory of
# /usr/local/share/fonts (plus the old flat copies some early versions dropped
# directly into it), so we only ever delete fonts this plugin installed — FPP
# core fonts and other plugins' fonts in that shared directory are left intact.
log_and_show "Removing bundled theme fonts..."
rm -rf /usr/local/share/fonts/fpp-plugin-textmylights
# Clean up old flat copies by exact filename for installs that predate the
# subdirectory move (never a blanket delete of *.ttf in the shared directory).
if [ -d /home/fpp/media/plugins/fpp-plugin-textmylights/fonts ]; then
    find /home/fpp/media/plugins/fpp-plugin-textmylights/fonts -type f \
        \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) -printf '%f\n' 2>/dev/null \
        | while IFS= read -r f; do rm -f "/usr/local/share/fonts/$f"; done 2>> "$LOG" || true
fi
# Old marker location from the flat-install versions (new marker lives inside the
# subdirectory removed above).
rm -f /usr/local/share/fonts/.tml-fonts-hash
command -v fc-cache >/dev/null 2>&1 && fc-cache -f /usr/local/share/fonts >> "$LOG" 2>&1 || true
log_and_show "✓ Bundled theme fonts removed"

# Remove the system integration this plugin added outside its own directory: the
# sudoers rule and the root-owned shared-memory permission helper. Leaving the
# sudoers rule behind would keep granting the fpp user a root command after the
# plugin is gone.
log_and_show "Removing sudoers rule and privileged helper..."
rm -f /etc/sudoers.d/90-fpp-sms-shm
rm -f /usr/local/bin/tml-fix-shm-perms
log_and_show "✓ Sudoers rule and helper removed"

# Remove the FPP scheduler command scripts the installer dropped into media/scripts so they
# stop appearing in FPP's Run Script dropdown after uninstall.
log_and_show "Removing scheduler command scripts..."
rm -f /home/fpp/media/scripts/TextMyLightsStart.sh /home/fpp/media/scripts/TextMyLightsStop.sh
log_and_show "✓ Scheduler command scripts removed"

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
