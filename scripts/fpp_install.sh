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
mkdir -p /home/fpp/media/logs

# Install log lives in the shared media/logs, so it is named after the plugin (§5).
LOG="/home/fpp/media/logs/fpp-plugin-textmylights-install.log"
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

# apt-install a package ONLY if it can be added WITHOUT removing anything. A Pi that is
# mid-OS-upgrade (lots of held-back packages) can have apt decide it must REMOVE core packages
# - even apache2, which would break FPP - just to satisfy one install. So we simulate first and
# refuse if the plan contains any removal. Never hangs on a conffile question (confold), never
# aborts the script. Returns 0 only if apt actually installed it.
apt_install_safe() {
    _pkg="$1"
    apt_update_once
    if apt-get install -s -y "$_pkg" 2>/dev/null | grep -q '^Remv '; then
        return 1   # apt would remove other packages - refuse (let the caller fall back)
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        -o Dpkg::Options::="--force-confold" "$_pkg" >> "$LOG" 2>&1
}

# Make a Python module importable. Preference: (1) already present, (2) the Debian package via
# apt_install_safe, (3) pip as a fallback so the plugin still installs on a Pi that isn't fully
# updated (this is how the plugin installed everything before the apt switch). Call with
# `|| true` so a single failure never aborts the whole install.
# Args: <import_name> <apt_package> <pip_package> <friendly label>
ensure_pydep() {
    _imp="$1"; _apt="$2"; _pip="$3"; _label="$4"
    if python3 -c "import $_imp" >/dev/null 2>&1; then
        log_and_show "$_label already installed"
        return 0
    fi
    log_and_show "Installing $_label... please wait"
    if apt_install_safe "$_apt" && python3 -c "import $_imp" >/dev/null 2>&1; then
        log_and_show "$_label complete (system package)"
        return 0
    fi
    # Fallback: pip (doesn't touch apt). Bootstrap pip3 the same safe way if it's missing.
    command -v pip3 >/dev/null 2>&1 || apt_install_safe python3-pip || true
    if command -v pip3 >/dev/null 2>&1; then
        pip3 install --break-system-packages --no-cache-dir "$_pip" >> "$LOG" 2>&1 || true
    fi
    if python3 -c "import $_imp" >/dev/null 2>&1; then
        log_and_show "$_label complete (pip fallback)"
        return 0
    fi
    log_and_show "WARNING: could not install $_label via apt or pip - the plugin may not work"
    return 1
}

log_and_show "========================================"
log_and_show "Text My Lights Plugin Installer"
log_and_show "$(date)"
log_and_show "========================================"
log_and_show ""
log_and_show "NOTE: Installation can take 3-5 minutes."
log_and_show "Please do not close this window."
log_and_show ""

# Python dependencies install via apt (Debian packages), preferred over pip because apt
# packages are reference-counted (and on FPP 10+ they're also declared in pluginInfo.json's
# dependencies.packages, which FPP installs). ensure_pydep() keeps that apt-first behavior but
# adds a pip FALLBACK, so the plugin still installs on a Pi whose apt is wedged mid-OS-upgrade
# (where a single `apt install` would otherwise try to remove core packages). Anything already
# importable is skipped instantly, which also keeps updates fast.

# Install system fonts used for rendering text on the overlay model. Some FPP base
# images ship with zero TrueType fonts installed, which would otherwise make PIL
# silently fall back to a tiny built-in bitmap font instead of a real one.
if [ ! -f /usr/share/fonts/truetype/freefont/FreeSans.ttf ]; then
    log_and_show "[1/7] Installing fonts (fonts-freefont-ttf)... please wait"
    if apt_install_safe fonts-freefont-ttf; then
        log_and_show "[1/7] Fonts complete"
    else
        log_and_show "[1/7] System fonts skipped (apt busy/Pi not fully updated); bundled theme fonts still install"
    fi
else
    log_and_show "[1/7] Fonts already installed"
fi

