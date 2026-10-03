#!/bin/bash
###############################################################################
# Text My Lights Plugin - Installation Script
###############################################################################
# Stop on the first unhandled failure so a broken dependency install can't leave
# the plugin half-installed with no visible error. (Plain `set -e` only - `-u`
# would break references like $SUDO before sourcing common, and pipefail breaks
# the `cmd | ...` idioms below. Steps that may legitimately fail are guarded with
# `|| true` or wrapped in `if`.)
set -e

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

# This whole script re-runs on every plugin UPDATE, not just first install, so
# it must skip work that's already done or updates take as long as a fresh
# install. Run `apt-get update` at most once, and only when we actually need to
# apt-install something missing.
APT_UPDATED=0
apt_update_once() {
    if [ "$APT_UPDATED" = "0" ]; then
        log_and_show "Updating package lists... please wait"
        # Soft-fail: a transient update blip shouldn't abort; a real missing-package
        # install failure below still aborts under set -e.
        apt-get update -qq >> "$LOG" 2>&1 || true
        APT_UPDATED=1
    fi
}

log_and_show "========================================"
log_and_show "Text My Lights Plugin Installer"
log_and_show "$(date)"
log_and_show "========================================"
log_and_show ""
log_and_show "NOTE: Installation can take 3-5 minutes."
log_and_show "Please do not close this window."
log_and_show ""

# Install pip3 if needed
if ! command -v pip3 &> /dev/null; then
    log_and_show "Installing pip3... please wait"
    apt_update_once
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip >> "$LOG" 2>&1
fi

# Install system fonts used for rendering text on the overlay model. Some FPP base
# images ship with zero TrueType fonts installed, which would otherwise make PIL
# silently fall back to a tiny built-in bitmap font instead of a real one.
if [ ! -f /usr/share/fonts/truetype/freefont/FreeSans.ttf ]; then
    log_and_show "[1/7] Installing fonts (fonts-freefont-ttf)... please wait"
    apt_update_once
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
# Skip the copy + fc-cache rebuild (the slow part) when the bundled fonts are
# unchanged since last run — otherwise every update pays the fc-cache cost.
FONT_MARKER="/usr/local/share/fonts/.tml-fonts-hash"
FONT_HASH=$(find "$PLUGIN_DIR/fonts" -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d' ' -f1)
if [ -f "$FONT_MARKER" ] && [ "$(cat "$FONT_MARKER" 2>/dev/null)" = "$FONT_HASH" ] && command -v fc-cache &> /dev/null; then
    log_and_show "[2/7] Theme fonts already up to date"
else
    log_and_show "[2/7] Installing bundled theme fonts... please wait"
    if ! command -v fc-cache &> /dev/null; then
        apt_update_once
        DEBIAN_FRONTEND=noninteractive apt-get install -y fontconfig >> "$LOG" 2>&1
    fi
    mkdir -p /usr/local/share/fonts
    # Clear previous runs' copies first so /usr/local/share/fonts always exactly
    # mirrors the current repo — otherwise a renamed/removed bundled font (e.g.
    # "Santa Christmas" -> "Present Snow") leaves its old file behind forever,
    # and _enumerate_fonts() then miscategorizes it as a "System" font since its
    # name no longer matches anything under fonts/<category>/. This directory is
    # exclusively managed by this plugin, so it's safe to clear.
    find /usr/local/share/fonts -maxdepth 1 -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) -delete 2>> "$LOG" || true
    find "$PLUGIN_DIR/fonts" -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) \
        -exec cp {} /usr/local/share/fonts/ \; 2>> "$LOG" || true
    fc-cache -f /usr/local/share/fonts >> "$LOG" 2>&1
    echo "$FONT_HASH" > "$FONT_MARKER"
    log_and_show "[2/7] Theme fonts complete"
fi

# Install packages — each is skipped instantly if already importable.
if python3 -c "import flask" >/dev/null 2>&1; then
    log_and_show "[3/7] Flask already installed"
else
    log_and_show "[3/7] Installing Flask... please wait"
    pip3 install --break-system-packages --no-cache-dir flask==3.0.0 >> "$LOG" 2>&1
    log_and_show "[3/7] Flask complete"
fi

if python3 -c "import twilio" >/dev/null 2>&1; then
    log_and_show "[4/7] Twilio already installed"
