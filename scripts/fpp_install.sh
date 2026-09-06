#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Installation Script
###############################################################################

# Source FPP common functions and set FPPDIR environment
. ${FPPDIR}/scripts/common

# Create directories FIRST before any logging to files
mkdir -p /home/fpp/media/config /home/fpp/media/logs

LOG="/home/fpp/media/logs/sms_plugin_install.log"
PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-textmylights"

# Log to both file and stdout so FPP UI shows progress
log_and_show() {
    echo "$1" | tee -a "$LOG"
}

log_and_show "========================================"
log_and_show "FPP SMS Twilio Plugin Installer"
log_and_show "$(date)"
log_and_show "========================================"
log_and_show ""
log_and_show "NOTE: Installation can take 3-5 minutes."
log_and_show "Please do not close this window."
log_and_show ""

log_and_show "Updating package lists... please wait"
apt-get update -qq >> "$LOG" 2>&1

# Install pip3 if needed
if ! command -v pip3 &> /dev/null; then
    log_and_show "Installing pip3... please wait"
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip >> "$LOG" 2>&1
fi

# Install system fonts used for rendering text on the overlay model. Some FPP base
# images ship with zero TrueType fonts installed, which would otherwise make PIL
# silently fall back to a tiny built-in bitmap font instead of a real one.
if [ ! -f /usr/share/fonts/truetype/freefont/FreeSans.ttf ]; then
    log_and_show "[1/7] Installing fonts (fonts-freefont-ttf)... please wait"
    DEBIAN_FRONTEND=noninteractive apt-get install -y fonts-freefont-ttf >> "$LOG" 2>&1
    log_and_show "[1/7] Fonts complete"
else
    log_and_show "[1/7] Fonts already installed"
fi

# Install the plugin's bundled theme fonts (see fonts/<category>/NOTICE.md in
# each category folder for license/attribution — all "100% Free" per
# dafont.com). Installed flat into /usr/local/share/fonts so both FPP's own
# font scanner and the plugin's own get_fpp_fonts() pick them up; fc-cache
# indexes them for fc-match resolution in sms_plugin.py's _find_font(). Every
# category subfolder under fonts/ (christmas/, halloween/, etc.) is picked up
# automatically — no script changes needed when a new category is added.
log_and_show "[2/7] Installing bundled theme fonts... please wait"
if ! command -v fc-cache &> /dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y fontconfig >> "$LOG" 2>&1
fi
mkdir -p /usr/local/share/fonts
# Clear previous runs' copies first so /usr/local/share/fonts always exactly
# mirrors the current repo — otherwise a renamed/removed bundled font (e.g.
# "Santa Christmas" -> "Present Snow") leaves its old file behind forever,
# and _enumerate_fonts() then miscategorizes it as a "System" font since its
# name no longer matches anything under fonts/<category>/. This directory is
# exclusively managed by this plugin, so it's safe to clear on every install.
find /usr/local/share/fonts -maxdepth 1 -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) -delete 2>> "$LOG"
find "$PLUGIN_DIR/fonts" -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) \
    -exec cp {} /usr/local/share/fonts/ \; 2>> "$LOG"
fc-cache -f /usr/local/share/fonts >> "$LOG" 2>&1
log_and_show "[2/7] Theme fonts complete"

# Install packages
log_and_show "[3/7] Installing Flask... please wait"
pip3 install --break-system-packages --no-cache-dir flask==3.0.0 >> "$LOG" 2>&1
log_and_show "[3/7] Flask complete"

log_and_show "[4/7] Installing Twilio... please wait (this is the slow one)"
pip3 install --break-system-packages --no-cache-dir twilio==8.10.0 >> "$LOG" 2>&1
TWILIO_EXIT=$?
if [ $TWILIO_EXIT -ne 0 ]; then
    log_and_show "ERROR: Twilio installation failed with exit code $TWILIO_EXIT"
    exit 1
fi
log_and_show "[4/7] Twilio complete"

log_and_show "[5/7] Installing Requests... please wait"
pip3 install --break-system-packages --no-cache-dir requests==2.31.0 >> "$LOG" 2>&1
log_and_show "[5/7] Requests complete"

log_and_show "[6/7] Installing Pillow (image rendering)... please wait"
pip3 install --break-system-packages --no-cache-dir pillow >> "$LOG" 2>&1
log_and_show "[6/7] Pillow complete"

log_and_show "[7/7] Installing zstandard (FSEQ zstd decompression)... please wait"
pip3 install --break-system-packages --no-cache-dir zstandard >> "$LOG" 2>&1
log_and_show "[7/7] zstandard complete"