# Install the plugin's bundled theme fonts (see fonts/<category>/NOTICE.md in
# each category folder for license/attribution — all "100% Free" per
# dafont.com). Installed into our OWN subdirectory of /usr/local/share/fonts so
# both FPP's own font scanner (PixelOverlay.cpp findFonts() recurses) and the
# plugin's own get_fpp_fonts() pick them up; fc-cache indexes them for fc-match
# resolution in sms_plugin.py's _find_font(). We deliberately do NOT write flat
# into /usr/local/share/fonts: that directory is shared — FPP core loads overlay
# fonts from it and FPP-Plugin-Matrix-Message moves user-uploaded fonts there —
# so clearing it would destroy fonts this plugin doesn't own. Keeping everything
# under fpp-plugin-textmylights/ lets us safely mirror the repo without touching
# anyone else's fonts. Every category subfolder under fonts/ (christmas/,
# halloween/, etc.) is picked up automatically — no script changes needed when a
# new category is added. Skip the copy + fc-cache rebuild (the slow part) when
# the bundled fonts are unchanged since last run — otherwise every update pays
# the fc-cache cost.
FONT_DEST="/usr/local/share/fonts/fpp-plugin-textmylights"
FONT_MARKER="$FONT_DEST/.tml-fonts-hash"
FONT_HASH=$(find "$PLUGIN_DIR/fonts" -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d' ' -f1)
if [ -f "$FONT_MARKER" ] && [ "$(cat "$FONT_MARKER" 2>/dev/null)" = "$FONT_HASH" ] && command -v fc-cache &> /dev/null; then
    log_and_show "[2/7] Theme fonts already up to date"
else
    log_and_show "[2/7] Installing bundled theme fonts... please wait"
    if ! command -v fc-cache &> /dev/null; then
        apt_install_safe fontconfig || true
    fi
    # Our fonts live ONLY in our own subdirectory (recreated below). We do NOT touch files
    # directly in the shared /usr/local/share/fonts: deleting there by filename could remove a
    # same-named font a user uploaded (e.g. via another plugin), which the plugin doesn't own.
    # Clear and recreate OUR subdirectory so it always exactly mirrors the repo —
    # otherwise a renamed/removed bundled font (e.g. "Santa Christmas" ->
    # "Present Snow") leaves its old file behind forever, and _enumerate_fonts()
    # then miscategorizes it as a "System" font. This subdirectory is exclusively
    # managed by this plugin, so it's safe to clear.
    rm -rf "$FONT_DEST"
    mkdir -p "$FONT_DEST"
    find "$PLUGIN_DIR/fonts" -type f \( -iname '*.ttf' -o -iname '*.otf' -o -iname '*.pfb' \) \
        -exec cp {} "$FONT_DEST/" \; 2>> "$LOG" || true
    fc-cache -f "$FONT_DEST" >> "$LOG" 2>&1
    echo "$FONT_HASH" > "$FONT_MARKER"
    log_and_show "[2/7] Theme fonts complete"
fi

# Required Python deps: apt first, pip fallback (see ensure_pydep). import-name / apt pkg / pip pkg.
ensure_pydep flask    python3-flask    flask    "[3/7] Flask"    || true
ensure_pydep twilio   python3-twilio   twilio   "[4/7] Twilio"   || true
ensure_pydep requests python3-requests requests "[5/7] Requests" || true
ensure_pydep PIL      python3-pil      pillow   "[6/7] Pillow (image rendering)" || true

# zstandard is OPTIONAL — only used to preview zstd-compressed FSEQ files; the plugin runs fine
# without it (ZSTD_AVAILABLE=False). Install the prebuilt Debian package only; do NOT pip-fall-
# back here, because the pip build is a C extension that can take many minutes or hang/OOM on a
# Pi with no prebuilt wheel. Skip entirely if already importable, and never block the installer.
if python3 -c "import zstandard" >/dev/null 2>&1; then
    log_and_show "[7/7] zstandard already installed"
elif apt_install_safe python3-zstandard && python3 -c "import zstandard" >/dev/null 2>&1; then
    log_and_show "[7/7] zstandard complete (system package)"
else
    log_and_show "[7/7] zstandard skipped — optional (zstd FSEQ preview only); plugin works without it"
fi

# NOTE: the blocklist (blocked_phones.json) and all other runtime data live in
# PLUGIN_DATA_DIR below, NOT in FPP's config/ dir (which is reserved for plugin.<name>
# settings files and is bundled into crash reports). The plugin creates the blocklist on
# demand and treats a missing file as empty, so nothing to seed here.

# Runtime data lives under the FPP-guideline location /home/fpp/media/plugindata/<repo>/.
PLUGIN_DATA_DIR="/home/fpp/media/plugindata/fpp-plugin-textmylights"

