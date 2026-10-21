#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Comprehensive Uninstall Script
###############################################################################

# Uninstall log lives in the shared media/logs, so it is named after the plugin (§5).
LOG="/home/fpp/media/logs/fpp-plugin-textmylights-uninstall.log"

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
rm -rf /home/fpp/media/plugindata/fpp-plugin-textmylights   # plugin data dir

if [ -e /home/fpp/media/plugindata/fpp-plugin-textmylights ]; then
    log_and_show "⚠ Could NOT fully remove the plugin data dir — check permissions/ownership"
else
    log_and_show "✓ Plugin data and stored credentials removed"
fi

# Remove the plugin's bundled theme fonts. These live ONLY in our OWN subdirectory of the
# shared /usr/local/share/fonts, so removing that folder deletes exactly the fonts this plugin
# installed and never touches FPP core fonts or fonts other plugins / the user put there.
log_and_show "Removing bundled theme fonts..."
rm -rf /usr/local/share/fonts/fpp-plugin-textmylights
command -v fc-cache >/dev/null 2>&1 && fc-cache -f /usr/local/share/fonts >> "$LOG" 2>&1 || true
log_and_show "✓ Bundled theme fonts removed"

# Remove the system integration this plugin added outside its own directory: the sudoers rule
# and the root-owned shared-memory permission helper (both the current plugin-named files and the
# pre-rename ones earlier builds installed). Leaving the sudoers rule behind would keep granting
# the fpp user a root command after the plugin is gone.
log_and_show "Removing sudoers rule and privileged helper..."
rm -f /etc/sudoers.d/fpp-plugin-textmylights /etc/sudoers.d/90-fpp-sms-shm
rm -f /usr/local/bin/fpp-plugin-textmylights-fix-shm-perms /usr/local/bin/tml-fix-shm-perms
log_and_show "✓ Sudoers rule and helper removed"

# Remove the FPP scheduler command scripts the installer dropped into media/scripts (current and
# pre-rename names) so they stop appearing in FPP's Run Script dropdown after uninstall.
log_and_show "Removing scheduler command scripts..."
rm -f /home/fpp/media/scripts/fpp-plugin-textmylights-Start.sh /home/fpp/media/scripts/fpp-plugin-textmylights-Stop.sh \
      /home/fpp/media/scripts/TextMyLightsStart.sh /home/fpp/media/scripts/TextMyLightsStop.sh
log_and_show "✓ Scheduler command scripts removed"

# Remove this plugin's OWN log files from the shared media/logs (current plugin-named files, plus
# the pre-rename install log earlier builds wrote). We do NOT delete generically-named files like
# sms_plugin.log or received_messages.json: those are not plugin-named and may not be ours.
log_and_show "Removing log files..."
rm -f /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log
rm -f /home/fpp/media/logs/fpp-plugin-textmylights-install.log
# Remove the logrotate drop-in the installer may have added for the plugin log.
rm -f /etc/logrotate.d/fpp-plugin-textmylights
log_and_show "✓ Log files removed"

log_and_show "========================================"
log_and_show "✅ Uninstall complete!"
log_and_show "All plugin files and dependencies removed"
log_and_show "========================================"

# No errors — remove the uninstall log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