# Create config files if they don't exist
[ ! -f "/home/fpp/media/config/blocked_phones.json" ] && echo "[]" > /home/fpp/media/config/blocked_phones.json

# Create the plugin data dir and an OWNER-ONLY secrets folder for credentials
# (Twilio auth token, Gmail app password). Kept out of plugin.json/logs/backups;
# 0700 so only the fpp user can read it. The plugin also ensures this at startup.
PLUGIN_DATA_DIR="/home/fpp/media/plugin.fpp-textmylights"
mkdir -p "$PLUGIN_DATA_DIR/secrets"
chown -R fpp:fpp "$PLUGIN_DATA_DIR" 2>/dev/null
chmod 700 "$PLUGIN_DATA_DIR/secrets" 2>/dev/null
[ -f "$PLUGIN_DATA_DIR/secrets/credentials.json" ] && chmod 600 "$PLUGIN_DATA_DIR/secrets/credentials.json" 2>/dev/null
log_and_show "Secrets folder ready: $PLUGIN_DATA_DIR/secrets (owner-only)"

# whitelist.txt and blacklist.txt ship with the plugin via git.
# Force git checkout to ensure they are present (FPP update may not pull all files).
cd "$PLUGIN_DIR" && git checkout -- whitelist.txt blacklist.txt >> "$LOG" 2>&1
if [ ! -f "$PLUGIN_DIR/whitelist.txt" ]; then
    log_and_show "WARNING: whitelist.txt still missing after git checkout - creating empty file"
    touch "$PLUGIN_DIR/whitelist.txt"
fi
if [ ! -f "$PLUGIN_DIR/blacklist.txt" ]; then
    log_and_show "WARNING: blacklist.txt still missing after git checkout - creating empty file"
    touch "$PLUGIN_DIR/blacklist.txt"
fi
chown fpp:fpp "$PLUGIN_DIR/whitelist.txt" "$PLUGIN_DIR/blacklist.txt" 2>/dev/null
chmod 664 "$PLUGIN_DIR/whitelist.txt" "$PLUGIN_DIR/blacklist.txt" 2>/dev/null

# Allow fpp user to chmod FPP shared memory files for pixel-accurate text rendering.
# FPP creates /dev/shm/FPP-Model-Data-* as root AFTER postStart.sh runs, so the
# plugin needs to be able to fix permissions at runtime without a FPPD restart.
SUDOERS_FILE="/etc/sudoers.d/90-fpp-sms-shm"
echo "fpp ALL=(ALL) NOPASSWD: /usr/bin/chmod 666 /dev/shm/FPP-Model-Data-*" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
log_and_show "Sudoers rule installed for pixel rendering (shm access)"

# Set permissions on config/logs directories
chown -R fpp:fpp /home/fpp/media/config /home/fpp/media/logs 2>/dev/null
touch /home/fpp/media/logs/sms_plugin.log
chmod 666 /home/fpp/media/logs/sms_plugin.log
chown fpp:fpp /home/fpp/media/logs/sms_plugin.log

# Install scheduler scripts into FPP's scripts directory so they appear in
# the scheduler under: Command → Run Script → TwilioStart / TwilioStop
mkdir -p /home/fpp/media/scripts
cp "$PLUGIN_DIR/scripts/fpp_activate.sh"   /home/fpp/media/scripts/TwilioStart.sh
cp "$PLUGIN_DIR/scripts/fpp_deactivate.sh" /home/fpp/media/scripts/TwilioStop.sh
chmod +x /home/fpp/media/scripts/TwilioStart.sh /home/fpp/media/scripts/TwilioStop.sh
chown fpp:fpp /home/fpp/media/scripts/TwilioStart.sh /home/fpp/media/scripts/TwilioStop.sh
log_and_show "Scheduler scripts installed: TwilioStart.sh / TwilioStop.sh"

log_and_show "========================================"
log_and_show "Installation complete!"
log_and_show "Restart FPPD to start the service"
log_and_show "========================================"

# Restart the plugin service if it's already running (e.g. during an update)
if pgrep -f sms_plugin.py > /dev/null 2>&1; then
    log_and_show "Restarting SMS plugin service..."
    pkill -f sms_plugin.py 2>/dev/null || true
    sleep 1
    setsid su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /dev/null 2>/home/fpp/media/logs/sms_plugin.log &" < /dev/null > /dev/null 2>&1
    log_and_show "SMS plugin service restarted"
fi

# Trigger the "FPPD Restart Required" banner in FPP's UI
setSetting "restartFlag" "1"

# No errors — remove the install log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