# Create the plugin data dir and an OWNER-ONLY secrets folder for credentials
# (Twilio auth token, Gmail app password). Kept out of plugin.json/logs/backups;
# 0700 so only the fpp user can read it. The plugin also ensures this at startup.
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
# Files this plugin installs into shared system folders are named after the plugin (§5) so it
# is clear they belong to Text My Lights. Remove the pre-rename copies this plugin created in
# earlier builds (own files only), including the old sudoers rule - otherwise it would keep
# granting fpp a root command for a helper we no longer ship.
rm -f /etc/sudoers.d/90-fpp-sms-shm /usr/local/bin/tml-fix-shm-perms

SHM_HELPER="/usr/local/bin/fpp-plugin-textmylights-fix-shm-perms"
install -o root -g root -m 0755 "$PLUGIN_DIR/scripts/tml-fix-shm-perms" "$SHM_HELPER" 2>/dev/null \
    || { cp "$PLUGIN_DIR/scripts/tml-fix-shm-perms" "$SHM_HELPER"; chown root:root "$SHM_HELPER"; chmod 0755 "$SHM_HELPER"; }

SUDOERS_FILE="/etc/sudoers.d/fpp-plugin-textmylights"
echo "fpp ALL=(ALL) NOPASSWD: $SHM_HELPER" > "$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
# Validate the sudoers file; remove it if malformed so we never wedge sudo.
if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
    rm -f "$SUDOERS_FILE"
    log_and_show "WARNING: sudoers rule failed validation and was removed; shm fixes will need a FPPD restart"
else
    log_and_show "Sudoers rule installed for pixel rendering (shm access, validated wrapper)"
fi

# A plugin must only chown files it created (FPP plugin guideline §5). We deliberately do NOT
# `chown -R fpp:fpp /home/fpp/media/config /home/fpp/media/logs` here: those directories are
# shared with FPP and every other plugin, and a blanket recursive chown would seize files
# another plugin or FPP itself created with a different owner on purpose. The plugin's own
# files are handled individually - plugindata at the secrets step above, and our log file just
# below - so there is nothing of ours left to fix here.
# FPP log viewer recognizes logs named plugin-<pluginname>.log in logDirectory.
touch /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log
# 0644, not world-writable: the plugin (fpp) writes it and FPP's log viewer reads it, but no
# other local user should be able to tamper with it. Contains texter phone numbers.
chmod 644 /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log
chown fpp:fpp /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log || true

# Install scheduler scripts into FPP's scripts directory so they appear in the scheduler under:
# Command → Run Script. They live in the shared media/scripts, so they are named after the
# plugin (§5): fpp-plugin-textmylights-Start.sh / -Stop.sh.
mkdir -p /home/fpp/media/scripts
# Remove the pre-rename copies this plugin created in earlier builds (own files only) so they
# stop appearing in FPP's Run Script dropdown. NOTE: update your FPP scheduler to run the new
# fpp-plugin-textmylights-Start.sh / -Stop.sh entries.
rm -f /home/fpp/media/scripts/TextMyLightsStart.sh /home/fpp/media/scripts/TextMyLightsStop.sh
cp "$PLUGIN_DIR/scripts/fpp_activate.sh"   /home/fpp/media/scripts/fpp-plugin-textmylights-Start.sh
cp "$PLUGIN_DIR/scripts/fpp_deactivate.sh" /home/fpp/media/scripts/fpp-plugin-textmylights-Stop.sh
chmod +x /home/fpp/media/scripts/fpp-plugin-textmylights-Start.sh /home/fpp/media/scripts/fpp-plugin-textmylights-Stop.sh
chown fpp:fpp /home/fpp/media/scripts/fpp-plugin-textmylights-Start.sh /home/fpp/media/scripts/fpp-plugin-textmylights-Stop.sh || true
log_and_show "Scheduler scripts installed: fpp-plugin-textmylights-Start.sh / -Stop.sh"

log_and_show "========================================"
log_and_show "Installation complete!"
log_and_show "Restart FPPD to start the service"
log_and_show "========================================"

# Restart the plugin service if it's already running (e.g. during an update).
if pgrep -f sms_plugin.py > /dev/null 2>&1; then
    log_and_show "Restarting SMS plugin service..."
    # Stop via the PID file (shared helper); it also waits for the old process to exit.
    "$PLUGIN_DIR/scripts/tml-stop.sh" || true
    setsid su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py >> /home/fpp/media/logs/plugin-fpp-plugin-textmylights.log 2>&1 &" < /dev/null > /dev/null 2>&1 || true
    log_and_show "SMS plugin service restarted"
fi

# Trigger the "FPPD Restart Required" banner in FPP's UI
setSetting "restartFlag" "1" || true

# No errors — remove the install log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