else
    log_and_show "[4/7] Installing Twilio... please wait (this is the slow one)"
    pip3 install --break-system-packages --no-cache-dir twilio==8.10.0 >> "$LOG" 2>&1
    TWILIO_EXIT=$?
    if [ $TWILIO_EXIT -ne 0 ]; then
        log_and_show "ERROR: Twilio installation failed with exit code $TWILIO_EXIT"
        exit 1
    fi
    log_and_show "[4/7] Twilio complete"
fi

if python3 -c "import requests" >/dev/null 2>&1; then
    log_and_show "[5/7] Requests already installed"
else
    log_and_show "[5/7] Installing Requests... please wait"
    pip3 install --break-system-packages --no-cache-dir requests==2.32.3 >> "$LOG" 2>&1
    log_and_show "[5/7] Requests complete"
fi

if python3 -c "import PIL" >/dev/null 2>&1; then
    log_and_show "[6/7] Pillow already installed"
else
    log_and_show "[6/7] Installing Pillow (image rendering)... please wait"
    pip3 install --break-system-packages --no-cache-dir pillow >> "$LOG" 2>&1
    log_and_show "[6/7] Pillow complete"
fi

# zstandard is OPTIONAL — only used to preview zstd-compressed FSEQ files; the
# plugin runs fine without it (ZSTD_AVAILABLE=False). Its pip build is a C
# extension that can take many minutes or hang/OOM on a Pi with no prebuilt
# wheel. Skip entirely if already importable (so updates don't re-attempt it);
# otherwise prefer the prebuilt Debian package, fall back to a time-bounded pip
# install, and never let this step block the installer from finishing.
if python3 -c "import zstandard" >/dev/null 2>&1; then
    log_and_show "[7/7] zstandard already installed"
elif { apt_update_once; DEBIAN_FRONTEND=noninteractive apt-get install -y python3-zstandard >> "$LOG" 2>&1; }; then
    log_and_show "[7/7] zstandard complete (system package)"
elif timeout 180 pip3 install --break-system-packages --no-cache-dir zstandard >> "$LOG" 2>&1; then
    log_and_show "[7/7] zstandard complete (pip)"
else
    log_and_show "[7/7] zstandard skipped — optional (zstd FSEQ preview only); plugin works without it"
fi

# NOTE: the blocklist (blocked_phones.json) and all other runtime data live in
# PLUGIN_DATA_DIR below, NOT in FPP's config/ dir (which is reserved for plugin.<name>
# settings files and is bundled into crash reports). The plugin creates the blocklist on
# demand and treats a missing file as empty, so nothing to seed here.

# Create the plugin data dir and an OWNER-ONLY secrets folder for credentials
# (Twilio auth token, Gmail app password). Kept out of plugin.json/logs/backups;
# 0700 so only the fpp user can read it. The plugin also ensures this at startup.
PLUGIN_DATA_DIR="/home/fpp/media/plugin.fpp-textmylights"
mkdir -p "$PLUGIN_DATA_DIR/secrets"
chown -R fpp:fpp "$PLUGIN_DATA_DIR" 2>/dev/null || true
chmod 700 "$PLUGIN_DATA_DIR/secrets" 2>/dev/null || true
if [ -f "$PLUGIN_DATA_DIR/secrets/credentials.json" ]; then
    chmod 600 "$PLUGIN_DATA_DIR/secrets/credentials.json" 2>/dev/null || true
fi
# Log only (not shown in the installer UI) — don't advertise the credentials path.
echo "Secrets folder ready: $PLUGIN_DATA_DIR/secrets (owner-only)" >> "$LOG"

# whitelist.txt and blacklist.txt ship with the plugin via git.
# Force git checkout to ensure they are present (FPP update may not pull all files).
cd "$PLUGIN_DIR" && git checkout -- whitelist.txt blacklist.txt >> "$LOG" 2>&1 || true
if [ ! -f "$PLUGIN_DIR/whitelist.txt" ]; then
    log_and_show "WARNING: whitelist.txt still missing after git checkout - creating empty file"
    touch "$PLUGIN_DIR/whitelist.txt"
fi
if [ ! -f "$PLUGIN_DIR/blacklist.txt" ]; then
    log_and_show "WARNING: blacklist.txt still missing after git checkout - creating empty file"
    touch "$PLUGIN_DIR/blacklist.txt"
fi
chown fpp:fpp "$PLUGIN_DIR/whitelist.txt" "$PLUGIN_DIR/blacklist.txt" 2>/dev/null || true
chmod 664 "$PLUGIN_DIR/whitelist.txt" "$PLUGIN_DIR/blacklist.txt" 2>/dev/null || true

# Allow the fpp user to make FPP shared-memory files writable for pixel-accurate text
# rendering. FPP creates /dev/shm/FPP-Model-Data-* as root AFTER postStart.sh runs, so the
# plugin needs to fix permissions at runtime without a FPPD restart.
#
# SECURITY: instead of granting a broad world-writable rule over all FPP-Model-Data files
# (whose sudo wildcard could also match a slash, letting a crafted model name traverse to
# arbitrary files), we install a small ROOT-OWNED wrapper that validates its argument and
# only ever touches a single file inside /dev/shm (giving it to the fpp user/group,
# group-writable only), and grant sudo to that wrapper alone.
SHM_HELPER="/usr/local/bin/tml-fix-shm-perms"
install -o root -g root -m 0755 "$PLUGIN_DIR/scripts/tml-fix-shm-perms" "$SHM_HELPER" 2>/dev/null \
    || { cp "$PLUGIN_DIR/scripts/tml-fix-shm-perms" "$SHM_HELPER"; chown root:root "$SHM_HELPER"; chmod 0755 "$SHM_HELPER"; }

SUDOERS_FILE="/etc/sudoers.d/90-fpp-sms-shm"
echo "fpp ALL=(ALL) NOPASSWD: $SHM_HELPER" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
# Validate the sudoers file; remove it if malformed so we never wedge sudo.
if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    rm -f "$SUDOERS_FILE"
    log_and_show "WARNING: sudoers rule failed validation and was removed; shm fixes will need a FPPD restart"
else
    log_and_show "Sudoers rule installed for pixel rendering (shm access, validated wrapper)"
fi

# Set permissions on config/logs directories
chown -R fpp:fpp /home/fpp/media/config /home/fpp/media/logs 2>/dev/null || true
# FPP log viewer recognizes logs named plugin-<pluginname>.log in logDirectory.
# Remove the old pre-convention name so it stops lingering after an update.
rm -f /home/fpp/media/logs/sms_plugin.log
touch /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log
# 0644, not world-writable: the plugin (fpp) writes it and FPP's log viewer reads it, but no
# other local user should be able to tamper with it. Contains texter phone numbers.
chmod 644 /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log
chown fpp:fpp /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log || true

# Install scheduler scripts into FPP's scripts directory so they appear in
# the scheduler under: Command → Run Script → TextMyLightsStart / TextMyLightsStop
mkdir -p /home/fpp/media/scripts
# Remove the old Twilio-named scripts from before the rename (full rename),
# including the TwilioStart/StopBeta.sh left over from the reverted beta/stable
# coexistence attempt, so they stop appearing in FPP's Run Script dropdown.
# NOTE: update your FPP scheduler to run TextMyLightsStart.sh / TextMyLightsStop.sh.
rm -f /home/fpp/media/scripts/TwilioStart.sh /home/fpp/media/scripts/TwilioStop.sh \
      /home/fpp/media/scripts/TwilioStartBeta.sh /home/fpp/media/scripts/TwilioStopBeta.sh
cp "$PLUGIN_DIR/scripts/fpp_activate.sh"   /home/fpp/media/scripts/TextMyLightsStart.sh
cp "$PLUGIN_DIR/scripts/fpp_deactivate.sh" /home/fpp/media/scripts/TextMyLightsStop.sh
chmod +x /home/fpp/media/scripts/TextMyLightsStart.sh /home/fpp/media/scripts/TextMyLightsStop.sh
chown fpp:fpp /home/fpp/media/scripts/TextMyLightsStart.sh /home/fpp/media/scripts/TextMyLightsStop.sh || true
log_and_show "Scheduler scripts installed: TextMyLightsStart.sh / TextMyLightsStop.sh"

log_and_show "========================================"
log_and_show "Installation complete!"
log_and_show "Restart FPPD to start the service"
log_and_show "========================================"

# Restart the plugin service if it's already running (e.g. during an update)
if pgrep -f sms_plugin.py > /dev/null 2>&1; then
    log_and_show "Restarting SMS plugin service..."
    pkill -f sms_plugin.py 2>/dev/null || true
    # Bounded wait for the old process to exit (instead of a flat sleep).
    for _i in $(seq 1 20); do pgrep -f sms_plugin.py >/dev/null 2>&1 || break; sleep 0.1; done
    setsid su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /dev/null 2>/home/fpp/media/logs/plugin-fpp-plugin-textmylights.log &" < /dev/null > /dev/null 2>&1 || true
    log_and_show "SMS plugin service restarted"
fi

# Trigger the "FPPD Restart Required" banner in FPP's UI
setSetting "restartFlag" "1" || true

# No errors — remove the install log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
