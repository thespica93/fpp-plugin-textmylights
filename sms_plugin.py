#!/usr/bin/env python3
"""
Text My Lights - FPP plugin: viewers text a name that appears on your display.
Supports Twilio and Google Voice as message sources.
"""

from flask import Flask, request, jsonify, render_template_string, Response, g, send_file
import logging
import json
import secrets as _secrets
import requests
from datetime import datetime, timedelta, timezone
import re
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from twilio.rest import Client
from collections import deque
import os
import struct
import io
import zipfile
import tempfile
import shutil
import imaplib
import smtplib
import email
import email.utils
from email.header import decode_header, make_header

# PIL/Pillow for pixel-accurate text rendering (optional - falls back to FPP text API if unavailable)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# zstandard for FSEQ zstd decompression (optional - install via fpp_install.sh)
try:
    import zstandard as _zstd_mod
    ZSTD_AVAILABLE = True
except ImportError:
    _zstd_mod = None
    ZSTD_AVAILABLE = False

_scroll_thread = None   # background PIL scroll animation thread
_scroll_stop = threading.Event()   # set to stop the scroll thread promptly (before it self-expires)


def _stop_scroll_thread(timeout=2.0):
    """Stop the current scroll animation thread and wait for it to fully exit (closing its
    shm handle) before returning. Callers that are about to write the shm buffer themselves
    (restoring waiting content, starting a new name) MUST call this first, otherwise a
    still-running animation frame can land AFTER the new content and 'reload' the old name."""
    global _scroll_thread
    _scroll_stop.set()
    th = _scroll_thread
    if th is not None and th.is_alive():
        th.join(timeout)
    _scroll_thread = None

# Configuration
PLUGIN_DIR      = os.path.dirname(os.path.abspath(__file__))

# All runtime data lives under one plugin folder
PLUGIN_DATA_DIR = "/home/fpp/media/plugin.fpp-textmylights"
CONFIG_FILE     = os.path.join(PLUGIN_DATA_DIR, "plugin.json")
# Credentials live in their own owner-only directory - NOT in plugin.json, logs,
# or backups. (True at-rest secrecy isn't possible on this hardware: an
# unattended service must be able to read them on boot, so any key would sit on
# the same card. This keeps them out of the shared config and off casual view.)
SECRETS_DIR     = os.path.join(PLUGIN_DATA_DIR, "secrets")
SECRETS_FILE    = os.path.join(SECRETS_DIR, "credentials.json")
SECRET_KEYS     = ("twilio_auth_token", "gv_app_password")
# Box-specific Master/Remote identity - kept on THIS Pi across a config import unless the
# user ticks "import mode" (so restoring settings never silently flips a master to a remote).
MODE_KEYS       = ("plugin_role", "selected_master")
# Placeholder shown in a saved secret field. Submitting it unchanged means
# "keep the stored secret"; clearing the field to empty means "remove it";
# any other value updates it. Must be something a real secret never equals.
SECRET_SENTINEL = "••••••••"  # 8 × •
LOG_FILE        = os.path.join(PLUGIN_DATA_DIR, "logs", "sms_plugin.log")
QUEUE_FILE      = os.path.join(PLUGIN_DATA_DIR, "queue_pending.json")
MESSAGES_DIR    = os.path.join(PLUGIN_DATA_DIR, "logs", "messages")
LAST_SID_FILE   = os.path.join(PLUGIN_DATA_DIR, "last_message_sid.txt")
LAST_GV_UID_FILE = os.path.join(PLUGIN_DATA_DIR, "last_gv_uid.txt")
BLOCKLIST_FILE  = os.path.join(PLUGIN_DATA_DIR, "blocked_phones.json")
# Per-day tally of blacklisted words each sender has texted, used by the profanity
# threshold auto-block (see register_profanity_strike). Stores a single date stamp;
# a new day wipes the tally (midnight reset).
PROFANITY_STRIKES_FILE = os.path.join(PLUGIN_DATA_DIR, "profanity_strikes.json")
# Admin whitelist-approval-over-SMS state (Google Voice only). admin_reply_ctx.json
# stores the thread context used to text the admin (GV can only reply into an existing
# thread) plus the phone it belongs to; pending_approvals.json is the FIFO list of
# outstanding name requests awaiting an admin Y/N. See _maybe_handle_admin_message().
ADMIN_CTX_FILE          = os.path.join(PLUGIN_DATA_DIR, "admin_reply_ctx.json")
PENDING_APPROVALS_FILE  = os.path.join(PLUGIN_DATA_DIR, "pending_approvals.json")
# Requests that timed out before the admin answered are moved here so a LATE "Y" can
# still add the name to the whitelist (for next time) without showing it now. Bounded.
EXPIRED_APPROVALS_FILE  = os.path.join(PLUGIN_DATA_DIR, "expired_approvals.json")
EXPIRED_APPROVALS_TTL_H = 24    # drop expired records older than this many hours
EXPIRED_APPROVALS_MAX   = 200   # hard cap on retained expired records
# The word the operator texts from the admin phone to connect/seed the reply context.
# It is reserved: it is never shown on the display (see process_incoming_message).
ADMIN_CONNECT_KEYWORD   = "admin"

FSEQ_SEQUENCE_PATH = '/home/fpp/media/sequences'
FPP_VIDEOS_PATH    = '/home/fpp/media/videos'
FPP_IMAGES_PATH    = '/home/fpp/media/images'
# Root helper (installed by fpp_install.sh) that makes a single
# /dev/shm/FPP-Model-Data-<model> file writable by the fpp user. The plugin may only
# invoke THIS via sudo - never `chmod` directly - so a model name can never be abused to
# change permissions on files outside /dev/shm. The helper re-validates its argument.
SHM_PERMS_HELPER = '/usr/local/bin/tml-fix-shm-perms'
FPP_PLAYLISTS_PATH = '/home/fpp/media/playlists'
FPP_CONFIG_DIR     = '/home/fpp/media/config'
# FPP keeps Pixel Overlay Models (the "matrix" the plugin draws text onto) as
# "Other" channel outputs, stored in co-other.json - NOT a dedicated
# model-overlays.json (which doesn't exist on standard installs). Confirmed via
# the on-device overlay diagnostic. Export finds the file dynamically
# (_find_overlay_config_file) so it adapts if a setup differs.
OVERLAY_MODELS_FILE = os.path.join(FPP_CONFIG_DIR, 'co-other.json')

def _find_overlay_config_file():
    """Return the FPP config file that actually holds the overlay model. Pixel
    Overlay Models live in co-other.json on standard installs; to be robust we
    also scan the config dir for the configured model name and prefer whatever
    file contains it. Returns None if nothing is found."""
    model = ''
    try:
        model = config.get('overlay_model_name', '') or ''
    except Exception:
        pass
    if model:
        try:
            for fn in sorted(os.listdir(FPP_CONFIG_DIR)):
                fp = os.path.join(FPP_CONFIG_DIR, fn)
                if not os.path.isfile(fp) or not fn.endswith('.json'):
                    continue
                if os.path.getsize(fp) > 2_000_000:
                    continue
                try:
                    with open(fp, 'r', errors='ignore') as f:
                        if model in f.read():
                            return fp
                except Exception:
                    pass
        except Exception:
            pass
    return OVERLAY_MODELS_FILE if os.path.isfile(OVERLAY_MODELS_FILE) else None

# Whitelist/blacklist source files stay in the plugin git repo directory
BLACKLIST_FILE = os.path.join(PLUGIN_DIR, "blacklist.txt")
BLACKLIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "blacklist_removed.txt")
BLACKLIST_ADDED_FILE = os.path.join(PLUGIN_DIR, "blacklist_added.txt")
WHITELIST_FILE = os.path.join(PLUGIN_DIR, "whitelist.txt")
WHITELIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "whitelist_removed.txt")
WHITELIST_ADDED_FILE = os.path.join(PLUGIN_DIR, "whitelist_added.txt")

# Create directory structure before logging setup
os.makedirs(os.path.join(PLUGIN_DATA_DIR, "logs", "messages"), exist_ok=True)
# Owner-only secrets directory (created at first run and on install)
os.makedirs(SECRETS_DIR, exist_ok=True)
try:
    os.chmod(SECRETS_DIR, 0o700)
except OSError:
    pass

# Setup logging - ensure the log directory exists, then write to file + stderr.
# The file handler is size-bounded (rotating) so the log can never fill the SD card:
# ~4 MB hard ceiling total (1 MB x 3 backups + the active file), oldest lines discarded.
import logging.handlers
_log_handlers = [logging.StreamHandler()]  # stderr always available via nohup
try:
    _log_handlers.append(logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3))
except Exception:
    pass  # directory may not exist on some FPP installs; stderr is the fallback
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_log_handlers
)
# Keep Flask/werkzeug at ERROR so its per-request access logging stays off (that WOULD be chatty).
logging.getLogger('werkzeug').setLevel(logging.ERROR)

import flask.cli
flask.cli.show_server_banner = lambda *args: None

app = Flask(__name__)
# Cap request bodies so an oversized upload can't exhaust memory. Config bundles
# (which may include .fseq content) are the only large uploads; 512 MB is generous
# for those while still bounding the damage. Applies to every endpoint.
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024
# Independent guard on a config-import bundle's *decompressed* size - a small .zip can
# expand to gigabytes (a "zip bomb"). Reject bundles whose contents exceed this.
MAX_IMPORT_UNCOMPRESSED = 1024 * 1024 * 1024  # 1 GB total across all entries

# ============================================================================
# NETWORK ACCESS CONTROL
# ----------------------------------------------------------------------------
# The service binds 0.0.0.0:5000 so the FPP web UI (running on a different
# machine - the user's browser) can iframe it. To keep anonymous LAN clients
# from reading credentials / controlling the show, every *network* request must
# carry an access token. The token is minted here and read by the FPP-served
# PHP pages (ui.php / messages.php), which are already behind FPP's own web
# server - so only someone who can load the FPP UI ever receives it.
#
# Loopback (127.0.0.1) is always trusted: the scheduler's activate/deactivate
# scripts and any on-box tooling reach us over localhost and need no token.
# Escape hatch: `touch <PLUGIN_DATA_DIR>/.disable_auth` then restart to disable
# network auth if you are ever locked out.
# ============================================================================
ACCESS_TOKEN_FILE = os.path.join(PLUGIN_DATA_DIR, ".access_token")
AUTH_DISABLE_FILE = os.path.join(PLUGIN_DATA_DIR, ".disable_auth")
_AUTH_COOKIE = "tml_token"

def _load_or_create_token():
    """Reuse a persisted token across restarts so already-open UIs keep working;
    mint one on first run. The token file is world-readable on purpose - the FPP
    web server (whatever user it runs as) must read it to embed in the UI, and
    local read access already implies full access to the plaintext config."""
    try:
        with open(ACCESS_TOKEN_FILE, 'r') as _f:
            _tok = _f.read().strip()
            if _tok:
                return _tok
    except OSError:
        pass
    _tok = _secrets.token_urlsafe(32)
    try:
        with open(ACCESS_TOKEN_FILE, 'w') as _f:
            _f.write(_tok)
        os.chmod(ACCESS_TOKEN_FILE, 0o644)
    except OSError as _e:
        logging.error(f"Could not persist access token: {_e}")
    return _tok

ACCESS_TOKEN = _load_or_create_token()
_auth_disabled_warned_at = 0.0   # throttle the "auth disabled" warning log

@app.before_request
def _require_access_token():
    # Trust the loopback interface (scheduler scripts, on-box curl, the poller
    # never hits HTTP). remote_addr comes from the socket peer; we never trust
    # X-Forwarded-For, so it cannot be spoofed to look local.
    if request.remote_addr in ('127.0.0.1', '::1'):
        return None
    if os.path.exists(AUTH_DISABLE_FILE):
        # Debug escape hatch - the whole UI is wide open to anyone on the network.
        # Warn (throttled) so this is never silently left enabled in production.
        global _auth_disabled_warned_at
        _now = time.time()
        if _now - _auth_disabled_warned_at > 300:
            _auth_disabled_warned_at = _now
            logging.warning("⚠️  AUTH DISABLED (.disable_auth present) - the plugin is "
                            "reachable WITHOUT a token by anyone on the network. Remove "
                            f"{AUTH_DISABLE_FILE} to re-enable access control.")
        return None
    # Inter-instance calls (a master pushing to a remote's /api/tml/* endpoints) come from
    # another FPP on the LAN, so the per-instance access token won't match. Instead of a
    # shared secret, trust the FPP MultiSync peers this box already recognizes (the same
    # systems FPP itself syncs with), plus any manually-listed remote IPs.
    if request.path.startswith('/api/tml/'):
        if request.remote_addr in _trusted_tml_peers():
            return None
        return Response("Not a recognized FPP peer.", status=403, mimetype='text/plain')
    # First load carries the token as a query param (embedded by the FPP UI);
    # we then set a cookie so subsequent same-origin fetches are authorized.
    qtok = request.args.get('token', '')
    if qtok and _secrets.compare_digest(qtok, ACCESS_TOKEN):
        g._set_auth_cookie = True
        return None
    ctok = request.cookies.get(_AUTH_COOKIE, '')
    if ctok and _secrets.compare_digest(ctok, ACCESS_TOKEN):
        return None
    return Response(
        "Access denied. Open this plugin from the FPP web UI "
        "(Content Setup → Text My Lights).",
        status=403, mimetype='text/plain')

IFRAME_RESIZE_SCRIPT = """<script>
(function() {
    function reportHeight() {
        window.parent.postMessage({ type: 'iframeHeight', height: document.body.scrollHeight }, '*');
    }
    window.addEventListener('load', reportHeight);
    new MutationObserver(reportHeight).observe(document.body, { subtree: true, childList: true, characterData: true });
})();
</script>"""

@app.after_request
def inject_iframe_resize(response):
    # Persist the access token as a cookie once a valid ?token= is presented, so
    # follow-up requests from the same browser don't need the query param.
    if getattr(g, '_set_auth_cookie', False):
        response.set_cookie(_AUTH_COOKIE, ACCESS_TOKEN, httponly=True,
                            samesite='Lax', max_age=60 * 60 * 24 * 365)
    if response.content_type.startswith('text/html'):
        body = response.get_data(as_text=True)
        body = body.replace('</body>', IFRAME_RESIZE_SCRIPT + '</body>')
        response.set_data(body)
    return response

# ============================================================================
# OPTIMIZED LIST CACHING - Module-level cache variables
# ============================================================================
_blacklist_cache = None
_blacklist_mtime = None

_whitelist_cache = None
_whitelist_mtime = None

_blocklist_cache = None
_blocklist_mtime = None

_fpp_data_cache = None
_fpp_data_cache_time = 0
_FPP_DATA_CACHE_TTL = 60  # seconds

# FPP runs locally - always use localhost
FPP_HOST = 'http://127.0.0.1'

# Default configuration
DEFAULT_CONFIG = {
    "enabled": False,
    # Multi-instance role (independent of FPP's own player/remote mode). "" = not yet
    # chosen → resolved to a default from the FPP instance's mode on first load.
    #   "master" - polls Twilio/GV, filters, responds, counts, AND pushes the chosen
    #              name/content to remotes (the single selection authority).
    #   "remote" - never polls/responds/counts; only renders name + content pushed by the
    #              master, using this instance's OWN overlay model / fonts / layout.
    "plugin_role": "",
    # Remote-side: the address of the ONE master this remote syncs to (chosen in the UI).
    # Empty = auto (follow the first master found, accept pushes from any trusted peer).
    "selected_master": "",
    # Which inbound message source feeds the pipeline: "twilio" | "google_voice"
    "message_source": "google_voice",
    "twilio_account_sid": "",
    "twilio_auth_token": "",
    "twilio_phone_number": "",
    # Google Voice source: scans the Gmail inbox that Voice forwards SMS to.
    # No public GV API exists; requires "Forward messages to email" enabled in
    # Google Voice and a Google App Password (2-Step Verification must be on).
    "gv_email": "",
    "gv_app_password": "",
    "gv_imap_host": "imap.gmail.com",
    "gv_imap_folder": "INBOX",
    # SMTP is used only for Google Voice outbound replies (reply-to-email trick)
    "gv_smtp_host": "smtp.gmail.com",
    "gv_smtp_port": 587,
    "poll_interval": 2,
    "display_duration": 10,
    # 0 = unlimited, matching the Google Voice default source (Twilio uses 5, applied
    # by the source selector's change handler when you switch to Twilio).
    "max_messages_per_phone": 0,
    "max_message_length": 30,
    "max_message_age_mins": 5,
    "one_word_only": False,
    "two_words_max": True,
    "use_whitelist": False,
    "profanity_filter": True,
    # Auto-block a sender after they text this many blacklisted words in one day
    # (tally resets at midnight). 0 disables the feature. Once blocked, only the
    # operator can release them from the Phone Blocklist. See register_profanity_strike().
    "profanity_threshold": 3,
    "fpp_host": "http://127.0.0.1",
    "default_playlist": "",
    # Waiting-content rotation list (v2.8+): each item is a background the plugin loops
    # while idle. Authoritative when non-empty; empty list falls back to the single
    # default_playlist above (the pre-list behavior). 1 item = play/loop it (no rotation);
    # 2+ items = the waiting rotator cycles them (round-robin/random), switching at the end
    # of each sequence (full FSEQ length) with a seamless overlap so there is no black gap.
    # See select_default_content_item() and waiting_rotator(). default_playlist is kept in
    # sync with list[0] so the required-field/validation/legacy paths still have a value.
    "default_content_list": [],
    "default_content_mode": "roundrobin",   # "roundrobin" | "random"
    "default_content_rr_index": -1,          # persisted round-robin cursor (index last shown)
    "name_display_playlist": "",
    # Names content list (v2.7+): each name picks one of these items as its background,
    # each item carrying its OWN text layout + duration. Authoritative when non-empty;
    # empty list falls back to name_display_playlist + the flat line_*/message_lines below
    # (the pre-list behavior). See select_names_content_item() and _names_item_defaults().
    "names_content_list": [],
    "names_content_mode": "roundrobin",   # "roundrobin" | "random"
    "names_content_rr_index": -1,          # persisted round-robin cursor (index last shown)
    "overlay_model_name": "",
    "text_color": "#FF0000",
    "text_font": "FreeSans",
    "text_position": "Center",
    "message_template": "Merry Christmas {name}!",  # legacy - migrated to message_lines on load
    "message_lines": ["Merry Christmas", "{name}!", "", ""],
    # Each box is the MAX area a line can render into; font size auto-fits to it
    # (largest size where the actual message text fits both w and h), then is
    # centered within the box. x/y < 0 means auto-position (horizontally centered /
    # vertically stacked among the other auto-positioned lines) - w/h are always
    # concrete since there's no "auto size" for the fit target itself.
    "line_boxes": [{"x": -1, "y": -1, "w": 300, "h": 60} for _ in range(4)],
    "line_colors": ["", "", "", ""],
    "line_movements": ["Center", "Center", "Center", "Center"],
    "line_speeds": [50, 50, 50, 50],
    "line_fonts": ["FreeSans", "FreeSans", "FreeSans", "FreeSans"],
    # Only meaningful for Center (static) lines -- scrolling lines are always
    # 'horizontal'. 'vertical_rotated' = whole line rotated 90deg; 'vertical_stacked' =
    # one upright character per row.
    "line_orientations": ["horizontal", "horizontal", "horizontal", "horizontal"],
    "custom_colors": [],
    "scroll_speed": 5,
    "overlay_model_width": 0,
    "overlay_model_height": 0,
    # All SMS responses default ON at install. When a row greys out (e.g. rate_limited
    # while GV is unlimited, or duplicate while duplicates are allowed), the stored
    # toggle stays ON - _response_is_muted() just suppresses the send - so the last
    # state is preserved and the response resumes if the row un-greys.
    "sms_response_show_not_live": True,
    "sms_response_success": True,
    "sms_response_profanity": True,
    "sms_response_rate_limited": True,
    # ON for the Google Voice default source, OFF for Twilio (the source selector's
    # change handler flips this when you switch). While ON, the Duplicate Name
    # response row greys out but its stored toggle stays ON (just muted).
    "allow_duplicate_names": True,
    "sms_response_duplicate": True,
    "sms_response_invalid_format": True,
    "sms_response_too_long": True,
    "sms_response_not_whitelisted": True,
    "sms_response_blocked": True,
    "response_show_not_live": "Ho, Ho, Ho, It looks like our show isn't running now. Try again later.",
    "response_success": "Merry Christmas! Your name will appear on our display soon! 🎄",
    "response_profanity": "Ho Ho Ho! That one didn't make the nice list. More texts with profanity may block your phone 🎅",
    "response_blocked": "Sorry, Your phone number has been blocked from sending messages.",
    "response_rate_limited": "You've reached the maximum number of messages allowed. Please try again tomorrow!",
    "response_duplicate": "You've already sent this name today!",
    "response_invalid_format": "Please send only 1 name ({words}, no sentences).",
    "response_too_long": "I'm sorry, your message exceeds our max message length. Please only send your name.",
    "response_not_whitelisted": "Sorry, that name is not on our approved list and cannot be shown.",
    # --- Admin whitelist approval over SMS (Google Voice ONLY) ---
    # When the whitelist is ON and a texter sends a name NOT on the list, optionally
    # text the show admin to approve it live. admin_phone empty = feature off (texter
    # just gets response_not_whitelisted, unchanged). Not built for Twilio: Twilio
    # outbound now needs an A2P campaign, so Twilio SMS replies are effectively off.
    # The admin must text the Google Voice number once to seed the reply context
    # (GV can only reply into an existing thread); until then the feature falls back
    # to the standard not-whitelisted reply. See _maybe_handle_admin_message() and
    # ADMIN_CTX_FILE / PENDING_APPROVALS_FILE.
    "admin_phone": "",
    "response_whitelist_pending": "Your name isn't on our whitelist, please wait a few moments while I get approval to display.",
    # A denied request (or a timed-out one) just sends response_not_whitelisted -
    # there is no separate "denied" message.
    "admin_approval_prompt": "New name request: '{name}'. Reply Y to add to whitelist, or N to deny.",
    # Minutes a pending request waits for an admin Y/N before it expires (0 = never).
    # On expiry the texter gets the standard not-whitelisted reply so they aren't left waiting.
    "admin_approval_timeout_mins": 5,
}

config = DEFAULT_CONFIG.copy()
twilio_client = None
last_message_sid = None
last_gv_uid = None
polling_thread = None
polling_source = None      # which message source the live polling_thread serves
polling_generation = 0     # bumped to retire an obsolete poller when source changes
_gv_reply_ctx = None       # reply target/headers for the GV message being processed
display_thread = None
stop_polling = False
stop_display = False

# Queue system
message_queue = deque()
currently_displaying = None
queue_lock = threading.Lock()

# The names-content item actually chosen for the display in progress (set by send_to_fpp),
# so the return/stop paths stop the right content and the worker uses its duration. None
# means the flat-config fallback (name over waiting) is in use.
_active_name_content = None       # e.g. "seq:Foo" / "img:bar.png" / ""
_active_display_duration = None   # int seconds for the current display

# The waiting content currently on the output as the BASE layer (what a name composites
# over, what a name-return reveals, what stop must clear). Set by start_default_playlist()
# for the single-content case and by the waiting rotator on each switch. Falls back to
# config['default_playlist'] when unset.
_active_waiting_content = ''
# Waiting-content rotator (only rotates when default_content_list has 2+ items and the show
# is enabled). The thread lives for the whole process, idling otherwise.
rotator_thread = None
stop_rotator = True               # pause flag: True = don't switch/idle. Cleared on start.
rotator_lock = threading.Lock()   # serializes a rotator switch against stop_show_playback teardown
_fseq_dur_cache = {}              # {seq_name: duration_seconds} - parsed FSEQ lengths

# ── Multi-instance (master/remote) ──────────────────────────────────────────
_resolved_role = None            # cached effective role ("master"/"remote")
_remotes_cache = []              # cached list of remote base URLs the master pushes to
_remotes_cache_time = 0.0
_masters_cache = []              # remote side: cached detailed master list [{address,base,name,phone}]
_masters_cache_time = 0.0
_REMOTES_CACHE_TTL = 30          # seconds between MultiSync discovery refreshes
_tml_peer_cache = set()          # cached IPs allowed to call /api/tml/* (FPP peers)
_tml_peer_cache_time = 0.0
_remote_last_state = None        # remote side: last state applied from a master push
_remote_last_state_time = 0.0
_remote_stop_requested = False   # remote side: True once the master broadcasts Stop. A remote
                                 # never self-stops on its local `enabled` flag (it's usually
                                 # never activated locally - only the master is Started); it
                                 # keeps returning to its waiting content between names and
                                 # only tears the show down when the master says to.
_last_fpp_mode = None            # last-seen FPP mode role, for the auto-follow watcher
_fpp_mode_detail = ''            # raw values _fpp_mode_role() last read, for on-device diagnosis


def _fpp_mode_role():
    """The plugin role implied by FPP's CURRENT mode - 'remote' ONLY when FPP is unambiguously
    in remote mode, else 'master' - or None when FPP can't be reached (so callers don't act on a
    transient failure). Primary source is the fppMode setting (string 'remote'/'player'/'master'
    or legacy int); falls back to /api/fppd/status (mode==8=REMOTE / mode_name). Records what it
    read in _fpp_mode_detail for on-device diagnosis. 'master' is the safe/full-function default
    for anything that is NOT clearly remote."""
    global _fpp_mode_detail
    # Primary: the fppMode setting value itself (most direct).
    try:
        r = requests.get(f"{FPP_HOST}/api/settings/fppMode", timeout=3)
        if r.status_code == 200:
            # FPP versions differ: newer returns a JSON OBJECT describing the setting
            # ({"value":"remote","options":{...},...}), older returns a bare JSON string
            # ("remote") or legacy int (8). Pull the actual mode out of whichever shape,
            # never match against the whole payload (the object contains the substring
            # "remote" in its options and would false-positive, or here false-NEGATIVE
            # against an exact compare and wrongly resolve to master).
            val = (r.text or '').strip()
            try:
                parsed = r.json()
                if isinstance(parsed, dict):
                    val = str(parsed.get('value', ''))
                elif isinstance(parsed, (str, int)):
                    val = str(parsed)
            except Exception:
                pass
            txt = val.strip().strip('"').lower()   # "remote" / "player" / "master" / legacy int
            if txt:
                # Remote ONLY when it's exactly 'remote' or the legacy remote int (8) - never a
                # loose substring match (which could trip on unexpected payloads). Everything
                # else (player, master, bridge, numbers) is treated as master.
                role = 'remote' if (txt == 'remote' or txt == '8') else 'master'
                _fpp_mode_detail = f"settings/fppMode value={txt!r} → {role}"
                return role
    except Exception as e:
        logging.debug(f"_fpp_mode_role: settings/fppMode unavailable ({e})")
    # Fallback: fppd status.
    try:
        r = requests.get(f"{FPP_HOST}/api/fppd/status", timeout=3)
        if r.status_code == 200:
            data = r.json()
            mode_name = str(data.get('mode_name', '')).strip().lower()
            mode_int = data.get('mode')
            if mode_name == 'remote' or mode_int == 8:
                _fpp_mode_detail = f"fppd/status mode_name={mode_name!r} mode={mode_int} → remote"
                return 'remote'
            if mode_name or mode_int is not None:
                _fpp_mode_detail = f"fppd/status mode_name={mode_name!r} mode={mode_int} → master"
                return 'master'
    except Exception as e:
        logging.debug(f"_fpp_mode_role: fppd status unavailable ({e})")
    _fpp_mode_detail = 'FPP unreachable'
    return None


def _default_plugin_role():
    """Suggested default role from FPP's mode; 'master' (the full-function role, correct for a
    lone box) when FPP can't be reached."""
    return _fpp_mode_role() or 'master'


def _fpp_mode_role_for_seed(attempts=5, delay=2):
    """Like _fpp_mode_role() but retried a few times, for the one-time first-install role
    seed. The plugin is launched by postStart.sh right as FPPD comes up, so FPP's API can
    still be warming up for the first few seconds - a single read could miss 'remote'.
    Returns 'remote'/'master', or None if FPP never answered in the window."""
    for i in range(attempts):
        role = _fpp_mode_role()
        if role is not None:
            return role
        if i < attempts - 1:
            time.sleep(delay)
    return None


def fpp_mode_watcher():
    """Track the FPP instance's own player/remote mode for diagnostics and as the AUTO default.

    IMPORTANT: this NEVER overwrites an explicit plugin_role. A manually chosen 'master'/'remote'
    is authoritative and persists across restarts/updates. FPP mode only supplies the default
    when plugin_role is unset (''): on an FPP mode change in that auto case we just invalidate
    the cached resolution so get_plugin_role() re-reads FPP's mode. (Earlier this clobbered the
    saved role on every startup - the first read looked like a 'change' - which flipped a manual
    master back to remote on each plugin update.)"""
    global _last_fpp_mode, _resolved_role
    logging.info("🔀 FPP mode watcher started (diagnostic + auto-default only; manual role wins)")
    while True:
        try:
            role = _fpp_mode_role()   # None while FPP is unreachable → leave _last_fpp_mode as-is
            if role is not None and role != _last_fpp_mode:
                first = _last_fpp_mode is None
                _last_fpp_mode = role
                logging.info(f"🔀 FPP mode {'at startup' if first else 'changed to'}: '{role}' "
                             f"[{_fpp_mode_detail}] | explicit plugin_role="
                             f"{(config.get('plugin_role') or '')!r} (explicit always wins)")
                # Only auto mode (no explicit choice) follows FPP; re-resolve lazily. An explicit
                # plugin_role is left completely untouched.
                if not (config.get('plugin_role') or '').strip():
                    _resolved_role = None
        except Exception as e:
            logging.debug(f"fpp_mode_watcher: {e}")
        time.sleep(10)


def get_plugin_role():
    """Effective role. An explicit config choice ('master'/'remote') always wins; when unset,
    fall back to the FPP-mode default (resolved once, then cached for the process)."""
    global _resolved_role
    role = (config.get('plugin_role') or '').strip().lower()
    if role in ('master', 'remote'):
        return role
    if _resolved_role is None:
        _resolved_role = _default_plugin_role()
    return _resolved_role


def is_remote():
    return get_plugin_role() == 'remote'


def _instance_label():
    """Friendly name this instance advertises to remotes: the FPP hostname."""
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return 'FPP'


def _instance_phone_label():
    """The source number/account this instance texts from, shown to remotes so they can tell
    multiple masters apart. Twilio → the phone number; Google Voice → the Gmail address."""
    if config.get('message_source', 'twilio') == 'google_voice':
        return (config.get('gv_email') or '').strip()
    return (config.get('twilio_phone_number') or '').strip()


def _local_ips():
    """Best-effort set of this host's own addresses, to exclude self from discovery."""
    import socket
    ips = {'127.0.0.1', '::1', 'localhost'}
    try:
        hn = socket.gethostname()
        ips.add(hn)
        ips.add(socket.gethostbyname(hn))
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


def _parse_host_port(target, default_port=5000):
    """Split a manual 'host' or 'host:port' target into (host, port)."""
    t = str(target).strip()
    if not t:
        return None
    if ':' in t and not t.startswith('['):
        host, _, p = t.rpartition(':')
        try:
            return host, int(p)
        except ValueError:
            return t, default_port
    return t, default_port


def _multisync_addresses():
    """IP addresses of the FPP systems this box knows via MultiSync (excludes self)."""
    addrs = set()
    try:
        r = requests.get(f"{FPP_HOST}/api/fppd/multiSyncSystems", timeout=3)
        if r.status_code == 200:
            data = r.json()
            systems = data.get('systems') if isinstance(data, dict) else data
            local = _local_ips()
            for s in (systems or []):
                if not isinstance(s, dict):
                    continue
                a = str(s.get('address') or s.get('ip') or '').strip()
                if a and a not in local and not s.get('local'):
                    addrs.add(a)
    except Exception as e:
        logging.debug(f"_multisync_addresses: failed ({e})")
    return addrs


def _trusted_tml_peers():
    """IPs allowed to call this instance's /api/tml/* endpoints WITHOUT the access token: the
    FPP MultiSync peers this box already recognizes. No shared secret - trust follows FPP's
    own sync network. Cached briefly."""
    global _tml_peer_cache, _tml_peer_cache_time
    now = time.time()
    if _tml_peer_cache and (now - _tml_peer_cache_time) < _REMOTES_CACHE_TTL:
        return _tml_peer_cache
    peers = set(_multisync_addresses())
    _tml_peer_cache, _tml_peer_cache_time = peers, now
    return peers


def _probe_plugin_peers():
    """Probe the FPP MultiSync systems and group the ones running this plugin by role:
    {'master': [base URLs], 'remote': [base URLs]}. Discovery is automatic - peers come from
    FPP's own MultiSync network, so the user only has to set each FPP's player/remote mode."""
    out = {'master': [], 'remote': []}
    seen = set()
    for host in _multisync_addresses():
        h = f"[{host}]" if (':' in host and not host.startswith('[')) else host  # bracket IPv6
        base = f"http://{h}:5000"
        if base in seen:
            continue
        seen.add(base)
        try:
            pr = requests.get(f"{base}/api/tml/ping", timeout=2)
            if pr.status_code == 200:
                j = pr.json()
                if j.get('plugin') == 'textmylights' and j.get('role') in out:
                    out[j['role']].append(base)
        except Exception:
            pass  # unreachable / not the plugin - skip silently
    return out


def discover_remotes(force=False):
    """Base URLs of peer instances the master should push to - ONLY FPP systems running this
    plugin in REMOTE mode. Cached briefly."""
    global _remotes_cache, _remotes_cache_time
    now = time.time()
    if not force and _remotes_cache and (now - _remotes_cache_time) < _REMOTES_CACHE_TTL:
        return _remotes_cache
    remotes = _probe_plugin_peers()['remote']
    _remotes_cache, _remotes_cache_time = remotes, now
    return remotes


def discover_masters(force=False):
    """Remote side: detailed list of reachable plugin MASTERS on the FPP MultiSync network,
    each `{address, base, name, phone}`, so the UI can present them for selection. Cached
    briefly (same TTL as the other discovery caches)."""
    global _masters_cache, _masters_cache_time
    now = time.time()
    if not force and _masters_cache and (now - _masters_cache_time) < _REMOTES_CACHE_TTL:
        return _masters_cache
    found = []
    for host in _multisync_addresses():
        h = f"[{host}]" if (':' in host and not host.startswith('[')) else host  # bracket IPv6
        base = f"http://{h}:5000"
        try:
            pr = requests.get(f"{base}/api/tml/ping", timeout=2)
            if pr.status_code == 200:
                j = pr.json()
                if j.get('plugin') == 'textmylights' and j.get('role') == 'master':
                    found.append({"address": host, "base": base,
                                  "name": j.get('name') or host, "phone": j.get('phone') or ''})
        except Exception:
            pass  # unreachable / not the plugin - skip silently
    found.sort(key=lambda m: (m.get('name') or '').lower())
    _masters_cache, _masters_cache_time = found, now
    return found


def _selected_master_addr():
    """Remote: the address of the plugin master this remote is pinned to ('' = none).
    The raw config value may be '' (never chosen - eligible for auto-select), 'none' (the user
    explicitly cleared the pin), or an address; both '' and 'none' mean "follow nobody now"."""
    raw = (config.get('selected_master') or '').strip()
    return '' if raw.lower() == 'none' else raw


def _maybe_auto_select_single_master():
    """Remote convenience: when the user has never chosen a master (selected_master unset) and
    exactly ONE plugin master is on the network, pin to it automatically. With 0 or 2+ masters,
    or once the user has explicitly picked/cleared ('none'), leave the choice alone. Returns
    True if it just auto-pinned."""
    if not is_remote():
        return False
    raw = (config.get('selected_master') or '').strip()
    if raw:   # already pinned to an address, or explicitly 'none' - don't auto-choose
        return False
    masters = discover_masters()
    if len(masters) == 1:
        config['selected_master'] = masters[0]['address']
        save_config()
        logging.info(f"🔗 Remote: auto-selected the only plugin master ({masters[0]['address']})")
        return True
    return False


def _find_master_base():
    """Remote: base URL of the selected master to sync FROM. A remote follows a master ONLY when
    one is explicitly picked in the 'Sync to Master' list - no master selected means follow
    nobody (returns None). Also None when the chosen master isn't currently reachable."""
    sel = _selected_master_addr()
    if not sel:
        return None  # nothing picked → the remote follows no master
    for m in discover_masters():
        if m['address'] == sel:
            return m['base']
    return None  # pinned master not currently reachable - don't silently follow another


def sync_names_content_from_master():
    """Remote: mirror the MASTER's Name Display content ids into this instance's own list so
    the Display-tab dropdown shows them - but only content that physically exists on this
    remote, and KEEPING this remote's own per-content overlay layout (different model size /
    positioning). Returns True if the list changed."""
    if not is_remote():
        return False
    # If exactly one master exists and nothing's been chosen yet, pin to it automatically.
    _maybe_auto_select_single_master()
    # Still no master picked (0 or 2+ masters, or explicitly cleared) → mirror nothing. Clear
    # any list a previous selection left behind so the Display dropdown shows nothing.
    if not _selected_master_addr():
        if config.get('names_content_list'):
            config['names_content_list'] = []
            config['names_content_rr_index'] = -1
            save_config()
            logging.info("🔁 Remote: no master selected - cleared synced name content list")
            return True
        return False
    master = _find_master_base()
    if not master:
        return False   # selected master unreachable → keep current (don't wipe saved layouts)
    try:
        r = requests.get(f"{master}/api/tml/content-list", timeout=3)
        if r.status_code != 200:
            return False
        master_ids = [c for c in (r.json().get('names') or []) if c]
    except Exception as e:
        logging.debug(f"sync_names_content_from_master: fetch failed ({e})")
        return False

    # Only content physically present on THIS remote, in the master's order.
    target = [c for c in master_ids if _content_exists_locally(c)]
    lst = config.get('names_content_list', []) or []
    existing = {it.get('content'): it for it in lst if it.get('content')}
    new_list = []
    for cid in target:
        if cid in existing:
            new_list.append(existing[cid])          # keep this remote's own layout
        else:
            item = _names_item_defaults()
            item['content'] = cid
            item['message_lines'] = ['{name}', '', '', '']   # sensible starting layout
            new_list.append(item)

    if [it.get('content') for it in new_list] != [it.get('content') for it in lst]:
        config['names_content_list'] = new_list
        if int(config.get('names_content_rr_index', -1) or -1) >= len(new_list):
            config['names_content_rr_index'] = -1
        save_config()
        logging.info(f"🔁 Remote: synced Name content list from master ({len(new_list)} item(s) "
                     f"present locally)")
        return True
    return False


def remote_content_sync():
    """Remote daemon: keep the Name-content dropdown mirrored from the master (~every 15s)."""
    while True:
        try:
            if is_remote():
                sync_names_content_from_master()
        except Exception as e:
            logging.debug(f"remote_content_sync: {e}")
        time.sleep(15)


def push_state_to_remotes(payload):
    """Master only: fire-and-forget the chosen display state (name + content id + timing -
    never sequence bytes) to every confirmed remote. Runs in a background thread so it never
    delays the master's own display or SMS reply. No-op unless this instance is the master."""
    if get_plugin_role() != 'master':
        return

    def _worker():
        try:
            remotes = discover_remotes()
            if not remotes:
                return
            for base in remotes:
                try:
                    requests.post(f"{base}/api/tml/state", json=payload, timeout=2)
                except Exception as e:
                    logging.debug(f"push to {base} failed: {e}")
        except Exception as e:
            logging.warning(f"push_state_to_remotes error: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def _push_waiting_state():
    """Master: tell remotes which waiting/background content is now active (a 'waiting event'
 - content id only, no name) so they mirror the master's rotation pick. The remote ignores
    a push for content it's already showing, so re-sending is harmless."""
    if get_plugin_role() != 'master':
        return
    push_state_to_remotes({'content': _active_waiting_content})


def broadcast_stop_to_remotes():
    """Master only: tell every confirmed remote to Stop too, so pressing Stop on the master
    takes the whole show down. Fire-and-forget in a background thread; each remote drains its
    own queue (any names the master already pushed finish first) before going dark."""
    if get_plugin_role() != 'master':
        return

    def _worker():
        try:
            remotes = discover_remotes()
            for base in remotes:
                try:
                    requests.post(f"{base}/api/tml/stop", timeout=2)
                except Exception as e:
                    logging.debug(f"stop broadcast to {base} failed: {e}")
        except Exception as e:
            logging.warning(f"broadcast_stop_to_remotes error: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def master_sync_heartbeat():
    """Master: re-assert the current WAITING content to remotes every few seconds so a remote
    that joins late (rebooted, powered on after the master) converges to the same background.
    Only fires while idle (no name showing); the remote no-ops if already on that content."""
    while True:
        try:
            # Only re-assert while the master's own show is live and idle. A STOPPED or
            # draining master must not keep pushing waiting content - that would re-arm a
            # remote that was just told to stop (the waiting push clears its stop flag).
            if (get_plugin_role() == 'master' and config.get('enabled', False)
                    and currently_displaying is None):
                push_state_to_remotes({'content': _active_waiting_content})
        except Exception as e:
            logging.debug(f"master_sync_heartbeat: {e}")
        time.sleep(10)


def _coerce_len(seq, n, fill):
    """Return a list of exactly n items from seq, truncating or padding with `fill`
    (dicts are copied so padded entries never share a reference)."""
    out = list(seq) if isinstance(seq, (list, tuple)) else []
    out = out[:n]
    while len(out) < n:
        out.append(dict(fill) if isinstance(fill, dict) else fill)
    return out


def _names_item_defaults():
    """A fresh names-content item with a default (blank, centered) text layout."""
    return {
        "content": "",
        "display_duration": 30,
        "message_lines": ["", "", "", ""],
        "line_boxes": [{"x": -1, "y": -1, "w": 300, "h": 60} for _ in range(4)],
        "line_colors": ["", "", "", ""],
        "line_movements": ["Center", "Center", "Center", "Center"],
        "line_speeds": [50, 50, 50, 50],
        "line_fonts": ["FreeSans", "FreeSans", "FreeSans", "FreeSans"],
        "line_orientations": ["horizontal", "horizontal", "horizontal", "horizontal"],
    }


def _names_item_from_flat_config():
    """Build a names-content item from the current flat config keys (the single
    name_display_playlist + global message_lines/line_*/display_duration). Used once to
    migrate the pre-list config into names_content_list[0]."""
    item = _names_item_defaults()
    item["content"] = config.get("name_display_playlist", "")
    try:
        item["display_duration"] = max(1, int(config.get("display_duration", 30) or 30))
    except (TypeError, ValueError):
        item["display_duration"] = 30
    item["message_lines"]     = _coerce_len(config.get("message_lines", []), 4, "")
    item["line_boxes"]        = _coerce_len(config.get("line_boxes", []), 4, {"x": -1, "y": -1, "w": 300, "h": 60})
    item["line_colors"]       = _coerce_len(config.get("line_colors", []), 4, "")
    item["line_movements"]    = _coerce_len(config.get("line_movements", []), 4, "Center")
    item["line_speeds"]       = _coerce_len(config.get("line_speeds", []), 4, 50)
    item["line_fonts"]        = _coerce_len(config.get("line_fonts", []), 4, "FreeSans")
    item["line_orientations"] = _coerce_len(config.get("line_orientations", []), 4, "horizontal")
    return item


def _sanitize_names_item(raw):
    """Coerce a client-supplied names item into the canonical shape (arrays length 4, sane
    defaults). Never trusts lengths/types from the request."""
    d = _names_item_defaults()
    if not isinstance(raw, dict):
        return d
    d["content"] = str(raw.get("content", "") or "")
    try:
        d["display_duration"] = max(1, int(raw.get("display_duration", 30) or 30))
    except (TypeError, ValueError):
        d["display_duration"] = 30
    d["message_lines"]     = _coerce_len(raw.get("message_lines", []), 4, "")
    d["line_boxes"]        = _coerce_len(raw.get("line_boxes", []), 4, {"x": -1, "y": -1, "w": 300, "h": 60})
    d["line_colors"]       = _coerce_len(raw.get("line_colors", []), 4, "")
    d["line_movements"]    = _coerce_len(raw.get("line_movements", []), 4, "Center")
    d["line_speeds"]       = _coerce_len(raw.get("line_speeds", []), 4, 50)
    d["line_fonts"]        = _coerce_len(raw.get("line_fonts", []), 4, "FreeSans")
    d["line_orientations"] = _coerce_len(raw.get("line_orientations", []), 4, "horizontal")
    return d


def select_names_content_item():
    """Pick the names-content item for the incoming name, or None to fall back to the flat
    config (name over the waiting content). Round-robin advances and persists a cursor;
    random avoids an immediate repeat. Returns the stored dict (callers read only)."""
    lst = config.get("names_content_list", []) or []
    if not lst:
        return None
    if len(lst) == 1:
        return lst[0]
    mode = config.get("names_content_mode", "roundrobin")
    prev = config.get("names_content_rr_index", -1)
    if mode == "random":
        choices = [i for i in range(len(lst)) if i != prev] or list(range(len(lst)))
        idx = random.choice(choices)
    else:
        idx = (prev + 1) % len(lst)
    config["names_content_rr_index"] = idx
    save_config()
    return lst[idx]


def _default_item_defaults():
    """A fresh waiting-content item. `display_duration` is only used for img: items (and as
    a fallback when a seq's FSEQ length can't be read); seq: items play their full length."""
    return {"content": "", "display_duration": 30}


def _sanitize_default_item(raw):
    """Coerce a client-supplied waiting item into the canonical {content, display_duration}
    shape. Never trusts types from the request."""
    d = _default_item_defaults()
    if not isinstance(raw, dict):
        return d
    d["content"] = str(raw.get("content", "") or "")
    try:
        d["display_duration"] = max(1, int(raw.get("display_duration", 30) or 30))
    except (TypeError, ValueError):
        d["display_duration"] = 30
    return d


def select_default_content_item():
    """Pick the next waiting-content item to rotate to, advancing/persisting the cursor.
    Round-robin walks the list in order; random avoids an immediate repeat. Returns the
    stored dict, or None when the list is empty (caller falls back to default_playlist)."""
    lst = config.get("default_content_list", []) or []
    if not lst:
        return None
    if len(lst) == 1:
        config["default_content_rr_index"] = 0
        return lst[0]
    mode = config.get("default_content_mode", "roundrobin")
    prev = config.get("default_content_rr_index", -1)
    if mode == "random":
        choices = [i for i in range(len(lst)) if i != prev] or list(range(len(lst)))
        idx = random.choice(choices)
    else:
        idx = (prev + 1) % len(lst)
    config["default_content_rr_index"] = idx
    save_config()
    return lst[idx]


def _fseq_duration_seconds(content):
    """Return the play length (seconds, rounded up) of a seq: waiting item from its FSEQ
    header, or None if it can't be determined. Cached by sequence name."""
    if not content or not content.startswith('seq:'):
        return None
    seq_name = os.path.basename(content[4:].removesuffix('.fseq'))  # filename only, no traversal
    if seq_name in _fseq_dur_cache:
        return _fseq_dur_cache[seq_name]
    dur = None
    try:
        filepath = os.path.join(FSEQ_SEQUENCE_PATH, seq_name + '.fseq')
        if os.path.exists(filepath):
            hdr = parse_fseq_header(filepath)
            ms = hdr.get('duration_ms', 0)
            if ms and ms > 0:
                dur = max(1, (int(ms) + 999) // 1000)   # ceil to whole seconds
    except Exception as e:
        logging.warning(f"Could not read FSEQ length for {seq_name}: {e}")
    _fseq_dur_cache[seq_name] = dur
    return dur


def load_config():
    """Load configuration from file, merging with defaults so new settings survive updates"""
    global config, twilio_client, last_message_sid, last_gv_uid
    try:
        with open(CONFIG_FILE, 'r') as f:
            loaded = json.load(f)

        secrets = load_secrets()
        # One-time migration: older versions stored credentials inside plugin.json.
        # Move any inline secrets into the owner-only secrets file and strip them
        # from the main config so they never get rewritten to plugin.json.
        migrated = False
        for k in SECRET_KEYS:
            if k in loaded:
                if loaded[k] and not secrets.get(k):
                    secrets[k] = loaded[k]
                    migrated = True
                del loaded[k]

        config.update(loaded)
        config.update(secrets)

        # If the plugin was updated and new default keys were added (or we just
        # migrated secrets out), save so the files stay complete/clean.
        present = set(loaded.keys()) | set(secrets.keys())
        new_keys = set(DEFAULT_CONFIG.keys()) - present
        if new_keys or migrated:
            save_config()
            if migrated:
                logging.info("Migrated inline credentials into the owner-only secrets file")
            if new_keys:
                logging.info(f"Saved {len(new_keys)} new default setting(s) after update: {new_keys}")

        # Migrate old scroll_speed values (pre-v2.6 stored raw px/s, now 1-10 scale)
        if config.get('scroll_speed', 5) > 10:
            config['scroll_speed'] = 5
            save_config()

        # Upgrade legacy Invalid Format defaults to the dynamic {words} default so
        # the reply reflects the active word limit. Only touches known old defaults,
        # never a genuinely customized message.
        _legacy_invalid = {
            "Please send only a name (1-2 words, no sentences).",
            "Please send only 1 name (1-2 words, no sentences).",
        }
        if config.get('response_invalid_format', '') in _legacy_invalid:
            config['response_invalid_format'] = DEFAULT_CONFIG['response_invalid_format']
            save_config()
            logging.info("Upgraded Invalid Format response to the dynamic {words} default")

        # Migrate old message_template to message_lines (introduced in v2.6)
        if 'message_lines' not in loaded and 'message_template' in loaded:
            tmpl = loaded.get('message_template', 'Merry Christmas {name}!')
            config['message_lines'] = [tmpl, '', '', '']
            save_config()
            logging.info(f"Migrated message_template '{tmpl}' to message_lines[0]")

        # Migrate the old single global Text Movement/Scroll Speed onto per-line
        # settings (introduced alongside per-line movement) so upgrading doesn't
        # reset everyone's lines back to Center/5.
        if 'line_movements' not in loaded:
            config['line_movements'] = [config.get('text_position', 'Center')] * 4
            save_config()
        if 'line_speeds' not in loaded:
            # *10: scroll_speed is still on the old 1-10 scale; line_speeds is 0-100.
            # Seeded fresh on the new scale, so no further scaling is ever needed.
            config['line_speeds'] = [config.get('scroll_speed', 5) * 10] * 4
            config['line_speeds_scale_migrated'] = True
            save_config()

        # Migrate line_speeds itself from the old 1-10(-by-tenths) scale to the new
        # 0-100 scale (introduced 2026-09) -- without this, speeds saved by anyone
        # already using per-line speed (e.g. "5") would silently become 10x slower
        # once reinterpreted on the new scale, rather than an equivalent "50". Only
        # runs once: the flag above is set here, and pre-set for anyone who just got
        # line_speeds seeded fresh (already on the new scale, above).
        if 'line_speeds' in loaded and not config.get('line_speeds_scale_migrated'):
            config['line_speeds'] = [min(100, round(float(s) * 10)) for s in config.get('line_speeds', [50, 50, 50, 50])]
            config['line_speeds_scale_migrated'] = True
            save_config()

        # Migrate the old single global Font onto per-line settings (introduced
        # alongside per-line font) so upgrading doesn't reset everyone's lines
        # back to FreeSans.
        if 'line_fonts' not in loaded:
            config['line_fonts'] = [config.get('text_font', 'FreeSans')] * 4
            save_config()

        # Migrate the old point-based line_positions + fixed line_font_sizes onto
        # the new box-based line_boxes (font size became auto-fit-to-box instead
        # of a fixed per-line number), so upgrading doesn't reset positioning.
        # Reads straight from the old raw file contents (loaded), not `config`,
        # since line_positions/line_font_sizes are no longer in DEFAULT_CONFIG.
        if 'line_boxes' not in loaded:
            old_positions = loaded.get('line_positions', [{'x': -1, 'y': -1}] * 4)
            old_sizes = loaded.get('line_font_sizes', [loaded.get('text_font_size', 48)] * 4)
            model_w = config.get('overlay_model_width', 0)
            default_w = round(model_w * 0.9) if model_w > 0 else 300
            boxes = []
            for i in range(4):
                pos = old_positions[i] if i < len(old_positions) else {'x': -1, 'y': -1}
                size = old_sizes[i] if i < len(old_sizes) else 48
                boxes.append({'x': pos.get('x', -1), 'y': pos.get('y', -1),
                              'w': default_w, 'h': round(size * 1.3)})
            config['line_boxes'] = boxes
            save_config()
            logging.info("Migrated line_positions/line_font_sizes to line_boxes")

        # names_content_list (v2.7) starts EMPTY on upgrade. While empty, send_to_fpp
        # falls back to the flat name_display_playlist + global text layout - identical to
        # the pre-list behavior, and the existing UI keeps working. The list is seeded from
        # the flat config (via _names_item_from_flat_config) the first time the new
        # per-content UI loads with content configured, at which point it becomes
        # authoritative. The new-default-key backfill above already ensures the key exists.
        if config.get('names_content_mode') not in ('roundrobin', 'random'):
            config['names_content_mode'] = 'roundrobin'

        if config['twilio_account_sid'] and config['twilio_auth_token']:
            twilio_client = Client(
                config['twilio_account_sid'],
                config['twilio_auth_token']
            )

        try:
            with open(LAST_SID_FILE, 'r') as f:
                last_message_sid = f.read().strip()
                logging.info(f"Loaded last message SID: {last_message_sid}")
        except:
            last_message_sid = None

        # Resume Google Voice dedup marker across restarts (None => anchor to
        # newest on first poll so the whole inbox isn't replayed)
        last_gv_uid = load_last_gv_uid()

        save_config()

        logging.info("Configuration loaded successfully")
    except FileNotFoundError:
        # FIRST INSTALL ONLY (no plugin.json yet). If THIS FPP box is already in Remote
        # mode, open the plugin as a remote too - seed an EXPLICIT plugin_role='remote'.
        # Seeding it explicitly (rather than leaving '' = auto) means later plugin updates
        # never re-evaluate or flip it; the stored role wins from here on. A non-remote box
        # is left on '' (auto -> master), unchanged, and if FPP can't be reached in time we
        # also leave '' so the auto-follow watcher resolves it later.
        if _fpp_mode_role_for_seed() == 'remote':
            config['plugin_role'] = 'remote'
            logging.info(f"First install on a Remote FPP: seeded plugin_role='remote' [{_fpp_mode_detail}]")
        save_config()
        logging.info("Created default configuration")
    except Exception as e:
        logging.error(f"Error loading config: {e}")

def load_secrets():
    """Read credentials from the owner-only secrets file. Returns {} if absent."""
    try:
        with open(SECRETS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logging.error(f"Error loading secrets: {e}")
        return {}

def save_config():
    """Persist configuration. Credentials are written to the owner-only secrets
    file (chmod 600); everything else goes to plugin.json (also 600, without the
    secrets). `config` in memory always holds the merged view."""
    try:
        # Secrets → owner-only file, never into plugin.json/logs/backups.
        secrets_out = {k: config[k] for k in SECRET_KEYS if config.get(k)}
        os.makedirs(SECRETS_DIR, exist_ok=True)
        try:
            os.chmod(SECRETS_DIR, 0o700)
        except OSError:
            pass
        with open(SECRETS_FILE, 'w') as f:
            json.dump(secrets_out, f, indent=2)
        try:
            os.chmod(SECRETS_FILE, 0o600)
        except OSError:
            pass

        # Everything except the secrets → main config file.
        main_out = {k: v for k, v in config.items() if k not in SECRET_KEYS}
        with open(CONFIG_FILE, 'w') as f:
            json.dump(main_out, f, indent=2)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        logging.info("Configuration saved")
    except Exception as e:
        logging.error(f"Error saving config: {e}")


def _response_is_muted(message_type):
    """Return True when this auto-response must not send right now because a
    companion setting has "greyed it out" - WITHOUT touching the stored toggle.

    The config page disables (greys) a response row when another setting makes
    it moot, but deliberately never unchecks the toggle, so the user's on/off
    choice survives the row un-greying. We mirror that on the backend: the send
    path honors the mute at runtime, while plugin.json keeps the last state. The
    toggle only ever changes when the user changes it.

 - Any response over Twilio - the plugin has no Twilio reply path, so the
        SMS Responses tab is hidden and nothing may send.
 - rate-limited when Max Messages Per Phone is 0 (nobody is ever limited).
 - duplicate    when Allow Duplicate Names is on (never a duplicate).

    Invalid-Format / Too-Long / Not-Whitelisted are intentionally NOT muted
    here: the single-name path greys them under the whitelist, but the
    grouped-text path reuses the Invalid-Format reply for whitelist rejects,
    and their single-name triggers can't fire in the greyed case anyway."""
    if config.get('message_source', 'twilio') != 'google_voice':
        return True
    if message_type == 'rate_limited' and config.get('max_messages_per_phone', 0) == 0:
        return True
    if message_type == 'duplicate' and config.get('allow_duplicate_names', False):
        return True
    return False

_font_path_cache = {}

def _resolve_font_path(font_name):
    """Resolve a font name to its file path (fc-match, falling back to a manual
    directory search), cached per name for the process lifetime. Box-fit sizing
    calls this many times per line (once per binary-search step) at different
    sizes, so the expensive part - locating the file - only happens once."""
    if font_name in _font_path_cache:
        return _font_path_cache[font_name]

    path = None
    # Use fontconfig (fc-match) - same resolution FPP uses for its font names
    try:
        import subprocess
        result = subprocess.run(
            ['fc-match', '--format=%{file}', font_name],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0 and result.stdout.strip():
            candidate = result.stdout.strip()
            if os.path.exists(candidate):
                path = candidate
    except Exception:
        pass

    if not path:
        # Fallback: manual search in common FPP font directories
        search_dirs = [
            '/usr/share/fonts/truetype/freefont',
            '/usr/share/fonts/truetype',
            '/usr/share/fonts/opentype',
            '/usr/share/fonts',
            '/usr/local/share/fonts',
            '/usr/share/fpp/fonts',
            '/home/fpp/media/fonts',
        ]
        font_name_lower = font_name.lower()
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for dirpath, _, filenames in os.walk(search_dir):
                for fname in filenames:
                    if fname.lower().endswith(('.ttf', '.otf')) and font_name_lower in fname.lower():
                        path = os.path.join(dirpath, fname)
                        break
                if path:
                    break
            if path:
                break

    _font_path_cache[font_name] = path
    return path

def _find_font(font_name, font_size):
    """Locate a PIL ImageFont matching font_name at a specific size. Returns ImageFont or None."""
    if not PIL_AVAILABLE:
        return None
    path = _resolve_font_path(font_name)
    if path:
        try:
            return ImageFont.truetype(path, font_size)
        except Exception:
            pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None

def _fit_text_to_box(draw, text, font_name, box_w, box_h, min_size=6, max_size=None):
    """Binary search the largest font size where `text` fits within box_w x box_h.
    Pass box_w=None to fit width only, or box_h=None to fit height only -- used for
    scrolling lines, where the text is expected to be larger than its box along the
    travel axis and moves across/through it rather than being shrunk to fit.
    max_size defaults to a cap that scales with whichever of box_w/box_h is given,
    rather than a fixed number -- otherwise a constraining dimension bigger than the
    cap leaves real headroom unused forever, since the search can never explore past
    it. Returns (font_or_None, text_w, text_h) at the best-fit size."""
    if not PIL_AVAILABLE or not text:
        return None, 0, 0
    if max_size is None:
        max_size = max([300] + [int(d * 2) for d in (box_w, box_h) if d is not None])
    lo, hi = min_size, max_size
    # Seed with the smallest size so a box too small for even min_size to fit still
    # renders something (slightly overflowing) instead of the line silently vanishing.
    best_font = _find_font(font_name, min_size)
    if best_font is not None:
        bbox = draw.textbbox((0, 0), text, font=best_font)
        best_w, best_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    else:
        best_w, best_h = len(text) * (min_size * 0.6), min_size
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _find_font(font_name, mid)
        if font is not None:
            bbox = draw.textbbox((0, 0), text, font=font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        else:
            w, h = len(text) * (mid * 0.6), mid
        if (box_w is None or w <= box_w) and (box_h is None or h <= box_h):
            best_font, best_w, best_h = font, w, h
            lo = mid + 1
        else:
            hi = mid - 1
    return best_font, best_w, best_h

def _hex_to_rgb(hex_str):
    """Parse a '#RRGGBB' (or 'RRGGBB') string into an (r, g, b) tuple."""
    hex_str = hex_str.lstrip('#')
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))


def _render_oriented_text_strip(text, font_name, box_w, box_h, color_rgb, orientation):
    """Auto-fit `text` to (box_w, box_h) per `orientation` and render it to a tightly-sized
    RGB strip (black background, matching the other strip/paste code in this file) for the
    caller to center/paste within its own box. orientation is 'horizontal' (default),
    'vertical_rotated' (whole line rotated 90 degrees), or 'vertical_stacked' (one
    character per row, each upright). Either box_w or box_h may be None to leave that
    dimension unconstrained -- used for T2B/B2T scrolling, where the travel axis has no
    fixed extent. Returns (strip_or_None, w, h)."""
    probe = Image.new('RGB', (1, 1))
    pdraw = ImageDraw.Draw(probe)

    if orientation == 'vertical_rotated':
        if box_h is None:
            # Scrolling (T2B/B2T): raw width unconstrained (the travel axis has no
            # fixed extent); raw height (becomes rotated width) fit to box_w.
            font, tw, th = _fit_text_to_box(pdraw, text, font_name, None, box_w)
        else:
            # Static (Center, no clip applied): raw width (the string's own length,
            # which becomes the rotated block's VERTICAL extent) must fit box_h, and
            # raw height (becomes the rotated block's horizontal extent/thickness)
            # must fit box_w -- both axes constrained, same "stays inside the
            # bounding box" contract as horizontal/stacked. Leaving box_w
            # unconstrained let short strings (e.g. a single character) pick an
            # oversized font whose thickness blew past the box's width.
            font, tw, th = _fit_text_to_box(pdraw, text, font_name, box_h, box_w)
        if font is None:
            return None, 0, 0
        strip = Image.new('RGB', (max(1, tw), max(1, th)), (0, 0, 0))
        ImageDraw.Draw(strip).text((0, 0), text, fill=color_rgb, font=font)
        rotated = strip.rotate(-90, expand=True)
        return rotated, rotated.width, rotated.height

    elif orientation == 'vertical_stacked':
        chars = list(text)
        if not chars:
            return None, 0, 0
        lo, hi = 6, 300
        best_font, best_w, best_lh = None, 1, lo
        while lo <= hi:
            mid = (lo + hi) // 2
            font = _find_font(font_name, mid)
            if font is not None:
                max_w = max_h = 0
                for c in chars:
                    bbox = pdraw.textbbox((0, 0), c, font=font)
                    max_w = max(max_w, bbox[2] - bbox[0])
                    max_h = max(max_h, bbox[3] - bbox[1])
            else:
                max_w, max_h = mid, mid
            total_h = max_h * len(chars)
            if (box_w is None or max_w <= box_w) and (box_h is None or total_h <= box_h):
                best_font, best_w, best_lh = font, max_w, max_h
                lo = mid + 1
            else:
                hi = mid - 1
        if best_font is None:
            return None, 0, 0
        total_h = best_lh * len(chars)
        strip = Image.new('RGB', (max(1, best_w), max(1, total_h)), (0, 0, 0))
        sdraw = ImageDraw.Draw(strip)
        for idx, c in enumerate(chars):
            bbox = pdraw.textbbox((0, 0), c, font=best_font)
            cw = bbox[2] - bbox[0]
            cx = max(0, (best_w - cw) // 2)
            cy = idx * best_lh
            sdraw.text((cx - bbox[0], cy - bbox[1]), c, fill=color_rgb, font=best_font)
        return strip, strip.width, strip.height

    else:  # 'horizontal'
        font, tw, th = _fit_text_to_box(pdraw, text, font_name, box_w, box_h)
        if font is None:
            return None, 0, 0
        strip = Image.new('RGB', (max(1, tw), max(1, th)), (0, 0, 0))
        ImageDraw.Draw(strip).text((0, 0), text, fill=color_rgb, font=font)
        return strip, strip.width, strip.height


def _sudo_fix_shm_perms(model_name):
    """Make /dev/shm/FPP-Model-Data-<model_name> writable by the fpp user via the root
    helper (which gives the file to the fpp user/group, group-writable only - never world).
    The model name is validated here (no path separators) AND again inside the helper, so it
    can never touch a file outside /dev/shm - this is the guard against an earlier broad
    world-writable sudoers rule that a crafted model name could abuse for path traversal.
    Returns True on success."""
    if not model_name or '/' in model_name or '\x00' in model_name or '\n' in model_name:
        logging.error(f"Refusing shm permission fix for unsafe model name: {model_name!r}")
        return False
    try:
        import subprocess
        result = subprocess.run(
            ['sudo', '-n', SHM_PERMS_HELPER, model_name],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return True
        logging.error(f"shm perms helper failed ({result.returncode}): "
                      f"{result.stderr.decode(errors='replace').strip()}")
    except Exception as e:
        logging.error(f"shm perms helper error: {e}")
    return False


def render_to_shm(line_items, model_name, width, height):
    """Render multiple text lines to FPP shared memory, each with its own box, color, font,
    and orientation.
    line_items: list of (text, box_x, box_y, box_w, box_h, color_hex, font_name, orientation)
    tuples. orientation is 'horizontal' (default), 'vertical_rotated', or 'vertical_stacked'
 - see _render_oriented_text_strip. Font size is auto-fit to (box_w, box_h), then the
    rendered text is centered within the box. box_x/box_y == -1 auto-centers the box itself
    on the canvas (vertical stacking among lines is resolved by the caller before this
    point, so box_y is normally already concrete). -1 is an exact sentinel, not just any
    negative value: scrolling lines may legitimately have negative box_x/box_y to position
    the box off-page. Returns True on success, False on failure."""
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        img = Image.new('RGB', (width, height), (0, 0, 0))

        for (text, box_x, box_y, box_w, box_h, color_hex, font_name, orientation) in line_items:
            if not text:
                continue
            resolved_bx = max(0, (width - box_w) // 2) if box_x == -1 else box_x
            resolved_by = max(0, (height - box_h) // 2) if box_y == -1 else box_y
            strip, sw, sh = _render_oriented_text_strip(text, font_name, box_w, box_h,
                                                          _hex_to_rgb(color_hex), orientation)
            if strip is not None:
                draw_x = resolved_bx + max(0, (box_w - sw) // 2)
                draw_y = resolved_by + max(0, (box_h - sh) // 2)
                img.paste(strip, (draw_x, draw_y))

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        raw = img.tobytes()
        expected = width * height * 3
        if len(raw) != expected:
            logging.error(f"render_to_shm: size mismatch ({len(raw)} != {expected})")
            return False

        def _write():
            with open(shm_path, 'r+b') as f:
                f.write(raw)

        try:
            _write()
        except PermissionError:
            # FPP creates shm files owned by root. Use the root helper (installed by
            # fpp_install.sh, allowed via the narrow sudoers rule) to hand this one file to
            # the fpp user/group so the plugin can write it.
            logging.warning(f"render_to_shm: permission denied on {shm_path} - fixing via helper")
            if _sudo_fix_shm_perms(model_name):
                _write()
            else:
                logging.error("render_to_shm: shm permission fix failed - is the sudoers "
                              "rule installed? (re-run the plugin install)")
                return False

        logging.info(f"render_to_shm: wrote {len(raw)} bytes to {shm_path} ({len(line_items)} lines)")
        return True
    except Exception as e:
        logging.error(f"render_to_shm failed: {e}")
        return False


def _text_coverage_mask(strip):
    """Build an alpha/coverage mask from an RGB text strip that was drawn as a colored
    glyph on a BLACK background: per-pixel max of R,G,B. Glyph pixels → opaque, the black
    background → transparent, anti-aliased edges → partial. Pass this as the mask when
    pasting text onto an image so only the glyph lands (no black box); the black
    background is what FPP's State-3 transparency handles for sequences, but over an
    Opaque (State 2) image we must do that blending ourselves. Returns None on failure
    (caller then pastes opaquely, as before)."""
    try:
        from PIL import ImageChops
        r, g, b = strip.split()
        return ImageChops.lighter(ImageChops.lighter(r, g), b)
    except Exception:
        return None


def render_image_to_shm(image_path, model_name, width, height, line_items=None):
    """Load an image file, resize to model dimensions, optionally composite text on top,
    then write to FPP shared memory.  Returns True on success.
    line_items: optional list of (text, box_x, box_y, box_w, box_h, color_hex, font_name,
    orientation) to draw over the image (same box-fit behavior as render_to_shm). State 2
    (Opaque) should be used so the image fully covers the background."""
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        img = Image.open(image_path).convert('RGB')
        img = img.resize((width, height), Image.LANCZOS)

        if line_items:
            for (text, box_x, box_y, box_w, box_h, color_hex, font_name, orientation) in line_items:
                if not text:
                    continue
                resolved_bx = max(0, (width - box_w) // 2) if box_x == -1 else box_x
                resolved_by = max(0, (height - box_h) // 2) if box_y == -1 else box_y
                strip, sw, sh = _render_oriented_text_strip(text, font_name, box_w, box_h,
                                                              _hex_to_rgb(color_hex), orientation)
                if strip is not None:
                    draw_x = resolved_bx + max(0, (box_w - sw) // 2)
                    draw_y = resolved_by + max(0, (box_h - sh) // 2)
                    # Mask so only the glyph lands on the image (no black box behind text).
                    img.paste(strip, (draw_x, draw_y), _text_coverage_mask(strip))

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        raw = img.tobytes()
        expected = width * height * 3
        if len(raw) != expected:
            logging.error(f"render_image_to_shm: size mismatch ({len(raw)} != {expected})")
            return False

        def _write():
            with open(shm_path, 'r+b') as f:
                f.write(raw)

        try:
            _write()
        except PermissionError:
            if _sudo_fix_shm_perms(model_name):
                _write()
            else:
                logging.error(f"render_image_to_shm: shm permission fix failed")
                return False

        logging.info(f"render_image_to_shm: wrote {image_path} → {shm_path}")
        return True
    except Exception as e:
        logging.error(f"render_image_to_shm failed: {e}")
        return False


def _overlay_model_dims():
    """Resolve the overlay model's pixel size (width, height) - the resolution every image
    and text frame is force-scaled to. Prefer the values stored in config (written when the
    model is picked in the UI); if either is missing/0 (stale or never-saved config), fetch
    the model's real dimensions live from FPP and cache them back so image waiting content
    never silently fails to render. Returns (0, 0) only when the size is truly unknown."""
    mw = int(config.get('overlay_model_width', 0) or 0)
    mh = int(config.get('overlay_model_height', 0) or 0)
    if mw > 0 and mh > 0:
        return mw, mh
    model = config.get('overlay_model_name', '')
    if not model:
        return mw, mh
    try:
        for m in get_fpp_models():
            if m.get('name') == model and int(m.get('width', 0) or 0) > 0 and int(m.get('height', 0) or 0) > 0:
                mw, mh = int(m['width']), int(m['height'])
                config['overlay_model_width'] = mw
                config['overlay_model_height'] = mh
                try:
                    save_config()
                except Exception:
                    pass
                logging.info(f"📐 Overlay model dims resolved live from FPP: {model} = {mw}x{mh}")
                return mw, mh
    except Exception as e:
        logging.warning(f"Could not resolve overlay model dims live from FPP: {e}")
    return mw, mh


def _fseq_fps_for_content(content, default=30.0):
    """Resolve the frame rate to animate overlay text at, read from the FSEQ header of
    the background sequence. xLights writes the sequence's step time into the header, so
    this paces the scrolling text to the exact clock FPP plays/outputs that sequence at
    (no rate beating between the overlay and the sequence underneath it).
    `content` is a config content value; only 'seq:' values carry a real fps. Returns
    `default` for non-sequence content or on any read error."""
    try:
        if not content or not content.startswith('seq:'):
            return default
        name = os.path.basename(content[4:].removesuffix('.fseq'))  # filename only, no traversal
        filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
        if not os.path.exists(filepath):
            return default
        hdr = parse_fseq_header(filepath)
        fps = hdr.get('fps') if hdr else None
        return fps if (fps and fps > 0) else default
    except Exception as e:
        logging.warning(f"could not read fps from sequence '{content}': {e}")
        return default


def animate_lines_via_shm(items, model_name, width, height, duration, fps=None, bg_image_path=None):
    """Animate independently-moving/colored/fitted text lines together in FPP shared memory.
    Runs in a background thread for `duration` seconds then stops.

    items: [(text, box_x, box_y, box_w, box_h, color_hex, movement, speed, font_name,
        orientation), ...]
        movement 'Center': text is auto-fit to (box_w, box_h) and centered in the box, fixed.
        orientation ('horizontal'/'vertical_rotated'/'vertical_stacked') applies to Center
        and to T2B/B2T (all three -- for rotated the glyphs read sideways, for stacked
        each upright character is its own row, both still travelling vertically). L2R/R2L
        are always horizontal glyphs -- the point of that movement is horizontal travel.
        See _render_oriented_text_strip.
        movement 'L2R'/'R2L'/'T2B'/'B2T': text height is auto-fit to box_h (width
            unconstrained - the text is expected to be wider than the box and travels
            across it). The box also acts as a clipping viewport: text is only visible
            while passing through it, appearing to enter and exit at the box's own edges
            rather than the full canvas edges.
        box_x/box_y == -1 auto-centers the box itself on the canvas (vertical stacking
        among lines is resolved by the caller before this point, so box_y is normally
        concrete). -1 is an exact sentinel: a scrolling line's box may otherwise have a
        genuinely negative box_x/box_y, positioning it off-page so its text can enter/exit
        before/after the model's visible edge instead of only at the edge itself.
        speed: 0-100, independent per line.
    Returns True if the thread started, False on error."""
    global _scroll_thread
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        # Pace to the background sequence's own frame rate (from its FSEQ header) when the
        # caller supplies it; fall back to 30 for non-sequence backgrounds. This drives
        # both the step-per-frame motion math (via _step_for below) and the frame clock.
        fps = fps if (fps and fps > 0) else 30

        # Pre-render each line to its own image strip (fit to its box) and resolve its
        # fixed axis/motion + clip rect.
        probe = Image.new('RGB', (1, 1))
        pdraw = ImageDraw.Draw(probe)
        prepared = []
        for (text, box_x, box_y, box_w, box_h, color_hex, movement, speed, font_name,
             orientation) in items:
            if not text:
                continue
            resolved_bx = max(0, (width - box_w) // 2) if box_x == -1 else box_x
            resolved_by = max(0, (height - box_h) // 2) if box_y == -1 else box_y
            scrolling = movement in ('L2R', 'R2L', 'T2B', 'B2T')
            vertical_scroll_oriented = (scrolling and movement in ('T2B', 'B2T')
                                         and orientation in ('vertical_rotated', 'vertical_stacked'))
            if vertical_scroll_oriented:
                # T2B/B2T with rotated or stacked text: the block (sideways-reading for
                # rotated, one upright character per row for stacked) travels vertically
                # through the box. Its width must fit box_w (centered horizontally,
                # fixed); its height is unconstrained since it's the travel axis -- pass
                # box_h=None through to _render_oriented_text_strip.
                strip, tw, th = _render_oriented_text_strip(text, font_name, box_w, None,
                                                              _hex_to_rgb(color_hex), orientation)
                if strip is None:
                    strip, tw, th = Image.new('RGB', (1, 1), (0, 0, 0)), 1, 1
            elif scrolling:
                # Horizontal glyphs -- all other scrolling cases (L2R/R2L always, and
                # T2B/B2T when not rotated/stacked). Orientation otherwise only applies
                # to fixed (Center) lines, where the box's own edges are the whole
                # viewport rather than a window the text travels through.
                # The font is only constrained on the CROSS axis -- the travel axis is
                # unconstrained since the text scrolls through it (using the box's full
                # extent there rather than being capped by whichever dimension happens
                # to be smaller). L2R/R2L travel along X, so height (box_h) is the
                # constraint; T2B/B2T travel along Y, so width (box_w) is.
                horiz_scroll = movement in ('L2R', 'R2L')
                box_w_fit = None if horiz_scroll else box_w
                box_h_fit = box_h if horiz_scroll else None
                font, tw, th = _fit_text_to_box(pdraw, text, font_name, box_w_fit, box_h_fit)
                tw, th = max(1, tw), max(1, th)
                strip = Image.new('RGB', (tw, th), (0, 0, 0))
                if font is not None:
                    ImageDraw.Draw(strip).text((0, 0), text, fill=_hex_to_rgb(color_hex), font=font)
            else:
                strip, tw, th = _render_oriented_text_strip(text, font_name, box_w, box_h,
                                                              _hex_to_rgb(color_hex), orientation)
                if strip is None:
                    strip, tw, th = Image.new('RGB', (1, 1), (0, 0, 0)), 1, 1

            entry = {'strip': strip, 'tw': tw, 'th': th, 'movement': movement,
                     'clip': (resolved_bx, resolved_by, box_w, box_h)}
            # Over an image background, paste only the glyph (mask out the strip's black
            # box). Over a black background (no image) leave it None - pasting opaquely on
            # black is identical and cheaper.
            entry['mask'] = _text_coverage_mask(strip) if bg_image_path else None
            # speed == 0 is the "fit to display time" sentinel: instead of a fixed
            # px/s speed (which, with dynamic-length text, either loops a short name
            # several times or cuts a long name off mid-scroll), time one complete
            # pass to span the whole display duration -- the text enters at the start
            # and fully exits right as the display window ends, regardless of length.
            # speed <= 0 encodes fit-to-time: 0 or -1 = one pass, -N = N passes.
            # (Negative because it shares the one speed field with the positive manual
            # px/s speeds -- no separate config key needed.)
            fit_to_time = (speed <= 0)
            fit_passes = max(1, -int(speed)) if speed < 0 else 1
            entry['fit'] = fit_to_time
            entry['fit_passes'] = fit_passes
            entry['wraps'] = 0
            entry['done'] = False

            def _step_for(loop_start, loop_end):
                # Fit: cover fit_passes complete passes over the whole `duration`
                # (each pass = one loop_start->loop_end traversal), so the text makes
                # exactly that many passes and fully exits right as the window ends.
                # Otherwise: fixed px/s from the speed value.
                if fit_to_time:
                    total = abs(loop_end - loop_start) * fit_passes
                    return max(0.1, total / max(1.0, duration * fps))
                return max(1.0, max(10, speed * 2) / fps)

            if movement in ('L2R', 'R2L'):
                entry['dy'] = resolved_by + max(0, (box_h - th) // 2)
                entry['pos'] = float(resolved_bx + box_w) if movement == 'R2L' else float(resolved_bx - tw)
                entry['dir'] = -1.0 if movement == 'R2L' else 1.0
                entry['loop_start'] = float(resolved_bx + box_w) if movement == 'R2L' else float(resolved_bx - tw)
                entry['loop_end']   = float(resolved_bx - tw) if movement == 'R2L' else float(resolved_bx + box_w)
                entry['step_px'] = _step_for(entry['loop_start'], entry['loop_end'])
            elif movement in ('T2B', 'B2T'):
                entry['dx'] = resolved_bx + max(0, (box_w - tw) // 2)
                entry['pos'] = float(resolved_by + box_h) if movement == 'B2T' else float(resolved_by - th)
                entry['dir'] = -1.0 if movement == 'B2T' else 1.0
                entry['loop_start'] = float(resolved_by + box_h) if movement == 'B2T' else float(resolved_by - th)
                entry['loop_end']   = float(resolved_by - th) if movement == 'B2T' else float(resolved_by + box_h)
                entry['step_px'] = _step_for(entry['loop_start'], entry['loop_end'])
            else:  # Center - fixed, centered in box
                entry['dx'] = resolved_bx + max(0, (box_w - tw) // 2)
                entry['dy'] = resolved_by + max(0, (box_h - th) // 2)
            prepared.append(entry)

        if not prepared:
            return False

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        if os.path.exists(shm_path) and not os.access(shm_path, os.W_OK):
            _sudo_fix_shm_perms(model_name)

        logging.info(f"🎬 animate_lines_via_shm: model={model_name} size={width}x{height} "
                     f"lines={len(prepared)} duration={duration}s")

        # Base frame the moving text is composited onto each frame: the (resized) image
        # background if one was supplied, otherwise black. Loaded ONCE here so scrolling
        # text can now ride over an image (previously image + movement fell back to black).
        base_frame = None
        if bg_image_path:
            try:
                base_frame = Image.open(bg_image_path).convert('RGB').resize((width, height), Image.LANCZOS)
            except Exception as ex:
                logging.warning(f"animate bg image load failed ({bg_image_path}): {ex}")
                base_frame = None
        if base_frame is None:
            base_frame = Image.new('RGB', (width, height), (0, 0, 0))

        def _clip_paste(frame, strip, mask, src_x, src_y, dst_x, dst_y, vis_w, vis_h, clip):
            # Intersect the paste rect with the line's own box, so scrolling text is only
            # visible while inside it - entering/exiting at the box edges instead of the
            # full canvas edges. `mask` (or None) is cropped the same way so text lands on
            # an image background without its black box.
            cx, cy, cw, ch = clip
            x0, y0 = max(dst_x, cx), max(dst_y, cy)
            x1, y1 = min(dst_x + vis_w, cx + cw), min(dst_y + vis_h, cy + ch)
            if x1 <= x0 or y1 <= y0:
                return
            crop_x0, crop_y0 = src_x + (x0 - dst_x), src_y + (y0 - dst_y)
            crop_box = (crop_x0, crop_y0, crop_x0 + (x1 - x0), crop_y0 + (y1 - y0))
            crop_mask = mask.crop(crop_box) if mask is not None else None
            frame.paste(strip.crop(crop_box), (x0, y0), crop_mask)

        def _pos_at(e, t):
            # Closed-form scroll position at elapsed time `t` (seconds). Motion is a pure
            # function of wall-clock time, NOT an accumulator advanced per frame, so a slow
            # or dropped render frame never causes stutter or drift - the text is always
            # exactly where it belongs for time t. Velocity (px/sec) = per-frame step * fps.
            loop_len = abs(e['loop_end'] - e['loop_start']) or 1.0
            dist = e['step_px'] * fps * t            # total distance travelled by time t
            passes_done = int(dist // loop_len)
            within = dist - passes_done * loop_len   # distance into the current pass
            if e.get('fit') and passes_done >= e['fit_passes']:
                return e['loop_end']                 # fit-to-time: hold fully exited after N passes
            return e['loop_start'] + e['dir'] * within

        def _animate():
            import time as _time
            # Frames are RENDERED on an absolute-deadline clock (start + n/fps) so cadence
            # is steady, and each line's POSITION is computed from elapsed wall time via
            # _pos_at, so even when the Pi can't keep up and frames land late/dropped, the
            # motion stays correct and smooth instead of juddering. The shm handle is opened
            # ONCE and reused (seek(0)+write) instead of open/close per frame.
            try:
                shm = open(shm_path, 'r+b')
            except Exception as ex:
                logging.error(f"animate shm open failed: {ex}")
                return
            frame_bytes = width * height * 3
            start = _time.time()
            n = 0
            try:
                while True:
                    t = _time.time() - start
                    if t >= duration or _scroll_stop.is_set():
                        break
                    frame = base_frame.copy()
                    for e in prepared:
                        mv = e['movement']
                        if mv in ('L2R', 'R2L'):
                            ix = int(_pos_at(e, t))
                            src_x = max(0, -ix); dst_x = max(0, ix)
                            vis_w = min(e['tw'] - src_x, width - dst_x)
                            if vis_w > 0:
                                _clip_paste(frame, e['strip'], e['mask'], src_x, 0, dst_x, e['dy'], vis_w, e['th'], e['clip'])
                        elif mv in ('T2B', 'B2T'):
                            iy = int(_pos_at(e, t))
                            src_y = max(0, -iy); dst_y = max(0, iy)
                            vis_h = min(e['th'] - src_y, height - dst_y)
                            if vis_h > 0:
                                _clip_paste(frame, e['strip'], e['mask'], 0, src_y, e['dx'], dst_y, e['tw'], vis_h, e['clip'])
                        else:
                            frame.paste(e['strip'], (e['dx'], e['dy']), e['mask'])
                    try:
                        buf = frame.tobytes()
                        shm.seek(0)
                        # One write() syscall for the whole buffer keeps the frame FPP reads
                        # as close to atomic as userspace allows (minimizes tearing), and
                        # flush() pushes it out immediately rather than at close time.
                        shm.write(buf if len(buf) == frame_bytes else buf[:frame_bytes])
                        shm.flush()
                    except Exception:
                        pass
                    # Absolute-deadline pacing: sleep until the next frame's scheduled time
                    # instead of a fixed 1/fps after the work. No cumulative drift; a late
                    # frame is absorbed by a shorter next sleep.
                    n += 1
                    delay = (start + n / fps) - _time.time()
                    if delay > 0:
                        _time.sleep(delay)
            finally:
                try:
                    shm.close()
                except Exception:
                    pass

        _stop_scroll_thread()      # ensure no prior animation is still writing the buffer
        _scroll_stop.clear()       # re-arm for this run
        _scroll_thread = threading.Thread(target=_animate, daemon=True)
        _scroll_thread.start()
        return True
    except Exception as e:
        logging.error(f"animate_lines_via_shm failed: {e}")
        return False


def get_fpp_playlists():
    """Get list of playlists from FPP"""
    try:
        fpp_host = FPP_HOST
        playlists = []
        
        try:
            response = requests.get(f"{fpp_host}/api/playlists", timeout=3)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    playlists = list(data.keys())
                elif isinstance(data, list):
                    playlists = data
                logging.info(f"Found {len(playlists)} playlists: {playlists}")
        except Exception as e:
            logging.error(f"Could not fetch playlists: {e}")
        
        return sorted(playlists)
        
    except Exception as e:
        logging.error(f"Error fetching FPP playlists: {e}")
        return []

# ---------------------------------------------------------------------------
# FSEQ preview helpers
# ---------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# FSEQ preview (NOT video capture): these functions read a single frame of pixel
# data out of an FPP .fseq sequence file so the config page can show a static
# background still behind the text layout editor. There is no camera, capture
# device, or continuous video decoding - it is one on-demand read of bytes from a
# local file, decompressed (zstd/zlib) only for the one frame being previewed.
# ----------------------------------------------------------------------------
def parse_fseq_header(filepath):
    """Parse an FSEQ v2 file header. Returns a metadata dict or raises ValueError."""
    with open(filepath, 'rb') as f:
        raw = f.read(32)
    if len(raw) < 32 or raw[0:4] != b'PSEQ':
        raise ValueError("Not a valid FSEQ file (missing PSEQ magic)")
    major_ver = raw[7]
    if major_ver != 2:
        raise ValueError(f"Unsupported FSEQ version {raw[7]}.{raw[6]}")

    chan_data_offset  = struct.unpack_from('<H', raw, 4)[0]
    channel_count     = struct.unpack_from('<I', raw, 10)[0]
    frame_count       = struct.unpack_from('<I', raw, 14)[0]
    step_time_ms      = raw[18]
    compression_type  = raw[19] & 0x0F   # 0=none, 1=zlib, 2=zstd (per xLights: 1=zstd)
    # Offset 20 and 21 are separate uint8 fields - NOT a single uint16
    num_comp_blocks   = raw[20]           # uint8
    num_sparse_ranges = raw[21]           # uint8

    # ── Auto-detect compression ──────────────────────────────────────────────
    # FSEQ v2.2 (minor_version >= 2) sometimes writes compression_type=0 in
    # byte 19 even though the data is actually compressed.  Probe the real data
    # at chan_data_offset for a compression magic and override:
    #   • zstd frame magic 0x28 0xB5 0x2F 0xFD (0xFD2FB528 little-endian) → type 2
    #   • zlib header 0x78 + valid FLG (CMF*256+FLG divisible by 31)        → type 1
    # Without this, a compressed file read as "uncompressed" paints raw
    # compressed bytes to the display → TV-snow noise in the preview.
    _ZSTD_MAGIC = b'\x28\xB5\x2F\xFD'
    with open(filepath, 'rb') as _f:
        _f.seek(chan_data_offset)
        _probe = _f.read(4)
    effective_ctype = compression_type
    if compression_type == 0 and _probe[:4] == _ZSTD_MAGIC:
        effective_ctype = 2   # override: treat as zstd
        logging.info(
            "FSEQ: header says uncompressed (byte 19 = 0) but zstd magic detected "
            "at chan_data_offset - treating as zstd (FSEQ v2.2 quirk)"
        )
    elif (compression_type == 0 and len(_probe) >= 2 and _probe[0] == 0x78
          and ((_probe[0] << 8 | _probe[1]) % 31 == 0)):
        effective_ctype = 1   # override: treat as zlib
        logging.info(
            "FSEQ: header says uncompressed (byte 19 = 0) but zlib magic detected "
            "at chan_data_offset - treating as zlib (FSEQ v2.2 quirk)"
        )

    # ── Compression block table ───────────────────────────────────────────────
    # When compression is active (or auto-detected), scan from offset 32 for
    # valid (firstFrame uint32, dataLen uint32) block entries.  FSEQ v2.2 may
    # report num_comp_blocks incorrectly in byte 20; derive actual count by
    # scanning until firstFrame >= frameCount or dataLen == 0.
    comp_blocks = []
    if effective_ctype in (1, 2):
        with open(filepath, 'rb') as _f:
            _f.seek(32)
            _blk_raw = _f.read(chan_data_offset - 32)
        _off = 0
        while _off + 7 < len(_blk_raw):
            ff = struct.unpack_from('<I', _blk_raw, _off)[0]
            ds = struct.unpack_from('<I', _blk_raw, _off + 4)[0]
            if ff >= frame_count or ds == 0:
                break
            comp_blocks.append({'first_frame': ff, 'data_size': ds})
            _off += 8

    # ── Sparse range table ────────────────────────────────────────────────────
    # For standard v2.0 files: sparse ranges follow the comp block table at
    # offset 32 + num_comp_blocks*8, each entry 6 bytes (uint24 + uint24).
    # For auto-detected zstd (v2.2): the block table fills the entire header
    # space; sparse ranges are absent or in a variable-length metadata section
    # we don't parse here - discard to ensure direct channel offset mapping.
    sparse_ranges = []
    if effective_ctype == compression_type and num_sparse_ranges > 0:
        # Standard v2.0: sparse ranges at fixed position after comp block table
        sr_table_offset = 32 + num_comp_blocks * 8
        with open(filepath, 'rb') as f:
            f.seek(sr_table_offset)
            sr_raw = f.read(num_sparse_ranges * 6)
        for i in range(num_sparse_ranges):
            start = sr_raw[i*6] | (sr_raw[i*6+1] << 8) | (sr_raw[i*6+2] << 16)
            count = sr_raw[i*6+3] | (sr_raw[i*6+4] << 8) | (sr_raw[i*6+5] << 16)
            sparse_ranges.append({'start': start, 'count': count})

    fps = 1000.0 / step_time_ms if step_time_ms > 0 else 25.0
    return {
        'filepath':              filepath,
        'chan_data_offset':      chan_data_offset,
        'channel_count':         channel_count,
        'frame_count':           frame_count,
        'step_time_ms':          step_time_ms,
        'fps':                   fps,
        'duration_ms':           frame_count * step_time_ms,
        'compression_type':      effective_ctype,
        'raw_compression_type':  compression_type,
        'num_comp_blocks':       num_comp_blocks,
        'num_sparse_ranges':     num_sparse_ranges,
        'comp_blocks':           comp_blocks,
        'sparse_ranges':         sparse_ranges,
    }


def _sparse_ch_to_frame_byte(sparse_ranges, logical_ch):
    """Map a 0-indexed logical channel number to its byte offset within a packed frame.

    For dense FSEQs (no sparse ranges) the offset equals the logical channel number.
    For sparse FSEQs the frame data only contains channels listed in the sparse range
    table, packed together in range order.  Returns None if the channel falls in a gap.
    """
    if not sparse_ranges:
        return logical_ch   # Dense FSEQ - direct 1:1 mapping

    byte_offset = 0
    for sr in sparse_ranges:
        if logical_ch < sr['start']:
            return None     # Channel is in a gap between ranges
        if logical_ch < sr['start'] + sr['count']:
            return byte_offset + (logical_ch - sr['start'])
        byte_offset += sr['count']
    return None             # Channel is after all ranges


def read_fseq_frame(header, frame_idx, start_ch, ch_count):
    """Return raw channel bytes for one frame's model slice.

    Handles uncompressed (type 0), zlib (type 1), and zstd (type 2) FSEQs.
    Correctly resolves sparse-range FSEQs by mapping the logical start channel
    to its actual byte offset within each packed frame.
    """
    import zlib as _zlib
    filepath      = header['filepath']
    total_ch      = header['channel_count']
    ctype         = header['compression_type']
    sparse_ranges = header.get('sparse_ranges', [])

    # --- Resolve logical channel → byte offset within a frame ---
    frame_byte = _sparse_ch_to_frame_byte(sparse_ranges, start_ch)
    if frame_byte is None:
        # Channel not found in any sparse range.  Possible reasons:
        #   • The FSEQ is model-specific (channels start at 0 in the file).
        #   • The FPP start_channel is the show-level number but the FSEQ only
        #     contains this model's channels.
        # Try offset 0 as a fallback.
        if ch_count <= total_ch:
            frame_byte = 0
            logging.warning(
                f"FSEQ preview: start_ch {start_ch} not in sparse ranges - "
                f"falling back to frame byte 0 (model-specific FSEQ?)"
            )
        else:
            raise ValueError(
                f"Model channel count {ch_count} exceeds FSEQ channel count {total_ch}"
            )
    elif not sparse_ranges and frame_byte + ch_count > total_ch and ch_count <= total_ch:
        # Dense, model-specific / partial export: the file holds ONLY this
        # model's channels starting at file offset 0, so the show-level start
        # channel would overrun the file (e.g. FSEQ channel_count == model
        # channel_count, but start_ch > 0).  Read from the top instead.
        logging.warning(
            f"FSEQ preview: model range {start_ch}..{start_ch + ch_count} exceeds "
            f"file channel_count {total_ch} - treating as model-specific export "
            f"(frame byte 0)"
        )
        frame_byte = 0

    if ctype == 0:
        # Uncompressed: seek directly to frame + channel byte offset
        offset = header['chan_data_offset'] + frame_idx * total_ch + frame_byte
        with open(filepath, 'rb') as f:
            f.seek(offset)
            return f.read(ch_count)

    elif ctype in (1, 2):
        # zlib (1) or zstd (2) block compression - same block table layout
        if ctype == 2 and not ZSTD_AVAILABLE:
            raise ValueError(
                "FSEQ uses zstd compression - run fpp_install.sh to install the "
                "'zstandard' library, then restart the plugin."
            )

        blocks = header['comp_blocks']
        if not blocks:
            raise ValueError(f"{'zlib' if ctype==1 else 'zstd'} FSEQ has no compression block table")

        # Find the block containing frame_idx
        block_idx = len(blocks) - 1
        for i in range(len(blocks) - 1):
            if blocks[i + 1]['first_frame'] > frame_idx:
                block_idx = i
                break

        block = blocks[block_idx]

        # Byte offset of this block's compressed data in the file
        data_offset = header['chan_data_offset']
        for i in range(block_idx):
            data_offset += blocks[i]['data_size']

        with open(filepath, 'rb') as f:
            f.seek(data_offset)
            compressed = f.read(block['data_size'])

        if ctype == 1:
            decompressed = _zlib.decompress(compressed)
        else:
            dctx = _zstd_mod.ZstdDecompressor()
            try:
                decompressed = dctx.decompress(compressed)
            except Exception:
                # Fallback with an explicit size cap - allow up to 64 frames per
                # block, which is far more than any real FSEQ uses (typically 1-4).
                decompressed = dctx.decompress(
                    compressed, max_output_size=total_ch * 64
                )

        local_frame  = frame_idx - block['first_frame']
        frame_offset = local_frame * total_ch + frame_byte
        return decompressed[frame_offset: frame_offset + ch_count]

    else:
        raise ValueError(f"FSEQ compression type {ctype} is not supported")


def get_model_channel_info(model_name):
    """Return (start_channel_1indexed, channel_count) for a named model from FPP's /api/models.
    channel_count is 3*w*h for RGB, 4*w*h for RGBW, etc. Returns (None, None) on failure."""
    try:
        resp = requests.get(f"{FPP_HOST}/api/models", timeout=3)
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        models = data if isinstance(data, list) else data.get('models', [])
        for m in models:
            name = m.get('Name') or m.get('name') or ''
            if name.lower() == model_name.lower():
                sc = (m.get('StartChannel') or m.get('startChannel')
                      or m.get('start_channel'))
                cc = (m.get('ChannelCount') or m.get('channelCount')
                      or m.get('channel_count'))
                return (int(sc) if sc is not None else None,
                        int(cc) if cc is not None else None)
        return None, None
    except Exception as e:
        logging.warning(f"Could not get channel info for '{model_name}': {e}")
        return None, None

# Keep old name as alias so nothing else breaks
def get_model_start_channel(model_name):
    sc, _ = get_model_channel_info(model_name)
    return sc


def get_fpp_sequences():
    """Get list of sequences from FPP"""
    try:
        fpp_host = FPP_HOST
        response = requests.get(f"{fpp_host}/api/sequence", timeout=3)
        if response.status_code == 200:
            sequences = response.json()
            result = sequences if isinstance(sequences, list) else []
            logging.info(f"FPP sequences raw response: {sequences}")
            logging.info(f"Found {len(result)} sequences: {result}")
            return result
        logging.warning(f"FPP sequences API returned {response.status_code}: {response.text}")
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP sequences: {e}")
        return []

def get_fpp_videos():
    # Video/Play Media support disabled - only .fseq, images, and playlists accepted
    return []


def get_fpp_images():
    """Get list of image files from FPP media/images directory."""
    image_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
    try:
        if os.path.isdir(FPP_IMAGES_PATH):
            return sorted(
                f for f in os.listdir(FPP_IMAGES_PATH)
                if os.path.splitext(f.lower())[1] in image_exts
            )
    except Exception as e:
        logging.error(f"Error listing FPP images: {e}")
    return []


def get_fpp_models():
    """Get list of overlay models from FPP, including pixel dimensions when available.
    Tries /api/overlays/models first (has dimensions), falls back to /api/models."""
    def extract_model(m):
        if not isinstance(m, dict):
            return None
        name = m.get('Name') or m.get('name')
        if not name:
            return None
        # FPP overlay models use rows/cols; channel output models use Width/Height
        w = int(m.get('Width') or m.get('width') or m.get('Cols') or m.get('cols') or
                m.get('Columns') or m.get('columns') or 0)
        h = int(m.get('Height') or m.get('height') or m.get('Rows') or m.get('rows') or 0)
        return {"name": name, "width": w, "height": h}

    def parse_response(data):
        models = []
        if isinstance(data, dict) and 'models' in data:
            for m in data['models']:
                obj = extract_model(m)
                if obj: models.append(obj)
        elif isinstance(data, list):
            for m in data:
                obj = extract_model(m)
                if obj: models.append(obj)
        elif isinstance(data, dict):
            models = [{"name": k, "width": 0, "height": 0} for k in data.keys()]
        return models

    try:
        fpp_host = FPP_HOST
        # /api/overlays/models is the overlay-specific endpoint and includes dimensions
        for endpoint in ['/api/overlays/models', '/api/models']:
            try:
                response = requests.get(f"{fpp_host}{endpoint}", timeout=3)
                if response.status_code == 200:
                    models = parse_response(response.json())
                    if models:
                        has_dims = any(m['width'] > 0 or m['height'] > 0 for m in models)
                        logging.info(f"Got {len(models)} models from {endpoint} (dims: {has_dims})")
                        return models
            except Exception:
                pass

        logging.warning("Could not fetch models from FPP")
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP models: {e}")
        return []

_FONT_EXTENSIONS = ('.ttf', '.otf', '.pfb')

def _enumerate_fonts():
    """Enumerate installed fonts by walking the filesystem instead of calling
    FPP's /api/overlays/fonts. That endpoint is unreliable: FPP's font scanner
    (PixelOverlay.cpp findFonts()) checks for a dot in the entry name before
    checking whether it's a directory, so any font subdirectory without a dot
    in its name (e.g. fonts-freefont-ttf's freefont/) is skipped and the scan
    never recurses into it - the endpoint then returns null. os.walk has no
    such bug.

    Returns a list of {'name', 'category', 'path'} dicts. Bundled fonts under
    this plugin's fonts/<category>/ (e.g. fonts/christmas/) are tagged with
    that category name; everything else found in the OS font directories is
    tagged "System". Names are deduped - a bundled font also gets copied into
    /usr/local/share/fonts by fpp_install.sh (so FPP's own scanner and
    fc-match can find it), so without dedup it would show up twice.
    """
    fonts = []
    claimed_names = set()

    bundled_root = os.path.join(PLUGIN_DIR, 'fonts')
    if os.path.isdir(bundled_root):
        for category in sorted(os.listdir(bundled_root)):
            cat_dir = os.path.join(bundled_root, category)
            if not os.path.isdir(cat_dir):
                continue
            for fname in sorted(os.listdir(cat_dir)):
                if fname.lower().endswith(_FONT_EXTENSIONS):
                    name = os.path.splitext(fname)[0]
                    fonts.append({'name': name, 'category': category.capitalize(),
                                  'path': os.path.join(cat_dir, fname)})
                    claimed_names.add(name)

    search_dirs = [
        '/usr/share/fonts/truetype',
        '/usr/share/fonts/X11/Type1',
        '/usr/local/share/fonts',
        '/usr/share/fonts/opentype',
        '/usr/share/fpp/fonts',
        '/home/fpp/media/fonts',
    ]
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for dirpath, _dirs, filenames in os.walk(search_dir):
            for fname in filenames:
                if fname.lower().endswith(_FONT_EXTENSIONS):
                    name = os.path.splitext(fname)[0]
                    if name in claimed_names:
                        continue
                    claimed_names.add(name)
                    fonts.append({'name': name, 'category': 'System',
                                  'path': os.path.join(dirpath, fname)})

    fonts.sort(key=lambda f: (f['category'] != 'System', f['category'].lower(), f['name'].lower()))
    return fonts

def get_fpp_fonts():
    """Font list for the config UI: name + category, grouped for <optgroup>
    rendering. .ttf/.pfb names are derived the same way FPP does - filename
    minus a fixed 4-char extension - so they match what FPP's native overlay
    text API expects. .otf is also included (needed for some bundled fonts,
    e.g. Christmas Garland) even though FPP's own scanner doesn't recognize it
    (isTTF() checks .ttf only): PIL renders .otf fine, and PIL is the plugin's
    primary rendering path, so those entries just won't resolve through the
    rarely-used native-overlay-text fallback (PIL unavailable or overlay
    dimensions unset).
    """
    fonts = _enumerate_fonts()
    logging.info(f"Found {len(fonts)} fonts on disk")
    return [{'name': f['name'], 'category': f['category']} for f in fonts]

def test_fpp_connection():
    """Test connection to FPP"""
    try:
        fpp_host = FPP_HOST
        response = requests.get(f"{fpp_host}/api/fppd/status", timeout=3)
        if response.status_code == 200:
            status = response.json()
            return True, status.get('fppd', 'Unknown')
        return False, "Unable to connect"
    except Exception as e:
        return False, str(e)

# ============================================================================
# OPTIMIZED WHITELIST LOADING - WITH CACHING
# ============================================================================
def load_removed_names():
    """Load names the user has explicitly deleted (so git pull can't re-add them)"""
    if not os.path.exists(WHITELIST_REMOVED_FILE):
        return set()
    try:
        with open(WHITELIST_REMOVED_FILE, 'r', encoding='latin-1') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_whitelist():
    """Load and cache the whitelist: global + user-added - user-removed"""
    global _whitelist_cache, _whitelist_mtime

    try:
        mtime_global  = os.path.getmtime(WHITELIST_FILE)          if os.path.exists(WHITELIST_FILE)          else 0
        mtime_added   = os.path.getmtime(WHITELIST_ADDED_FILE)    if os.path.exists(WHITELIST_ADDED_FILE)    else 0
        mtime_removed = os.path.getmtime(WHITELIST_REMOVED_FILE)  if os.path.exists(WHITELIST_REMOVED_FILE)  else 0
        current_mtime = (mtime_global, mtime_added, mtime_removed)

        if _whitelist_cache is None or _whitelist_mtime != current_mtime:
            global_names = set()
            if os.path.exists(WHITELIST_FILE):
                with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                    global_names = {line.strip().lower() for line in f if line.strip() and not line.startswith('#')}

            removed = load_removed_names()
            added = load_whitelist_added()

            # If any user-added names are now in the global list, remove from added (global has priority)
            overlap = added & global_names
            if overlap:
                added -= overlap
                with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(sorted(added)) + '\n' if added else '')

            effective = (global_names - removed) | added
            _whitelist_cache = effective  # keep as set for O(1) lookup
            _whitelist_mtime = current_mtime
            logging.info(f"Loaded {len(_whitelist_cache)} names into whitelist cache")

        return _whitelist_cache

    except Exception as e:
        logging.error(f"Error reading whitelist: {e}")
        return []

# ============================================================================
# OPTIMIZED BLOCKLIST LOADING - WITH CACHING
# ============================================================================
def load_blocklist():
    """Load and cache blocked phone numbers, reload if file has changed"""
    global _blocklist_cache, _blocklist_mtime
    
    try:
        current_mtime = os.path.getmtime(BLOCKLIST_FILE)
        
        # Only reload if file changed or not yet loaded
        if _blocklist_cache is None or _blocklist_mtime != current_mtime:
            with open(BLOCKLIST_FILE, 'r') as f:
                blocked = json.load(f)
            
            _blocklist_cache = blocked if isinstance(blocked, list) else []
            _blocklist_mtime = current_mtime
            logging.info(f"Loaded {len(_blocklist_cache)} numbers into blocklist cache")
        
        return _blocklist_cache
        
    except FileNotFoundError:
        return []
    except Exception as e:
        logging.error(f"Error reading blocklist: {e}")
        return []

def save_blocklist(blocklist):
    """Save blocked phone numbers and invalidate cache"""
    global _blocklist_cache, _blocklist_mtime
    
    try:
        with open(BLOCKLIST_FILE, 'w') as f:
            json.dump(blocklist, f, indent=2)
        
        # Update cache immediately
        _blocklist_cache = blocklist
        _blocklist_mtime = os.path.getmtime(BLOCKLIST_FILE)
        
        logging.info(f"Blocklist saved: {len(blocklist)} numbers")
    except Exception as e:
        logging.error(f"Error saving blocklist: {e}")

def is_blocked(phone):
    """Check if phone number is blocked"""
    blocklist = load_blocklist()
    return phone in blocklist

def block_phone(phone):
    """Add phone number to blocklist"""
    blocklist = load_blocklist()
    if phone not in blocklist:
        blocklist.append(phone)
        save_blocklist(blocklist)
        logging.info(f"🚫 Blocked phone number: {phone}")
        return True
    return False

def unblock_phone(phone):
    """Remove phone number from blocklist"""
    blocklist = load_blocklist()
    if phone in blocklist:
        blocklist.remove(phone)
        save_blocklist(blocklist)
        logging.info(f"✅ Unblocked phone number: {phone}")
        return True
    return False

def is_on_whitelist(name):
    """Check if name is on the approved whitelist"""
    if not config.get('use_whitelist', False):
        return True
    
    whitelist = load_whitelist()
    if not whitelist:
        return True
    
    name_lower = name.lower().strip()
    return name_lower in whitelist

# ============================================================================
# ADMIN WHITELIST APPROVAL OVER SMS (Google Voice only)
# ----------------------------------------------------------------------------
# When the whitelist is on and a texter sends a name that is not on it, text the
# show admin so they can approve it live by replying Y/N. GV can only SEND by
# replying into an existing email thread, so the admin must text the Google Voice
# number once to seed a reply context; we store that (admin_reply_ctx.json) and
# the texter's own context in each pending record (pending_approvals.json) so we
# can reply to whoever we need to, asynchronously, after the admin answers.
# ============================================================================

def _normalize_phone(value):
    """Reduce a phone value to comparable digits so '+1 (555) 123-4567',
    '15551234567' and '5551234567' all compare equal: strip non-digits, then drop a
    leading US country code."""
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    return digits

def load_admin_ctx():
    """Load the stored admin reply context ({to, message_id, references, subject,
    phone}) or None. Read fresh each call - it is tiny and changes rarely."""
    try:
        if os.path.exists(ADMIN_CTX_FILE):
            with open(ADMIN_CTX_FILE, 'r') as f:
                ctx = json.load(f)
                return ctx if isinstance(ctx, dict) else None
    except Exception as e:
        logging.error(f"Error loading admin reply ctx: {e}")
    return None

def save_admin_ctx(ctx):
    try:
        with open(ADMIN_CTX_FILE, 'w') as f:
            json.dump(ctx, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving admin reply ctx: {e}")

def clear_admin_ctx():
    """Forget the seeded admin context (used when admin_phone changes so the
    bootstrap banner reappears until the new number texts in)."""
    try:
        if os.path.exists(ADMIN_CTX_FILE):
            os.remove(ADMIN_CTX_FILE)
    except Exception as e:
        logging.error(f"Error clearing admin reply ctx: {e}")

def admin_ctx_is_seeded():
    """True only when an admin reply context is stored AND it belongs to the number
    currently configured as admin_phone (digit-normalized). Drives the config-page
    bootstrap banner and the approval feature's can-we-text-the-admin check."""
    admin_phone = config.get('admin_phone', '').strip()
    if not admin_phone:
        return False
    ctx = load_admin_ctx()
    if not ctx or not ctx.get('to'):
        return False
    return _normalize_phone(ctx.get('phone', '')) == _normalize_phone(admin_phone)

_last_admin_seed_scan = 0.0
_last_admin_verify_scan = 0.0

def seed_admin_ctx_from_inbox(force=False):
    """Seed the admin reply context from the most recent Google Voice email ALREADY in
    the inbox that came from the configured admin number. This lets the feature turn on
    from existing texting history, instead of only from a brand-new text the poller
    happens to see after the number is set. Lightweight (headers only) and throttled so
    the status poll can call it cheaply. Returns True if a context is now seeded."""
    global _last_admin_seed_scan
    if config.get('message_source') != 'google_voice':
        return False
    admin_phone = config.get('admin_phone', '').strip()
    if not admin_phone:
        return False
    if admin_ctx_is_seeded():
        return True
    now = time.time()
    if not force and (now - _last_admin_seed_scan) < 20:
        return False
    _last_admin_seed_scan = now

    email_addr = config.get('gv_email', '').strip()
    app_pw = config.get('gv_app_password', '').strip()
    if not email_addr or not app_pw:
        return False

    want = _normalize_phone(admin_phone)
    imap = None
    try:
        # Explicit timeout: this can run from the config page's status request, so a
        # hung Gmail connection must not stall the web response.
        imap = imaplib.IMAP4_SSL(config.get('gv_imap_host', 'imap.gmail.com'), timeout=20)
        imap.login(email_addr, app_pw)
        imap.select(config.get('gv_imap_folder', 'INBOX'))
        typ, data = imap.uid('search', None, 'FROM', 'txt.voice.google.com')
        raw_uids = data[0].split() if (typ == 'OK' and data and data[0]) else []
        uids = sorted((int(u) for u in raw_uids), reverse=True)  # newest first
        # Only headers are needed to match the sender and build the reply context.
        # Scan newest-first and stop at the first match - the admin usually texts often
        # so the match is near the top; the cap just bounds a one-time setup scan (this
        # never runs once a context is seeded). Reads Gmail history, so age does not
        # matter as long as the email is still in the inbox.
        hdr_spec = '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID REFERENCES)])'
        for uid in uids[:500]:
            typ, msg_data = imap.uid('fetch', str(uid), hdr_spec)
            if typ != 'OK' or not msg_data or not msg_data[0]:
                continue
            hdr = email.message_from_bytes(msg_data[0][1])
            disp, addr = email.utils.parseaddr(str(hdr.get('From', '')))
            disp = _gv_decode_header(disp)
            fid = _gv_sender_id(hdr, disp)
            if _normalize_phone(fid) != want or not addr:
                continue
            ctx = {
                'to': addr,
                'message_id': str(hdr.get('Message-ID', '')).strip(),
                'references': str(hdr.get('References', '')).strip(),
                'subject': _gv_decode_header(str(hdr.get('Subject', ''))),
                'phone': want,
            }
            save_admin_ctx(ctx)
            logging.info("🔑 Admin reply context seeded from existing inbox history")
            return True
        logging.info("Admin ctx scan: no existing GV message from the admin number found")
        return False
    except Exception as e:
        logging.error(f"Admin ctx inbox scan failed: {e}")
        return False
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass

def verify_admin_ctx(force=False):
    """Confirm the stored admin reply context still points at a live email thread.

    The reply context only works while the Google Voice conversation email is still
    in the inbox - if the operator deletes (or archives out) that thread, Google
    Voice can no longer route our reply and approvals silently break. We re-check
    cheaply by searching the inbox DIRECTLY for the stored Message-ID (no full
    mailbox scan). If it is gone, we clear the context so the setup banner reverts
    and the operator knows to text the number again.

    Returns True if the context is still valid (or could not be checked this call),
    False only when we positively confirmed the thread no longer exists and cleared
    it. Transient failures (no network, Gmail hiccup) NEVER clear a good context."""
    global _last_admin_verify_scan
    if config.get('message_source') != 'google_voice':
        return False
    if not admin_ctx_is_seeded():
        return False
    now = time.time()
    if not force and (now - _last_admin_verify_scan) < 60:
        return True  # recently verified; assume still good
    _last_admin_verify_scan = now

    ctx = load_admin_ctx()
    msgid = (ctx or {}).get('message_id', '').strip()
    if not msgid:
        # No Message-ID to target (older/partial context). Can't verify directly;
        # leave it in place rather than risk clearing a working context.
        return True

    email_addr = config.get('gv_email', '').strip()
    app_pw = config.get('gv_app_password', '').strip()
    if not email_addr or not app_pw:
        return True  # can't check without credentials; don't clear

    imap = None
    try:
        imap = imaplib.IMAP4_SSL(config.get('gv_imap_host', 'imap.gmail.com'), timeout=20)
        imap.login(email_addr, app_pw)
        imap.select(config.get('gv_imap_folder', 'INBOX'))
        # Targeted header search for exactly the stored thread message.
        typ, data = imap.uid('search', None, 'HEADER', 'Message-ID', msgid)
        found = (typ == 'OK' and data and data[0] and len(data[0].split()) > 0)
        if found:
            return True
        # Positively absent from the inbox: the thread was deleted/archived.
        clear_admin_ctx()
        logging.warning("Admin reply thread no longer in inbox - cleared context; "
                        "live approvals paused until the admin texts the number again")
        return False
    except Exception as e:
        # Network/Gmail error - do NOT clear a context we simply couldn't reach.
        logging.error(f"Admin ctx verify failed (leaving context in place): {e}")
        return True
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass

def load_pending_approvals():
    try:
        if os.path.exists(PENDING_APPROVALS_FILE):
            with open(PENDING_APPROVALS_FILE, 'r') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        logging.error(f"Error loading pending approvals: {e}")
    return []

def save_pending_approvals(items):
    try:
        with open(PENDING_APPROVALS_FILE, 'w') as f:
            json.dump(items, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving pending approvals: {e}")

def load_expired_approvals():
    """Load the recently-expired requests (TTL/size pruned on read)."""
    try:
        if os.path.exists(EXPIRED_APPROVALS_FILE):
            with open(EXPIRED_APPROVALS_FILE, 'r') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    return []
                cutoff = time.time() - EXPIRED_APPROVALS_TTL_H * 3600
                data = [it for it in data if float(it.get('expired_ts', 0) or 0) >= cutoff]
                return data[-EXPIRED_APPROVALS_MAX:]
    except Exception as e:
        logging.error(f"Error loading expired approvals: {e}")
    return []

def save_expired_approvals(items):
    try:
        with open(EXPIRED_APPROVALS_FILE, 'w') as f:
            json.dump(items[-EXPIRED_APPROVALS_MAX:], f, indent=2)
    except Exception as e:
        logging.error(f"Error saving expired approvals: {e}")

def record_expired_approvals(expired):
    """Append timed-out requests to the expired store so a late 'Y' can still
    whitelist the name (without showing it)."""
    if not expired:
        return
    store = load_expired_approvals()
    now = time.time()
    for it in expired:
        rec = dict(it)
        rec['expired_ts'] = now
        store.append(rec)
    save_expired_approvals(store)

def prune_pending_approvals(items=None):
    """Drop pendings older than admin_approval_timeout_mins. On expiry, reply to the
    texter with the standard not-whitelisted message so they are not left waiting
    forever. Returns the surviving list (persisted only if something changed)."""
    if items is None:
        items = load_pending_approvals()
    timeout = int(config.get('admin_approval_timeout_mins', 5) or 0)
    if timeout <= 0:
        return items
    cutoff = time.time() - timeout * 60
    survivors, expired = [], []
    for it in items:
        if float(it.get('created_ts', 0) or 0) < cutoff:
            expired.append(it)
        else:
            survivors.append(it)
    for it in expired:
        logging.info(f"⌛ Pending approval expired: '{it.get('name')}' "
                     f"from {str(it.get('texter_phone', ''))[-4:]}")
        _send_feature_reply(it.get('texter_ctx'),
                            config.get('response_not_whitelisted', ''),
                            'not_whitelisted_timeout')
        log_message(it.get('texter_phone', ''), it.get('body', ''),
                    it.get('name', ''), "admin_timeout")
    if expired:
        # Keep them briefly so a late admin 'Y' can still whitelist the name.
        record_expired_approvals(expired)
        save_pending_approvals(survivors)
    return survivors

def add_name_to_whitelist(name):
    """Add a name to the user whitelist (the non-HTTP core of api_add_whitelist):
    lowercase, add to WHITELIST_ADDED_FILE, un-remove from WHITELIST_REMOVED_FILE,
    invalidate the cache. Returns True on success."""
    global _whitelist_cache, _whitelist_mtime
    name = (name or '').strip().lower()
    if not name:
        return False
    try:
        global_names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                global_names = {line.strip().lower() for line in f if line.strip()}
        removed = load_removed_names()
        if name not in global_names:
            added = load_whitelist_added()
            if name not in added:
                added.add(name)
                with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(sorted(added)) + '\n')
        if name in removed:
            removed.discard(name)
            with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n' if removed else '')
        _whitelist_cache = None
        _whitelist_mtime = None
        logging.info(f"Added '{name}' to whitelist via admin approval")
        return True
    except Exception as e:
        logging.error(f"Error adding '{name}' to whitelist: {e}")
        return False

def _send_feature_reply(ctx, text, message_type):
    """Send a Google Voice reply for the admin-approval feature using an explicit
    stored ctx (the texter's or the admin's). No-op if text or ctx is missing.
    GV-only feature, so this always routes through send_gv_reply()."""
    if not text or not ctx:
        return False
    return send_gv_reply(text, message_type, ctx=ctx)

def _parse_admin_decision(body):
    """Interpret an admin reply as an approve/deny decision. Accepts a bare
    Y/YES/N/NO (case-insensitive), optionally followed by a target name
    ('Y Grandma'). Returns (decision, target): decision is 'approve', 'deny', or
    None (not a decision); target is the name to match or '' for FIFO."""
    if not body:
        return None, ''
    parts = body.strip().split(None, 1)
    if not parts:
        return None, ''
    head = parts[0].strip().lower().rstrip('.!,')
    target = parts[1].strip() if len(parts) > 1 else ''
    if head in ('y', 'yes'):
        return 'approve', target
    if head in ('n', 'no'):
        return 'deny', target
    return None, ''

def _maybe_handle_admin_message(from_number, body):
    """If Google Voice is the source and `from_number` is the configured admin,
    refresh the stored admin reply context from the current message and, when the
    body is a bare Y/N, resolve the matching pending approval. Returns True only
    when a Y/N was consumed (caller stops processing); False otherwise so the
    admin's ordinary texts still flow through the normal pipeline."""
    if config.get('message_source') != 'google_voice':
        return False
    admin_phone = config.get('admin_phone', '').strip()
    if not admin_phone:
        return False
    if _normalize_phone(from_number) != _normalize_phone(admin_phone):
        return False

    # Seed/refresh the reply context for the admin's number from THIS message, so
    # the operator only has to text the GV number once and it self-refreshes after.
    if _gv_reply_ctx and _gv_reply_ctx.get('to'):
        ctx = dict(_gv_reply_ctx)
        ctx['phone'] = _normalize_phone(admin_phone)
        save_admin_ctx(ctx)
        logging.info("🔑 Admin reply context refreshed from inbound admin message")

    # The dedicated connect keyword: texting "admin" is the suggested way to seed the
    # reply context on first setup. It is consumed here (never shown as a name) - the
    # save above already captured the context we needed from it.
    if body.strip().lower() == ADMIN_CONNECT_KEYWORD:
        logging.info("🔗 Admin connect keyword received - reply context established")
        return True

    decision, target = _parse_admin_decision(body)
    if decision is None:
        return False  # admin texted a real name - let normal processing handle it

    # A name-targeted reply ('Y Grandma') resolves that request; otherwise FIFO
    # (oldest first). Shared by the active and the expired lists.
    def _pick(items):
        if not items:
            return None
        if target:
            for i, it in enumerate(items):
                if it.get('name', '').lower() == target.lower():
                    return items.pop(i)
        return items.pop(0)

    # Pull the admin's target out of the RAW pending list first - BEFORE pruning - so
    # a late Y/N on a request that has passed its timeout is handled here (silently),
    # not swept up by prune_pending_approvals() which would fire the Not-on-Whitelist
    # timeout reply at the texter.
    pending = load_pending_approvals()
    record = _pick(pending)

    if record is not None:
        save_pending_approvals(pending)
        name = record.get('name', '')
        texter_ctx = record.get('texter_ctx')
        texter_phone = record.get('texter_phone', '')
        # Is this request already past its approval timeout? A late decision NEVER
        # replies to the texter: a late Y whitelists for next time (no show), a late N
        # does nothing.
        timeout = int(config.get('admin_approval_timeout_mins', 5) or 0)
        is_late = timeout > 0 and (float(record.get('created_ts', 0) or 0) < time.time() - timeout * 60)
        if decision == 'approve':
            add_name_to_whitelist(name)
            if is_late:
                log_message(texter_phone, record.get('body', ''), name, "admin_approved_late")
                logging.info(f"✅ Admin approved '{name}' after timeout - whitelisted only, not shown")
            else:
                add_to_queue(name, texter_phone, record.get('body', name))
                log_message(texter_phone, record.get('body', ''), name, "admin_approved")
                _send_feature_reply(texter_ctx, config.get('response_success', ''), 'success')
                logging.info(f"✅ Admin approved '{name}' - added to whitelist and queued")
        else:  # deny
            if is_late:
                log_message(texter_phone, record.get('body', ''), name, "admin_denied_late")
                logging.info(f"🚫 Admin denied '{name}' after timeout - no action")
            else:
                log_message(texter_phone, record.get('body', ''), name, "admin_denied")
                # A denial just sends the standard Not-on-Whitelist response.
                _send_feature_reply(texter_ctx, config.get('response_not_whitelisted', ''), 'not_whitelisted')
                logging.info(f"🚫 Admin denied '{name}'")
        # Now sweep any OTHER genuinely-expired-and-unanswered requests (sends their
        # timeout reply); safe, since the one we just handled is already removed.
        prune_pending_approvals()
        return True

    # No active pending (all already pruned) - the request may be in the expired store.
    # A late Y whitelists for next time (no show); a late N does nothing. Either way,
    # the texter is never contacted here.
    expired = load_expired_approvals()
    exp_rec = _pick(expired)
    if exp_rec is not None:
        save_expired_approvals(expired)
        name = exp_rec.get('name', '')
        if decision == 'approve':
            add_name_to_whitelist(name)
            log_message(exp_rec.get('texter_phone', ''), exp_rec.get('body', ''), name, "admin_approved_late")
            logging.info(f"✅ Admin approved '{name}' after timeout - whitelisted only, not shown")
        else:  # late deny
            logging.info(f"🚫 Admin denied '{name}' after timeout - no action")
        return True

    logging.info("Admin Y/N received but no pending (or recently expired) approvals; ignoring")
    return True  # consume it so a bare 'Y'/'N' is never shown as a name

def _maybe_request_admin_approval(name, from_number, body):
    """When admin approval is active, create a pending request, text the admin the
    approval prompt, and tell the texter to wait. Returns True if the approval flow
    was started (caller skips the standard not-whitelisted reply), False to fall
    back to it. GV-only; requires a seeded admin reply context."""
    # The approval flow only runs while the show is live (Text My Lights Start). When
    # it is not, the texter gets the Show Not Live response instead - that is handled
    # by the enabled-check earlier in process_incoming_message, which returns before
    # this branch is ever reached; this guard encodes the same intent defensively.
    if not config.get('enabled', False):
        return False
    if config.get('message_source') != 'google_voice':
        return False
    admin_phone = config.get('admin_phone', '').strip()
    if not admin_phone:
        return False
    # Never prompt the admin for a name that would be rejected as profanity anyway.
    if config.get('profanity_filter') and contains_profanity(body):
        return False
    admin_ctx = load_admin_ctx()
    if not admin_ctx or not admin_ctx.get('to') or \
            _normalize_phone(admin_ctx.get('phone', '')) != _normalize_phone(admin_phone):
        logging.warning("Admin approval configured but admin has not texted the GV "
                        "number yet (no reply context); using not-whitelisted reply")
        return False

    pending = prune_pending_approvals()
    texter_ctx = dict(_gv_reply_ctx) if _gv_reply_ctx else None

    # Dedupe by (texter phone, name): a repeat just re-assures the texter; no re-prompt.
    for it in pending:
        if _normalize_phone(it.get('texter_phone', '')) == _normalize_phone(from_number) \
                and it.get('name', '').lower() == name.lower():
            logging.info(f"Duplicate pending approval for '{name}' from {from_number[-4:]}; not re-prompting")
            _send_feature_reply(texter_ctx, config.get('response_whitelist_pending', ''), 'whitelist_pending')
            log_message(from_number, body, name, "admin_pending")
            return True

    record = {
        "name": name,
        "texter_phone": from_number,
        "texter_ctx": texter_ctx,
        "body": body,
        "created_ts": time.time(),
    }
    pending.append(record)
    # Cap the list so a flood can never grow it without bound (oldest dropped).
    MAX_PENDING = 50
    if len(pending) > MAX_PENDING:
        pending = pending[-MAX_PENDING:]
    save_pending_approvals(pending)

    prompt = config.get('admin_approval_prompt', '').replace('{name}', name)
    _send_feature_reply(admin_ctx, prompt, 'admin_prompt')
    _send_feature_reply(texter_ctx, config.get('response_whitelist_pending', ''), 'whitelist_pending')
    log_message(from_number, body, name, "admin_pending")
    logging.info(f"📨 Requested admin approval for '{name}' from {from_number[-4:]}")
    return True

def send_sms_response(to_phone, message_type):
    """Send an SMS response to the user based on message type.

    Twilio: sends via the Twilio API. Google Voice: sends by replying to the
    forwarding email (Google Voice converts an email reply into an outbound SMS),
    using the reply context captured by the poller for the current message."""
    if not config.get(f'sms_response_{message_type}', False):
        return False

    # Honor the "greyed out" state at send time without ever having persisted
    # the toggle to off, so the stored on/off choice survives the row un-greying.
    if _response_is_muted(message_type):
        return False

    # Get the appropriate response message
    response_key = f"response_{message_type}"
    response_message = config.get(response_key, "")

    if not response_message:
        logging.warning(f"No response message configured for type: {message_type}")
        return False

    # {words} expands to the active word-limit phrase ("1 word" / "2 words"),
    # so the Invalid Format reply always matches the current Name Format Rule.
    response_message = response_message.replace('{words}', word_rule_phrase())

    return send_sms_text(to_phone, response_message, message_type)


def send_sms_text(to_phone, text, message_type="message"):
    """Send arbitrary SMS text to a recipient via the active message source.

    Lower-level than send_sms_response(): it does NOT consult the per-type
    enable toggles or response templates, so it's used for dynamically built
    messages such as the multi-name batch summary. Routing (Twilio REST vs
    Google Voice reply-to-email) matches send_sms_response()."""
    if not text:
        return False

    # Google Voice: reply-to-email path
    if config.get('message_source') == 'google_voice':
        return send_gv_reply(text, message_type)

    # Twilio: REST API path
    if not twilio_client:
        logging.warning("Cannot send SMS: Twilio client not initialized")
        return False

    try:
        twilio_client.messages.create(
            body=text,
            from_=config['twilio_phone_number'],
            to=to_phone
        )
        logging.info(f"📤 Sent SMS to {to_phone[-4:]}: {message_type}")
        return True
    except Exception as e:
        logging.error(f"Error sending SMS: {e}")
        return False


def _sanitize_header(value):
    """Strip CR/LF (and stray control chars) from values that come from an inbound
    email before they go into outbound reply headers, so a crafted message can't
    inject extra headers (LOW-2 - email header injection)."""
    if value is None:
        return ''
    return re.sub(r'[\r\n\x00]+', ' ', str(value)).strip()

def send_gv_reply(text, message_type="", ctx=None):
    """Send an outbound SMS via Google Voice by replying to the forwarding email.

    Replying to the notification email from the same Gmail account causes Google
    Voice to deliver the reply body as an SMS to the original sender. Uses the
    reply context (target address + threading headers) - an explicit `ctx` when
    given (e.g. a manual reply from the queue page, resolved from the message
    log), otherwise the one captured by the poller for the message currently
    being processed."""
    ctx = ctx if ctx is not None else _gv_reply_ctx
    if not ctx or not ctx.get('to'):
        logging.warning("GV reply: no reply context for current message; cannot respond")
        return False

    email_addr = config.get('gv_email', '').strip()
    app_pw = config.get('gv_app_password', '').strip()
    if not email_addr or not app_pw:
        logging.warning("GV reply: Gmail address / app password not configured")
        return False

    try:
        from email.mime.text import MIMEText
        reply = MIMEText(text, 'plain', 'utf-8')
        reply['From'] = email_addr
        reply['To'] = _sanitize_header(ctx['to'])
        subj = _sanitize_header(ctx.get('subject', '')) or "Re: text message"
        reply['Subject'] = subj if subj[:3].lower() == 're:' else ('Re: ' + subj)
        # Thread the reply to the original so Google Voice associates it with the
        # right conversation.
        if ctx.get('message_id'):
            reply['In-Reply-To'] = _sanitize_header(ctx['message_id'])
            refs = _sanitize_header((ctx.get('references', '') + ' ' + ctx['message_id']).strip())
            reply['References'] = refs

        host = config.get('gv_smtp_host', 'smtp.gmail.com')
        port = int(config.get('gv_smtp_port', 587))
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(email_addr, app_pw)
            s.sendmail(email_addr, [ctx['to']], reply.as_string())
        logging.info(f"📤 Sent Google Voice reply ({message_type}) to {ctx['to']}")
        return True
    except Exception as e:
        logging.error(f"Error sending Google Voice reply: {e}")
        return False

def extract_name(message):
    """Extract name from SMS message and convert to proper case"""
    message = message.strip()
    message = re.sub(r'^(hi|hello|hey|merry christmas|happy holidays)[,!.\s]*', '', message, flags=re.IGNORECASE)
    message = re.sub(r'[^a-zA-Z\s-]', '', message)
    message = message.strip()
    
    if message:
        message = message.title()

    # No length truncation here - an over-length name is REJECTED by
    # is_valid_name() (Too Long) rather than silently trimmed to fit, so the
    # sender is told to shorten it instead of a chopped name being displayed.
    return message if message else "Guest"

def is_non_name_message(body):
    """True if an inbound message is a phone 'tapback'/reaction or contains no
    letters at all (emoji / punctuation only).

    These are courtesy replies to the display notification, not name
    submissions, so callers should silently ignore them instead of firing an
    invalid_format (or any) auto-response.

    A message with zero ASCII letters can never yield a valid name anyway -
    extract_name() strips to [a-zA-Z\\s-], so it would collapse to "Guest" -
    which makes dropping it safe as well as correct."""
    text = (body or '').strip()
    if not text:
        return True

    # iOS / RCS tapback reactions delivered over SMS, e.g.:
    #   Loved "…"   Liked "…"   Disliked "…"   Laughed at "…"
    #   Emphasized "…"   Questioned "…"   Reacted 😂 to "…"
    if re.match(r'^(loved|liked|disliked|laughed at|emphasized|questioned|reacted\b.*?\bto)\s+["\'“‘”’]',
                text, flags=re.IGNORECASE):
        return True

    # No ASCII letters anywhere → emoji / symbols / punctuation only
    if not re.search(r'[a-zA-Z]', text):
        return True

    return False

def is_valid_name(text):
    """Validate a name. Returns (ok, reason) where reason is '' when ok, else a
    code the caller maps to a response:
 - 'too_long'   : exceeds Max Message Length
 - 'word_count' : violates the One Word / Two Words rule

    Length is checked FIRST, so an over-length name reports 'too_long' even when
    it also breaks the word rule. Length is enforced regardless of the word
    toggles (the caller only skips this whole check when the whitelist is on,
    where Max Message Length doesn't apply)."""
    text = ' '.join(text.split())
    words = text.split()
    word_count = len(words)

    max_len = config.get('max_message_length', 30)
    if len(text) > max_len:
        return False, "too_long"

    if config.get('one_word_only', False):
        if word_count != 1:
            return False, "word_count"
    elif config.get('two_words_max', True):
        if word_count > 2:
            return False, "word_count"

    return True, ""

def word_rule_phrase():
    """Human phrase for the current name word-limit ('1 word' / '2 words').

    Used to expand the {words} placeholder in the Invalid Format auto-response
    and in the UI help text, so the reply always matches the active Name Format
    Rule. Mirrors is_valid_name()'s precedence (One Word Only wins)."""
    if config.get('one_word_only', False):
        return "1 word"
    if config.get('two_words_max', True):
        return "2 words"
    return "1-2 words"

# ============================================================================
# OPTIMIZED PROFANITY FILTER - WITH CACHING AND PRE-COMPILED REGEX
# ============================================================================
def load_blacklist_removed():
    """Load words the user has explicitly removed from the profanity filter"""
    if not os.path.exists(BLACKLIST_REMOVED_FILE):
        return set()
    try:
        with open(BLACKLIST_REMOVED_FILE, 'r', encoding='latin-1') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_blacklist_added():
    """Load words the user has added beyond the global list"""
    if not os.path.exists(BLACKLIST_ADDED_FILE):
        return set()
    try:
        with open(BLACKLIST_ADDED_FILE, 'r', encoding='utf-8') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_whitelist_added():
    """Load names the user has added beyond the global list"""
    if not os.path.exists(WHITELIST_ADDED_FILE):
        return set()
    try:
        with open(WHITELIST_ADDED_FILE, 'r', encoding='utf-8') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_blacklist_words():
    """Return effective word list: global + user-added - user-removed"""
    try:
        global_words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                global_words = {line.strip().lower() for line in f if line.strip() and not line.startswith('#')}

        removed = load_blacklist_removed()
        added = load_blacklist_added()

        # If any user-added words are now in the global list, remove from added (global has priority)
        overlap = added & global_words
        if overlap:
            added -= overlap
            with open(BLACKLIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(added)) + '\n' if added else '')

        effective = (global_words - removed) | added
        return sorted(effective)
    except Exception as e:
        logging.error(f"Error reading blacklist words: {e}")
        return []

def load_blacklist():
    """Load and cache the profanity blacklist as a single combined regex for fast one-pass matching"""
    global _blacklist_cache, _blacklist_mtime

    try:
        mtime_global  = os.path.getmtime(BLACKLIST_FILE)         if os.path.exists(BLACKLIST_FILE)  else 0
        mtime_added   = os.path.getmtime(BLACKLIST_ADDED_FILE)   if os.path.exists(BLACKLIST_ADDED_FILE)   else 0
        mtime_removed = os.path.getmtime(BLACKLIST_REMOVED_FILE) if os.path.exists(BLACKLIST_REMOVED_FILE) else 0
        current_mtime = (mtime_global, mtime_added, mtime_removed)

        if _blacklist_cache is None or _blacklist_mtime != current_mtime:
            words = load_blacklist_words()
            if words:
                # Single combined pattern - one regex pass instead of N passes
                combined = '|'.join(r'\b' + re.escape(w) + r'\b' for w in words)
                _blacklist_cache = re.compile(combined)
            else:
                _blacklist_cache = None
            _blacklist_mtime = current_mtime
            logging.info(f"Loaded {len(words)} words into profanity filter cache (combined regex)")

        return _blacklist_cache

    except Exception as e:
        logging.error(f"Error reading blacklist: {e}")
        return None

def contains_profanity(text):
    """Check for profanity using a single combined regex pattern"""
    if not config['profanity_filter']:
        return False

    pattern = load_blacklist()
    if not pattern:
        return False

    text_lower = text.lower()

    if pattern.search(text_lower):
        logging.info(f"🚫 Profanity detected in '{text}'")
        return True

    return False

def count_profanity_words(text):
    """Number of blacklisted-word occurrences in text (0 if the filter is off or
    nothing matches). The combined blacklist regex has no capturing groups, so
    findall yields one entry per matched word."""
    if not config.get('profanity_filter', True):
        return 0
    pattern = load_blacklist()
    if not pattern:
        return 0
    return len(pattern.findall(text.lower()))

def _load_profanity_strikes():
    """Read the per-day profanity tally file, or {} if missing/unreadable."""
    try:
        with open(PROFANITY_STRIKES_FILE, 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logging.error(f"Error reading profanity strikes: {e}")
        return {}

def register_profanity_strike(phone, text):
    """Tally a sender's blacklisted-word count for TODAY and auto-add them to the
    phone blocklist once they reach profanity_threshold words in a single day.

    The tally carries a date stamp; the first strike on a new day wipes it, so a
    sender who never hits the threshold is effectively back to 0 at midnight. Once
    blocked, only the operator can release them (via the Phone Blocklist page).

    A threshold of 0 disables the feature entirely (nothing is tracked). Returns
    True only on the message that crosses the threshold and triggers the block."""
    try:
        threshold = int(config.get('profanity_threshold', 3) or 0)
    except (TypeError, ValueError):
        threshold = 0
    if threshold <= 0:
        return False

    words = count_profanity_words(text)
    if words <= 0:
        return False

    today = datetime.now().date().isoformat()
    data = _load_profanity_strikes()
    if data.get('date') != today:   # new day → reset the whole tally
        data = {'date': today, 'counts': {}}
    counts = data.setdefault('counts', {})
    counts[phone] = int(counts.get(phone, 0) or 0) + words

    try:
        with open(PROFANITY_STRIKES_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving profanity strikes: {e}")

    if counts[phone] >= threshold and not is_blocked(phone):
        block_phone(phone)
        return True
    return False

def _parse_log_date(log_entry):
    """Safely parse the date from a log entry's timestamp field."""
    try:
        return datetime.fromisoformat(log_entry.get('timestamp', '')).date()
    except (ValueError, TypeError):
        return None

def get_day_log_path(date=None):
    """Return the path to the daily message log file for the given date (default: today)."""
    if date is None:
        date = datetime.now().date()
    return os.path.join(MESSAGES_DIR, f"messages_{date.isoformat()}.json")

def mask_phone(p):
    """Redact a phone number to its last 4 digits for display/API responses -
    full numbers are kept only in the on-disk logs and never sent to the browser.
    Passes through the 'Local Testing' sentinel and empty values unchanged."""
    if not p or p == 'Local Testing':
        return p
    digits = re.sub(r'\D', '', str(p))
    return '***' + digits[-4:] if len(digits) >= 4 else '***'

def redact_messages(messages, log_date):
    """Return copies of message log entries with phone numbers masked to last-4,
    tagged with their log date so the UI can reference a message for blocking
    without ever holding the full number (see _phone_from_log_ref)."""
    out = []
    for m in messages:
        m = dict(m)
        masked = mask_phone(m.get('phone_full') or m.get('phone'))
        m['phone'] = masked
        m['phone_full'] = masked
        m['_log_date'] = log_date
        # Expose only whether a reply is possible; the stored reply context
        # (real reply-to address + threading headers) never leaves the server.
        m['can_respond'] = bool((m.get('reply_ctx') or {}).get('to'))
        m.pop('reply_ctx', None)
        out.append(m)
    return out

def _phone_from_log_ref(date_str, ts):
    """Resolve the full phone number of a stored message by (date, timestamp).
    Full numbers stay server-side; the UI only ever holds the masked value plus
    this reference, so blocking-from-history still works without exposing PII."""
    try:
        if date_str:
            path = get_day_log_path(datetime.strptime(date_str, "%Y-%m-%d").date())
        else:
            path = get_day_log_path()
        with open(path, 'r') as f:
            messages = json.load(f)
        for m in messages:
            if m.get('timestamp') == ts:
                return m.get('phone_full') or m.get('phone')
    except Exception as e:
        logging.error(f"Block-by-reference resolve failed: {e}")
    return None

def _reply_ctx_from_log_ref(date_str, ts):
    """Resolve the stored Google Voice reply context of a logged message by
    (date, timestamp). Mirrors _phone_from_log_ref: the reply-to address and
    threading headers stay server-side, so the browser only ever holds the
    (date, ts) reference - never the real address."""
    try:
        if date_str:
            path = get_day_log_path(datetime.strptime(date_str, "%Y-%m-%d").date())
        else:
            path = get_day_log_path()
        with open(path, 'r') as f:
            messages = json.load(f)
        for m in messages:
            if m.get('timestamp') == ts:
                return m.get('reply_ctx')
    except Exception as e:
        logging.error(f"Reply-context resolve failed: {e}")
    return None

def _client_error(context, exc, status=None):
    """Log the real exception server-side and return a generic message to the
    browser, so internal paths / exception detail never leak in a response
    (LOW-3). Preserves the original HTTP status when one is given."""
    logging.error(f"{context}: {exc}")
    body = jsonify({"success": False,
                    "error": "An internal error occurred. See the plugin log for details."})
    return (body, status) if status else body

def cleanup_old_logs():
    """Delete daily message log files older than 7 days from MESSAGES_DIR."""
    try:
        cutoff = datetime.now().date() - timedelta(days=7)
        for filename in os.listdir(MESSAGES_DIR):
            if not filename.startswith("messages_") or not filename.endswith(".json"):
                continue
            date_str = filename[len("messages_"):-len(".json")]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if file_date < cutoff:
                    os.remove(os.path.join(MESSAGES_DIR, filename))
                    logging.info(f"Deleted old log: {filename}")
            except ValueError:
                pass
    except Exception as e:
        logging.error(f"Error during cleanup_old_logs: {e}")

def save_queue():
    """Persist the current in-memory queue to QUEUE_FILE. Must be called OUTSIDE queue_lock."""
    try:
        snapshot = list(message_queue)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving queue: {e}")

def load_queue_from_file():
    """Restore queued items from QUEUE_FILE into the deque at startup. Returns count restored."""
    try:
        with open(QUEUE_FILE, 'r') as f:
            items = json.load(f)
        restored = 0
        for item in items:
            if item.get('status') == 'queued':
                message_queue.append(item)
                restored += 1
        if restored:
            logging.info(f"Queue restore: {restored} item(s) loaded from disk")
        return restored
    except FileNotFoundError:
        return 0
    except Exception as e:
        logging.error(f"Error restoring queue: {e}")
        return 0

def get_message_count(phone):
    """Get number of messages from a phone number today"""
    try:
        with open(get_day_log_path(), 'r') as f:
            logs = json.load(f)
        today = datetime.now().date()
        return sum(1 for log in logs
                   if log.get('phone_full') == phone
                   and _parse_log_date(log) == today
                   and log.get('counts_toward_limit', True))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    except Exception as e:
        logging.error(f"Error in get_message_count: {e}")
        return 0

def has_sent_name_today(phone, name):
    """Check if this phone has already sent this specific name today"""
    try:
        with open(get_day_log_path(), 'r') as f:
            logs = json.load(f)
        today = datetime.now().date()
        for log in logs:
            if (log.get('phone_full') == phone
                    and log.get('extracted_name', '').lower() == name.lower()
                    and log.get('status', '') in ('displayed', 'displaying', 'queued')
                    and _parse_log_date(log) == today):
                return True
        return False
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    except Exception as e:
        logging.error(f"Error in has_sent_name_today: {e}")
        return False

def save_last_sid(sid):
    """Save the last processed message SID to file"""
    try:
        with open(LAST_SID_FILE, 'w') as f:
            f.write(sid)
    except Exception as e:
        logging.error(f"Error saving last SID: {e}")

def save_last_gv_uid(uid):
    """Persist the last processed Google Voice IMAP UID for dedup across restarts"""
    try:
        with open(LAST_GV_UID_FILE, 'w') as f:
            f.write(str(uid))
    except Exception as e:
        logging.error(f"Error saving last GV UID: {e}")

def load_last_gv_uid():
    """Read the persisted Google Voice IMAP UID, or None if not set yet"""
    try:
        with open(LAST_GV_UID_FILE, 'r') as f:
            val = f.read().strip()
            return val or None
    except Exception:
        return None

def log_message(phone, message, name, status, counts=True):
    """Log received message to today's daily log file.

    counts=False marks this row as NOT counting toward the per-phone daily
    message limit (see get_message_count). Used for rejected/queued grouped
    (multi-name) texts, which never consume a sender's allowance."""
    try:
        log_path = get_day_log_path()
        try:
            with open(log_path, 'r') as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logs = []
        entry = {
            "timestamp": datetime.now().isoformat(),
            "phone": phone,
            "phone_full": phone,
            "message": message,
            "extracted_name": name,
            "status": status,
            "counts_toward_limit": counts
        }
        # Under Google Voice, stash how to reply to this exact message (target
        # address + threading headers) so the queue page's Respond button can
        # answer it later. Kept server-side only - redact_messages() strips it
        # before anything reaches the browser.
        if config.get('message_source') == 'google_voice' and _gv_reply_ctx and _gv_reply_ctx.get('to'):
            entry["reply_ctx"] = _gv_reply_ctx
        logs.append(entry)
        with open(log_path, 'w') as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        logging.error(f"Error logging message: {e}")

def update_message_status(phone, name, new_status):
    """Update the status of a message - searches today's file, then yesterday's if not found."""
    def _update_in_file(path):
        try:
            with open(path, 'r') as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        found = False
        for log in reversed(logs):
            if log.get('phone_full') == phone and log.get('extracted_name') == name:
                log['status'] = new_status
                log['status_updated'] = datetime.now().isoformat()
                found = True
                break
        if found:
            try:
                with open(path, 'w') as f:
                    json.dump(logs, f, indent=2)
            except Exception as e:
                logging.error(f"Error writing status update: {e}")
        return found

    try:
        today = datetime.now().date()
        if not _update_in_file(get_day_log_path(today)):
            _update_in_file(get_day_log_path(today - timedelta(days=1)))
    except Exception as e:
        logging.error(f"Error updating message status: {e}")

def add_to_queue(name, phone, message, override=None):
    """Add a message to the display queue. `override` (remote path) carries the master's
    pushed {content, duration} so the display uses that selection instead of choosing one."""
    global message_queue

    try:
        queue_item = {
            "name": name,
            "phone": phone,
            "phone_last4": phone[-4:],
            "message": message,
            "timestamp": datetime.now().isoformat(),
            "status": "queued"
        }
        if override is not None:
            queue_item["override"] = override
        
        logging.info(f"📋 Created queue item: {queue_item}")
        
        with queue_lock:
            message_queue.append(queue_item)
            queue_position = len(message_queue)

        save_queue()  # persist OUTSIDE queue_lock

        logging.info(f"📋 Added to queue (position {queue_position}): {name}")
        return True
    except Exception as e:
        logging.error(f"💥 ERROR in add_to_queue: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False

def _content_exists_locally(content):
    """True if the plugin content id (seq:/img:) exists on THIS instance. Empty content (no
    background) and non-seq/img values are treated as present. Used by the remote to decide
    whether it can honor a master-pushed content or must keep its current background."""
    if not content:
        return True
    if content.startswith('seq:'):
        nm = os.path.basename(content[4:].removesuffix('.fseq'))
        return os.path.exists(os.path.join(FSEQ_SEQUENCE_PATH, nm + '.fseq'))
    if content.startswith('img:'):
        nm = os.path.basename(content[4:])
        return os.path.exists(os.path.join(FPP_IMAGES_PATH, nm))
    return True


def send_to_fpp(name, override=None):
    """Send name to FPP - Start name sequence and display text overlay.

    override (remote path): {'content': <id>, 'duration': <int>} - the master's chosen
    content/timing. The remote renders it with ITS OWN layout (matched item, else flat) and
    falls back to no background switch when it lacks the pushed content. When override is
    None (master / standalone), content is selected locally exactly as before."""
    try:
        fpp_host = FPP_HOST
        # Pick which names content (with its OWN text layout + duration) to use for THIS
        # name. None => no list configured; fall back to the flat config (name over the
        # waiting content), exactly as before. The chosen content + duration are stashed in
        # module globals for display_worker() and the return/stop paths.
        global _active_name_content, _active_display_duration
        _forced_content = None
        if override is not None:
            # Remote: content/timing pushed by the master. Keep the local look: match the
            # pushed content to THIS instance's own names_content_list item for its layout.
            _forced_content = override.get('content', '') or ''
            if _forced_content and not _content_exists_locally(_forced_content):
                logging.info(f"ℹ️  Remote: pushed content '{_forced_content}' not present here "
                             f"- overlaying name on current background")
                _forced_content = ''
            _item = next((it for it in (config.get('names_content_list', []) or [])
                          if it.get('content', '') == _forced_content), None)
        else:
            _item = select_names_content_item()

        if _item is not None:
            name_playlist         = _item.get('content', '')
            message_lines         = _item.get('message_lines', ['', '', '', ''])
            line_boxes_cfg        = _item.get('line_boxes', [])
            line_colors_cfg       = _item.get('line_colors', [])
            line_movements_cfg    = _item.get('line_movements', [])
            line_speeds_cfg       = _item.get('line_speeds', [])
            line_fonts_cfg        = _item.get('line_fonts', [])
            line_orientations_cfg = _item.get('line_orientations', [])
            _active_display_duration = int(_item.get('display_duration', config.get('display_duration', 30)) or 30)
        else:
            name_playlist         = config.get('name_display_playlist', '')
            message_lines         = config.get('message_lines', ['Merry Christmas', '{name}!', '', ''])
            line_boxes_cfg        = config.get('line_boxes', [])
            line_colors_cfg       = config.get('line_colors', [])
            line_movements_cfg    = config.get('line_movements', [])
            line_speeds_cfg       = config.get('line_speeds', [])
            line_fonts_cfg        = config.get('line_fonts', [])
            line_orientations_cfg = config.get('line_orientations', [])
            _active_display_duration = int(config.get('display_duration', 30) or 30)
        if override is not None:
            # Background + timing are the master's selection; layout above stays local.
            name_playlist = _forced_content
            if override.get('duration'):
                _active_display_duration = int(override['duration'])
        _active_name_content = name_playlist
        # Master: mirror this exact name + chosen content + duration to the remotes so every
        # projector shows the same selection (incl. random/round-robin picks). Skipped when
        # override is set (this IS a remote applying a push) so remotes never re-push.
        if override is None and get_plugin_role() == 'master':
            push_state_to_remotes({'name': name,
                                   'content': _active_name_content,
                                   'duration': _active_display_duration})
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')

        global_text_color = config.get('text_color', '#FF0000')
        if not global_text_color.startswith('#'):
            global_text_color = '#' + global_text_color
        global_scroll_speed = config.get('scroll_speed', 5)
        global_font = config.get('text_font', 'FreeSans')
        default_box = {'x': -1, 'y': -1, 'w': 300, 'h': 60}

        # Collect non-empty rendered lines + their saved box/colors/movement/speed/font.
        # A line with no override for a given setting falls back to the matching global
        # default. Font size is not stored - it's auto-fit to the line's box at render time.
        rendered_lines = []  # [(rendered_text, box_x, box_y, box_w, box_h, color_hex, movement, speed, font_name, orientation), ...]
        for i, tmpl_line in enumerate(message_lines):
            if not tmpl_line.strip():
                continue
            rendered = tmpl_line.replace('{name}', name)
            box = line_boxes_cfg[i] if i < len(line_boxes_cfg) and line_boxes_cfg[i] else default_box
            line_color = line_colors_cfg[i] if i < len(line_colors_cfg) and line_colors_cfg[i] else global_text_color
            if not line_color.startswith('#'):
                line_color = '#' + line_color
            movement = line_movements_cfg[i] if i < len(line_movements_cfg) and line_movements_cfg[i] else 'Center'
            # NOTE: guard on `is not None`, not truthiness -- speed 0 (and negative values)
            # are the fit-to-display-time encoding: 0/-1 = one pass, -N = N passes. A plain
            # `and line_speeds_cfg[i]` would treat 0 as "unset" and fall back to the global
            # fixed speed, silently disabling fit mode on the real device.
            speed = line_speeds_cfg[i] if (i < len(line_speeds_cfg) and line_speeds_cfg[i] is not None) else global_scroll_speed
            font_name = line_fonts_cfg[i] if i < len(line_fonts_cfg) and line_fonts_cfg[i] else global_font
            orientation = line_orientations_cfg[i] if i < len(line_orientations_cfg) and line_orientations_cfg[i] else 'horizontal'
            rendered_lines.append((rendered, box.get('x', -1), box.get('y', -1), box.get('w', 300), box.get('h', 60),
                                    line_color, movement, speed, font_name, orientation))

        # Compute stacked Y defaults (group centered vertically). Each line's own box height
        # determines its own height in the stack.
        mh_pre = config.get('overlay_model_height', 0)
        line_heights = [item[4] for item in rendered_lines]
        total_stack_height = sum(line_heights)
        stack_start_y = max(0, (mh_pre - total_stack_height) // 2) if mh_pre > 0 else 0

        # Resolve each line's box Y (stacked default when unset). Box X stays -1
        # (auto-centered horizontally inside the render functions) unless explicitly positioned.
        all_items = []  # [(text, box_x, resolved_box_y, box_w, box_h, color_hex, movement, speed, font_name, orientation), ...]
        cumulative_y = stack_start_y
        for idx, (rendered, bx, by, bw, bh, lcolor, movement, speed, font_name, orientation) in enumerate(rendered_lines):
            resolved_y = cumulative_y if by == -1 else by
            all_items.append((rendered, bx, resolved_y, bw, bh, lcolor, movement, speed, font_name, orientation))
            cumulative_y += line_heights[idx]

        # True if at least one line scrolls - decides whether the fast one-shot static
        # render is enough, or the animated per-line renderer is needed.
        any_moving = any(item[6] != 'Center' for item in all_items)

        # FPP API fallback: join lines with newline
        display_message = '\n'.join(item[0] for item in rendered_lines) if rendered_lines else name

        logging.info(f"🎄 Displaying '{name}' (content={name_playlist or 'none'}, model={overlay_model or 'none'})")

        # Step 1: Start the name display playlist/sequence/video/image (background)
        if name_playlist:
            try:
                import urllib.parse
                # Experiment: DON'T stop the waiting effect. The waiting FSEQ effect
                # keeps looping and the names content starts as its own FSEQ effect
                # that plays ON TOP of it (overlay text renders above both). Nothing
                # foreground is touched. return_to_default_playlist() later stops just
                # the names effect, leaving the waiting effect running underneath.
                if name_playlist.startswith('seq:'):
                    # FSEQ Effect (loop=true, background=true): plays as background so
                    # overlay model renders on top with correct text colors.
                    seq_name = name_playlist[4:].removesuffix('.fseq')
                    effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
                    requests.get(effect_url, timeout=3)

                elif name_playlist.startswith('img:'):
                    # Image background - will be composited with text in Step 2 below
                    pass

                else:
                    command = "Start Playlist"
                    encoded_playlist = urllib.parse.quote(name_playlist)
                    command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_playlist}/true/false"
                    requests.get(command_url, timeout=3)

                time.sleep(0.3)

            except Exception as e:
                logging.error(f"💥 ERROR starting name playlist: {e}")
        else:
            # No names content configured - seq:/playlist waiting content composites
            # correctly underneath the overlay and is left running.
            pass

        # Step 2: Display text ON TOP of the sequence
        if overlay_model:
            try:
                text_position = config.get('text_position', 'Center')  # used only by the non-PIL fallback below
                text_color = global_text_color
                text_font = config.get('text_font', 'FreeSans')
                # The non-PIL fallback below has no concept of per-line boxes - approximate
                # a single FontSize from the first line's box height (box_h is index 4).
                font_size = all_items[0][4] if all_items else 48
                scroll_speed = config.get('scroll_speed', 20)

                import urllib.parse

                encoded_model = urllib.parse.quote(overlay_model)

                # Map config abbreviations to FPP API full strings
                position_map = {
                    'Center': 'Center',
                    'L2R': 'Left to Right',
                    'R2L': 'Right to Left',
                    'T2B': 'Top to Bottom',
                    'B2T': 'Bottom to Top',
                }
                fpp_position = position_map.get(text_position, 'Center')

                state_url = f"{fpp_host}/api/overlays/model/{encoded_model}/state"
                text_url  = f"{fpp_host}/api/overlays/model/{encoded_model}/text"

                mw, mh = _overlay_model_dims()

                shm_rendered = False
                scroll_started = False

                # For img: names content, composite text onto the image (State 2 = Opaque).
                # When no Names content is configured, fall back to an img: Default Waiting
                # content so the name displays over it instead of wiping it with plain text.
                # Both static AND scrolling text composite over the image background now
                # (static via render_image_to_shm, scrolling via animate_lines_via_shm's
                # bg_image_path).
                img_source = name_playlist if name_playlist else (_active_waiting_content or config.get('default_playlist', ''))
                img_bg_path = None
                if img_source.startswith('img:'):
                    img_bg_path = os.path.join(FPP_IMAGES_PATH, img_source[4:])
                    if not os.path.exists(img_bg_path):
                        logging.warning(f"⚠️ Image not found: {img_bg_path}")
                        img_bg_path = None

                # Blanking policy (avoids the transition flash):
                # - Incoming IMAGE (State 2, Opaque): do NOT blank. The current overlay
                #    (e.g. the previous image) stays on screen while we load/resize the new
                #    one - the slow part - and render_image_to_shm swaps it in with a single
                #    write(), so image→image changes have no blank frame at all.
                # - Incoming text/seq (State 3, Transparent): blank first, so the previous
                #    name can't linger while the new text frame is built. (Text path unchanged
                # - it already works cleanly.)
                if not img_bg_path:
                    requests.put(state_url, json={"State": 0}, timeout=3)

                if PIL_AVAILABLE and mw > 0 and mh > 0:
                    if not any_moving:
                        line_items = [(t, bx, by, bw, bh, c, fn, o) for (t, bx, by, bw, bh, c, _m, _s, fn, o) in all_items]
                        if img_bg_path:
                            shm_rendered = render_image_to_shm(
                                img_bg_path, overlay_model, mw, mh,
                                line_items=line_items
                            )
                        else:
                            shm_rendered = render_to_shm(
                                line_items, overlay_model, mw, mh
                            )
                    else:
                        # Per-content duration (fit-to-time scroll windows use it too).
                        duration = _active_display_duration or config.get('display_duration', 30)
                        # Pace the overlay animation to the background sequence's fps
                        # (from its FSEQ header) so scrolling motion is locked to the same
                        # clock FPP outputs the sequence at. The background is the names
                        # seq: if one is set, else the default waiting seq:.
                        bg_content = (name_playlist if (name_playlist and name_playlist.startswith('seq:'))
                                      else (_active_waiting_content or config.get('default_playlist', '')))
                        anim_fps = _fseq_fps_for_content(bg_content)
                        # Scrolling text now composites over the image background too
                        # (img_bg_path is None → black background, unchanged behavior).
                        scroll_started = animate_lines_via_shm(
                            all_items, overlay_model, mw, mh, duration,
                            fps=anim_fps, bg_image_path=img_bg_path
                        )
                        if scroll_started:
                            time.sleep(0.05)  # let first frame land before enabling overlay
                elif mw == 0 or mh == 0:
                    logging.warning(f"⚠️ PIL skipped: overlay dimensions not saved ({mw}x{mh}). "
                                    f"Re-select the model in config to save dimensions.")
                elif not PIL_AVAILABLE:
                    logging.warning("⚠️ Pillow not installed - using FPP text API (no X/Y positioning). "
                                    "Run plugin install to add Pillow.")

                if not shm_rendered and not scroll_started:
                    text_payload = {
                        "Message": display_message,
                        "Color": text_color,
                        "Font": text_font,
                        "FontSize": font_size,
                        "Position": fpp_position,
                        "PixelsPerSecond": scroll_speed * 20,
                        "AntiAlias": True,
                        "AutoEnable": False
                    }
                    requests.put(text_url, json=text_payload, timeout=10)

                # State 2 (Opaque) for image background so it covers the display fully -
                # for both the static composite (shm_rendered) and the scroll composite
                # (scroll_started) paths. State 3 (Transparent RGB) for normal/FSEQ
                # background (black = transparent).
                overlay_state = 2 if (img_bg_path and (shm_rendered or scroll_started)) else 3
                requests.put(state_url, json={"State": overlay_state}, timeout=3)

            except Exception as e:
                logging.error(f"💥 ERROR sending text command: {e}")
                import traceback
                logging.error(traceback.format_exc())

        return True
        
    except Exception as e:
        logging.error(f"💥 CRITICAL ERROR in send_to_fpp: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False
def _start_video_looping(_fpp_host, _vid_name):
    # Video/Play Media support disabled
    logging.info("⚠️  _start_video_looping called but video support is disabled")
    return False


def start_default_playlist(content=None):
    """Start a single waiting content item (playlist/sequence/image) and record it as the
    active base layer. `content` defaults to config['default_playlist']; the rotator and the
    image-restore path pass an explicit item. For sequences (seq:), uses FSEQ Effect
    (loop=true, background=true) so it loops seamlessly as a background effect."""
    global _active_waiting_content
    import urllib.parse
    fpp_host = FPP_HOST
    default_playlist = content if content is not None else config.get('default_playlist', '')

    if not default_playlist:
        logging.info("ℹ️  No default playlist configured - skipping auto-start")
        return False

    # This content becomes the base waiting layer (what names composite over / return to).
    _active_waiting_content = default_playlist
    logging.info(f"▶️  Starting waiting content: {default_playlist}")

    try:
        if default_playlist.startswith('seq:'):
            # FSEQ Effect Start uses the display name WITHOUT .fseq extension
            seq_name = default_playlist[4:]
            seq_name = seq_name.removesuffix('.fseq')

            # loop=true, background=true: loops natively, auto-suppressed by foreground
            # sequences, auto-resumes when foreground stops
            effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
            response = requests.get(effect_url, timeout=3)

            if response.status_code == 200:
                return True

            logging.error(f"❌ FSEQ Effect Start failed: {response.status_code} - {response.text}")
            return False

        elif default_playlist.startswith('img:'):
            # Static image - render to overlay model shared memory
            img_name = default_playlist[4:]
            img_path = os.path.join(FPP_IMAGES_PATH, img_name)
            overlay_model = config.get('overlay_model_name', '')
            mw, mh = _overlay_model_dims()   # falls back to a live FPP lookup if config dims are 0
            if PIL_AVAILABLE and overlay_model and mw > 0 and mh > 0 and os.path.exists(img_path):
                ok = render_image_to_shm(img_path, overlay_model, mw, mh)
                if ok:
                    encoded = urllib.parse.quote(overlay_model)
                    state_url = f"{fpp_host}/api/overlays/model/{encoded}/state"
                    requests.put(state_url, json={"State": 2}, timeout=3)  # Opaque
                    return True
            logging.warning(f"⚠️  Image background failed: PIL={PIL_AVAILABLE} model={overlay_model} "
                            f"dims={mw}x{mh} exists={os.path.exists(img_path) if img_path else False}")
            return False

        else:
            command = "Start Playlist"
            command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{urllib.parse.quote(default_playlist)}/true/true"
            response = requests.get(command_url, timeout=3)

            if response.status_code == 200:
                return True
            else:
                logging.error(f"❌ Failed to start playlist: {response.status_code}")
                return False
    except Exception as e:
        logging.error(f"Error starting default playlist: {e}")
        return False


def _switch_waiting_content(new_content, prev_content):
    """Seamlessly switch the base waiting layer from prev_content to new_content with NO
    black gap: the new content is started FIRST (a later-started FSEQ effect / an opaque
    image overlay renders on top), and only THEN is the previous content torn down. Updates
    _active_waiting_content. Used by the rotator; safe to call with prev_content == '' for
    the first item."""
    global _active_waiting_content
    import urllib.parse
    fpp_host = FPP_HOST
    if not new_content:
        return False

    # 1) Bring up the NEW content on top of whatever is currently showing.
    ok = start_default_playlist(new_content)   # sets _active_waiting_content = new_content

    # 2) Tear down the PREVIOUS content now that the new one covers it. Never touch it when
    #    it's the same file (a repeat) - that would stop what we just started.
    try:
        if prev_content and prev_content != new_content:
            if prev_content.startswith('seq:'):
                seq_name = prev_content[4:].removesuffix('.fseq')
                requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}", timeout=3)
                logging.info(f"⏹️  Rotator stopped previous waiting seq: {seq_name}")
            elif prev_content.startswith('img:') and not new_content.startswith('img:'):
                # Previous was an opaque image overlay and the new content is a seq/playlist
                # underneath - turn the overlay off so the new content shows through.
                overlay_model = config.get('overlay_model_name', '')
                if overlay_model:
                    enc = urllib.parse.quote(overlay_model)
                    requests.put(f"{fpp_host}/api/overlays/model/{enc}/state", json={"State": 0}, timeout=3)
                    logging.info("🧹 Rotator cleared previous waiting image overlay")
            # img: → img: needs nothing (new render already overwrote the overlay buffer).
    except Exception as e:
        logging.warning(f"Rotator teardown of previous waiting content failed: {e}")
    _push_waiting_state()   # master: mirror this waiting selection to the remotes
    return ok


def _rotator_should_idle():
    """The rotator only rotates when NOT paused, the show is enabled, and 2+ waiting items
    are configured. Otherwise it idles (single-content / disabled / stopped). Remotes never
    self-rotate - their waiting content is chosen by the master and applied via a push."""
    if is_remote():
        return True
    if stop_rotator or not config.get('enabled', False):
        return True
    return len((config.get('default_content_list', []) or [])) < 2


def _rotator_busy():
    """True while a name owns the display - the rotator holds the current waiting content
    (looping underneath) rather than switching, and resumes once the queue drains."""
    with queue_lock:
        return (currently_displaying is not None) or (len(message_queue) > 0)


def _waiting_hold_seconds(content):
    """How long to keep `content` on screen before rotating: the full sequence length for
    seq: (from the FSEQ header), else the matching list item's display_duration, else 30."""
    d = _fseq_duration_seconds(content)
    if d:
        return d
    for it in (config.get('default_content_list', []) or []):
        if it.get('content') == content:
            return int(it.get('display_duration', 30) or 30)
    return 30


def waiting_rotator():
    """Long-lived daemon that rotates the waiting-content list while the show is idle.

    Rotates only when 2+ items are configured and the show is enabled/not paused. Model:
    HOLD the currently-active item for its full length, THEN advance to the next - so the
    first item (brought up by start_waiting_content) plays fully before rotation begins.
    Rotation is suspended while a name owns the display; the current waiting content keeps
    looping underneath and the name composites on top."""
    logging.info("🔁 Waiting rotator thread started")
    while True:
        try:
            if _rotator_should_idle() or _rotator_busy():
                time.sleep(0.5)
                continue

            # 1) Make sure SOMETHING valid is up (first tick, or the active item was removed
            #    from the list). start_waiting_content normally brings up item 0 already.
            with rotator_lock:
                if _rotator_should_idle():
                    continue
                contents = [it.get('content', '') for it in (config.get('default_content_list', []) or [])]
                if not _active_waiting_content or _active_waiting_content not in contents:
                    item = select_default_content_item()
                    if item and item.get('content'):
                        _switch_waiting_content(item['content'], _active_waiting_content)
                cur = _active_waiting_content

            # 2) Hold the current item for its full length, waking early to stop/pause, when
            #    disabled, or when a name arrives. Fire the overlap-switch a hair BEFORE the
            #    natural end so the next content covers the tail instead of the current
            #    (loop=true) sequence briefly restarting from frame 0 - a seamless handoff.
            hold = _waiting_hold_seconds(cur)
            logging.info(f"🔁 Rotator showing waiting content '{cur}' for {hold}s")
            deadline = time.time() + max(0.5, hold - 0.25)
            while time.time() < deadline and not _rotator_should_idle() and not _rotator_busy():
                time.sleep(min(0.5, max(0.05, deadline - time.time())))

            # 3) Advance to the next item - but never switch under a name or after a stop
            #    (re-checked under the lock). If busy, loop back and re-hold the current item.
            if _rotator_should_idle() or _rotator_busy():
                continue
            with rotator_lock:
                if _rotator_should_idle() or _rotator_busy():
                    continue
                nxt = select_default_content_item()
                if nxt and nxt.get('content') and nxt['content'] != _active_waiting_content:
                    _switch_waiting_content(nxt['content'], _active_waiting_content)
        except Exception as e:
            logging.error(f"Error in waiting_rotator: {e}")
            time.sleep(1.0)


def start_waiting_content():
    """Start the waiting background layer. With a rotation list of 2+ items, bring up the
    first item and un-pause the (always-running) rotator; otherwise start the single
    default_playlist / the sole list item exactly as before. Returns True if something was
    started."""
    global stop_rotator
    lst = config.get('default_content_list', []) or []

    if len(lst) >= 2:
        stop_rotator = False   # un-pause the rotator
        # Bring up the first item immediately so there's no gap before the rotator's first
        # tick; ongoing rotation is handled by the thread.
        with rotator_lock:
            first = select_default_content_item()
            if first and first.get('content'):
                return _switch_waiting_content(first['content'], _active_waiting_content)
        return False

    # 0-1 items: single-content behavior. A 1-item list uses that item; else default_playlist.
    if len(lst) == 1 and lst[0].get('content'):
        ok = start_default_playlist(lst[0]['content'])
    else:
        ok = start_default_playlist()
    _push_waiting_state()   # master: mirror the single waiting content to the remotes
    return ok


def return_to_default_playlist():
    """Clear text overlay and stop the names sequence/playlist.
    If the default is a seq: (FSEQ Effect background), the background auto-resumes.
    If the default is a playlist, restart it explicitly."""
    try:
        fpp_host = FPP_HOST
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')

        import urllib.parse

        # Don't blank between queued names - the next name's display handles its own
        # (flash-free) overlay transition. Blanking here would flash between names.
        with queue_lock:
            queue_length = len(message_queue)
        if queue_length > 0:
            logging.info(f"📋 Queue has {queue_length} more names - skipping return-to-default")
            return

        # Stop whatever names content was ACTUALLY shown for this name (round-robin/random
        # picks per name), not the static config key. Falls back to the flat key.
        name_playlist   = _active_name_content if _active_name_content is not None else config.get('name_display_playlist', '')
        # The base waiting layer to return to is whatever is CURRENTLY active (the rotator
        # holds this steady during a name display), falling back to the single default.
        default_content = _active_waiting_content or config.get('default_playlist', '')
        returning_to_image = default_content.startswith('img:')

        # Kill the name's scroll animation and wait for it to fully exit BEFORE we write
        # the waiting content. Otherwise a last in-flight animation frame can land after
        # the waiting image and briefly 'reload' the name.
        _stop_scroll_thread()

        def _clear_overlay():
            # Turn the text/image overlay OFF (State 0). Used only when we are NOT
            # returning to an image - a seq:/none waiting background shows through once
            # the overlay is off. (An image waiting background is instead restored by
            # start_default_playlist, which overwrites the overlay buffer and sets State 2
            # in one step, so there is no blank frame.)
            if not overlay_model:
                return
            try:
                enc = urllib.parse.quote(overlay_model)
                requests.put(f"{fpp_host}/api/overlays/model/{enc}/state", json={"State": 0}, timeout=3)
            except Exception as e:
                logging.warning(f"Could not clear overlay: {e}")

        if not name_playlist:
            # No names content configured. Nothing on the output to stop (seq:/playlist
            # waiting was never stopped); just restore the overlay for the waiting content.
            if returning_to_image:
                start_default_playlist(default_content)   # image + State 2, overwrites overlay, no blank
            else:
                _clear_overlay()
            return

        # 1) Stop the NAME content by type. Never a blanket Stop Now - the main
        #    scheduler and any coexisting foreground must keep running.
        if name_playlist.startswith('seq:'):
            seq_name = name_playlist[4:].removesuffix('.fseq')
            # CRITICAL: don't stop the name seq if it's the SAME sequence as the active
            # waiting background. This is the common case on remotes (and any setup that
            # reuses one background for both names and waiting): the master pushes a name
            # whose content id equals the waiting content, so the single looping FSEQ is
            # serving as both layers. Stopping it here would tear down the waiting
            # background, leaving the output black until the master's ~10s heartbeat
            # re-pushes it. Just leave it looping and clear the overlay below.
            waiting_seq = default_content[4:].removesuffix('.fseq') if default_content.startswith('seq:') else None
            if seq_name == waiting_seq:
                logging.info(f"⏭️  Name seq '{seq_name}' is also the waiting background - leaving it "
                             f"running, only clearing the overlay")
            else:
                # Stop the names FSEQ Effect - waiting FSEQ (if any) keeps running underneath
                requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}", timeout=3)
        elif name_playlist.startswith('img:'):
            # Image name used the overlay only - nothing on the output to stop.
            pass
        else:
            # Names content is a foreground playlist - stop just that playlist
            # (not a blanket Stop Now), so any coexisting foreground isn't killed.
            requests.get(f"{fpp_host}/api/playlists/stop", timeout=3)

        # 2) Restore WAITING content on the overlay.
        # - img: waiting → re-render it and set State 2 Opaque. The name display
        #      overwrote the overlay buffer (text frames or a name image), so this must
        #      run for every name type. start_default_playlist writes the image and flips
        #      to State 2 in one step, so an image→image return has NO blank frame.
        # - seq:/none waiting → just turn the overlay off; the seq (still looping
        #      underneath) or the bare output shows through. No blank either (the
        #      background was there the whole time).
        if returning_to_image:
            start_default_playlist(default_content)
        else:
            _clear_overlay()

    except Exception as e:
        logging.error(f"Error in return_to_default_playlist: {e}")


def stop_show_playback():
    """Stop ONLY the plugin's own content - its background FSEQ effect(s) and its
    text/image overlay. The plugin always runs as a BACKGROUND layer, so this
    deliberately never issues 'Stop Now' or a blanket playlist stop: any OTHER
    foreground sequence on the Pi (e.g. a static house display running the pixels)
    keeps playing. This is the 'lights off' action shared by Stop and the end of a
    graceful drain - it does NOT touch config['enabled'] (the caller owns that).

    (If the plugin's own waiting content is itself a foreground playlist/video -
    not a background effect - we stop that one playlist, since in that case it IS
    the plugin's own foreground.)"""
    global stop_rotator, _active_waiting_content
    # Pause the waiting rotator and hold its lock across teardown so it can't start a new
    # item mid-stop (it re-checks the pause flag under this same lock before switching).
    stop_rotator = True
    try:
        with rotator_lock:
            _stop_show_playback_locked()
    except Exception as e:
        logging.warning(f"Could not stop FPP playback: {e}")


def _stop_show_playback_locked():
    """Teardown body of stop_show_playback, run while holding rotator_lock."""
    global _active_waiting_content
    try:
        import urllib.parse

        # Stop any running name scroll animation first so it can't rewrite the overlay
        # buffer after we clear it below.
        _stop_scroll_thread()

        # Stop the plugin's own background FSEQ effects: the single waiting content, every
        # seq: item in the WAITING rotation list, the flat name content, and every seq: item
        # in the names list (any could be the one currently looping). FSEQ Effect Stop on a
        # non-running seq is harmless. ALSO stop whatever is ACTUALLY running right now
        # (_active_waiting_content / _active_name_content) - on a REMOTE these are pushed by
        # the master and are NOT in this instance's own config lists, so without them the
        # remote's waiting seq would keep looping after a Stop.
        _wait_seq = [it.get('content', '') for it in (config.get('default_content_list', []) or [])]
        _names_seq = [it.get('content', '') for it in (config.get('names_content_list', []) or [])]
        for content in [config.get('default_playlist', ''), config.get('name_display_playlist', ''),
                        _active_waiting_content or '', _active_name_content or '',
                        *_wait_seq, *_names_seq]:
            if content.startswith('seq:'):
                seq_name = content[4:].removesuffix('.fseq')
                r = requests.get(f"{FPP_HOST}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}", timeout=3)
                logging.info(f"🛑 FSEQ Effect Stop ({seq_name}): {r.status_code}")

        # Clear the plugin's text/image overlay so nothing is left on the model.
        overlay_model = config.get('overlay_model_name', '')
        if overlay_model:
            encoded = urllib.parse.quote(overlay_model)
            requests.put(f"{FPP_HOST}/api/overlays/model/{encoded}/state", json={"State": 0}, timeout=3)
            logging.info("🛑 Overlay cleared")

        # Only if the plugin's OWN waiting content is a foreground playlist/video
        # (not a background seq:/img:) do we stop the foreground - that playlist is
        # the plugin's own. Never for seq:/img:, so a coexisting show is untouched.
        default = config.get('default_playlist', '')
        if default and not default.startswith(('seq:', 'img:')):
            r = requests.get(f"{FPP_HOST}/api/playlists/stop", timeout=3)
            logging.info(f"🛑 Stopped plugin foreground playlist: {r.status_code}")

        # Nothing is on the output now - clear the active-waiting marker so a later restart
        # brings its first item up as a clean switch rather than a same-file no-op.
        _active_waiting_content = ''
    except Exception as e:
        logging.warning(f"Could not stop FPP playback: {e}")


def display_worker():
    """Background worker that displays messages from the queue"""
    global currently_displaying, message_queue, stop_display
    
    logging.info("🎬 Display worker thread started")
    
    while not stop_display:
        try:
            with queue_lock:
                if len(message_queue) == 0:
                    currently_displaying = None
                    _next_item = None
                else:
                    _next_item = message_queue.popleft()

            if _next_item is None:
                time.sleep(0.1)
                continue

            save_queue()  # item popped - remove from persistent queue before display starts

            currently_displaying = _next_item
            
            name = currently_displaying['name']
            phone = currently_displaying['phone']
            
            logging.info(f"🎬 NOW DISPLAYING: {name} (from {phone[-4:]})")

            try:
                update_message_status(phone, name, "displaying")
            except Exception as e:
                logging.error(f"💥 Error updating status to displaying: {e}")

            try:
                # On a remote, the item carries the master's pushed {content, duration}.
                send_to_fpp(name, override=currently_displaying.get('override'))
            except Exception as e:
                logging.error(f"💥 Error sending to FPP: {e}")

            # Per-content duration chosen by send_to_fpp for the item actually shown.
            display_duration = int(_active_display_duration or config.get('display_duration', 30))

            try:
                name_playlist_chk = _active_name_content or ''
                overlay_model_chk = config.get('overlay_model_name', '')
                if not name_playlist_chk and overlay_model_chk:
                    # No names content - FPP can reset the overlay state while the waiting
                    # content is active (e.g. playlist steps, effect transitions).
                    # Re-enable State 3 every 2 s to keep the text on screen for the full duration.
                    import urllib.parse as _ul
                    _surl = f"{FPP_HOST}/api/overlays/model/{_ul.quote(overlay_model_chk)}/state"
                    _end = time.time() + display_duration
                    while time.time() < _end and not stop_display:
                        try:
                            requests.put(_surl, json={"State": 3}, timeout=2)
                        except Exception:
                            pass
                        _rem = _end - time.time()
                        if _rem > 0:
                            time.sleep(min(2.0, _rem))
                else:
                    time.sleep(display_duration)
            except Exception as e:
                logging.error(f"💥 Error during display: {e}")
            
            # Graceful stop: if the show was stopped while names were still displaying/queued,
            # keep showing each one, but once this was the LAST queued name, stop the waiting
            # content instead of resuming it. A REMOTE is driven by the master, not its own
            # `enabled` flag (it's usually never Started locally) - it only stops when the
            # master has broadcast Stop; otherwise it always returns to its waiting content.
            try:
                if is_remote():
                    stopping = _remote_stop_requested
                else:
                    stopping = not config.get('enabled', False)
                with queue_lock:
                    more_queued = len(message_queue) > 0
                if stopping and not more_queued:
                    logging.info("🛑 Graceful stop: last name shown - stopping waiting content")
                    stop_show_playback()
                else:
                    return_to_default_playlist()
            except Exception as e:
                logging.error(f"💥 Error returning to default / stopping: {e}")
            
            logging.info(f"✅ FINISHED DISPLAYING: {name}")
            
            try:
                update_message_status(phone, name, "displayed")
            except Exception as e:
                logging.error(f"💥 Error updating status to displayed: {e}")
            
            currently_displaying = None
            
        except Exception as e:
            logging.error(f"💥 Error in display worker: {e}")
            import traceback
            logging.error(traceback.format_exc())
            currently_displaying = None
            time.sleep(1)
    
    logging.info("🛑 Display worker stopped")

def get_queue_status():
    """Get current queue status for display on web page"""
    try:
        if queue_lock.acquire(timeout=2):
            try:
                queue_list = list(message_queue)
                current = currently_displaying
            finally:
                queue_lock.release()
        else:
            queue_list = []
            current = None
    except Exception as e:
        logging.error(f"Error getting queue status: {e}")
        queue_list = []
        current = None
    
    status = {
        "currently_displaying": current,
        "queue": queue_list,
        "queue_length": len(queue_list),
        "show_live": config.get('enabled', False)
    }
    
    return status

def parse_name_list(body):
    """If `body` is a genuine multi-name list - names separated by commas or
    line breaks, with 2+ that pass name validation - return the list of
    extracted names. Otherwise return None, signalling the caller to handle the
    message as a single submission (unchanged behavior).

    Only commas and line breaks separate names - NEVER spaces - so multi-word
    names like "Jean Luke" and hyphenated names like "Jean-Luke" each stay a
    single name."""
    tokens = [t.strip() for t in re.split(r'[\n\r,]+', body)]
    tokens = [t for t in tokens if t and re.search(r'[a-zA-Z]', t)]
    if len(tokens) < 2:
        return None

    max_names = config.get('max_names_per_text', 25)
    names = []
    valid_count = 0
    for t in tokens[:max_names]:
        nm = extract_name(t)
        if nm == "Guest":          # greeting-only / no usable letters - drop noise
            continue
        names.append(nm)
        if is_valid_name(nm)[0]:
            valid_count += 1

    # Require at least two real names before treating it as a list, so an
    # ordinary sentence that happens to contain a comma isn't chopped up.
    if valid_count < 2:
        return None
    return names

def process_incoming_message(from_number, body):
    """Run one inbound message through the full pipeline: show-live check →
    blocked → name extraction → rate limit → duplicate → whitelist → profanity
    → queue, sending the appropriate auto-response along the way.

    Source-agnostic - used by both poll_twilio() and poll_google_voice(). Only
    needs the sender identity and message text; per-source dedup bookkeeping
    (SID / IMAP UID) stays in the caller. Behavior is identical to the logic
    that previously lived inline in poll_twilio()."""
    # Admin whitelist-approval interception (Google Voice only). Runs first so the
    # admin's reply context is refreshed on every inbound from them and a bare Y/N
    # approval is never mistaken for a name submission. Any other admin message
    # returns False here and falls through to the normal pipeline below.
    if _maybe_handle_admin_message(from_number, body):
        return

    # "admin" is the reserved connect keyword (how the admin phone seeds its reply
    # context). It must never reach the display as a name - drop it silently for any
    # sender. The admin's own "admin" text is already consumed above; this covers a
    # non-admin (or not-yet-configured) sender texting the same word.
    if body.strip().lower() == ADMIN_CONNECT_KEYWORD:
        logging.info(f"🙈 Ignored reserved 'admin' keyword from {from_number[-4:]}")
        return

    # Phone 'tapback' reactions and emoji-only replies to the display
    # notification are courtesy responses, not name submissions - silently
    # drop them so we never fire an invalid_format (or any) auto-response.
    # Returning normally lets the caller advance its dedup marker.
    if is_non_name_message(body):
        logging.info(f"🙈 Ignored reaction/emoji-only reply from {from_number[-4:]}: '{body[:30]}'")
        return

    if not config.get('enabled', False):
        # Show not live - reply if enabled, then discard
        if not is_blocked(from_number):
            send_sms_response(from_number, "show_not_live")
            log_message(from_number, body, "", "show_not_live")
            logging.info(f"🔴 Show not live reply sent to {from_number[-4:]}")
        return

    # Exactly one branch fires - only one SMS response is ever sent per message
    if is_blocked(from_number):
        logging.info(f"🚫 Blocked: {from_number[-4:]}")
        log_message(from_number, body, "", "blocked")
        send_sms_response(from_number, "blocked")

    elif parse_name_list(body) is not None:
        # ── Grouped / multi-name text (commas or line breaks, 2+ valid names) ──
        # To keep the sender experience simple, a grouped text is all-or-nothing
        # and is ONLY accepted when the box is fully "open": no rate limiting,
        # duplicates allowed, and every name valid / whitelisted. Any restriction
        # rejects the whole text with the Invalid Format reply, and that
        # rejection does NOT count against the sender's daily message allowance.
        names     = parse_name_list(body)
        max_msgs  = config.get('max_messages_per_phone', 0)
        allow_dup = config.get('allow_duplicate_names', False)
        use_wl    = config.get('use_whitelist', False)

        # Mirror the single-name rule: validate format when the whitelist is off,
        # validate against the whitelist when it's on (never both).
        bad_format = (not use_wl) and any(not is_valid_name(nm)[0] for nm in names)
        not_wl     = use_wl and any(not is_on_whitelist(nm) for nm in names)

        if max_msgs > 0 or not allow_dup or bad_format or not_wl:
            logging.info(f"❌ Grouped text not allowed here → invalid: {from_number[-4:]} "
                         f"(rate_limit={max_msgs>0}, dup_off={not allow_dup}, "
                         f"bad_format={bad_format}, not_whitelisted={not_wl})")
            log_message(from_number, body, "", "invalid_format", counts=False)
            send_sms_response(from_number, "invalid_format")

        elif config['profanity_filter'] and contains_profanity(body):
            # Any profanity anywhere fails the whole message.
            logging.info(f"❌ Grouped text profanity rejected: {from_number[-4:]}")
            log_message(from_number, body, "", "profanity", counts=False)
            send_sms_response(from_number, "profanity")
            if register_profanity_strike(from_number, body):
                logging.info(f"🚫 Profanity threshold reached - auto-blocked {from_number[-4:]}")

        else:
            # Fully open + clean → queue every name, one Success reply.
            queued = []
            for nm in names:
                if add_to_queue(nm, from_number, body):
                    logging.info(f"✅ Queued: {nm}")
                    log_message(from_number, body, nm, "queued", counts=False)
                    queued.append(nm)
                else:
                    logging.warning(f"❌ Queue error: {nm}")
                    log_message(from_number, body, nm, "error", counts=False)
            if queued:
                logging.info(f"✅ Grouped text queued {len(queued)} name(s): {from_number[-4:]}")
                send_sms_response(from_number, "success")

    else:
        # ── Single-name text (original behavior) ──
        name = extract_name(body)
        logging.debug(f"👤 Extracted name: '{name}'")
        max_msgs = config.get('max_messages_per_phone', 0)
        msg_count = get_message_count(from_number) if max_msgs > 0 else 0
        is_valid, reason = is_valid_name(name)

        if max_msgs > 0 and msg_count >= max_msgs:
            logging.info(f"⛔ Rate limited: {from_number[-4:]}")
            log_message(from_number, body, "", "rate_limited")
            send_sms_response(from_number, "rate_limited")

        elif not config.get('allow_duplicate_names', False) and has_sent_name_today(from_number, name):
            logging.info(f"🔄 Duplicate name: {name}")
            log_message(from_number, body, name, "duplicate_name_today")
            send_sms_response(from_number, "duplicate")

        elif not is_valid and not config.get('use_whitelist', False):
            # Too Long (over Max Message Length) gets its own response; any other
            # format failure (word count) gets the Invalid Format response.
            resp = "too_long" if reason == "too_long" else "invalid_format"
            logging.info(f"❌ {resp}: '{body[:30]}'")
            log_message(from_number, body, name, resp)
            send_sms_response(from_number, resp)

        elif not is_on_whitelist(name):
            # If admin approval is active (GV + admin_phone + seeded), ask the admin
            # to approve instead of rejecting outright; the texter gets the pending
            # reply and later Success (Y) or admin-denied (N). Otherwise unchanged.
            if not _maybe_request_admin_approval(name, from_number, body):
                logging.info(f"❌ Not on whitelist: {name}")
                log_message(from_number, body, name, "not_on_whitelist")
                send_sms_response(from_number, "not_whitelisted")

        elif config['profanity_filter'] and contains_profanity(body):
            logging.info(f"❌ Profanity rejected")
            log_message(from_number, body, name, "profanity")
            send_sms_response(from_number, "profanity")
            if register_profanity_strike(from_number, body):
                logging.info(f"🚫 Profanity threshold reached - auto-blocked {from_number[-4:]}")

        else:
            if add_to_queue(name, from_number, body):
                logging.info(f"✅ Queued: {name}")
                log_message(from_number, body, name, "queued")
                send_sms_response(from_number, "success")
            else:
                logging.warning(f"❌ Queue error: {name}")
                log_message(from_number, body, name, "error")


def poll_twilio():
    """Poll Twilio for new messages"""
    global last_message_sid, stop_polling

    logging.info("🚀 Twilio polling started")
    first_run = last_message_sid is None
    thread_start_time = datetime.now(timezone.utc)  # used to skip pre-start messages on first run
    _current_day = datetime.now().date()
    my_gen = polling_generation  # exit if a source switch retires this poller

    while not stop_polling and my_gen == polling_generation:
        try:
            # Midnight cleanup - delete daily log files older than 7 days
            today = datetime.now().date()
            if today != _current_day:
                _current_day = today
                try:
                    cleanup_old_logs()
                    logging.info(f"🌙 Midnight: old daily logs cleaned up for {today}")
                except Exception as e:
                    logging.error(f"Error during midnight cleanup: {e}")

            if not twilio_client:
                time.sleep(config.get('poll_interval', 2))
                continue

            logging.debug("📡 Polling Twilio for new messages...")

            messages = twilio_client.messages.list(
                to=config['twilio_phone_number'],
                date_sent_after=datetime.utcnow() - timedelta(minutes=10),
                limit=20
            )

            logging.debug(f"📨 Found {len(messages)} total messages in last 10 minutes")

            new_messages = []
            for msg in messages:
                if last_message_sid and msg.sid == last_message_sid:
                    logging.debug(f"✓ Reached last processed message SID: {last_message_sid[:10]}...")
                    break
                new_messages.append(msg)

            logging.debug(f"🆕 Found {len(new_messages)} NEW messages to process")

            if first_run:
                # Anchor to the newest SID so future polls don't re-process old messages
                if messages:
                    last_message_sid = messages[0].sid
                    save_last_sid(messages[0].sid)
                # Filter new_messages to only those that arrived after this thread started
                # so we don't replay messages that predate the polling session
                new_messages = [
                    m for m in new_messages
                    if m.date_sent and m.date_sent >= thread_start_time
                ]
                logging.info(
                    f"⚙️ First run: baseline SID set, {len(new_messages)} post-start message(s) to process"
                )
                first_run = False
                if not new_messages:
                    time.sleep(config['poll_interval'])
                    continue
                # fall through to process any messages that arrived after thread start
            
            for msg in reversed(new_messages):
                from_number = msg.from_
                body = msg.body

                logging.info(f"📱 SMS from {from_number[-4:]}: '{body[:30]}'")  # keep at INFO - new message is significant

                try:
                    process_incoming_message(from_number, body)
                    # Advance the dedup marker only on success - an exception
                    # leaves the SID unsaved so the message is retried next poll.
                    last_message_sid = msg.sid
                    save_last_sid(msg.sid)
                    logging.debug(f"💾 Saved SID: {msg.sid[:10]}...")

                except Exception as e:
                    logging.error(f"💥 EXCEPTION processing message: {e}")
                    import traceback
                    logging.error(traceback.format_exc())
            
        except Exception as e:
            logging.error(f"💥 Error polling Twilio: {e}")
        
        time.sleep(config['poll_interval'])

    logging.info("🛑 Twilio polling stopped")


# ============================================================================
# GOOGLE VOICE SOURCE (Gmail IMAP scanning)
# ----------------------------------------------------------------------------
# Google Voice has no public API. When "Forward messages to email" is enabled in
# Voice settings, each incoming SMS is emailed to the linked Gmail account from
# an @txt.voice.google.com address. We scan that inbox over IMAP and feed parsed
# messages through the same process_incoming_message() pipeline as Twilio.
# Inbound-only in v1: no outbound auto-responses (see send_sms_response()).
# ============================================================================

# Markers that begin the Google Voice footer, which sits BELOW the SMS text.
# Everything from the earliest marker onward is footer and is discarded. These
# must be strings that only ever appear in the footer - NOT the bare
# "voice.google.com" URL, which also appears in the logo link ABOVE the message.
_GV_FOOTER_MARKERS = (
    "YOUR ACCOUNT",
    "To respond to this text message",
    "This email was sent to you",
    "This message was sent to you",
)


def _gv_decode_header(value):
    """Decode an RFC 2047 encoded header (e.g. a UTF-8 sender name) to str."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def _gv_get_part_body(msg, want_type):
    """Return the decoded body of the first part matching want_type
    ('text/plain' or 'text/html'), or '' if none."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == want_type and \
               'attachment' not in str(part.get('Content-Disposition', '')).lower():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    return payload.decode(charset, errors='replace')
        return ''
    if msg.get_content_type() == want_type:
        payload = msg.get_payload(decode=True)
        if payload is None:
            return msg.get_payload() or ''
        charset = msg.get_content_charset() or 'utf-8'
        return payload.decode(charset, errors='replace')
    return ''


def _gv_html_to_text(html):
    """Crudely convert HTML to text for the fallback path: drop <style>/<script>,
    replace tags with spaces, and unescape entities."""
    import html as _html_mod
    html = re.sub(r'(?is)<(style|script|head).*?</\1>', ' ', html)
    text = re.sub(r'(?s)<[^>]+>', ' ', html)
    return _html_mod.unescape(text)


def _gv_extract_message(text):
    """Pull just the SMS text out of a Google Voice forwarding-email body.

    The GV plain-text layout is:
        <blank lines>
        <https://voice.google.com>       <- logo link (skip)
        the actual message               <- one or more lines (keep)
        YOUR ACCOUNT <...> HELP CENTER   <- footer starts here (cut)
    So: cut everything from the footer down, then drop leading blank lines and
    any bare <URL> logo/link lines, and join what's left.

    The surviving lines are joined with newlines (not spaces) so that a
    multi-line submission - e.g. a name list typed one-per-line - stays
    multi-line, matching how Twilio delivers the raw SMS body. The payload was
    already transfer-decoded upstream, so these newlines are the sender's real
    line breaks, not email soft-wraps."""
    if not text:
        return ""
    # Cut the footer (and everything after it)
    cut = len(text)
    for marker in _GV_FOOTER_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    head = text[:cut]

    message_lines = []
    for ln in head.splitlines():
        s = ln.strip()
        if not s:
            continue
        # Skip the Google Voice logo/link line(s): a line that is only a <URL>
        if re.fullmatch(r'<https?://[^>]+>', s):
            continue
        # Skip any leftover bare voice.google.com link fragments
        if 'voice.google.com' in s and re.fullmatch(r'[<>\s]*https?://\S+[<>\s]*', s):
            continue
        message_lines.append(s)
    return '\n'.join(message_lines).strip()


def _gv_normalize_phone(text):
    """Pull the first phone-number-looking run out of text and return it in
    E.164-ish form (+1XXXXXXXXXX for US), or None."""
    if not text:
        return None
    m = re.search(r'\+?\d[\d\-\.\s()]{6,}\d', text)
    if not m:
        return None
    digits = re.sub(r'\D', '', m.group(0))
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) >= 7:
        return '+' + digits
    return None


def _gv_sender_id(msg, display_name):
    """Resolve the sender's phone number for a Google Voice forward, so the key
    used for display / blocklist / rate-limiting is the actual number regardless
    of whether the sender is a saved contact.

    Sources, most reliable first:
      1. Subject: "New text message from <name> (610) 809-3236" (sender only)
      2. From local-part: "<yourGVnum>.<sendernum>.<token>@txt.voice.google.com"
      3. The display name (contact name, or the raw number for unknown senders)
    """
    # 1) Subject line - contains only the sender's number
    subj = _gv_decode_header(str(msg.get('Subject', '')))
    num = _gv_normalize_phone(subj)
    if num:
        return num

    # 2) From address local-part - segments are <GVnum>.<sendernum>.<token>;
    #    the sender's number is the 2nd all-digit segment (1st is your own GV number)
    _n, addr = email.utils.parseaddr(str(msg.get('From', '')))
    local = addr.split('@', 1)[0]
    numeric_segs = [s for s in local.split('.') if s.isdigit() and len(s) >= 10]
    if len(numeric_segs) >= 2:
        num = _gv_normalize_phone(numeric_segs[1])
        if num:
            return num
    if len(numeric_segs) == 1:
        num = _gv_normalize_phone(numeric_segs[0])
        if num:
            return num

    # 3) Fall back to the display name (already the raw number for unknown senders)
    if display_name and re.fullmatch(r'[\d\s\-\.\(\)\+]+', display_name):
        n = _gv_normalize_phone(display_name)
        if n:
            return n
    return display_name or "Guest"


def parse_gv_email(raw_bytes):
    """Parse a Google Voice SMS-forwarding email into (from_id, body).

    The forwarding format is undocumented and can change, so this is deliberately
    defensive and logs the raw email on failure so drift is diagnosable. Returns
    None for mail that isn't a parseable GV SMS.

    Sender identity: the From display name is the saved contact name, or - for an
    unknown sender - the raw phone number. When it looks like a number we
    normalize it to digits so blocklist / rate-limit keys line up with how a
    number would be stored; otherwise the contact name is used as the key.
    """
    try:
        msg = email.message_from_bytes(raw_bytes)

        # Only handle mail actually forwarded by Google Voice
        from_hdr = str(msg.get('From', ''))
        if 'voice.google.com' not in from_hdr.lower():
            return None

        display_name, _addr = email.utils.parseaddr(from_hdr)
        display_name = _gv_decode_header(display_name)

        # The message text lives in the text/plain part, between the logo link
        # and the footer. Fall back to the HTML part if plain yields nothing.
        body = _gv_extract_message(_gv_get_part_body(msg, 'text/plain'))
        if not body:
            body = _gv_extract_message(_gv_html_to_text(_gv_get_part_body(msg, 'text/html')))

        if not body:
            logging.warning("GV email parsed but message body was empty; raw logged at debug")
            logging.debug(f"GV raw (empty body): {raw_bytes[:2000]!r}")
            return None

        # Resolve the sender's actual phone number (Subject / From address),
        # falling back to the display name - so blocklist / rate-limit / display
        # key on the real number whether or not the sender is a saved contact.
        from_id = _gv_sender_id(msg, display_name)

        # Reply context: how to answer this message via the reply-to-email trick.
        # Replying to the From address (with the original threading headers) makes
        # Google Voice deliver the reply body as an SMS to the sender.
        reply_ctx = {
            'to': _addr,
            'message_id': str(msg.get('Message-ID', '')).strip(),
            'references': str(msg.get('References', '')).strip(),
            'subject': _gv_decode_header(str(msg.get('Subject', ''))),
        }

        return from_id, body, reply_ctx
    except Exception as e:
        logging.error(f"Error parsing GV email: {e}")
        logging.debug(f"GV raw (parse error): {raw_bytes[:2000]!r}")
        return None


def poll_google_voice():
    """Poll a Gmail inbox (IMAP) for Google Voice SMS-forwarding emails and feed
    them through the shared processing pipeline. Mirrors poll_twilio()'s loop
    shape (midnight cleanup, first-run anchoring, per-message dedup)."""
    global last_gv_uid, stop_polling, _gv_reply_ctx

    logging.info("🚀 Google Voice polling started")
    first_run = last_gv_uid is None
    _current_day = datetime.now().date()
    my_gen = polling_generation  # exit if a source switch retires this poller

    while not stop_polling and my_gen == polling_generation:
        try:
            # Midnight cleanup - delete daily log files older than 7 days
            today = datetime.now().date()
            if today != _current_day:
                _current_day = today
                try:
                    cleanup_old_logs()
                    logging.info(f"🌙 Midnight: old daily logs cleaned up for {today}")
                except Exception as e:
                    logging.error(f"Error during midnight cleanup: {e}")

            email_addr = config.get('gv_email', '').strip()
            app_pw = config.get('gv_app_password', '').strip()
            if not email_addr or not app_pw:
                time.sleep(config.get('poll_interval', 2))
                continue

            imap = None
            try:
                imap = imaplib.IMAP4_SSL(config.get('gv_imap_host', 'imap.gmail.com'))
                imap.login(email_addr, app_pw)
                imap.select(config.get('gv_imap_folder', 'INBOX'))

                # UIDs of all Google Voice messages (IMAP FROM matches a substring)
                typ, data = imap.uid('search', None, 'FROM', 'txt.voice.google.com')
                raw_uids = data[0].split() if (typ == 'OK' and data and data[0]) else []
                uids = [u.decode() if isinstance(u, bytes) else str(u) for u in raw_uids]

                # Keep only UIDs newer than the last processed one (UIDs are
                # monotonic within a mailbox)
                if last_gv_uid:
                    try:
                        last_int = int(last_gv_uid)
                        uids = [u for u in uids if int(u) > last_int]
                    except ValueError:
                        pass

                if first_run:
                    # Anchor to the newest UID so we don't replay the inbox backlog
                    if uids:
                        newest = max(int(u) for u in uids)
                        last_gv_uid = str(newest)
                        save_last_gv_uid(last_gv_uid)
                    first_run = False
                    uids = []
                    logging.info("⚙️ GV first run: baseline UID set, backlog skipped")

                for uid in sorted(uids, key=int):
                    try:
                        typ, msg_data = imap.uid('fetch', uid, '(RFC822)')
                        if typ == 'OK' and msg_data and msg_data[0]:
                            raw = msg_data[0][1]
                            parsed = parse_gv_email(raw)
                            if parsed:
                                from_id, body, reply_ctx = parsed
                                logging.info(f"📱 GV SMS from {from_id[-4:]}: '{body[:30]}'")
                                # Make the reply target available to send_sms_response
                                # for the duration of this message's processing.
                                _gv_reply_ctx = reply_ctx
                                try:
                                    process_incoming_message(from_id, body)
                                finally:
                                    _gv_reply_ctx = None
                    except Exception as e:
                        logging.error(f"💥 EXCEPTION processing GV message {uid}: {e}")
                        import traceback
                        logging.error(traceback.format_exc())
                    # Advance the dedup marker even for skipped/unparseable mail so
                    # the same UID isn't fetched again forever
                    last_gv_uid = uid
                    save_last_gv_uid(uid)
            finally:
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:
                        pass

        except Exception as e:
            logging.error(f"💥 Error polling Google Voice: {e}")

        time.sleep(config.get('poll_interval', 2))

    logging.info("🛑 Google Voice polling stopped")


def start_polling_if_needed():
    """Ensure the polling thread for the currently selected message source is
    running (and that no poller for the *other* source is). Returns True if a
    poller is (or is now) running for the selected source.

    If a poller for a different source is already live, its generation is bumped
    so it exits on its next loop, and a fresh poller is started - this lets the
    provider be switched from the UI without a service restart."""
    global polling_thread, polling_source, polling_generation

    # Remotes never talk to Twilio/Google - they only render names the master pushes.
    if is_remote():
        return False

    source = config.get('message_source', 'twilio')
    if source == 'google_voice':
        if not (config.get('gv_email') and config.get('gv_app_password')):
            logging.warning("⚠️  Google Voice selected but email/app password not set; polling not started")
            return False
        target = poll_google_voice
    else:
        if not twilio_client:
            return False
        target = poll_twilio

    # Correct poller already running - nothing to do
    if polling_thread and polling_thread.is_alive() and polling_source == source:
        return True

    # Bumping the generation retires any poller currently running for the other
    # source (it sees my_gen != polling_generation and exits its loop).
    polling_generation += 1
    polling_thread = threading.Thread(target=target, daemon=True)
    polling_source = source
    polling_thread.start()
    logging.info(f"▶️  Polling started ({source})")
    return True


@app.route('/')
def index():
    """Main configuration page"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Text My Lights - Configuration</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #4CAF50; }
            .section { background: #f8f8f8; padding: 20px; margin: 20px 0; border-radius: 5px; border: 1px solid #ddd; }
            label { display: block; margin: 10px 0 5px; font-weight: bold; }
            input, select, textarea { width: 100%; padding: 8px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; background: #fff; color: #333; box-sizing: border-box; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px; }
            button:hover { background: #45a049; }
            .test-btn { background: #2196F3; }
            .test-btn:hover { background: #0b7dda; }
            .view-btn { background: #FF9800; }
            .view-btn:hover { background: #e68900; }
            .refresh-btn { background: #9C27B0; }
            .refresh-btn:hover { background: #7B1FA2; }
            .checkbox-label { display: inline; margin-left: 10px; font-weight: normal; vertical-align: middle; }
            .toggle-switch { position: relative; display: inline-block; width: 44px; height: 26px; flex-shrink: 0; vertical-align: middle; }
            .toggle-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
            .toggle-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background: #555; border-radius: 26px; transition: background .2s; }
            .toggle-slider:before { position: absolute; content: ""; height: 20px; width: 20px; left: 3px; bottom: 3px; background: #fff; border-radius: 50%; transition: transform .2s; box-shadow: 0 1px 3px rgba(0,0,0,.4); }
            .toggle-switch input:checked + .toggle-slider { background: #4CAF50; }
            .toggle-switch input:checked + .toggle-slider:before { transform: translateX(18px); }
            .success { color: #4CAF50; }
            .error { color: #f44336; }
            .info { background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 20px 0; border: 1px solid #90caf9; color: #333; }
            .queue-info { background: #f3e5f5; padding: 15px; border-radius: 5px; margin: 20px 0; border: 1px solid #ce93d8; color: #333; }
            h3 { color: #4CAF50; margin-top: 20px; margin-bottom: 10px; }
            .help-text { font-size: 12px; color: #666; margin-top: 5px; }
            select[id$="_font"] option { padding: 8px; font-size: 14px; }
            .columns { display: flex; gap: 20px; margin: 0; align-items: stretch; }
            .column { flex: 1; min-width: 0; display: flex; flex-direction: column; }
            .column .section { flex: 0 0 auto; }
            .column .section:last-child { flex: 1; }
            .top-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 15px 0; padding: 15px; background: #f8f8f8; border-radius: 5px; border: 1px solid #ddd; }
            .tabs { display: flex; gap: 0; margin: 20px 0 0 0; border-bottom: 2px solid #4CAF50; }
            .tab-btn { background: #f0f0f0; color: #555; padding: 7px 14px; border: 1px solid #ddd; border-bottom: none; border-radius: 4px 4px 0 0; cursor: pointer; font-size: 13px; font-weight: bold; margin-right: 2px; }
            .tab-btn.active { background: #4CAF50; color: white; border-color: #4CAF50; }
            .tab-btn:hover:not(.active) { background: #e8e8e8; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>

        <!-- SMS cost disclaimer - shown on every role (master and remote) -->
        <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:6px; padding:10px 14px; margin:14px 0 0 0; font-size:13px; line-height:1.5;">
            <strong>DISCLAIMER:</strong> The author of this plugin is NOT responsible for SMS charges that may be incurred by using this plugin, or any inappropriate content that may be displayed from incorrectly configured settings.
        </div>

        <!-- Tab navigation -->
        <div class="tabs" style="display:flex; align-items:center; gap:2px;">
            <button class="tab-btn active" onclick="showTab('settings', this)">⚙️ Settings</button>
            <button class="tab-btn" onclick="showTab('display', this)">🖥️ Display</button>
            <button class="tab-btn" id="tabbtn-sms" onclick="showTab('sms', this)">📱 SMS Responses</button>
            <button class="tab-btn" id="tabbtn-testing" onclick="showTab('testing', this)">🧪 Testing</button>
            <button class="view-btn" id="btn_view_queue" onclick="viewMessages()" style="margin:0 0 0 10px; padding:7px 14px; font-size:13px;">📋 View Message Queue</button>
            <span id="autosave_status" style="font-size:13px; margin-left:8px;"></span>
            <div style="margin-left:auto; display:flex; gap:4px; align-items:center;">
                <button id="btn_plugin_toggle" onclick="pluginToggle()" style="background:#2e7d32; color:#fff; border:none; padding:6px 12px; border-radius:4px; font-size:12px; font-weight:bold; cursor:pointer;">▶ Start</button>
            </div>
        </div>

        <!-- Plugin Live Banner -->
        <div id="plugin_live_banner" style="display:none; background:#1b5e20; color:#fff; padding:10px 16px; border-radius:5px; margin-top:10px; font-size:14px; font-weight:bold; align-items:center; gap:10px;">
            <span style="display:inline-block; width:12px; height:12px; background:#69f0ae; border-radius:50%; box-shadow:0 0 6px #69f0ae;"></span>
            Plugin is Live - Press Stop or run the "Text My Lights Stop" script to stop displaying incoming messages.
        </div>

        <!-- Plugin Not Live Banner -->
        <div id="plugin_not_live_banner" style="display:none; background:#b71c1c; color:#fff; padding:10px 16px; border-radius:5px; margin-top:10px; font-size:14px; font-weight:bold; align-items:center; gap:10px;">
            <span style="display:inline-block; width:12px; height:12px; background:#ff8a80; border-radius:50%; box-shadow:0 0 6px #ff8a80;"></span>
            <span>Plugin is Not Live - Press Start or run the "Text My Lights Start" script to display incoming messages.<br>
            <span style="font-weight:normal; font-size:12px;">Note: Viewers can still send messages, messaging rates will apply, but no messages will be displayed.</span></span>
        </div>

        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content active">
            <div class="columns">

                <!-- LEFT COLUMN: Twilio + FPP Display + Message Settings -->
                <div class="column">
                    <div class="section">
                        <h2>🖥️ Projector Role (Master / Remote)</h2>
                        <p class="help-text">The <strong>Master</strong> is where the configuration lives and text messages are received. Each <strong>Remote</strong> only displays the names the Master pushes to it.</p>
                        <p class="help-text" style="margin-top:4px;"><em>Master/Remote here is this <strong>plugin's</strong> role - separate from FPP's own Player/Remote mode. The plugin master does not need to be the FPP master.</em></p>
                        <label>This instance is:</label>
                        <select id="plugin_role" onchange="onRoleChange()">
                            <option value="master" {{ 'selected' if effective_role != 'remote' else '' }}>Master - handles texts &amp; pushes names</option>
                            <option value="remote" {{ 'selected' if effective_role == 'remote' else '' }}>Remote - only displays pushed names</option>
                        </select>

                        <div id="remote_mode_note" style="display:none; background:#e3f2fd; border:1px solid #90caf9; color:#0d47a1; border-radius:5px; padding:8px 12px; margin-top:10px; font-size:13px;">
                            ℹ️ <strong>Remote mode:</strong> the Master's content must also exist on this remote - recommend using <strong>Config → Export/Import</strong>.
                        </div>
                        <div id="master_sync_box" style="display:none; margin-top:12px;">
                            <label style="display:flex; align-items:center; gap:8px;">🔗 Sync to Plugin Master
                                <button type="button" class="test-btn" id="btn_refresh_masters" onclick="refreshMasters(this)" style="padding:2px 10px; font-size:12px;">🔄 Refresh</button>
                            </label>
                            <div id="masters_list" style="margin-top:6px;">
                                <p class="help-text" id="masters_empty">Looking for plugin masters on the network…</p>
                            </div>
                        </div>
                    </div>
                    <div class="section" id="message_source_section">
                        <h2>Message Source</h2>
                        <label>SMS Provider:</label>
                        <select id="message_source">
                            <option value="twilio" {{ 'selected' if config.get('message_source','twilio') != 'google_voice' else '' }}>Twilio</option>
                            <option value="google_voice" {{ 'selected' if config.get('message_source','twilio') == 'google_voice' else '' }}>Google Voice (Gmail)</option>
                        </select>
                        <p class="help-text"><a id="provider_help_link" href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=help.php#twilio" target="_top">View Twilio Configuration</a></p>

                        <!-- Twilio credentials - shown when Message Source = Twilio -->
                        <div id="twilio_creds">
                            <h3 style="margin:14px 0 6px;">Twilio Settings</h3>
                            <div style="background:#f8d7da; border:2px solid #f5c6cb; color:#721c24; border-radius:6px; padding:12px 16px; margin:4px 0 12px; font-size:13px;">
                                &#9940; <strong>Twilio SMS auto-responses are not enabled.</strong>
                                <span style="font-weight:normal; display:block; margin-top:6px;">
                                    Sending reply texts from a Twilio number requires A2P 10DLC brand &amp; campaign registration (or toll-free verification), which is not set up for this plugin. Receiving texted names still works and they will appear on your display, but no SMS replies are sent. Use Google Voice if you need auto-responses.
                                </span>
                            </div>
                            <label>Twilio Account SID:</label>
                            <input type="text" id="account_sid" value="{{ config.twilio_account_sid }}" placeholder="Starts with AC...">

                            <label>Twilio Auth Token:</label>
                            <input type="password" id="auth_token" autocomplete="new-password" onfocus="this.select()"
                                   value="{{ secret_sentinel if config.twilio_auth_token else '' }}"
                                   placeholder="Twilio Auth Token">

                            <label>Twilio Phone Number:</label>
                            <input type="text" id="phone_number" value="{{ config.twilio_phone_number }}" placeholder="+1234567890">

                            <button class="test-btn" onclick="testConnection()">🔌 Test Twilio Connection</button>
                            <div id="twilio_test_result" style="margin-top: 8px; font-size: 14px;"></div>
                        </div>

                        <!-- Google Voice credentials - shown when Message Source = Google Voice -->
                        <div id="gv_creds" style="display:none;">
                            <h3 style="margin:14px 0 6px;">Google Voice Settings</h3>
                            <label>Gmail Address:</label>
                            <input type="text" id="gv_email" value="{{ config.get('gv_email','') }}" placeholder="you@gmail.com">

                            <label>App Password:</label>
                            <input type="password" id="gv_app_password" autocomplete="new-password" onfocus="this.select()"
                                   value="{{ secret_sentinel if config.get('gv_app_password') else '' }}"
                                   placeholder="16-character app password">

                            <button class="test-btn" onclick="testGoogleVoice()">🔌 Test Google Voice Connection</button>
                            <div id="gv_test_result" style="margin-top: 8px; font-size: 14px;"></div>
                        </div>

                        <label>Poll Interval (seconds):</label>
                        <input type="number" id="poll_interval" value="{{ config.poll_interval }}" min="1" max="60">

                        <!-- Live Name Approval - Google Voice only; shown/hidden by updateSourceUI() -->
                        <div id="gv_approval" style="display:none;">
                            <hr style="border:none; border-top:1px solid #444; margin:16px 0;">
                            <h3 style="margin:14px 0 6px;">🙋 Live Name Approval (optional) <span id="live_approval_wl_state" style="font-size:13px; font-weight:normal; margin-left:6px; padding:2px 8px; border-radius:10px;"></span></h3>
                            <p class="help-text" style="margin:4px 0 8px;">When the whitelist is on and the show is live, a texter who sends a name that is not on the list can be approved by you over text. Leave the number blank to turn this off. </p>

                            <div id="admin_no_gv_warning" style="{{ '' if (config.get('admin_phone','') and not admin_gv_linked) else 'display:none;' }} background:#fdecea; border:1px solid #f44336; color:#b71c1c; border-radius:6px; padding:10px 14px; margin-bottom:10px; font-size:13px;">
                                🔴 <strong>No Google Voice account linked.</strong> Enter and test your Gmail address and App Password above first. Until a Google Voice account is connected there is no number to text, so live approvals cannot be set up.
                            </div>
                            <div id="admin_bootstrap_banner" style="{{ '' if (config.get('admin_phone','') and not admin_ctx_seeded and admin_gv_linked) else 'display:none;' }} background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:6px; padding:10px 14px; margin-bottom:10px; font-size:13px;">
                                ⚠️ <strong>Action needed:</strong> from the admin phone (<span id="admin_banner_num">{{ config.get('admin_phone','') }}</span>), text the word <strong>admin</strong> to your Google Voice number to connect. You will not receive approval requests until you do. The word "admin" is never shown on the display.
                            </div>
                            <div id="admin_connected_note" style="{{ '' if (config.get('admin_phone','') and admin_ctx_seeded) else 'display:none;' }} background:#e8f5e9; border:1px solid #66bb6a; color:#2e7d32; border-radius:6px; padding:8px 12px; margin-bottom:10px; font-size:13px;">
                                ✅ Admin phone connected - live approvals are active.
                            </div>
                            <div id="admin_thread_warning" style="{{ '' if (config.get('admin_phone','') and admin_gv_linked) else 'display:none;' }} background:#fdecea; border:1px solid #f44336; color:#b71c1c; border-radius:6px; padding:8px 12px; margin-bottom:10px; font-size:13px;">
                                🔴 <strong>Keep the "admin" email:</strong> Google Voice does not have the ability to send outbound messages without an exisiting email thread. The "admin" email must remain in your Gmail inbox for approvals to work.
                            </div>

                            <label>Admin Phone Number:</label>
                            <input type="text" id="admin_phone" value="{{ config.get('admin_phone','') }}" placeholder="e.g. 5551234567" style="width:100%; max-width:260px;">
                        </div>
                    </div>

                    <!-- FPP Display Settings: shown on BOTH master and remote (the overlay
                         model + names content are per-projector; a remote needs them). -->
                    <div class="section">
                        <h2 style="margin-top: 0;">FPP Display Settings</h2>

                        <div id="fpp_content_live_warning" style="display:none; background:#b71c1c; color:#fff; border-radius:5px; padding:8px 12px; margin-bottom:10px; font-size:13px;">
                            🔴 <strong>Plugin is Live</strong>; run Text My Lights Stop or press Stop to edit
                        </div>
                        <div id="fpp_content_inputs">
                            <div style="display:flex; gap:20px; flex-wrap:wrap; align-items:flex-start;">
                              <div style="flex:1; min-width:280px;" id="waiting_config_col">
                            <label>Default "Waiting" Content: <span style="color:#f44336;font-size:12px;">* required</span> <span class="help-text" style="font-weight:normal;margin-left:6px;">📺 Loops while waiting for texts. Add 2+ to rotate between them (each sequence plays full length.)</span></label>
                            <!-- Hidden legacy single-value select: kept in sync with the first
                                 list item. Drives the canvas preview background + the
                                 deleted-file prune, and is the value saved as default_playlist. -->
                            <select id="default_playlist" style="display:none;">
                                <option value="">-- Select content --</option>
                            </select>
                            <div id="waiting_content_list_box" style="border:1px solid #ddd; border-radius:5px; padding:10px; background:#fff;">
                                <div id="waiting_content_items"></div>
                                <button type="button" onclick="openManageWaitingModal()" style="margin-top:8px; font-size:13px; padding:6px 14px; cursor:pointer; background:#1976d2; color:#fff; border:none; border-radius:4px;">🗂️ Add / Arrange Waiting Content</button>
                                <div id="waiting_mode_row" style="display:none; margin-top:12px; padding-top:10px; border-top:1px solid #eee;">
                                    <span style="font-size:13px; color:#555; margin-right:10px;">When a sequence ends, play:</span>
                                    <label style="margin-right:14px; cursor:pointer; color:#333; font-size:13px;"><input type="radio" name="waiting_mode" value="roundrobin" onchange="onWaitingModeChange('roundrobin')" style="width:auto;margin:0 5px 0 0;vertical-align:middle;">Round Robin (in order)</label>
                                    <label style="cursor:pointer; color:#333; font-size:13px;"><input type="radio" name="waiting_mode" value="random" onchange="onWaitingModeChange('random')" style="width:auto;margin:0 5px 0 0;vertical-align:middle;">Random</label>
                                </div>
                            </div>
                            <div id="waiting_content_none_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:6px; font-size:13px;">
                                ⚠️ No Waiting content selected - required before you can Start the show.
                            </div>
                              </div>
                              <div style="flex:1; min-width:280px;" id="names_config_col">
                            <label>Name Display Content: <span class="help-text" style="font-weight:normal;margin-left:6px;">🎬 Background(s) shown when a name appears. Add one or more - each gets its own text layout on the Display tab.</span></label>
                            <div id="names_content_remote_note" style="display:none; background:#e3f2fd; border:1px solid #90caf9; color:#0d47a1; border-radius:5px; padding:8px 12px; margin-bottom:6px; font-size:13px;">
                                ℹ️ This list is <strong>synced from the Master</strong> (only content that also exists on this Pi appears). Pick a content below to set <em>this</em> projector's text layout for it on the Display tab - your overlay model, sizing, and positioning are independent of the Master.
                            </div>
                            <div id="names_content_list_box" style="border:1px solid #ddd; border-radius:5px; padding:10px; background:#fff;">
                                <div id="names_content_items"></div>
                                <button type="button" id="btn_manage_names" onclick="openManageContentModal()" style="margin-top:8px; font-size:13px; padding:6px 14px; cursor:pointer; background:#1976d2; color:#fff; border:none; border-radius:4px;">🗂️ Add / Arrange Content</button>
                                <div id="names_mode_row" style="display:none; margin-top:12px; padding-top:10px; border-top:1px solid #eee;">
                                    <span style="font-size:13px; color:#555; margin-right:10px;">When a name arrives, pick:</span>
                                    <label style="margin-right:14px; cursor:pointer; color:#333; font-size:13px;"><input type="radio" name="names_mode" value="roundrobin" onchange="onNamesModeChange('roundrobin')" style="width:auto;margin:0 5px 0 0;vertical-align:middle;">Round Robin</label>
                                    <label style="cursor:pointer; color:#333; font-size:13px;"><input type="radio" name="names_mode" value="random" onchange="onNamesModeChange('random')" style="width:auto;margin:0 5px 0 0;vertical-align:middle;">Random</label>
                                </div>
                            </div>
                            <div id="name_display_none_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:6px; font-size:13px;">
                                ⚠️ No Names content - names will appear directly over the Waiting content (using the Display-tab text layout).
                            </div>
                              </div>
                            </div>

                            <!-- Manage Names Content modal: Available (left) → Names list (right), with arrows.
                                 Top-aligned + high z-index (matching the export modal's overlay) and paired with
                                 a scroll-to-top on open, so it lands in view inside the auto-height FPP iframe
                                 where position:fixed is relative to the full plugin height, not the viewport. -->
                            <div id="manage_content_modal" onclick="if(event.target===this)closeManageContentModal()" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.55); z-index:100000; align-items:flex-start; justify-content:center; padding-top:24px; box-sizing:border-box;">
                                <div onclick="event.stopPropagation()" style="background:#fff; color:#333; border-radius:8px; padding:22px; width:94%; max-width:740px; box-shadow:0 8px 30px rgba(0,0,0,0.35); max-height:90vh; overflow:auto; box-sizing:border-box;">
                                    <h3 style="margin-top:0;">Manage Names Content</h3>
                                    <p class="help-text" style="margin-top:4px;">Select content on the left and click ▶ to add it to your Names list. Reorder the list with ▲ / ▼ (order matters for Round Robin). Remove with ◀.</p>
                                    <div style="display:flex; gap:10px; align-items:stretch;">
                                        <div style="flex:1; min-width:0;">
                                            <label style="font-size:13px;">Available Content</label>
                                            <select id="mng_available" multiple size="12" style="width:100%; height:280px; box-sizing:border-box;"></select>
                                        </div>
                                        <div style="display:flex; flex-direction:column; justify-content:center; gap:10px;">
                                            <button type="button" onclick="mngAdd()" title="Add to Names list" style="padding:6px 10px; cursor:pointer;">▶</button>
                                            <button type="button" onclick="mngRemove()" title="Remove from Names list" style="padding:6px 10px; cursor:pointer;">◀</button>
                                        </div>
                                        <div style="flex:1; min-width:0;">
                                            <label style="font-size:13px;">Names List (in order)</label>
                                            <select id="mng_selected" multiple size="12" style="width:100%; height:280px; box-sizing:border-box;"></select>
                                        </div>
                                        <div style="display:flex; flex-direction:column; justify-content:center; gap:10px;">
                                            <button type="button" onclick="mngMoveUp()" title="Move up" style="padding:6px 10px; cursor:pointer;">▲</button>
                                            <button type="button" onclick="mngMoveDown()" title="Move down" style="padding:6px 10px; cursor:pointer;">▼</button>
                                        </div>
                                    </div>
                                    <div style="margin-top:16px; display:flex; justify-content:flex-end; gap:8px;">
                                        <button type="button" onclick="closeManageContentModal()" style="background:#2e7d32; color:#fff; padding:8px 20px; border:none; border-radius:4px; cursor:pointer;">Done</button>
                                    </div>
                                </div>
                            </div>

                            <!-- Manage Waiting Content modal - same two-pane picker as Names, but the
                                 right list is the waiting-content rotation (no per-item text layout). -->
                            <div id="manage_waiting_modal" onclick="if(event.target===this)closeManageWaitingModal()" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.55); z-index:100000; align-items:flex-start; justify-content:center; padding-top:24px; box-sizing:border-box;">
                                <div onclick="event.stopPropagation()" style="background:#fff; color:#333; border-radius:8px; padding:22px; width:94%; max-width:740px; box-shadow:0 8px 30px rgba(0,0,0,0.35); max-height:90vh; overflow:auto; box-sizing:border-box;">
                                    <h3 style="margin-top:0;">Manage Waiting Content</h3>
                                    <p class="help-text" style="margin-top:4px;">Select content on the left and click ▶ to add it. With 2+ items the plugin rotates them while idle - each sequence plays its full length, then the next starts seamlessly (no black gap). Reorder with ▲ / ▼ (order matters for Round Robin). Remove with ◀.</p>
                                    <div style="display:flex; gap:10px; align-items:stretch;">
                                        <div style="flex:1; min-width:0;">
                                            <label style="font-size:13px;">Available Content</label>
                                            <select id="wmng_available" multiple size="12" style="width:100%; height:280px; box-sizing:border-box;"></select>
                                        </div>
                                        <div style="display:flex; flex-direction:column; justify-content:center; gap:10px;">
                                            <button type="button" onclick="wmngAdd()" title="Add to Waiting list" style="padding:6px 10px; cursor:pointer;">▶</button>
                                            <button type="button" onclick="wmngRemove()" title="Remove from Waiting list" style="padding:6px 10px; cursor:pointer;">◀</button>
                                        </div>
                                        <div style="flex:1; min-width:0;">
                                            <label style="font-size:13px;">Waiting List (in order)</label>
                                            <select id="wmng_selected" multiple size="12" style="width:100%; height:280px; box-sizing:border-box;"></select>
                                        </div>
                                        <div style="display:flex; flex-direction:column; justify-content:center; gap:10px;">
                                            <button type="button" onclick="wmngMoveUp()" title="Move up" style="padding:6px 10px; cursor:pointer;">▲</button>
                                            <button type="button" onclick="wmngMoveDown()" title="Move down" style="padding:6px 10px; cursor:pointer;">▼</button>
                                        </div>
                                    </div>
                                    <div style="margin-top:16px; display:flex; justify-content:flex-end; gap:8px;">
                                        <button type="button" onclick="closeManageWaitingModal()" style="background:#2e7d32; color:#fff; padding:8px 20px; border:none; border-radius:4px; cursor:pointer;">Done</button>
                                    </div>
                                </div>
                            </div>

                            <label>Overlay Model Name: <button type="button" onclick="refreshFPPLists(this)" style="font-size:11px;padding:2px 7px;margin-left:8px;cursor:pointer;">↻ Refresh Lists</button> <span class="help-text" style="font-weight:normal;margin-left:6px;">📝 The pixel overlay model for text (e.g., "Texting Matrix"). On a remote this is local to this projector.</span></label>
                            <select id="overlay_model_name">
                                <option value="">-- None --</option>
                            </select>
                        </div>
                    </div>

                    <div class="section" id="message_settings_section">
                        <h2 style="margin-top: 0;">Message Settings</h2>

                        <!-- Display Duration moved to the Display tab (it is now per Names
                             content). This hidden field holds the fallback used when no Names
                             content is configured, and keeps import/export compatible. -->
                        <input type="hidden" id="display_duration" value="{{ config.display_duration }}">

                        <label>Max Messages Per Phone (0 = unlimited):</label>
                        <input type="number" id="max_messages" value="{{ config.max_messages_per_phone }}" min="0" max="100">

                        <div id="max_length_section">
                            <label>Max Message Length:</label>
                            <input type="number" id="max_length" value="{{ config.max_message_length }}" min="10" max="200">
                        </div>
                        <div id="max_length_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:6px; font-size:13px;">
                            ⚠️ <strong>Max Message Length is disabled</strong> - whitelist is enabled. Names are validated against the approved list, not by length.
                        </div>

                    </div>
                </div>

            </div>

            <!-- Filters - full width -->
            <div class="section" id="filters_section" style="margin-top:12px;">
                <h2>Filters</h2>
                <div style="display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap;">

                    <!-- Sub-col 1: Profanity + Whitelist -->
                    <div style="flex:1; min-width:220px;">
                        <div id="blacklist_section">
                            <label class="toggle-switch"><input type="checkbox" id="profanity_filter" {{ 'checked' if config.profanity_filter else '' }} onchange="checkFiltersState(); saveConfig();"><span class="toggle-slider"></span></label>
                            <label class="checkbox-label">Enable Profanity Filter</label><br>
                            <button class="view-btn" onclick="showBlacklistWarning()" style="margin-top:6px;">🚫 Manage Blacklist</button>
                            <div style="margin-top:10px; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                                <label for="profanity_threshold" style="font-weight:bold;">Auto-block after</label>
                                <input type="number" id="profanity_threshold" value="{{ config.get('profanity_threshold', 3) }}" min="0" max="100" style="width:60px;">
                                <label for="profanity_threshold">blacklisted words / day</label>
                            </div>
                            <p class="help-text" style="margin-top:4px;">ℹ️ When a sender texts this many blacklisted words in one day, their number is added to the Phone Blocklist (only you can release it). The daily tally resets at midnight. Set to <strong>0</strong> to turn off auto-blocking.</p>
                        </div>
                        <div id="profanity_disabled_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Profanity filter is disabled</strong> - this is not recommended. Re-enable it to filter names against the Blacklist, or enable the Whitelist instead.
                        </div>
                        <div id="blacklist_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Blacklist inactive</strong> - whitelist is enabled. All names are validated against the whitelist.
                        </div>

                        <hr style="border:none; border-top:1px solid #444; margin:15px 0;">

                        <label class="toggle-switch"><input type="checkbox" id="use_whitelist" {{ 'checked' if config.get('use_whitelist', False) else '' }} onchange="updateFormatRules(); checkFiltersState(); checkWhitelistResponseState(); updateLiveApprovalWlState(); updateAdminApprovalUI(); saveConfig();"><span class="toggle-slider"></span></label>
                        <label class="checkbox-label">Enable Name Whitelist - only allow approved names</label><br>
                        <button class="view-btn" onclick="location.href='/whitelist'" style="margin-top:6px;">📋 Manage Whitelist</button>
                    </div>

                    <!-- Sub-col 2: Name Format Rules -->
                    <div style="flex:1; min-width:220px;">
                        <div id="format_rules_section">
                            <h3 style="margin-bottom:6px;">Name Format Rules</h3>
                            <div id="format_rules_disabled_note" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-bottom:8px; font-size:13px;">
                                ⚠️ Name format rules are disabled when the whitelist is active.
                            </div>
                            <div id="format_rules_inputs">
                                <label class="toggle-switch"><input type="checkbox" id="one_word_only" {{ 'checked' if config.get('one_word_only', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('two_words_max').checked = false; checkFormatWarning(); updateWordsPreview(); saveConfig();"><span class="toggle-slider"></span></label>
                                <label class="checkbox-label">One Word Only (e.g., "John" ✓, "John Smith" ✗)</label><br>

                                <label class="toggle-switch"><input type="checkbox" id="two_words_max" {{ 'checked' if config.get('two_words_max', True) else '' }}
                                       onchange="if(this.checked) document.getElementById('one_word_only').checked = false; checkFormatWarning(); updateWordsPreview(); saveConfig();"><span class="toggle-slider"></span></label>
                                <label class="checkbox-label">Two Words Maximum (e.g., "John Smith" ✓, sentences ✗)</label><br>

                                <div id="format_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:10px 14px; margin:8px 0; font-size:13px;">
                                    ⚠️ <strong>Warning:</strong> With no format rules enabled, viewers can send any message up to your Max Message Length. This is not recommended.
                                </div>
                            </div>
                            <p id="hyphen_note" class="help-text">ℹ️ Hyphenated names like "Jean-Luc" count as one word. All names are converted to Proper Case.</p>
                        </div>
                    </div>

                    <!-- Sub-col 3: Phone Blocklist + Duplicate Names -->
                    <div style="flex:0 0 220px;">
                        <label style="font-weight:bold; margin-bottom:4px;">Phone Blocklist</label>
                        <button onclick="location.href='/blocklist'" style="background:#f44336; margin-top:4px; display:block;">🚫 View Blocklist</button>

                        <hr style="border:none; border-top:1px solid #444; margin:15px 0;">

                        <label class="toggle-switch"><input type="checkbox" id="allow_duplicate_names" {{ 'checked' if config.get('allow_duplicate_names', False) else '' }} onchange="checkDuplicateState(); saveConfig();"><span class="toggle-slider"></span></label>
                        <label class="checkbox-label">Allow Duplicate Names - same phone number can submit the same name multiple times per day</label>
                    </div>

                </div>

                <!-- Multiple names (grouped texts) explainer -->
                <div style="background:#e7f3ff; border:1px solid #4a90d9; color:#1a3d5c; border-radius:6px; padding:10px 14px; margin-top:14px; font-size:13px; line-height:1.55;">
                    <strong>📝 Multiple names in one text</strong><br>
                    Texters can submit several names at once, separated by <strong>commas or line breaks</strong>
                    (e.g. <em>"Alex, Sam, Jordan"</em>). Multi-word names like <em>"Mary Jane"</em> stay intact -
                    only commas and line breaks split names, never spaces.
                    <br><br>
                    To keep replies simple, a grouped text is <strong>all-or-nothing</strong> and is only accepted
                    when the box is fully open:
                    <ul style="margin:6px 0 0 18px; padding:0;">
                        <li><strong>Max Messages Per Phone</strong> must be <strong>0</strong> (no rate limiting)</li>
                        <li><strong>Allow Duplicate Names</strong> must be <strong>on</strong></li>
                        <li>Every name must pass your format rules - or, if the <strong>Whitelist</strong> is on,
                            <strong>all</strong> names must be on it</li>
                    </ul>
                    <div style="margin-top:6px;">
                        If any of these isn't met, the whole grouped text gets the <strong>Invalid Format</strong>
                        reply - and it does <strong>not</strong> count toward the sender's daily limit. Profanity
                        anywhere fails the whole message. An accepted group sends <strong>one Success</strong> reply.
                    </div>
                </div>
            </div>
            <script>
                function updateFormatRules() {
                    // While the whitelist is on the backend ignores the word-format rules
                    // (see is_valid_name callers), so GREY the toggles but NEVER change
                    // their checked state. The old code unchecked them and relied on a
                    // snapshot to restore; that let saveConfig persist the unchecked
                    // state and permanently lose the choice. Grey-only preserves it.
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var inputs = document.getElementById('format_rules_inputs');
                    var note = document.getElementById('format_rules_disabled_note');
                    inputs.style.opacity = whitelistOn ? '0.4' : '1';
                    inputs.style.pointerEvents = whitelistOn ? 'none' : '';
                    note.style.display = whitelistOn ? 'block' : 'none';
                    checkFormatWarning();
                }
                function checkFormatWarning() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var oneWord = document.getElementById('one_word_only').checked;
                    var twoWords = document.getElementById('two_words_max').checked;
                    var warn = !whitelistOn && !oneWord && !twoWords;
                    var rulesActive = !whitelistOn && (oneWord || twoWords);
                    document.getElementById('format_warning').style.display = warn ? 'block' : 'none';
                    document.getElementById('hyphen_note').style.opacity = rulesActive ? '1' : '0.4';
                }
                function checkDuplicateState() {
                    var allowDupes = document.getElementById('allow_duplicate_names').checked;
                    var row = document.getElementById('row_duplicate');
                    var cb = document.getElementById('sms_response_duplicate');
                    var warn = document.getElementById('duplicate_disabled_warning');
                    if (!row) return;
                    // Only disable/grey the row - never change the checkbox's own state,
                    // so turning Allow Duplicate Names back off restores the prior on/off choice.
                    if (cb) cb.disabled = allowDupes;
                    if (allowDupes) {
                        row.classList.add('locked');
                        row.classList.remove('enabled');
                    } else {
                        row.classList.remove('locked');
                        toggleResp('duplicate');  // reflect the preserved state
                    }
                    if (warn) warn.style.display = allowDupes ? '' : 'none';
                }
                function checkFiltersState() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var profanityOn = document.getElementById('profanity_filter').checked;
                    var section = document.getElementById('blacklist_section');
                    section.style.opacity = whitelistOn ? '0.4' : '1';
                    section.style.pointerEvents = whitelistOn ? 'none' : '';
                    var maxLenSection = document.getElementById('max_length_section');
                    maxLenSection.style.opacity = whitelistOn ? '0.4' : '1';
                    maxLenSection.style.pointerEvents = whitelistOn ? 'none' : '';
                    document.getElementById('max_length_disabled_warning').style.display = whitelistOn ? 'block' : 'none';
                    document.getElementById('blacklist_disabled_warning').style.display = whitelistOn ? 'block' : 'none';
                    document.getElementById('profanity_disabled_warning').style.display = (!whitelistOn && !profanityOn) ? 'block' : 'none';
                }
                // Invalid-Format response is meaningless when the whitelist is on
                // (names are validated against the list, not format rules). Lock the
                // row live when whitelist is enabled, and restore it when disabled.
                function checkWhitelistResponseState() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var row = document.getElementById('row_invalid_format');
                    var cb = document.getElementById('sms_response_invalid_format');
                    var warn = document.getElementById('invalid_format_disabled_warning');
                    if (!row) return;  // SMS-responses tab not parsed yet (init runs later)
                    // Only disable/grey the row - never change the checkbox's own state,
                    // so toggling the whitelist off restores the prior on/off choice.
                    if (cb) cb.disabled = whitelistOn;
                    if (warn) warn.style.display = whitelistOn ? '' : 'none';
                    if (whitelistOn) {
                        row.classList.add('locked');
                        row.classList.remove('enabled');
                    } else {
                        row.classList.remove('locked');
                        toggleResp('invalid_format');  // reflect the preserved state
                    }

                    // Too Long response is also meaningless with the whitelist on
                    // (Max Message Length doesn't apply), so lock it the same way.
                    var tlRow = document.getElementById('row_too_long');
                    var tlCb = document.getElementById('sms_response_too_long');
                    var tlWarn = document.getElementById('too_long_disabled_warning');
                    if (tlRow) {
                        if (tlCb) tlCb.disabled = whitelistOn;
                        if (tlWarn) tlWarn.style.display = whitelistOn ? '' : 'none';
                        if (whitelistOn) {
                            tlRow.classList.add('locked');
                            tlRow.classList.remove('enabled');
                        } else {
                            tlRow.classList.remove('locked');
                            toggleResp('too_long');  // reflect the preserved state
                        }
                    }

                    // Not-on-Whitelist response is the inverse: it can only fire while
                    // the whitelist is ON (names are checked against the list), so grey
                    // it out when the whitelist is off.
                    var nwRow = document.getElementById('row_not_whitelisted');
                    var nwCb = document.getElementById('sms_response_not_whitelisted');
                    var nwWarn = document.getElementById('not_whitelisted_disabled_warning');
                    if (nwRow) {
                        if (nwCb) nwCb.disabled = !whitelistOn;
                        if (nwWarn) nwWarn.style.display = whitelistOn ? 'none' : '';
                        if (!whitelistOn) {
                            nwRow.classList.add('locked');
                            nwRow.classList.remove('enabled');
                        } else {
                            nwRow.classList.remove('locked');
                            toggleResp('not_whitelisted');  // reflect the preserved state
                        }
                    }
                }
                // Rate-Limited response is meaningless when Max Messages Per Phone is 0
                // (unlimited) - no one is ever rate limited. Lock the row live.
                function checkRateLimitResponseState() {
                    var mmEl = document.getElementById('max_messages');
                    var unlimited = !mmEl || parseInt(mmEl.value || '0', 10) === 0;
                    var row = document.getElementById('row_rate_limited');
                    var cb = document.getElementById('sms_response_rate_limited');
                    var warn = document.getElementById('rate_limited_disabled_warning');
                    if (!row) return;  // SMS-responses tab not parsed yet (init runs later)
                    // Only disable/grey the row - never change the checkbox's own state,
                    // so raising Max Messages above 0 restores the prior on/off choice.
                    if (unlimited) {
                        row.classList.add('locked');
                        row.classList.remove('enabled');
                        if (cb) cb.disabled = true;
                    } else {
                        row.classList.remove('locked');
                        if (cb) cb.disabled = false;
                        toggleResp('rate_limited');  // reflect the preserved state
                    }
                    if (warn) warn.style.display = unlimited ? '' : 'none';
                }
                // Live preview for the {words} placeholder in the Invalid Format
                // response - mirrors word_rule_phrase() on the backend.
                function updateWordsPreview() {
                    var el = document.getElementById('words_preview');
                    if (!el) return;  // SMS-responses tab not parsed yet
                    var one = document.getElementById('one_word_only');
                    var two = document.getElementById('two_words_max');
                    el.textContent = (one && one.checked) ? '1 word'
                                   : (two && two.checked) ? '2 words'
                                   : '1-2 words';
                }
                // A Remote only displays names the Master pushes - everything else (texts,
                // replies, limits, filters, testing, queue, Start) is handled on the Master,
                // so hide it here to avoid confusion.
                function applyRoleVisibility(remote) {
                    var showIf = function(id, show){ var el=document.getElementById(id); if(el) el.style.display = show ? '' : 'none'; };
                    showIf('message_source_section', !remote);
                    showIf('message_settings_section', !remote);
                    showIf('master_discovery_box', !remote);
                    showIf('filters_section', !remote);
                    // Waiting + Name Display content are master-driven (the remote's names
                    // list syncs from the master), so hide BOTH content columns on a remote.
                    // Only the Overlay Model selector stays - it is this projector's own model.
                    showIf('waiting_config_col', !remote);
                    showIf('names_config_col', !remote);
                    showIf('btn_sync_pos_master', remote);   // remote-only: copy master's layout
                    // SMS Responses are Google-Voice-only, so for a non-remote the source
                    // (not just the role) decides whether the tab shows. Without this check
                    // this 5s-interval role sync would re-show the tab after updateSourceUI()
                    // hid it for Twilio - the bug where SMS Responses kept coming back.
                    var _srcGV = ((document.getElementById('message_source')||{}).value) === 'google_voice';
                    showIf('tabbtn-sms', !remote && _srcGV);
                    showIf('tabbtn-testing', !remote);
                    showIf('btn_view_queue', !remote);
                    showIf('btn_plugin_toggle', !remote);
                    // Live/not-live banners are master-only status; the remote is push-driven.
                    var lb=document.getElementById('plugin_live_banner'); if(lb && remote) lb.style.display='none';
                    var nb=document.getElementById('plugin_not_live_banner'); if(nb && remote) nb.style.display='none';
                    var note = document.getElementById('remote_mode_note');
                    if (note) note.style.display = remote ? 'block' : 'none';
                    showIf('master_sync_box', remote);   // remote-only: pick which master to follow
                    // Kick the masters auto-refresh on when switching into remote view.
                    if (remote && typeof window.startMastersAutoRefresh === 'function') window.startMastersAutoRefresh();
                    // If a now-hidden tab is active, fall back to Settings.
                    if (remote) {
                        var active = document.querySelector('.tab-content.active');
                        if (active && (active.id === 'tab-sms' || active.id === 'tab-testing')) {
                            var sbtn = document.querySelector('.tab-btn'); // first = Settings
                            if (typeof showTab === 'function' && sbtn) showTab('settings', sbtn);
                        }
                    }
                }
                window.applyRoleVisibility = applyRoleVisibility;

                // --- "Sync to Master" picker (remote only) --------------------------------
                var _mastersTimer = null;
                function _esc(s) {
                    return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
                        return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c];
                    });
                }
                function renderMasters(d) {
                    var box = document.getElementById('masters_list');
                    if (!box) return;
                    var masters = (d && d.masters) || [];
                    if (!masters.length) {
                        box.innerHTML = '<p class="help-text">No plugin masters found yet. Make sure another FPP is running this plugin in <strong>Master</strong> role on the same MultiSync network, then press Refresh.</p>';
                        return;
                    }
                    var html = '';
                    masters.forEach(function(m) {
                        var label = m.name || m.address;
                        var checked = m.selected ? ' checked' : '';
                        html += '<label style="display:flex; align-items:center; gap:10px; padding:6px 0; margin:0; font-weight:normal; cursor:pointer;">'
                             +  '<input type="checkbox" class="master_pick" data-addr="' + _esc(m.address) + '"' + checked + ' onchange="selectMaster(this)" style="width:auto; margin:0; flex-shrink:0;">'
                             +  '<span style="line-height:1.3;"><strong>' + _esc(label) + '</strong>'
                             +  '<span class="help-text" style="margin-left:8px;">' + _esc(m.address) + '</span>'
                             +  '</span>'
                             +  '</label>';
                    });
                    if (d.selected && d.selected_reachable === false) {
                        html += '<p class="help-text" style="color:#c62828;">warning: the selected master is not reachable right now.</p>';
                    }
                    box.innerHTML = html;
                }
                function loadMasters(force) {
                    fetch('/api/plugin/masters' + (force ? '?refresh=1' : ''))
                        .then(function(r){ return r.json(); })
                        .then(function(d){ if (d && d.is_remote) renderMasters(d); })
                        .catch(function(){});
                }
                window.loadMasters = loadMasters;
                function selectMaster(cb) {
                    // Single-select: unchecking the current one clears the pin (auto / any master).
                    var picks = document.querySelectorAll('.master_pick');
                    picks.forEach(function(p){ if (p !== cb) p.checked = false; });
                    var address = cb.checked ? (cb.getAttribute('data-addr') || '') : '';
                    fetch('/api/plugin/select-master', {
                        method: 'POST', headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({address: address})
                    }).then(function(){
                        loadMasters(false);
                        // Refresh the background-preview hint now that the master pin changed
                        // (content resyncs server-side); a short delay lets the re-mirror land.
                        setTimeout(function(){ if (window.toggleFseqPreview) window.toggleFseqPreview(); }, 800);
                    }).catch(function(){});
                }
                window.selectMaster = selectMaster;
                function refreshMasters(btn) {
                    if (btn) {
                        btn.disabled = true; var t = btn.textContent; btn.textContent = '...';
                        setTimeout(function(){ btn.disabled = false; btn.textContent = t; }, 1200);
                    }
                    loadMasters(true);
                }
                window.refreshMasters = refreshMasters;
                function startMastersAutoRefresh() {
                    if (_mastersTimer) return;
                    loadMasters(false);
                    _mastersTimer = setInterval(function(){ loadMasters(false); }, 10000);
                }
                window.startMastersAutoRefresh = startMastersAutoRefresh;

                function onRoleChange() {
                    var sel = document.getElementById('plugin_role');
                    var remote = sel && sel.value === 'remote';
                    window._roleManualUntil = Date.now() + 4000;  // let the save land before reconcileRole re-reads
                    applyRoleVisibility(remote);
                    // Persist the role EXPLICITLY and ONLY from this user action, so editing
                    // other settings can never convert auto ('') into an explicit role.
                    fetch('/api/config', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ plugin_role: (sel && sel.value) || '' })
                    }).catch(function(){});
                }
                window.onRoleChange = onRoleChange;
                // Apply role visibility on first paint (reconcileRole() below keeps it live).
                (function(){
                    var sel = document.getElementById('plugin_role');
                    applyRoleVisibility(sel && sel.value === 'remote');
                })();
                updateFormatRules();
                checkFiltersState();
                checkDuplicateState();
                updateWordsPreview();
            </script>

            <!-- Backup & Restore -->
            <div class="section">
                <h2>💾 Backup &amp; Restore</h2>
                <p class="help-text" style="margin-bottom:12px;">Export all plugin settings, the content it uses (playlists, sequences, images), and the overlay model into one file - then import it on another Pi to reproduce this setup exactly. <strong>Credentials are not included</strong>; re-enter them after importing. <a id="backup_help_link" href="#" target="_top">Learn more</a></p>
                <div id="backup_actions" style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
                    <button type="button" class="test-btn" onclick="openExportModal()"
                       style="background:#4CAF50;">⬇️ Export Config</button>
                    <input type="file" id="import_file" accept=".zip,application/zip" style="display:none;" onchange="importConfig(this)">
                    <button type="button" class="test-btn" onclick="document.getElementById('import_file').click()">⬆️ Import Config</button>
                    <span id="import_status" style="font-size:13px;"></span>
                </div>
                <label style="display:block; margin-top:10px; font-size:13px; font-weight:normal;">
                    <input type="checkbox" id="import_mode_cb" style="width:auto; margin:0 8px 0 0; vertical-align:middle;">
                    Also import the <strong>Master/Remote mode</strong> from the bundle.
                    <span class="help-text" style="font-weight:normal;">Off by default, so importing keeps THIS Pi's role (and which master a remote follows). Tick only if you want the bundle's role to replace it.</span>
                </label>
            </div>

            <!-- Local export modal - only used when the page is opened directly
                 (not inside the FPP iframe). In the normal framed case the modal is
                 owned by the parent (ui.php) so it is a true fixed, centred overlay
                 that ignores scrolling. Here position:fixed works because a directly
                 opened page is a normal scrolling document. -->
            <div id="export_modal" onclick="if(event.target===this)closeExportModal()" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:100000; align-items:center; justify-content:center;">
                <div id="export_dialog" style="background:#fff; color:#333; max-width:460px; width:92%; border-radius:8px; padding:22px; box-shadow:0 8px 30px rgba(0,0,0,0.35); max-height:88vh; overflow-y:auto; box-sizing:border-box;">
                    <h3 style="margin:0 0 6px; color:#333;">Export Config</h3>
                    <p class="help-text" style="margin:0 0 14px;">Choose what to include. Only the content <strong>this plugin is set to use</strong> is exported - never all of FPP's files. <strong>Credentials are never included.</strong></p>
                    <label style="display:flex; gap:10px; align-items:flex-start; font-weight:normal; margin:0 0 12px;">
                        <input type="checkbox" id="exp_settings" checked style="width:auto; margin:3px 0 0;">
                        <span><strong>Plugin settings</strong><br><span class="help-text">Display lines, message rules, response text, filters, poll interval, selected content &amp; overlay model.</span></span>
                    </label>
                    <label style="display:flex; gap:10px; align-items:flex-start; font-weight:normal; margin:0 0 12px;">
                        <input type="checkbox" id="exp_lists" checked style="width:auto; margin:3px 0 0;">
                        <span><strong>Blocked numbers &amp; word lists</strong><br><span class="help-text">Blocked phone numbers and your whitelist / blacklist words.</span></span>
                    </label>
                    <label style="display:flex; gap:10px; align-items:flex-start; font-weight:normal; margin:0 0 12px;">
                        <input type="checkbox" id="exp_content" checked style="width:auto; margin:3px 0 0;">
                        <span><strong>Content files</strong><br><span class="help-text">The Waiting &amp; Name Display sequences, images, and videos this plugin uses - copied file-for-file. Can be large.</span></span>
                    </label>
                    <label style="display:flex; gap:10px; align-items:flex-start; font-weight:normal; margin:0 0 18px;">
                        <input type="checkbox" id="exp_overlay" checked style="width:auto; margin:3px 0 0;">
                        <span><strong>Overlay model (matrix)</strong><br><span class="help-text">The FPP Pixel Overlay Model the names are drawn onto.</span></span>
                    </label>
                    <div style="display:flex; gap:10px; justify-content:flex-end;">
                        <button type="button" id="exp_cancel" onclick="closeExportModal()" style="background:#9e9e9e;">Cancel</button>
                        <button type="button" id="exp_go" class="test-btn" style="background:#4CAF50; min-width:110px;" onclick="doExport()">Export</button>
                    </div>
                </div>
            </div>

            <!-- Backup & Restore JS lives in its OWN script block (never merged with
                 the settings-init functions above) so that any problem here can never
                 prevent the credential-block toggle / settings init from running.
                 Strings are plain ASCII to avoid any encoding edge cases. -->
            <script>
                // Point the "Learn more" link at FPP's own web server (default port),
                // not this plugin service (:5000) where the page actually runs - a
                // relative plugin.php link would 404 against :5000.
                (function() {
                    var hl = document.getElementById('backup_help_link');
                    if (hl) hl.href = window.location.protocol + '//' + window.location.hostname
                        + '/plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=help.php#backup';
                })();

                // Fetch the export (same-origin, so the auth cookie is sent), then
                // save it. Using fetch (not a bare anchor) lets us know when the file
                // has fully downloaded and surface server errors. Returns a Promise.
                function _tmlDownload(sel) {
                    var url = '/api/config/export?settings=' + (sel.s ? 1 : 0)
                            + '&lists=' + (sel.l ? 1 : 0) + '&content=' + (sel.c ? 1 : 0)
                            + '&overlay=' + (sel.o ? 1 : 0);
                    return fetch(url).then(function(r) {
                        if (!r.ok) throw new Error('Server returned ' + r.status);
                        var cd = r.headers.get('Content-Disposition') || '';
                        var m = /filename="?([^";]+)"?/.exec(cd);
                        var name = (m && m[1]) ? m[1] : 'textmylights-config.zip';
                        return r.blob().then(function(b) { return { blob: b, name: name }; });
                    }).then(function(o) {
                        var u = URL.createObjectURL(o.blob);
                        var a = document.createElement('a');
                        a.href = u; a.download = o.name;
                        document.body.appendChild(a); a.click(); a.remove();
                        setTimeout(function() { URL.revokeObjectURL(u); }, 2000);
                    });
                }
                // The parent-owned modal sends the chosen sections here to download,
                // and waits for tml_exportDone before it closes.
                window.addEventListener('message', function(e) {
                    if (e.data && e.data.type === 'tml_export' && e.data.sel) {
                        _tmlDownload(e.data.sel)
                            .then(function() { try { window.parent.postMessage({ type: 'tml_exportDone', success: true }, '*'); } catch (x) {} })
                            .catch(function(err) { try { window.parent.postMessage({ type: 'tml_exportDone', success: false, error: String((err && err.message) || err) }, '*'); } catch (x) {} });
                    }
                });
                window.openExportModal = function() {
                    // Framed (normal case): let the parent show a true fixed, centred
                    // overlay that ignores scrolling. Fall back to the local modal only
                    // when the page is opened directly (not inside the FPP iframe).
                    if (window.parent !== window) {
                        try { window.parent.postMessage({ type: 'tml_openExport' }, '*'); return; } catch (e) {}
                    }
                    document.getElementById('exp_go').disabled = false;
                    document.getElementById('exp_go').textContent = 'Export';
                    document.getElementById('export_modal').style.display = 'flex';
                };
                window.closeExportModal = function() {
                    if (window._tmlExporting) return;   // don't close mid-download
                    document.getElementById('export_modal').style.display = 'none';
                };
                window.doExport = function() {
                    if (window._tmlExporting) return;
                    var sel = {
                        s: document.getElementById('exp_settings').checked,
                        l: document.getElementById('exp_lists').checked,
                        c: document.getElementById('exp_content').checked,
                        o: document.getElementById('exp_overlay').checked
                    };
                    if (!sel.s && !sel.l && !sel.c && !sel.o) { alert('Select at least one thing to export.'); return; }
                    var go = document.getElementById('exp_go');
                    window._tmlExporting = true;
                    go.disabled = true; go.textContent = 'Exporting...';
                    document.getElementById('exp_cancel').disabled = true;
                    _tmlDownload(sel).then(function() {
                        go.textContent = 'Downloaded';
                        window._tmlExporting = false;
                        setTimeout(function() {
                            closeExportModal();
                            go.disabled = false; go.textContent = 'Export';
                            document.getElementById('exp_cancel').disabled = false;
                        }, 900);
                    }).catch(function(err) {
                        window._tmlExporting = false;
                        go.disabled = false; go.textContent = 'Export';
                        document.getElementById('exp_cancel').disabled = false;
                        alert('Export failed: ' + ((err && err.message) || err));
                    });
                };
                // Holds the file the user picked, until they confirm the import in
                // the (parent-owned) modal.
                var _tmlImportFile = null;

                // POST the chosen bundle to the import endpoint (same-origin, cookie
                // auth). Returns a Promise resolving to the server's JSON result.
                function _tmlDoImport(file) {
                    var fd = new FormData();
                    fd.append('file', file);
                    var modeCb = document.getElementById('import_mode_cb');
                    fd.append('import_mode', (modeCb && modeCb.checked) ? '1' : '0');
                    return fetch('/api/config/import', { method: 'POST', body: fd })
                        .then(function(r) { return r.json(); });
                }

                window.importConfig = function(input) {
                    var file = input.files && input.files[0];
                    input.value = '';                 // let the same file be re-picked later
                    if (!file) return;
                    _tmlImportFile = file;
                    // Framed (normal case): let the parent show a fixed, centred confirm
                    // modal and drive the progress UI.
                    if (window.parent !== window) {
                        try { window.parent.postMessage({ type: 'tml_openImport', name: file.name }, '*'); return; } catch (e) {}
                    }
                    // Fallback (page opened directly): confirm + import inline.
                    if (!confirm('Import configuration from "' + file.name + '"? '
                        + 'This overwrites the plugin settings, block/whitelist, the referenced '
                        + 'content files, and the overlay model on THIS Pi. Your saved credentials '
                        + 'are kept. Continue?')) { _tmlImportFile = null; return; }
                    var status = document.getElementById('import_status');
                    status.style.color = '#555'; status.textContent = 'Importing...';
                    _tmlDoImport(file).then(function(d) {
                        if (d.success) {
                            status.style.color = '#2e7d32';
                            status.textContent = 'Imported. Reloading...';
                            setTimeout(function() { location.reload(); }, 1400);
                        } else {
                            status.style.color = '#c62828';
                            status.textContent = 'Error: ' + (d.error || 'Import failed');
                        }
                    }).catch(function() {
                        status.style.color = '#c62828';
                        status.textContent = 'Import request failed.';
                    });
                };

                // Parent modal drives the framed import: it asks us to run it, waits
                // for tml_importDone, then reloads this iframe on success.
                window.addEventListener('message', function(e) {
                    if (!e.data) return;
                    if (e.data.type === 'tml_doImport' && _tmlImportFile) {
                        var f = _tmlImportFile; _tmlImportFile = null;
                        _tmlDoImport(f).then(function(d) {
                            try { window.parent.postMessage({ type: 'tml_importDone',
                                success: !!d.success, error: d.error || '',
                                warnings: (d.warnings || []).length, note: d.note || '' }, '*'); } catch (x) {}
                        }).catch(function(err) {
                            try { window.parent.postMessage({ type: 'tml_importDone', success: false,
                                error: String((err && err.message) || err) }, '*'); } catch (x) {}
                        });
                    }
                    if (e.data.type === 'tml_cancelImport') { _tmlImportFile = null; }
                    if (e.data.type === 'tml_reloadFrame') { location.reload(); }
                });
            </script>

        </div>

        <!-- Display Settings Tab -->
        <div id="tab-display" class="tab-content">
            <div class="columns">

                <!-- LEFT COLUMN: Message Lines editor -->
                <div class="column">
                    <div class="section">
                        <h2>Message Lines</h2>

                        <label style="font-size:11px; color:#888; font-weight:bold;">Use {name} as placeholder for texts in any line. Empty lines are skipped.</label>
                        <style>
                            .line-card { background:#3a3a3a; border:1px solid #555; border-radius:5px; padding:8px 8px 6px; margin-bottom:6px; }
                            .line-row { display:flex; align-items:center; gap:6px; }
                            .line-row input[type="text"] { margin-bottom:0; padding:6px; }
                            .line-label { width:46px; font-size:12px; color:#aaa; flex-shrink:0; }
                            .pos-badge { font-size:11px; color:#888; white-space:nowrap; min-width:80px; text-align:right; font-family:monospace; }
                            .reset-line-btn { background:#444; border:none; color:#ccc; padding:2px 7px; font-size:12px; border-radius:3px; cursor:pointer; flex-shrink:0; }
                            .reset-line-btn:hover { background:#666; }
                            .line-color-group { position:relative; display:flex; align-items:center; flex-shrink:0; }
                            .line-color-swatch { width:24px; height:24px; padding:0; border:1px solid #666; border-radius:4px 0 0 4px; cursor:pointer; background:none; }
                            .color-palette-btn { width:16px; height:24px; padding:0; border:1px solid #666; border-left:none; border-radius:0 4px 4px 0; background:#444; color:#ccc; font-size:9px; cursor:pointer; }
                            .color-palette-btn:hover { background:#666; }
                            .color-palette-popover { position:absolute; top:28px; right:0; z-index:50; background:#2a2a2a; border:1px solid #666; border-radius:5px; padding:8px; width:140px; box-shadow:0 4px 12px rgba(0,0,0,0.5); }
                            .color-palette-swatches { display:flex; flex-wrap:wrap; gap:4px; }
                            .color-palette-swatch { width:20px; height:20px; border:1px solid #666; border-radius:3px; padding:0; cursor:pointer; }
                            .color-palette-save-btn { margin-top:6px; width:100%; font-size:11px; background:#444; color:#ccc; border:1px dashed #888; border-radius:3px; padding:4px; cursor:pointer; }
                            .color-palette-empty { font-size:10px; color:#888; text-align:center; padding:4px 0; }
                            .line-movement-row { margin-top:5px; padding-top:5px; border-top:1px solid #555; }
                            .line-mini-label { font-weight:normal; font-size:12px; color:#aaa; flex-shrink:0; }
                            .line-group-controls { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
                            .line-group-controls select { width:auto; margin-bottom:0; padding:6px; flex:1; min-width:160px; }
                            .line-speed-row { display:flex; align-items:center; gap:6px; }
                            .line-speed-row label { margin:0; font-weight:normal; font-size:12px; color:#aaa; }
                            .line-speed-row input[type="number"] { width:56px; margin-bottom:0; padding:6px; }
                            .line-speed-auto { display:inline-flex; align-items:center; gap:4px; cursor:pointer; white-space:nowrap; }
                            .line-speed-auto input[type="checkbox"] { width:auto; margin:0; cursor:pointer; }
                            .line-speed-sub { display:inline-flex; align-items:center; gap:6px; }
                        </style>
                        {# The editor renders the ACTIVE names-content item (item 0 when a
                           list exists) or the flat config when the list is empty. JS handles
                           switching to other items after fonts have loaded. #}
                        {% set _ncl = config.get('names_content_list') or [] %}
                        {% set _active = _ncl[0] if _ncl else config %}
                        {% set ml = _active.get('message_lines') or ['Merry Christmas', '{name}!', '', ''] %}
                        {% set lc = _active.get('line_colors') or ['', '', '', ''] %}
                        {% set lm = _active.get('line_movements') or ['Center', 'Center', 'Center', 'Center'] %}
                        {% set ls = _active.get('line_speeds') or [50, 50, 50, 50] %}
                        {# Per-line speed value with a safe fallback. speed <= 0 encodes
                           fit-to-time: 0/-1 = 1 pass, -N = N passes. #}
                        {% set s0 = ls[0] if ls|length > 0 else 50 %}
                        {% set s1 = ls[1] if ls|length > 1 else 50 %}
                        {% set s2 = ls[2] if ls|length > 2 else 50 %}
                        {% set s3 = ls[3] if ls|length > 3 else 50 %}
                        {% set lf = _active.get('line_fonts') or ['FreeSans', 'FreeSans', 'FreeSans', 'FreeSans'] %}
                        {% set lo = _active.get('line_orientations') or ['horizontal', 'horizontal', 'horizontal', 'horizontal'] %}
                        <div id="message_lines_section">
                            <div class="line-card">
                                <div class="line-row">
                                    <span class="line-label">Line 1:</span>
                                    <input type="text" id="line_1" value="{{ ml[0] if ml|length > 0 else 'Merry Christmas' }}" placeholder="e.g. Merry Christmas" style="flex:1;" onblur="saveConfig()">
                                    <div class="line-color-group">
                                        <input type="color" id="line_1_color" class="line-color-swatch" value="{{ lc[0] if lc|length > 0 and lc[0] else '#FF0000' }}" title="Line 1 color" onchange="onLineColorChange(0)">
                                        <button type="button" class="color-palette-btn" onclick="toggleColorPalette(0)" title="Saved colors">▾</button>
                                        <div id="line_1_palette_popover" class="color-palette-popover" style="display:none;"></div>
                                    </div>
                                    <span id="line_1_pos" class="pos-badge">auto</span>
                                    <button type="button" class="reset-line-btn" onclick="resetLine(0)" title="Reset to auto-center">✕</button>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Move:</span>
                                        <select id="line_1_movement" onchange="onLineMovementChange(0)">
                                            <option value="Center" {{ 'selected' if lm[0] == 'Center' else '' }}>Static</option>
                                            <option value="L2R" {{ 'selected' if lm[0] == 'L2R' else '' }}>Scroll Left to Right</option>
                                            <option value="R2L" {{ 'selected' if lm[0] == 'R2L' else '' }}>Scroll Right to Left</option>
                                            <option value="T2B" {{ 'selected' if lm[0] == 'T2B' else '' }}>Scroll Top to Bottom</option>
                                            <option value="B2T" {{ 'selected' if lm[0] == 'B2T' else '' }}>Scroll Bottom to Top</option>
                                        </select>
                                        <div id="line_1_speed_row" class="line-speed-row" style="{{ '' if lm[0] != 'Center' else 'display:none;' }}">
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration - the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_1_speed_auto" {{ 'checked' if s0 <= 0 else '' }} onchange="onLineSpeedAutoChange(0)"> Fit to time</label>
                                            <span id="line_1_speed_wrap" class="line-speed-sub" style="{{ 'display:none;' if s0 <= 0 else '' }}">
                                                <label>Speed:</label>
                                                <input type="number" id="line_1_speed" min="1" max="100" step="1" value="{{ s0 if s0 > 0 else 50 }}" onchange="onLineSpeedChange(0)">
                                            </span>
                                            <span id="line_1_passes_wrap" class="line-speed-sub" style="{{ '' if s0 <= 0 else 'display:none;' }}">
                                                <label>Times:</label>
                                                <input type="number" id="line_1_passes" min="1" max="20" step="1" value="{{ (-s0) if s0 < 0 else 1 }}" onchange="onLinePassesChange(0)">
                                            </span>
                                        </div>
                                        <span id="line_1_orientation_row" style="display:{{ 'inline-flex' if lm[0] == 'Center' else 'none' }}; align-items:center; gap:6px;">
                                            <span class="line-mini-label">Style:</span>
                                            <select id="line_1_orientation" onchange="onLineOrientationChange(0)">
                                                <option value="horizontal" {{ 'selected' if lo[0] == 'horizontal' else '' }}>Horizontal</option>
                                                <option value="vertical_rotated" {{ 'selected' if lo[0] == 'vertical_rotated' else '' }}>Vertical (Rotated)</option>
                                                <option value="vertical_stacked" {{ 'selected' if lo[0] == 'vertical_stacked' else '' }}>Vertical (Stacked)</option>
                                            </select>
                                        </span>
                                    </div>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Font:</span>
                                        <select id="line_1_font" onchange="onLineFontChange(0)">
                                            <option value="">Loading fonts...</option>
                                        </select>
                                    </div>
                                </div>
                            </div>
                            <div class="line-card">
                                <div class="line-row">
                                    <span class="line-label">Line 2:</span>
                                    <input type="text" id="line_2" value="{{ ml[1] if ml|length > 1 else '{name}!' }}" style="flex:1;" onblur="saveConfig()">
                                    <div class="line-color-group">
                                        <input type="color" id="line_2_color" class="line-color-swatch" value="{{ lc[1] if lc|length > 1 and lc[1] else '#FF0000' }}" title="Line 2 color" onchange="onLineColorChange(1)">
                                        <button type="button" class="color-palette-btn" onclick="toggleColorPalette(1)" title="Saved colors">▾</button>
                                        <div id="line_2_palette_popover" class="color-palette-popover" style="display:none;"></div>
                                    </div>
                                    <span id="line_2_pos" class="pos-badge">auto</span>
                                    <button type="button" class="reset-line-btn" onclick="resetLine(1)" title="Reset to auto-center">✕</button>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Move:</span>
                                        <select id="line_2_movement" onchange="onLineMovementChange(1)">
                                            <option value="Center" {{ 'selected' if lm[1] == 'Center' else '' }}>Static</option>
                                            <option value="L2R" {{ 'selected' if lm[1] == 'L2R' else '' }}>Scroll Left to Right</option>
                                            <option value="R2L" {{ 'selected' if lm[1] == 'R2L' else '' }}>Scroll Right to Left</option>
                                            <option value="T2B" {{ 'selected' if lm[1] == 'T2B' else '' }}>Scroll Top to Bottom</option>
                                            <option value="B2T" {{ 'selected' if lm[1] == 'B2T' else '' }}>Scroll Bottom to Top</option>
                                        </select>
                                        <div id="line_2_speed_row" class="line-speed-row" style="{{ '' if lm[1] != 'Center' else 'display:none;' }}">
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration - the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_2_speed_auto" {{ 'checked' if s1 <= 0 else '' }} onchange="onLineSpeedAutoChange(1)"> Fit to time</label>
                                            <span id="line_2_speed_wrap" class="line-speed-sub" style="{{ 'display:none;' if s1 <= 0 else '' }}">
                                                <label>Speed:</label>
                                                <input type="number" id="line_2_speed" min="1" max="100" step="1" value="{{ s1 if s1 > 0 else 50 }}" onchange="onLineSpeedChange(1)">
                                            </span>
                                            <span id="line_2_passes_wrap" class="line-speed-sub" style="{{ '' if s1 <= 0 else 'display:none;' }}">
                                                <label>Times:</label>
                                                <input type="number" id="line_2_passes" min="1" max="20" step="1" value="{{ (-s1) if s1 < 0 else 1 }}" onchange="onLinePassesChange(1)">
                                            </span>
                                        </div>
                                        <span id="line_2_orientation_row" style="display:{{ 'inline-flex' if lm[1] == 'Center' else 'none' }}; align-items:center; gap:6px;">
                                            <span class="line-mini-label">Style:</span>
                                            <select id="line_2_orientation" onchange="onLineOrientationChange(1)">
                                                <option value="horizontal" {{ 'selected' if lo[1] == 'horizontal' else '' }}>Horizontal</option>
                                                <option value="vertical_rotated" {{ 'selected' if lo[1] == 'vertical_rotated' else '' }}>Vertical (Rotated)</option>
                                                <option value="vertical_stacked" {{ 'selected' if lo[1] == 'vertical_stacked' else '' }}>Vertical (Stacked)</option>
                                            </select>
                                        </span>
                                    </div>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Font:</span>
                                        <select id="line_2_font" onchange="onLineFontChange(1)">
                                            <option value="">Loading fonts...</option>
                                        </select>
                                    </div>
                                </div>
                            </div>
                            <div class="line-card">
                                <div class="line-row">
                                    <span class="line-label">Line 3:</span>
                                    <input type="text" id="line_3" value="{{ ml[2] if ml|length > 2 else '' }}" placeholder="" style="flex:1;" onblur="saveConfig()">
                                    <div class="line-color-group">
                                        <input type="color" id="line_3_color" class="line-color-swatch" value="{{ lc[2] if lc|length > 2 and lc[2] else '#FF0000' }}" title="Line 3 color" onchange="onLineColorChange(2)">
                                        <button type="button" class="color-palette-btn" onclick="toggleColorPalette(2)" title="Saved colors">▾</button>
                                        <div id="line_3_palette_popover" class="color-palette-popover" style="display:none;"></div>
                                    </div>
                                    <span id="line_3_pos" class="pos-badge">auto</span>
                                    <button type="button" class="reset-line-btn" onclick="resetLine(2)" title="Reset to auto-center">✕</button>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Move:</span>
                                        <select id="line_3_movement" onchange="onLineMovementChange(2)">
                                            <option value="Center" {{ 'selected' if lm[2] == 'Center' else '' }}>Static</option>
                                            <option value="L2R" {{ 'selected' if lm[2] == 'L2R' else '' }}>Scroll Left to Right</option>
                                            <option value="R2L" {{ 'selected' if lm[2] == 'R2L' else '' }}>Scroll Right to Left</option>
                                            <option value="T2B" {{ 'selected' if lm[2] == 'T2B' else '' }}>Scroll Top to Bottom</option>
                                            <option value="B2T" {{ 'selected' if lm[2] == 'B2T' else '' }}>Scroll Bottom to Top</option>
                                        </select>
                                        <div id="line_3_speed_row" class="line-speed-row" style="{{ '' if lm[2] != 'Center' else 'display:none;' }}">
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration - the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_3_speed_auto" {{ 'checked' if s2 <= 0 else '' }} onchange="onLineSpeedAutoChange(2)"> Fit to time</label>
                                            <span id="line_3_speed_wrap" class="line-speed-sub" style="{{ 'display:none;' if s2 <= 0 else '' }}">
                                                <label>Speed:</label>
                                                <input type="number" id="line_3_speed" min="1" max="100" step="1" value="{{ s2 if s2 > 0 else 50 }}" onchange="onLineSpeedChange(2)">
                                            </span>
                                            <span id="line_3_passes_wrap" class="line-speed-sub" style="{{ '' if s2 <= 0 else 'display:none;' }}">
                                                <label>Times:</label>
                                                <input type="number" id="line_3_passes" min="1" max="20" step="1" value="{{ (-s2) if s2 < 0 else 1 }}" onchange="onLinePassesChange(2)">
                                            </span>
                                        </div>
                                        <span id="line_3_orientation_row" style="display:{{ 'inline-flex' if lm[2] == 'Center' else 'none' }}; align-items:center; gap:6px;">
                                            <span class="line-mini-label">Style:</span>
                                            <select id="line_3_orientation" onchange="onLineOrientationChange(2)">
                                                <option value="horizontal" {{ 'selected' if lo[2] == 'horizontal' else '' }}>Horizontal</option>
                                                <option value="vertical_rotated" {{ 'selected' if lo[2] == 'vertical_rotated' else '' }}>Vertical (Rotated)</option>
                                                <option value="vertical_stacked" {{ 'selected' if lo[2] == 'vertical_stacked' else '' }}>Vertical (Stacked)</option>
                                            </select>
                                        </span>
                                    </div>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Font:</span>
                                        <select id="line_3_font" onchange="onLineFontChange(2)">
                                            <option value="">Loading fonts...</option>
                                        </select>
                                    </div>
                                </div>
                            </div>
                            <div class="line-card">
                                <div class="line-row">
                                    <span class="line-label">Line 4:</span>
                                    <input type="text" id="line_4" value="{{ ml[3] if ml|length > 3 else '' }}" placeholder="" style="flex:1;" onblur="saveConfig()">
                                    <div class="line-color-group">
                                        <input type="color" id="line_4_color" class="line-color-swatch" value="{{ lc[3] if lc|length > 3 and lc[3] else '#FF0000' }}" title="Line 4 color" onchange="onLineColorChange(3)">
                                        <button type="button" class="color-palette-btn" onclick="toggleColorPalette(3)" title="Saved colors">▾</button>
                                        <div id="line_4_palette_popover" class="color-palette-popover" style="display:none;"></div>
                                    </div>
                                    <span id="line_4_pos" class="pos-badge">auto</span>
                                    <button type="button" class="reset-line-btn" onclick="resetLine(3)" title="Reset to auto-center">✕</button>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Move:</span>
                                        <select id="line_4_movement" onchange="onLineMovementChange(3)">
                                            <option value="Center" {{ 'selected' if lm[3] == 'Center' else '' }}>Static</option>
                                            <option value="L2R" {{ 'selected' if lm[3] == 'L2R' else '' }}>Scroll Left to Right</option>
                                            <option value="R2L" {{ 'selected' if lm[3] == 'R2L' else '' }}>Scroll Right to Left</option>
                                            <option value="T2B" {{ 'selected' if lm[3] == 'T2B' else '' }}>Scroll Top to Bottom</option>
                                            <option value="B2T" {{ 'selected' if lm[3] == 'B2T' else '' }}>Scroll Bottom to Top</option>
                                        </select>
                                        <div id="line_4_speed_row" class="line-speed-row" style="{{ '' if lm[3] != 'Center' else 'display:none;' }}">
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration - the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_4_speed_auto" {{ 'checked' if s3 <= 0 else '' }} onchange="onLineSpeedAutoChange(3)"> Fit to time</label>
                                            <span id="line_4_speed_wrap" class="line-speed-sub" style="{{ 'display:none;' if s3 <= 0 else '' }}">
                                                <label>Speed:</label>
                                                <input type="number" id="line_4_speed" min="1" max="100" step="1" value="{{ s3 if s3 > 0 else 50 }}" onchange="onLineSpeedChange(3)">
                                            </span>
                                            <span id="line_4_passes_wrap" class="line-speed-sub" style="{{ '' if s3 <= 0 else 'display:none;' }}">
                                                <label>Times:</label>
                                                <input type="number" id="line_4_passes" min="1" max="20" step="1" value="{{ (-s3) if s3 < 0 else 1 }}" onchange="onLinePassesChange(3)">
                                            </span>
                                        </div>
                                        <span id="line_4_orientation_row" style="display:{{ 'inline-flex' if lm[3] == 'Center' else 'none' }}; align-items:center; gap:6px;">
                                            <span class="line-mini-label">Style:</span>
                                            <select id="line_4_orientation" onchange="onLineOrientationChange(3)">
                                                <option value="horizontal" {{ 'selected' if lo[3] == 'horizontal' else '' }}>Horizontal</option>
                                                <option value="vertical_rotated" {{ 'selected' if lo[3] == 'vertical_rotated' else '' }}>Vertical (Rotated)</option>
                                                <option value="vertical_stacked" {{ 'selected' if lo[3] == 'vertical_stacked' else '' }}>Vertical (Stacked)</option>
                                            </select>
                                        </span>
                                    </div>
                                </div>
                                <div class="line-movement-row">
                                    <div class="line-group-controls">
                                        <span class="line-mini-label">Font:</span>
                                        <select id="line_4_font" onchange="onLineFontChange(3)">
                                            <option value="">Loading fonts...</option>
                                        </select>
                                    </div>
                                </div>
                            </div>
                        </div>
                        <p class="help-text">🎨 Click the ▾ next to a line's color to save or recall colors. Each card's Movement controls that line only.</p>
                    </div>
                </div>

                <!-- RIGHT COLUMN: Live Preview -->
                <div class="column">
                    <div class="section">
                        <h2>Preview</h2>

                        <!-- Canvas: per-line drag in static mode; block preview in scroll modes -->
                        <div id="canvas_section">
                            <p id="canvas_hint" style="font-weight:bold; font-size:13px; color:#4fc3f7; margin:4px 0 8px;">🖱️ Click a line to select it, then drag inside its box to move it, or drag an edge/corner to resize. Text auto-sizes to fill the box - the box is the MAX size text can be.</p>
                            <p class="help-text" style="margin:-4px 0 8px;">↔️ For scrolling text (Left/Right/Top/Bottom movement), the box is also where the text is allowed to show - it enters and exits at the box's own edges, not the display's, and always starts fully off-page before scrolling in.</p>
                            <canvas id="matrix_canvas" style="width:100%; display:block; background:#000; border:2px solid #555; border-radius:4px; cursor:default;"></canvas>
                            <div style="display:flex; gap:8px; margin-top:6px; align-items:center;">
                                <button type="button" onclick="resetAllLines()" style="background:#555; padding:6px 12px; font-size:12px;">Reset All to Center</button>
                                <button type="button" id="btn_sync_pos_master" onclick="syncPositionFromMaster(this)" style="display:none; background:#1976d2; color:#fff; padding:6px 12px; font-size:12px;" title="Copy the Master's text layout for this content, scaled to this projector's model">🔗 Sync Position from Master</button>
                                <span id="pos_display" style="font-size:12px; color:#888;"></span>
                                <span id="sync_pos_status" style="font-size:12px; color:#888;"></span>
                            </div>

                            <div style="margin-top:10px; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                                <label style="margin:0;">Display Duration (seconds):</label>
                                <input type="number" id="content_duration" min="1" max="600" style="width:90px; margin:0;" onchange="onContentDurationChange()">
                                <span class="help-text" id="content_duration_scope" style="margin:0;"></span>
                            </div>
                            <p class="help-text" style="margin:6px 0 0;">💡 Scrolling lines set to "Fit to time" use this as their scroll window.</p>

                            <!-- Canvas background preview (FSEQ / video / image) -->
                            <div style="margin-top:10px; padding:10px; background:#616161; border:1px solid #777; border-radius:4px;">
                                <span style="font-size:13px; font-weight:bold; color:#eee;">Background Preview</span>
                                <span id="fseq_scrub_hint" style="font-weight:normal; font-size:11px; color:#bbb; margin-left:6px;">Use scroll bar to move preview.</span>
                                <div id="fseq_preview_controls" style="margin-top:8px;">

                                    <!-- Per-content editor: pick which Names content's text you are
                                         arranging/previewing. Shown only when >1 content is configured. -->
                                    <div id="preview_content_row" style="display:none; margin-bottom:8px; padding:8px; background:#3a3a3a; border:1px solid #555; border-radius:4px;">
                                        <select id="preview_content_select" onchange="onPreviewContentChange()" style="width:100%;"></select>
                                    </div>

                                    <div id="fseq_scrubber_row" style="display:none;">
                                        <div style="display:flex; align-items:center; gap:8px;">
                                            <span id="fseq_time_display" style="font-size:12px; color:#aaa; min-width:85px; white-space:nowrap;">0:00 / 0:00</span>
                                            <input type="range" id="fseq_scrubber" min="0" max="100" value="0" step="1"
                                                   style="flex:1;" oninput="fseqScrub(this.value)">
                                            <button type="button" onclick="clearFseqPreview()" style="padding:4px 8px; font-size:11px; background:#555; color:#fff; border:none; border-radius:3px; cursor:pointer;">Clear</button>
                                        </div>
                                        <div id="fseq_status" style="font-size:11px; color:#888; margin-top:4px; min-height:16px;"></div>
                                    </div>
                                    <div id="fseq_load_status" style="font-size:11px; color:#888; margin-top:4px; min-height:16px;"></div>
                                </div>
                            </div>
                        </div>

                        <input type="hidden" id="overlay_model_width" value="{{ config.get('overlay_model_width', 0) }}">
                        <input type="hidden" id="overlay_model_height" value="{{ config.get('overlay_model_height', 0) }}">
                        <script>
                            {% set _ncl2 = config.get('names_content_list') or [] %}
                            {% set _active2 = _ncl2[0] if _ncl2 else config %}
                            window._lineBoxesInit = {{ (_active2.get('line_boxes') or [{'x':-1,'y':-1,'w':300,'h':60},{'x':-1,'y':-1,'w':300,'h':60},{'x':-1,'y':-1,'w':300,'h':60},{'x':-1,'y':-1,'w':300,'h':60}]) | tojson }};
                            window._lineMovementsInit = {{ (_active2.get('line_movements') or ['Center', 'Center', 'Center', 'Center']) | tojson }};
                            window._lineSpeedsInit = {{ (_active2.get('line_speeds') or [50, 50, 50, 50]) | tojson }};
                            window._lineFontsInit = {{ (_active2.get('line_fonts') or ['FreeSans', 'FreeSans', 'FreeSans', 'FreeSans']) | tojson }};
                            window._lineOrientationsInit = {{ (_active2.get('line_orientations') or ['horizontal', 'horizontal', 'horizontal', 'horizontal']) | tojson }};
                            window._customColorsInit = {{ config.get('custom_colors', []) | tojson }};
                            window._namesContentListInit = {{ (config.get('names_content_list') or []) | tojson }};
                            window._namesContentModeInit = {{ config.get('names_content_mode', 'roundrobin') | tojson }};
                            window._flatNameContentInit = {{ config.get('name_display_playlist', '') | tojson }};
                            window._flatDisplayDurationInit = {{ config.get('display_duration', 30) | tojson }};
                            window._waitingContentListInit = {{ (config.get('default_content_list') or []) | tojson }};
                            window._waitingContentModeInit = {{ config.get('default_content_mode', 'roundrobin') | tojson }};
                            window._flatDefaultContentInit = {{ config.get('default_playlist', '') | tojson }};
                        </script>
                    </div>
                </div>

            </div>
        </div>

        <!-- SMS Responses Tab -->
        <div id="tab-sms" class="tab-content">
            <div class="section" style="border: 2px solid #2196F3; margin-top: 20px;">
                <h2>📱 SMS Auto-Response Settings</h2>
                <p class="help-text">💡 Enable a response for each event type individually. Only one response is ever sent per incoming message.</p>
                <!-- Twilio-specific delivery warnings - hidden when Google Voice is the source -->
                <div id="twilio_sms_warnings">
                    <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:10px 14px; margin:10px 0; font-size:13px;">
                        ⚠️ <strong>Message &amp; data rates may apply.</strong>
                    </div>
                    <div style="background:#f8d7da; border:2px solid #f5c6cb; color:#721c24; border-radius:6px; padding:12px 16px; margin:10px 0; font-size:14px; font-weight:bold;">
                        ⛔ SMS responses will NOT be delivered unless your Twilio number is registered:<br>
                        <span style="font-weight:normal; font-size:13px; display:block; margin-top:6px;">
                            • <strong>Local 10-digit number</strong> - requires a valid A2P 10DLC brand &amp; campaign approval<br>
                            • <strong>Toll-free number</strong> - requires a completed toll-free verification (recommended)
                        </span>
                    </div>
                </div>

                <style>
                    .resp-row { border: 1px solid #ddd; border-radius: 6px; padding: 12px 14px; margin-bottom: 10px; background: #fafafa; }
                    .resp-row.enabled { background: #f0f7ff; border-color: #90caf9; }
                    .resp-row.locked { pointer-events: none; background: #f0f0f0; border-color: #ccc; }
                    .resp-row.locked .resp-toggle { opacity: 0.4; }
                    .resp-toggle { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-weight: bold; font-size: 14px; }
                    .resp-row textarea { opacity: 0.4; pointer-events: none; transition: opacity .2s; }
                    .resp-row.locked textarea { opacity: 0.4; }
                    .resp-row.enabled textarea { opacity: 1; pointer-events: auto; }
                    .reset-default-btn { margin-top: 4px; background: #eee; color: #333; border: 1px solid #ccc; border-radius: 4px; padding: 3px 10px; font-size: 12px; cursor: pointer; opacity: 0.4; pointer-events: none; transition: opacity .2s; }
                    .resp-row.enabled .reset-default-btn { opacity: 1; pointer-events: auto; }
                    .resp-locked-note { font-size: 13px; color: #856404; background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 7px 10px; margin: 4px 0 6px; }
                </style>

                <script>
                function toggleResp(id) {
                    var row = document.getElementById('row_' + id);
                    var cb = document.getElementById('sms_response_' + id);
                    if (!row || !cb) return;   // never let a missing row abort init
                    row.classList.toggle('enabled', cb.checked);
                }
                // The authoritative default response text, straight from the server's
                // DEFAULT_CONFIG, so "Reset to default" always restores the true default
                // (and automatically tracks any default we change in a future update).
                window._respDefaults = {{ response_defaults | tojson }};
                function resetResp(id) {
                    var ta = document.getElementById('response_' + id);
                    if (!ta) return;
                    var def = (window._respDefaults || {})['response_' + id];
                    if (def === undefined) return;
                    ta.value = def;
                    if (window.saveConfig) saveConfig();
                }
                // Inject a "Reset to default" button after each response textarea once.
                function addResetButtons() {
                    ['show_not_live','blocked','profanity','duplicate','invalid_format','too_long','rate_limited','not_whitelisted','success'].forEach(function(id) {
                        var ta = document.getElementById('response_' + id);
                        if (!ta || ta._hasReset) return;
                        var btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'reset-default-btn';
                        btn.textContent = '↩️ Reset to default';
                        btn.onclick = function() { resetResp(id); };
                        ta.insertAdjacentElement('afterend', btn);
                        ta._hasReset = true;
                    });
                }
                function initRespRows() {
                    addResetButtons();
                    ['show_not_live','blocked','profanity','duplicate','invalid_format','too_long','rate_limited','not_whitelisted','success'].forEach(function(id) {
                        toggleResp(id);
                    });
                }
                </script>

                <div id="row_success" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_success" {{ 'checked' if config.get('sms_response_success', False) else '' }} onchange="toggleResp('success')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_success" style="margin-left:10px;vertical-align:middle;">✅ Success - Send Response</label>
                    </div>
                    <textarea id="response_success" rows="2">{{ config.get('response_success', 'Thanks! Your name will appear on our display soon! 🎄') }}</textarea>
                </div>

                <div id="row_show_not_live" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_show_not_live" {{ 'checked' if config.get('sms_response_show_not_live', False) else '' }} onchange="toggleResp('show_not_live')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_show_not_live" style="margin-left:10px;vertical-align:middle;">🔴 Show Not Live - Send Response</label>
                    </div>
                    <p class="help-text" style="margin:4px 0 6px;">Sent to anyone who texts while the show is not active.</p>
                    <textarea id="response_show_not_live" rows="2">{{ config.get('response_show_not_live', "Ho, Ho, Ho, It looks like our show isn't running now. Try again later.") }}</textarea>
                </div>

                <div id="row_invalid_format" class="resp-row{% if config.get('use_whitelist', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_invalid_format"
                               {{ 'checked' if config.get('sms_response_invalid_format', False) else '' }}
                               {{ 'disabled' if config.get('use_whitelist', False) else '' }}
                               onchange="toggleResp('invalid_format')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_invalid_format" style="margin-left:10px;vertical-align:middle;">❌ Invalid Format - Send Response</label>
                    </div>
                    <p class="help-text">💡 Type <code>{words}</code> anywhere in this message to auto-fill your current word limit - it becomes "<span id="words_preview">2 words</span>" in the reply, based on your <strong>Name Format Rules</strong> (One Word Only → "1 word", Two Words Maximum → "2 words").</p>
                    <p id="invalid_format_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('use_whitelist', False) else 'display:none;' }}">⚠️ Invalid Format responses are disabled when the whitelist is active - all names are validated against the whitelist instead of format rules.</p>
                    <textarea id="response_invalid_format" rows="2">{{ config.get('response_invalid_format', 'Please send only 1 name ({words}, no sentences).') }}</textarea>
                </div>

                <div id="row_too_long" class="resp-row{% if config.get('use_whitelist', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_too_long"
                               {{ 'checked' if config.get('sms_response_too_long', False) else '' }}
                               {{ 'disabled' if config.get('use_whitelist', False) else '' }}
                               onchange="toggleResp('too_long')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_too_long" style="margin-left:10px;vertical-align:middle;">📏 Message Too Long - Send Response</label>
                    </div>
                    <p class="help-text">📏 Sent when a message is longer than your <strong>Max Message Length</strong>. Applies whether or not the word-count rules are on.</p>
                    <p id="too_long_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('use_whitelist', False) else 'display:none;' }}">⚠️ Too Long responses are disabled when the whitelist is active - names are validated against the whitelist, not by length.</p>
                    <textarea id="response_too_long" rows="2">{{ config.get('response_too_long', "I'm sorry, your message exceeds our max message length. Please only send your name.") }}</textarea>
                </div>

                <div id="row_profanity" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_profanity" {{ 'checked' if config.get('sms_response_profanity', False) else '' }} onchange="toggleResp('profanity')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_profanity" style="margin-left:10px;vertical-align:middle;">🤬 Profanity Detected - Send Response</label>
                    </div>
                    <textarea id="response_profanity" rows="2">{{ config.get('response_profanity', 'Sorry, your message contains inappropriate content and cannot be displayed.') }}</textarea>
                </div>

                <div id="row_blocked" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_blocked" {{ 'checked' if config.get('sms_response_blocked', False) else '' }} onchange="toggleResp('blocked')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_blocked" style="margin-left:10px;vertical-align:middle;">🚫 Blocked Number - Send Response</label>
                    </div>
                    <textarea id="response_blocked" rows="2">{{ config.get('response_blocked', 'You have been blocked from sending messages.') }}</textarea>
                </div>

                <div id="row_rate_limited" class="resp-row{% if config.get('max_messages_per_phone', 0) == 0 %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_rate_limited"
                               {{ 'checked' if config.get('sms_response_rate_limited', False) else '' }}
                               {{ 'disabled' if config.get('max_messages_per_phone', 0) == 0 else '' }}
                               onchange="toggleResp('rate_limited')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_rate_limited" style="margin-left:10px;vertical-align:middle;">⛔ Rate Limited - Send Response</label>
                    </div>
                    <p id="rate_limited_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('max_messages_per_phone', 0) == 0 else 'display:none;' }}">⚠️ Rate-Limited responses are disabled when Max Messages Per Phone is 0 (unlimited).</p>
                    <textarea id="response_rate_limited" rows="2">{{ config.get('response_rate_limited', "You've reached the maximum number of messages allowed. Please try again tomorrow!") }}</textarea>
                </div>

                <div id="row_duplicate" class="resp-row{% if config.get('allow_duplicate_names', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_duplicate"
                               {{ 'checked' if config.get('sms_response_duplicate', False) else '' }}
                               {{ 'disabled' if config.get('allow_duplicate_names', False) else '' }}
                               onchange="toggleResp('duplicate')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_duplicate" style="margin-left:10px;vertical-align:middle;">🔄 Duplicate Name - Send Response</label>
                    </div>
                    <p id="duplicate_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('allow_duplicate_names', False) else 'display:none;' }}">⚠️ <strong>Allow Duplicate Names is enabled</strong> - This response is disabled.</p>
                    <textarea id="response_duplicate" rows="2">{{ config.get('response_duplicate', "You've already sent this name today!") }}</textarea>
                </div>

                <div id="row_not_whitelisted" class="resp-row{% if not config.get('use_whitelist', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_not_whitelisted"
                               {{ 'checked' if config.get('sms_response_not_whitelisted', False) else '' }}
                               {{ 'disabled' if not config.get('use_whitelist', False) else '' }}
                               onchange="toggleResp('not_whitelisted')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_not_whitelisted" style="margin-left:10px;vertical-align:middle;">📋 Not on Whitelist - Send Response</label>
                    </div>
                    <p id="not_whitelisted_disabled_warning" class="resp-locked-note" style="{{ '' if not config.get('use_whitelist', False) else 'display:none;' }}">⚠️ <strong>Name Whitelist is disabled</strong> - This response is disabled.</p>
                    <textarea id="response_not_whitelisted" rows="2">{{ config.get('response_not_whitelisted', 'Sorry, that name is not on our approved list.') }}</textarea>
                </div>

                <!-- Live Name Approval messages - only shown when an Admin Phone Number is
                     set (configured on the Message Source tab) and Google Voice is the source. -->
                <div id="admin_approval_responses" class="resp-row enabled" style="{{ '' if (config.get('message_source','twilio')=='google_voice' and config.get('admin_phone','')) else 'display:none;' }} margin-left:22px; border-left:3px solid #90caf9;">
                    <div class="resp-toggle" style="font-weight:bold;">🙋 Live Name Approval (Google Voice)</div>
                    <p id="admin_approval_wl_note" class="resp-locked-note" style="{{ '' if not config.get('use_whitelist', False) else 'display:none;' }}">⚠️ <strong>Name Whitelist is disabled</strong> - Live Name Approval only applies when the whitelist is on.</p>
                    <div id="admin_approval_inner">
                    <p class="help-text" style="margin:2px 0 12px;">An extension of the <strong>Not on Whitelist</strong> response above: when a texter sends a name that is not on the whitelist and you have an <strong>Admin Phone Number</strong> set (Message Source tab), you get the Y/N prompt; the texter gets the waiting message, then the Success response (Y). A denial (N) sends the <strong>Not on Whitelist</strong> response above.</p>

                    <label>Approval Timeout (minutes):</label>
                    <input type="number" id="admin_approval_timeout_mins" min="0" max="1440" value="{{ config.get('admin_approval_timeout_mins', 5) }}" style="width:90px;">
                    <p class="help-text" style="margin:3px 0 14px;">How long a request waits for your Y/N before it expires. 0 = never expire.</p>

                    <label>Text to Admin (approval prompt):</label>
                    <textarea id="admin_approval_prompt" rows="2">{{ config.get('admin_approval_prompt', "New name request: '{name}'. Reply Y to add to whitelist, or N to deny.") }}</textarea>
                    <p class="help-text" style="margin:3px 0 2px;">Use <code>{name}</code> where the requested name should appear.</p>
                    <button type="button" class="reset-default-btn" style="opacity:1; pointer-events:auto;" onclick="resetAdminField('admin_approval_prompt')">↩️ Reset to default</button>

                    <label style="margin-top:14px; display:block;">Reply to Texter (while waiting):</label>
                    <textarea id="response_whitelist_pending" rows="2">{{ config.get('response_whitelist_pending', "Your name isn't on our whitelist, please wait a few moments while I get approval to display.") }}</textarea>
                    <button type="button" class="reset-default-btn" style="opacity:1; pointer-events:auto;" onclick="resetAdminField('response_whitelist_pending')">↩️ Reset to default</button>
                    </div>
                </div>

                <script>
                    // Live Name Approval wiring. The Admin Phone field lives on the Message
                    // Source tab; these message fields live here. Wire everything after the
                    // DOM is parsed so elements in both tabs exist.
                    window._adminDefaults = {
                        admin_approval_prompt: "New name request: '{name}'. Reply Y to add to whitelist, or N to deny.",
                        response_whitelist_pending: "Your name isn't on our whitelist, please wait a few moments while I get approval to display."
                    };
                    function resetAdminField(id) {
                        var el = document.getElementById(id);
                        if (!el || !window._adminDefaults[id]) return;
                        el.value = window._adminDefaults[id];
                        if (window.saveConfig) saveConfig();
                    }
                    // Show the approval messages only when Google Voice is the source AND an
                    // admin phone is set. The banner's shown number and the warning/connected
                    // state are NOT updated from this live-typed value - they are driven by the
                    // SAVED number the server reports in pollAdminStatus(), so nothing flips
                    // while you are still typing.
                    function updateAdminApprovalUI() {
                        var isGV = ((document.getElementById('message_source')||{}).value) === 'google_voice';
                        var phone = (((document.getElementById('admin_phone')||{}).value) || '').trim();
                        var sec = document.getElementById('admin_approval_responses');
                        if (sec) sec.style.display = (isGV && phone) ? '' : 'none';
                        // Live Name Approval only applies to names not on the whitelist, so grey
                        // it out (but keep the fields' values) when the whitelist is off.
                        var wlOn = !!((document.getElementById('use_whitelist')||{}).checked);
                        var inner = document.getElementById('admin_approval_inner');
                        var note = document.getElementById('admin_approval_wl_note');
                        if (inner) { inner.style.opacity = wlOn ? '1' : '0.4'; inner.style.pointerEvents = wlOn ? '' : 'none'; }
                        if (note) note.style.display = wlOn ? 'none' : 'block';
                    }
                    window.updateAdminApprovalUI = updateAdminApprovalUI;
                    // Reflect the live whitelist on/off state next to the Live Name Approval
                    // heading (approvals only matter while the whitelist is on).
                    function updateLiveApprovalWlState() {
                        var el = document.getElementById('live_approval_wl_state');
                        var wl = document.getElementById('use_whitelist');
                        if (!el || !wl) return;
                        if (wl.checked) {
                            el.textContent = 'Whitelist is On';
                            el.style.background = '#e8f5e9'; el.style.color = '#2e7d32';
                        } else {
                            el.textContent = 'Whitelist is Off';
                            el.style.background = '#fdecea'; el.style.color = '#b71c1c';
                        }
                    }
                    window.updateLiveApprovalWlState = updateLiveApprovalWlState;
                    document.addEventListener('DOMContentLoaded', function() {
                        var ap = document.getElementById('admin_phone');
                        if (ap) {
                            // No 'input' handler on purpose: the banner/connected state must not
                            // react to each keystroke. Only when the number is committed (change
                            // or blur) do we save it and, once the save lands, re-check - the
                            // server then runs its mailbox scan against the new number.
                            function onAdminPhoneCommitted() {
                                updateAdminApprovalUI();
                                if (window.saveConfig) saveConfig();
                                // saveConfig is debounced ~300ms then POSTs; give it time to land
                                // so the status poll scans the just-saved number.
                                setTimeout(pollAdminStatus, 1200);
                            }
                            ap.addEventListener('change', onAdminPhoneCommitted);
                            ap.addEventListener('blur', onAdminPhoneCommitted);
                        }
                        ['admin_approval_timeout_mins','admin_approval_prompt','response_whitelist_pending'].forEach(function(id) {
                            var el = document.getElementById(id);
                            if (!el) return;
                            el.addEventListener('change', function(){ if (window.saveConfig) saveConfig(); });
                            el.addEventListener('blur', function(){ if (window.saveConfig) saveConfig(); });
                        });
                        // Poll the seeded status so the bootstrap banner clears live once a text
                        // from the admin number is found (new, or from existing Gmail history).
                        function pollAdminStatus() {
                            var banner = document.getElementById('admin_bootstrap_banner');
                            var connected = document.getElementById('admin_connected_note');
                            var threadWarn = document.getElementById('admin_thread_warning');
                            var noGv = document.getElementById('admin_no_gv_warning');
                            if (!banner || !connected) return;
                            var isGV = ((document.getElementById('message_source')||{}).value) === 'google_voice';
                            if (!isGV) {
                                banner.style.display = 'none'; connected.style.display = 'none';
                                if (threadWarn) threadWarn.style.display = 'none';
                                if (noGv) noGv.style.display = 'none';
                                return;
                            }
                            fetch('/api/plugin/admin-approval-status')
                                .then(function(r){ return r.json(); })
                                .then(function(d){
                                    // Drive the shown number and the warning/connected state from
                                    // the SAVED number the server reports - never the live input -
                                    // so nothing flips while the operator is still typing, and the
                                    // server has already run its mailbox scan for this number.
                                    var savedPhone = ((d && d.admin_phone) || '').toString().trim();
                                    var num = document.getElementById('admin_banner_num');
                                    if (num) num.textContent = savedPhone;
                                    if (!savedPhone) {
                                        banner.style.display = 'none'; connected.style.display = 'none';
                                        if (threadWarn) threadWarn.style.display = 'none';
                                        if (noGv) noGv.style.display = 'none';
                                        return;
                                    }
                                    // No Google Voice account linked yet: show the link-account
                                    // warning instead of the "text admin" banner - there is no
                                    // number to text until credentials are connected.
                                    if (d && !d.gv_linked) {
                                        if (noGv) noGv.style.display = 'block';
                                        banner.style.display = 'none'; connected.style.display = 'none';
                                        if (threadWarn) threadWarn.style.display = 'none';
                                        return;
                                    }
                                    if (noGv) noGv.style.display = 'none';
                                    // The "keep the thread" reminder stands once a number is saved.
                                    if (threadWarn) threadWarn.style.display = 'block';
                                    if (d && d.seeded) { banner.style.display = 'none'; connected.style.display = 'block'; }
                                    else { banner.style.display = 'block'; connected.style.display = 'none'; }
                                })
                                .catch(function(){});
                        }
                        setInterval(pollAdminStatus, 5000);
                        pollAdminStatus();
                        updateAdminApprovalUI();
                        updateLiveApprovalWlState();
                    });
                </script>

            </div>
        </div>

        <!-- Testing Tab -->
        <div id="tab-testing" class="tab-content">

            <div id="test_message_section" class="section" style="border: 2px solid #FF9800; margin-top: 20px;">
                <h2>🧪 Message Testing</h2>

                <div id="show_not_live_banner" style="display:none; background:#ffecb3; border:1px solid #FF9800; border-radius:6px; padding:10px 14px; margin-bottom:14px; color:#7a4f00; font-size:14px;">
                    🔴 Show is not live - press <strong>Start</strong> or run the <strong>Text My Lights Start</strong> script to activate the display before testing.
                </div>

                <div id="test_form_inner">
                    <p style="color: #FF9800; font-size: 14px;">
                        ⚠️ Use this to test messages without sending actual texts. Works without SMS credentials.
                    </p>

                    <label>Test Name:</label>
                    <input type="text" id="test_name" placeholder="Enter a name to test">

                    <button class="test-btn" onclick="submitTestMessage()">🧪 Submit Test Message</button>

                    <div id="test_result" style="margin-top: 10px;"></div>
                </div>
            </div>

        </div>

        <script>
            // Tracks the last-known live state so the single toggle button knows
            // whether a click should Start (activate) or Stop (deactivate).
            var _pluginLive = false;

            function pluginToggle() {
                var btn = document.getElementById('btn_plugin_toggle');
                var goingLive = !_pluginLive;
                var url = goingLive ? '/api/activate' : '/api/deactivate';
                btn.disabled = true; btn.textContent = '...';
                fetch(url, {method:'POST'})
                .then(r => r.json())
                .then(function(d) {
                    if (goingLive && d.success === false) { alert('Start failed: ' + (d.error || 'Unknown error')); }
                    updateLiveStatus();
                })
                .catch(function() { alert((goingLive ? 'Start' : 'Stop') + ' request failed.'); })
                .finally(function() { btn.disabled = false; });
            }

            function updateLiveStatus() {
                // On a Remote there is no Start/Stop or live state - the Master runs the show.
                // Keep those controls hidden and skip the master-only status UI entirely.
                var _rs = document.getElementById('plugin_role');
                if (_rs && _rs.value === 'remote') {
                    var t=document.getElementById('btn_plugin_toggle'); if(t) t.style.display='none';
                    var lb=document.getElementById('plugin_live_banner'); if(lb) lb.style.display='none';
                    var nb=document.getElementById('plugin_not_live_banner'); if(nb) nb.style.display='none';
                    return;
                }
                fetch('/api/queue/status').then(r => r.json()).then(data => {
                    const live = data.show_live === true;
                    _pluginLive = live;

                    // Single Start/Stop toggle reflects current live state
                    const toggle = document.getElementById('btn_plugin_toggle');
                    if (toggle) {
                        toggle.textContent = live ? '■ Stop' : '▶ Start';
                        toggle.style.background = live ? '#c62828' : '#2e7d32';
                    }

                    // Testing tab banner (show is NOT live warning)
                    const notLiveBanner = document.getElementById('show_not_live_banner');
                    const form = document.getElementById('test_form_inner');
                    if (notLiveBanner) notLiveBanner.style.display = live ? 'none' : 'block';
                    if (form) {
                        form.style.opacity = live ? '1' : '0.4';
                        form.style.pointerEvents = live ? '' : 'none';
                    }

                    // Settings tab: "Plugin is Live" / "Plugin is Not Live" banner at top
                    const liveBanner = document.getElementById('plugin_live_banner');
                    const notLiveTopBanner = document.getElementById('plugin_not_live_banner');
                    if (liveBanner) liveBanner.style.display = live ? 'flex' : 'none';
                    if (notLiveTopBanner) notLiveTopBanner.style.display = live ? 'none' : 'flex';

                    // Lock content dropdowns when live
                    const liveWarning = document.getElementById('fpp_content_live_warning');
                    const contentInputs = document.getElementById('fpp_content_inputs');
                    if (liveWarning) liveWarning.style.display = live ? 'block' : 'none';
                    if (contentInputs) {
                        contentInputs.style.opacity = live ? '0.4' : '';
                        contentInputs.style.pointerEvents = live ? 'none' : '';
                    }
                }).catch(() => {});
            }

            // Resolves a line's movement ('Center'|'L2R'|'R2L'|'T2B'|'B2T'), defaulting to Center
            function getLineMovement(i) {
                return (window._lineMovements && window._lineMovements[i]) || 'Center';
            }

            // Shows/hides a single line's own Speed input based on that line's movement
            function updateLineSpeedRowVisibility(i) {
                var row = document.getElementById('line_' + (i + 1) + '_speed_row');
                if (row) row.style.display = (getLineMovement(i) === 'Center') ? 'none' : '';
            }

            // Orientation only applies to Center (static) lines -- a scrolling line is
            // always horizontal (the point of L2R/R2L/T2B/B2T is travel along one axis;
            // rotating or stacking the glyphs on top of that isn't supported).
            function getLineOrientation(i) {
                return (window._lineOrientations && window._lineOrientations[i]) || 'horizontal';
            }
            function isVerticalOrientation(o) {
                return o === 'vertical_rotated' || o === 'vertical_stacked';
            }
            function updateLineOrientationRowVisibility(i) {
                var m = getLineMovement(i);
                // Rotated/stacked text both work for Center (fixed) and T2B/B2T (travels
                // vertically -- rotated glyphs read sideways, stacked keeps each character
                // upright, one per row) -- not L2R/R2L, where the point of the movement is
                // horizontal travel and neither combines with that meaningfully.
                var applicable = (m === 'Center' || m === 'T2B' || m === 'B2T');
                var row = document.getElementById('line_' + (i + 1) + '_orientation_row');
                if (row) row.style.display = applicable ? 'inline-flex' : 'none';

                var sel = document.getElementById('line_' + (i + 1) + '_orientation');
                if (!sel) return;
                var stackedOpt = sel.querySelector('option[value="vertical_stacked"]');
                if (!stackedOpt) return;
                stackedOpt.disabled = !applicable;
                // Not just the Stacked option -- Rotated is equally inapplicable once the
                // movement is L2R/R2L, so reset (and swap the box back, via
                // onLineOrientationChange) for either vertical value, not only Stacked.
                if (!applicable && isVerticalOrientation(sel.value)) {
                    sel.value = 'horizontal';
                    onLineOrientationChange(i);
                }
            }

            // Per-line Movement select
            function onLineMovementChange(i) {
                var el = document.getElementById('line_' + (i + 1) + '_movement');
                if (!el) return;
                window._lineMovements = window._lineMovements || ['Center','Center','Center','Center'];
                var newMovement = el.value;
                window._lineMovements[i] = newMovement;
                // T2B/B2T reads much better with the text itself rotated to match its
                // vertical travel (a single horizontal line moving straight up/down is an
                // unusual look) -- default to that unless the line already has an explicit
                // orientation, so switching to T2B/B2T "just works" without an extra step.
                if ((newMovement === 'T2B' || newMovement === 'B2T') && getLineOrientation(i) === 'horizontal') {
                    var orientSel = document.getElementById('line_' + (i + 1) + '_orientation');
                    if (orientSel) { orientSel.value = 'vertical_rotated'; onLineOrientationChange(i); }
                }
                updateLineSpeedRowVisibility(i);
                updateLineOrientationRowVisibility(i);
                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                if (typeof saveConfig === 'function') saveConfig();
            }

            // Per-line Orientation select. Swaps the box's W/H when crossing the
            // horizontal/vertical boundary (either direction) so switching to vertical
            // starts from a sensible portrait-shaped box instead of a leftover wide one.
            function onLineOrientationChange(i) {
                var el = document.getElementById('line_' + (i + 1) + '_orientation');
                if (!el) return;
                window._lineOrientations = window._lineOrientations || ['horizontal','horizontal','horizontal','horizontal'];
                var prev = getLineOrientation(i);
                var next = el.value;
                if (isVerticalOrientation(prev) !== isVerticalOrientation(next)) {
                    var b = window._lineBoxes && window._lineBoxes[i];
                    // _lineBoxes are stored in MODEL pixel space (window._canvasModelW/H),
                    // not the preview canvas's own raster size -- matrix_canvas.width is
                    // always a fixed 640px-wide bitmap scaled to the model's aspect ratio,
                    // a completely different number from the model's real width/height
                    // whenever the model isn't 640px wide. Comparing/assigning against the
                    // canvas element here compared box coordinates against the wrong
                    // coordinate space and could inflate the box's model-space size well
                    // past the model's actual extent.
                    var modelW = window._canvasModelW, modelH = window._canvasModelH;
                    if (b && modelW && modelH) {
                        // A plain w<->h swap is wrong when the box was sized to span the
                        // full overlay along its old axis -- model width and height are
                        // rarely equal, so reusing the raw old number leaves the new axis
                        // either short of, or overflowing, the overlay's actual extent.
                        // Detect "was full span" before swapping and, if so, snap the new
                        // axis to the overlay's real size on that axis instead.
                        var wasFullW = b.w >= modelW - 2;
                        var wasFullH = b.h >= modelH - 2;
                        if (wasFullW && wasFullH) {
                            // Box already covered the entire model in both dimensions --
                            // there's no meaningful "shape" to transpose (the model itself
                            // usually isn't square), so keep it covering the entire model
                            // after the flip too instead of collapsing one axis down to the
                            // other's old (unrelated) size.
                            b.w = modelW; b.h = modelH;
                        } else {
                            var t = b.w; b.w = b.h; b.h = t;
                            if (isVerticalOrientation(next) && wasFullW) b.h = modelH;
                            else if (!isVerticalOrientation(next) && wasFullH) b.w = modelW;
                        }
                    } else if (b) {
                        var t2 = b.w; b.w = b.h; b.h = t2;
                    }
                }
                window._lineOrientations[i] = next;
                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                if (typeof saveConfig === 'function') saveConfig();
            }

            // Per-line manual Speed input (1-100 px/s). Fit-to-time uses a separate encoding
            // (speed <= 0) set via the checkbox / Times box below, never typed here.
            function onLineSpeedChange(i) {
                var el = document.getElementById('line_' + (i + 1) + '_speed');
                if (!el) return;
                var v = Math.round(Math.min(100, Math.max(1, parseInt(el.value, 10) || 1)));
                el.value = v;
                window._lineSpeeds = window._lineSpeeds || [50,50,50,50];
                window._lineSpeeds[i] = v;
                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                if (typeof saveConfig === 'function') saveConfig();
            }

            // Per-line "Fit to display time" checkbox. Checked swaps the Speed box for the
            // Times (pass count) box and stores speed = -times (fit-to-time encoding, read
            // by the preview + backend). Unchecked restores the manual px/s speed.
            function onLineSpeedAutoChange(i) {
                var auto = document.getElementById('line_' + (i + 1) + '_speed_auto');
                if (!auto) return;
                var speedWrap = document.getElementById('line_' + (i + 1) + '_speed_wrap');
                var passesWrap = document.getElementById('line_' + (i + 1) + '_passes_wrap');
                window._lineSpeeds = window._lineSpeeds || [50,50,50,50];
                if (auto.checked) {
                    if (speedWrap) speedWrap.style.display = 'none';
                    if (passesWrap) passesWrap.style.display = '';
                    var pEl = document.getElementById('line_' + (i + 1) + '_passes');
                    var p = pEl ? Math.round(Math.min(20, Math.max(1, parseInt(pEl.value, 10) || 1))) : 1;
                    if (pEl) pEl.value = p;
                    window._lineSpeeds[i] = -p;  // negative = fit-to-time, N passes
                } else {
                    if (speedWrap) speedWrap.style.display = '';
                    if (passesWrap) passesWrap.style.display = 'none';
                    var sEl = document.getElementById('line_' + (i + 1) + '_speed');
                    var v = sEl ? Math.round(Math.min(100, Math.max(1, parseInt(sEl.value, 10) || 50))) : 50;
                    if (sEl) sEl.value = v;
                    window._lineSpeeds[i] = v;
                }
                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                if (typeof saveConfig === 'function') saveConfig();
            }

            // Per-line "Times" (pass count) input, shown only in fit-to-time mode. Stores
            // speed = -times so the backend/preview make that many complete passes over the
            // display duration.
            function onLinePassesChange(i) {
                var el = document.getElementById('line_' + (i + 1) + '_passes');
                if (!el) return;
                var p = Math.round(Math.min(20, Math.max(1, parseInt(el.value, 10) || 1)));
                el.value = p;
                window._lineSpeeds = window._lineSpeeds || [50,50,50,50];
                window._lineSpeeds[i] = -p;
                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                if (typeof saveConfig === 'function') saveConfig();
            }

            // Per-line Font select
            function onLineFontChange(i) {
                // Can't call getLineFont(i) here - it's local to initCanvasPreview()'s
                // closure, not visible in this scope. Read the select directly instead,
                // same as onLineMovementChange/onLineSpeedChange do for their inputs.
                var el = document.getElementById('line_' + (i + 1) + '_font');
                var name = el ? el.value : null;
                ensureFontLoaded(name).then(function() {
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                });
                if (typeof saveConfig === 'function') saveConfig();
            }


            function updateModelAspect(width, height) {
                if (width > 0 && height > 0) {
                    window._canvasModelW = width;
                    window._canvasModelH = height;
                    var c = document.getElementById('matrix_canvas');
                    if (c) { c.width = 640; c.height = Math.round(640 * height / width); }
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                }
            }

            function initValignButtons() {
                // No-op: v_align removed; per-line Y positioning handles vertical placement.
            }

            function initCanvasPreview() {
                var canvas = document.getElementById('matrix_canvas');
                if (!canvas) return;
                var ctx = canvas.getContext('2d');
                if (!ctx) return;

                window._canvasModelW = parseInt(document.getElementById('overlay_model_width').value) || 640;
                window._canvasModelH = parseInt(document.getElementById('overlay_model_height').value) || 360;
                canvas.width  = 640;
                canvas.height = Math.round(640 * window._canvasModelH / window._canvasModelW);

                // Load per-line boxes from config (injected as JS by server to avoid HTML
                // attribute quote issues). Each box is the MAX area a line renders into -
                // font size auto-fits to it. x/y < 0 means auto-position; w/h are always
                // concrete (there's no "auto size" for the fit target itself).
                var initLB = (window._lineBoxesInit && Array.isArray(window._lineBoxesInit))
                    ? window._lineBoxesInit
                    : [{x: -1, y: -1, w: 300, h: 60}, {x: -1, y: -1, w: 300, h: 60},
                       {x: -1, y: -1, w: 300, h: 60}, {x: -1, y: -1, w: 300, h: 60}];
                while (initLB.length < 4) initLB.push({x: -1, y: -1, w: 300, h: 60});
                window._lineBoxes = initLB;

                // Load per-line movement + speed from config
                var initLM = (window._lineMovementsInit && Array.isArray(window._lineMovementsInit))
                    ? window._lineMovementsInit.slice()
                    : ['Center', 'Center', 'Center', 'Center'];
                while (initLM.length < 4) initLM.push('Center');
                window._lineMovements = initLM;

                var initLS = (window._lineSpeedsInit && Array.isArray(window._lineSpeedsInit))
                    ? window._lineSpeedsInit.slice()
                    : [50, 50, 50, 50];
                while (initLS.length < 4) initLS.push(50);
                window._lineSpeeds = initLS;

                // Orientation only applies to Center (static) lines -- see getLineOrientation
                var initLO = (window._lineOrientationsInit && Array.isArray(window._lineOrientationsInit))
                    ? window._lineOrientationsInit.slice()
                    : ['horizontal', 'horizontal', 'horizontal', 'horizontal'];
                while (initLO.length < 4) initLO.push('horizontal');
                window._lineOrientations = initLO;

                var selectedLine = -1;
                var hoveredLine  = -1;
                var lineRects    = [null, null, null, null]; // canvas-pixel rects, filled by render
                var dragging     = false;
                var dragOffX     = 0, dragOffY = 0;

                // modelScaleX/Y (model units -> canvas px) are recomputed by
                // renderCanvasPreview() every render and read by the mouse handlers below to
                // convert between canvas-px and model-unit coordinates. gutterOriginX/Y and
                // modelPxW/H are always 0,0 and canvas.width,canvas.height respectively (the
                // model fills the whole canvas) -- kept as named values since several call
                // sites read them, rather than inlining canvas.width/height everywhere.
                var modelScaleX = 1, modelScaleY = 1;
                var gutterOriginX = 0, gutterOriginY = 0;
                var modelPxW = 0, modelPxH = 0;

                function getLineText(i) {
                    var el = document.getElementById('line_' + (i + 1));
                    return el ? el.value.replace('{name}', 'Santa') : '';
                }

                // Reads this line's own color directly from its color input
                function getLineColor(i) {
                    var el = document.getElementById('line_' + (i + 1) + '_color');
                    return (el && el.value) || '#FF0000';
                }

                // Reads this line's own font directly from its font input
                function getLineFont(i) {
                    var el = document.getElementById('line_' + (i + 1) + '_font');
                    return (el && el.value) || 'sans-serif';
                }

                // Binary search the largest font size where `text` fits within boxW x boxH.
                // Pass boxW = Infinity to fit height only (used for scrolling lines, where
                // the text is expected to be wider than its box and travels across it).
                // Height uses actualBoundingBoxAscent/Descent -- the real measured extent of
                // this specific text run -- rather than a flat fontSize*1.2 estimate. A
                // generic estimate undersells how tall decorative/dingbat fonts actually
                // render (e.g. graphics that rise well above a normal cap-height), letting an
                // oversized fit through that then gets clipped by a scrolling line's box
                // (Center draws without a clip, so the same oversized fit there just overflows
                // invisibly instead of visibly losing its top). Matches the backend's PIL
                // textbbox-based measurement in _fit_text_to_box.
                function fitTextSize(text, fontName, boxW, boxH) {
                    var lo = 6;
                    // The cap has to scale with the box, not be a fixed number -- otherwise
                    // a constraining dimension bigger than the cap leaves real headroom
                    // unused forever, since the search can never explore past it (seen with
                    // a 300px-tall box: the search maxed out at 300 even though that size's
                    // actual ascent+descent was only ~225, well under the 300 limit).
                    var hi = Math.max(300, isFinite(boxW) ? Math.ceil(boxW * 2) : 0,
                                           isFinite(boxH) ? Math.ceil(boxH * 2) : 0);
                    var best = { size: lo, ascent: lo * 0.8, descent: lo * 0.2 };
                    // actualBoundingBoxAscent/Descent are measured relative to whatever
                    // textBaseline is current at measureText() time -- not always
                    // 'alphabetic' -- so it must be pinned here rather than inherited from
                    // whatever the caller last set (renderCanvasPreview leaves it at 'top',
                    // which made ascent come back negative and the fit wildly wrong).
                    ctx.textBaseline = 'alphabetic';
                    while (lo <= hi) {
                        var mid = Math.floor((lo + hi) / 2);
                        ctx.font = mid + 'px "' + fontName + '", sans-serif';
                        var metrics = ctx.measureText(text);
                        var w = metrics.width;
                        var ascent = metrics.actualBoundingBoxAscent || mid * 0.8;
                        var descent = metrics.actualBoundingBoxDescent || mid * 0.2;
                        var h = ascent + descent;
                        if (w <= boxW && h <= boxH) {
                            best = { size: mid, ascent: ascent, descent: descent };
                            lo = mid + 1;
                        } else {
                            hi = mid - 1;
                        }
                    }
                    return best;
                }

                // Like fitTextSize, but for 'vertical_stacked' orientation: each character
                // sits on its own row (upright, not rotated), all sharing one font size --
                // the largest where the widest character fits boxW and all rows stacked fit
                // boxH. lineHeight is the per-row height (tallest character's ascent+descent).
                function fitStackedTextSize(chars, fontName, boxW, boxH) {
                    var lo = 6;
                    // See fitTextSize -- the cap must scale with the box or real headroom
                    // goes unused. For stacked text, a single row's height only needs to
                    // reach boxH / chars.length (the total stack height constraint divided
                    // across all the rows), not the full boxH.
                    var hi = Math.max(300, isFinite(boxW) ? Math.ceil(boxW * 2) : 0,
                                           isFinite(boxH) ? Math.ceil((boxH / chars.length) * 2) : 0);
                    var best = { size: lo, ascent: lo * 0.8, descent: lo * 0.2, lineHeight: lo };
                    ctx.textBaseline = 'alphabetic';
                    while (lo <= hi) {
                        var mid = Math.floor((lo + hi) / 2);
                        ctx.font = mid + 'px "' + fontName + '", sans-serif';
                        var maxW = 0, maxAscent = 0, maxDescent = 0;
                        for (var ci = 0; ci < chars.length; ci++) {
                            var metrics = ctx.measureText(chars[ci]);
                            maxW = Math.max(maxW, metrics.width);
                            maxAscent = Math.max(maxAscent, metrics.actualBoundingBoxAscent || mid * 0.8);
                            maxDescent = Math.max(maxDescent, metrics.actualBoundingBoxDescent || mid * 0.2);
                        }
                        var lineHeight = maxAscent + maxDescent;
                        var totalH = lineHeight * chars.length;
                        if (maxW <= boxW && totalH <= boxH) {
                            best = { size: mid, ascent: maxAscent, descent: maxDescent, lineHeight: lineHeight };
                            lo = mid + 1;
                        } else {
                            hi = mid - 1;
                        }
                    }
                    return best;
                }

                // Per-line scroll speed, defaulting to 50. speed <= 0 is REAL (fit-to-time:
                // 0/-1 = one pass, -N = N passes), so this must not use `|| 50`, which would
                // coerce 0 back to 50 and silently disable fit mode.
                function getLineSpeed(i) {
                    var v = window._lineSpeeds && window._lineSpeeds[i];
                    return (v === undefined || v === null) ? 50 : v;
                }
                function isFitSpeed(lineSpeed) { return lineSpeed <= 0; }
                function fitPassCount(lineSpeed) { return lineSpeed < 0 ? -lineSpeed : 1; }
                // Per-frame scroll step (in the same coordinate space as loopStart/loopEnd).
                // Fit-to-time (speed <= 0): cover `passes` complete loopStart->loopEnd
                // traversals across the whole `displayDur`, so it makes exactly that many
                // passes in the window. Otherwise a fixed px/s speed, scaled from model
                // space to that coordinate space by axisScale. Mirrors _step_for().
                function scrollStepPx(lineSpeed, loopStart, loopEnd, displayDur, fps, axisScale) {
                    if (isFitSpeed(lineSpeed)) {
                        var total = Math.abs(loopEnd - loopStart) * fitPassCount(lineSpeed);
                        return Math.max(0.1, total / Math.max(1, displayDur * fps));
                    }
                    return Math.max(1, Math.max(10, lineSpeed * 2) / fps) * axisScale;
                }
                // Simulate scroll position at the current scrub time. Fixed-speed loops
                // forever (snap back to loopStart). Fit-to-time loops for passes 1..N-1 then
                // holds at loopEnd once all N passes are done. Mirrors _animate's per-frame
                // advance in animate_lines_via_shm.
                function scrollPosAt(loopStart, loopEnd, dirSign, stepPx, scrubSeconds, fps, fitMode, passes) {
                    var frames = Math.round((scrubSeconds || 0) * fps);
                    var pos = loopStart, wraps = 0, done = false;
                    for (var f = 0; f < frames; f++) {
                        if (done) { pos = loopEnd; continue; }
                        pos += dirSign * stepPx;
                        var overshot = (dirSign < 0 && pos < loopEnd) || (dirSign > 0 && pos > loopEnd);
                        if (overshot) {
                            if (fitMode && ++wraps >= passes) { pos = loopEnd; done = true; }
                            else pos = loopStart;
                        }
                    }
                    return pos;
                }
                function getDisplayDuration() {
                    // Per-content duration field (falls back to the hidden global field).
                    var el = document.getElementById('content_duration') || document.getElementById('display_duration');
                    return (el && parseInt(el.value, 10)) || 10;
                }

                function updateBadges() {
                    for (var i = 0; i < 4; i++) {
                        var badge = document.getElementById('line_' + (i + 1) + '_pos');
                        if (!badge) continue;
                        var b = window._lineBoxes[i];
                        var txt = (b.x === -1 && b.y === -1) ? 'auto' : ('X:' + b.x + ' Y:' + b.y);
                        badge.textContent = txt;
                        badge.style.color = (i === selectedLine) ? '#4CAF50' : '#888';
                    }
                }

                // Returns the 8 resize handle points (4 corners + 4 edge midpoints) for a
                // box rect, each tagged with its handle key and CSS resize cursor.
                var HANDLE_SIZE = 8;
                function getHandlePoints(r) {
                    var midX = r.x + r.w / 2, midY = r.y + r.h / 2;
                    return {
                        nw: {x: r.x,       y: r.y,       cursor: 'nwse-resize'},
                        se: {x: r.x + r.w, y: r.y + r.h, cursor: 'nwse-resize'},
                        ne: {x: r.x + r.w, y: r.y,       cursor: 'nesw-resize'},
                        sw: {x: r.x,       y: r.y + r.h, cursor: 'nesw-resize'},
                        n:  {x: midX,      y: r.y,       cursor: 'ns-resize'},
                        s:  {x: midX,      y: r.y + r.h, cursor: 'ns-resize'},
                        e:  {x: r.x + r.w, y: midY,      cursor: 'ew-resize'},
                        w:  {x: r.x,       y: midY,      cursor: 'ew-resize'}
                    };
                }

                // Draws the box outline + (when selected) its 8 resize handles. The box
                // itself is now the visible/draggable/resizable element, replacing the old
                // text-hugging highlight rectangle.
                function drawBoxDecoration(boxX, boxY, boxW, boxH, i) {
                    ctx.save();
                    ctx.strokeStyle = (i === selectedLine) ? '#4CAF50' :
                                       (i === hoveredLine) ? 'rgba(255,255,255,0.5)' : 'rgba(255,255,255,0.2)';
                    ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
                    ctx.strokeRect(boxX, boxY, boxW, boxH);
                    ctx.restore();
                    if (i === selectedLine) {
                        ctx.save();
                        ctx.fillStyle = '#4CAF50';
                        var pts = getHandlePoints({x: boxX, y: boxY, w: boxW, h: boxH});
                        for (var key in pts) {
                            var p = pts[key];
                            ctx.fillRect(p.x - HANDLE_SIZE / 2, p.y - HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE);
                        }
                        ctx.restore();
                    }
                }

                function renderCanvasPreview() {
                    var mw = window._canvasModelW || 640;
                    var mh = window._canvasModelH || 360;

                    // Kick off loading of every line's font (no-op if already loaded/loading).
                    // ensureFontLoaded repaints when a font finishes, so a preview drawn before
                    // a custom font is ready - the common post-reboot / two-different-fonts case
                    // - corrects itself without needing to toggle the font dropdown.
                    if (typeof ensureFontLoaded === 'function') {
                        for (var _ff = 0; _ff < 4; _ff++) {
                            try { ensureFontLoaded(getLineFont(_ff)); } catch(e) {}
                        }
                    }

                    // The model fills the whole canvas -- no off-page margin. Scrolling text
                    // already starts fully hidden on its own (see the scroll-position
                    // simulation below: it starts at loop_start, past the box's own clip
                    // edge, before ever reaching the visible model area) without needing the
                    // box itself to extend past the model edge.
                    modelScaleX = canvas.width / mw;
                    modelScaleY = canvas.height / mh;
                    gutterOriginX = 0;
                    gutterOriginY = 0;
                    modelPxW = canvas.width;
                    modelPxH = canvas.height;

                    ctx.fillStyle = '#000';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    if (window._fseqBgImage) {
                        ctx.imageSmoothingEnabled = false;
                        ctx.drawImage(window._fseqBgImage, 0, 0, canvas.width, canvas.height);
                        ctx.imageSmoothingEnabled = true;
                    }

                    var posLabel = '';
                    ctx.textBaseline = 'top';

                    lineRects = [null, null, null, null];

                    // Precompute each non-empty line's scaled box height for auto-stacking
                    var boxHeights = [0, 0, 0, 0], totalStackHeight = 0;
                    for (var pi = 0; pi < 4; pi++) {
                        if (!getLineText(pi)) continue;
                        boxHeights[pi] = window._lineBoxes[pi].h * modelScaleY;
                        totalStackHeight += boxHeights[pi];
                    }
                    var stackStartY = gutterOriginY + (modelPxH - totalStackHeight) / 2;

                    var cumulativeY = stackStartY;
                    for (var i = 0; i < 4; i++) {
                        var lineText = getLineText(i);
                        if (!lineText) { lineRects[i] = null; continue; }
                        var movement = getLineMovement(i);
                        var scrolling = (movement === 'L2R' || movement === 'R2L' || movement === 'T2B' || movement === 'B2T');
                        var scrollX = (movement === 'L2R' || movement === 'R2L');
                        var scrollY = (movement === 'T2B' || movement === 'B2T');
                        var b = window._lineBoxes[i];
                        var fontName = getLineFont(i);

                        var boxW = b.w * modelScaleX, boxH = b.h * modelScaleY;
                        var boxX = b.x === -1 ? (modelPxW - boxW) / 2 : b.x * modelScaleX;
                        var boxY = b.y === -1 ? cumulativeY : b.y * modelScaleY;
                        boxX = Math.max(0, Math.min(canvas.width - boxW, boxX));
                        boxY = Math.max(0, Math.min(canvas.height - boxH, boxY));

                        // scrollY-gated below rather than forced here -- L2R/R2L stay
                        // horizontal regardless of what's stored (the Style dropdown is
                        // hidden for them), and T2B/B2T can be 'vertical_rotated'.
                        var orientation = getLineOrientation(i);

                        lineRects[i] = {x: boxX, y: boxY, w: boxW, h: boxH};
                        // Decoration (dashed border + resize handles) is drawn in a separate
                        // pass after ALL lines' text below -- drawing it here, before this
                        // line's own text, let a large glyph paint over its own handles
                        // (worse yet, one line's text could cover another line's handles
                        // too, since canvas draws are strictly back-to-front).

                        var vertScrollOriented = scrollY && (orientation === 'vertical_rotated' || orientation === 'vertical_stacked');

                        if (scrolling && !vertScrollOriented) {
                            // The font is only constrained on the CROSS axis -- the travel
                            // axis is unconstrained since the text scrolls through it, so it
                            // can use the box's full extent there rather than being capped by
                            // whichever dimension happens to be smaller. L2R/R2L travel along
                            // X, so height (boxH) is the constraint; T2B/B2T travel along Y,
                            // so width (boxW) is.
                            var fit = scrollX ? fitTextSize(lineText, fontName, Infinity, boxH)
                                               : fitTextSize(lineText, fontName, boxW, Infinity);
                            var fitSize = fit.size;
                            ctx.font = fitSize + 'px "' + fontName + '", sans-serif';
                            var textW = ctx.measureText(lineText).width;
                            var textH = fit.ascent + fit.descent;

                            // Simulate the exact backend scroll position at the current scrub
                            // time (see animate_lines_via_shm's per-frame loop): a constant
                            // per-frame step that SNAPS back to the starting edge once it fully
                            // exits the box, rather than smoothly wrapping -- so at t=0 the text
                            // sits fully off-page at its starting edge, not visible in the box
                            // like a static "sitting on the screen" snapshot. Runs in canvas-px
                            // (the step is scaled by the same model->canvas factor as everything
                            // else here) so it stays proportionally correct at any preview size.
                            var scrollFps = 30;
                            var lineSpeed = getLineSpeed(i);
                            var fitMode = isFitSpeed(lineSpeed);
                            var horizScroll = scrollX; // scrollX/scrollY already computed above
                            var loopStart, loopEnd, dirSign;
                            if (horizScroll) {
                                loopStart = (movement === 'R2L') ? (boxX + boxW) : (boxX - textW);
                                loopEnd   = (movement === 'R2L') ? (boxX - textW) : (boxX + boxW);
                                dirSign   = (movement === 'R2L') ? -1 : 1;
                            } else {
                                loopStart = (movement === 'B2T') ? (boxY + boxH) : (boxY - textH);
                                loopEnd   = (movement === 'B2T') ? (boxY - textH) : (boxY + boxH);
                                dirSign   = (movement === 'B2T') ? -1 : 1;
                            }
                            var stepPxCanvas = scrollStepPx(lineSpeed, loopStart, loopEnd,
                                getDisplayDuration(), scrollFps, horizScroll ? modelScaleX : modelScaleY);
                            var scrollPos = scrollPosAt(loopStart, loopEnd, dirSign, stepPxCanvas,
                                window._scrubSeconds, scrollFps, fitMode, fitPassCount(lineSpeed));

                            var drawX = horizScroll ? scrollPos : (boxX + Math.max(0, (boxW - textW) / 2));
                            // drawTop is the visual top of the text; fillText itself (baseline
                            // 'alphabetic') needs the baseline Y, which sits fit.ascent below
                            // that -- using the font's generic 'top' metric here (as a plain
                            // top-baseline fillText would) is what let decorative fonts render
                            // above where we thought the top was, since actualBoundingBoxAscent
                            // can exceed it.
                            var drawTop = horizScroll ? (boxY + Math.max(0, (boxH - textH) / 2)) : scrollPos;
                            var drawBaseline = drawTop + fit.ascent;

                            var arrowFont = 'bold ' + Math.max(8, Math.round(fitSize * 0.4)) + 'px sans-serif';
                            var arrowTxt = {L2R:'→', R2L:'←', T2B:'↓', B2T:'↑'}[movement];
                            ctx.save();
                            ctx.fillStyle = 'rgba(255,255,255,0.35)'; ctx.font = arrowFont; ctx.textBaseline = 'top';
                            var aw = ctx.measureText(arrowTxt).width;
                            var ax, ay;
                            if (movement === 'T2B' || movement === 'B2T') {
                                ax = boxX + (boxW - aw) / 2;
                                ay = (movement === 'B2T') ? boxY + boxH - textH - 2 : boxY + 2;
                            } else {
                                ax = (movement === 'R2L') ? boxX + boxW - aw - 2 : boxX + 2;
                                ay = boxY + 2;
                            }
                            ctx.fillText(arrowTxt, ax, ay);
                            ctx.restore();

                            // Clip to the intersection of the box and the model's true visible
                            // area - matches the runtime, where a box extending past the model
                            // edge is cut off there regardless of how far the box itself
                            // continues (there are no pixels beyond the model edge to draw into).
                            var clipX0 = Math.max(boxX, gutterOriginX), clipY0 = Math.max(boxY, gutterOriginY);
                            var clipX1 = Math.min(boxX + boxW, gutterOriginX + modelPxW), clipY1 = Math.min(boxY + boxH, gutterOriginY + modelPxH);
                            if (clipX1 > clipX0 && clipY1 > clipY0) {
                                ctx.save();
                                ctx.beginPath(); ctx.rect(clipX0, clipY0, clipX1 - clipX0, clipY1 - clipY0); ctx.clip();
                                ctx.font = fitSize + 'px "' + fontName + '", sans-serif'; ctx.textBaseline = 'alphabetic';
                                ctx.fillStyle = getLineColor(i);
                                ctx.fillText(lineText, drawX, drawBaseline);
                                ctx.restore();
                            }
                        } else if (scrolling && orientation === 'vertical_rotated' && scrollY) {
                            // T2B/B2T with rotated text: the rotated block reads sideways while
                            // travelling vertically through the box, like the horizontal-glyph
                            // scrolling branch above but with the glyphs themselves turned 90
                            // degrees. Fit: rotated width must fit boxW (centered horizontally,
                            // fixed); rotated height is unconstrained since it's the travel axis
                            // -- equivalent to fitting raw (unrotated) height against boxW with
                            // raw width free.
                            var fitTR = fitTextSize(lineText, fontName, Infinity, boxW);
                            ctx.font = fitTR.size + 'px "' + fontName + '", sans-serif';
                            var rawWTR = ctx.measureText(lineText).width;
                            var rawHTR = fitTR.ascent + fitTR.descent;
                            var rotatedW = rawHTR, rotatedH = rawWTR; // dims after rotation

                            var lineSpeedTR = getLineSpeed(i);
                            var fitModeTR = isFitSpeed(lineSpeedTR);
                            var loopStartTR = (movement === 'B2T') ? (boxY + boxH) : (boxY - rotatedH);
                            var loopEndTR   = (movement === 'B2T') ? (boxY - rotatedH) : (boxY + boxH);
                            var dirSignTR   = (movement === 'B2T') ? -1 : 1;
                            var stepPxCanvasTR = scrollStepPx(lineSpeedTR, loopStartTR, loopEndTR,
                                getDisplayDuration(), 30, modelScaleY);
                            var posTR = scrollPosAt(loopStartTR, loopEndTR, dirSignTR, stepPxCanvasTR,
                                window._scrubSeconds, 30, fitModeTR, fitPassCount(lineSpeedTR));
                            var dxTR = boxX + Math.max(0, (boxW - rotatedW) / 2);

                            var arrowFontTR = 'bold ' + Math.max(8, Math.round(fitTR.size * 0.4)) + 'px sans-serif';
                            var arrowTxtTR = (movement === 'B2T') ? '↑' : '↓';
                            ctx.save();
                            ctx.fillStyle = 'rgba(255,255,255,0.35)'; ctx.font = arrowFontTR; ctx.textBaseline = 'top';
                            var awTR = ctx.measureText(arrowTxtTR).width;
                            ctx.fillText(arrowTxtTR, boxX + (boxW - awTR) / 2, boxY + 2);
                            ctx.restore();

                            var clipX0TR = Math.max(boxX, gutterOriginX), clipY0TR = Math.max(boxY, gutterOriginY);
                            var clipX1TR = Math.min(boxX + boxW, gutterOriginX + modelPxW), clipY1TR = Math.min(boxY + boxH, gutterOriginY + modelPxH);
                            if (clipX1TR > clipX0TR && clipY1TR > clipY0TR) {
                                ctx.save();
                                ctx.beginPath(); ctx.rect(clipX0TR, clipY0TR, clipX1TR - clipX0TR, clipY1TR - clipY0TR); ctx.clip();
                                ctx.translate(dxTR + rotatedW / 2, posTR + rotatedH / 2);
                                ctx.rotate(-Math.PI / 2);
                                ctx.textBaseline = 'alphabetic';
                                ctx.fillStyle = getLineColor(i);
                                ctx.fillText(lineText, -rawWTR / 2, (fitTR.ascent - fitTR.descent) / 2);
                                ctx.restore();
                            }
                        } else if (scrolling && orientation === 'vertical_stacked' && scrollY) {
                            // T2B/B2T with stacked text: each character stays upright, one per
                            // row, and the whole stack travels vertically through the box --
                            // same simulation approach as the rotated branch above, just with
                            // the stack's own total height as the "moving" extent and no
                            // rotate transform.
                            var charsVS = lineText.split('');
                            var fitVS = fitStackedTextSize(charsVS, fontName, boxW, Infinity);
                            var totalHVS = fitVS.lineHeight * charsVS.length;

                            var lineSpeedVS = getLineSpeed(i);
                            var fitModeVS = isFitSpeed(lineSpeedVS);
                            var loopStartVS = (movement === 'B2T') ? (boxY + boxH) : (boxY - totalHVS);
                            var loopEndVS   = (movement === 'B2T') ? (boxY - totalHVS) : (boxY + boxH);
                            var dirSignVS   = (movement === 'B2T') ? -1 : 1;
                            var stepPxCanvasVS = scrollStepPx(lineSpeedVS, loopStartVS, loopEndVS,
                                getDisplayDuration(), 30, modelScaleY);
                            var posVS = scrollPosAt(loopStartVS, loopEndVS, dirSignVS, stepPxCanvasVS,
                                window._scrubSeconds, 30, fitModeVS, fitPassCount(lineSpeedVS));

                            var arrowFontVS = 'bold ' + Math.max(8, Math.round(fitVS.size * 0.4)) + 'px sans-serif';
                            var arrowTxtVS = (movement === 'B2T') ? '↑' : '↓';
                            ctx.save();
                            ctx.fillStyle = 'rgba(255,255,255,0.35)'; ctx.font = arrowFontVS; ctx.textBaseline = 'top';
                            var awVS = ctx.measureText(arrowTxtVS).width;
                            ctx.fillText(arrowTxtVS, boxX + (boxW - awVS) / 2, boxY + 2);
                            ctx.restore();

                            var clipX0VS = Math.max(boxX, gutterOriginX), clipY0VS = Math.max(boxY, gutterOriginY);
                            var clipX1VS = Math.min(boxX + boxW, gutterOriginX + modelPxW), clipY1VS = Math.min(boxY + boxH, gutterOriginY + modelPxH);
                            if (clipX1VS > clipX0VS && clipY1VS > clipY0VS) {
                                ctx.save();
                                ctx.beginPath(); ctx.rect(clipX0VS, clipY0VS, clipX1VS - clipX0VS, clipY1VS - clipY0VS); ctx.clip();
                                ctx.font = fitVS.size + 'px "' + fontName + '", sans-serif';
                                ctx.textBaseline = 'alphabetic';
                                ctx.fillStyle = getLineColor(i);
                                for (var ciVS = 0; ciVS < charsVS.length; ciVS++) {
                                    var cwVS = ctx.measureText(charsVS[ciVS]).width;
                                    var cxVS = boxX + Math.max(0, (boxW - cwVS) / 2);
                                    var cyVS = posVS + ciVS * fitVS.lineHeight + fitVS.ascent;
                                    ctx.fillText(charsVS[ciVS], cxVS, cyVS);
                                }
                                ctx.restore();
                            }
                        } else if (orientation === 'vertical_rotated') {
                            // Both axes constrained: raw width (becomes the rotated block's
                            // VERTICAL extent) must fit boxH, and raw height (becomes the
                            // rotated block's horizontal extent/thickness) must fit boxW --
                            // same "stays inside the bounding box" contract as horizontal/
                            // stacked. Leaving boxW unconstrained let short strings (e.g. a
                            // single character) pick an oversized font whose thickness blew
                            // past the box's width.
                            var fitR = fitTextSize(lineText, fontName, boxH, boxW);
                            ctx.font = fitR.size + 'px "' + fontName + '", sans-serif';
                            var rawW = ctx.measureText(lineText).width;
                            ctx.save();
                            ctx.translate(boxX + boxW / 2, boxY + boxH / 2);
                            ctx.rotate(-Math.PI / 2);
                            ctx.textBaseline = 'alphabetic';
                            ctx.fillStyle = getLineColor(i);
                            // Centers the (unrotated) text on the box's center: horizontally
                            // by its own width, vertically by the midpoint of its ascent/descent.
                            ctx.fillText(lineText, -rawW / 2, (fitR.ascent - fitR.descent) / 2);
                            ctx.restore();
                        } else if (orientation === 'vertical_stacked') {
                            var chars = lineText.split('');
                            var fitS = fitStackedTextSize(chars, fontName, boxW, boxH);
                            ctx.font = fitS.size + 'px "' + fontName + '", sans-serif';
                            ctx.textBaseline = 'alphabetic';
                            ctx.fillStyle = getLineColor(i);
                            var totalH = fitS.lineHeight * chars.length;
                            var stackTop = boxY + Math.max(0, (boxH - totalH) / 2);
                            for (var ci = 0; ci < chars.length; ci++) {
                                var cw = ctx.measureText(chars[ci]).width;
                                var cx = boxX + Math.max(0, (boxW - cw) / 2);
                                var cy = stackTop + ci * fitS.lineHeight + fitS.ascent;
                                ctx.fillText(chars[ci], cx, cy);
                            }
                        } else {
                            var fitH = fitTextSize(lineText, fontName, boxW, boxH);
                            ctx.font = fitH.size + 'px "' + fontName + '", sans-serif';
                            var textWH = ctx.measureText(lineText).width;
                            var textHH = fitH.ascent + fitH.descent;
                            var drawXH = boxX + Math.max(0, (boxW - textWH) / 2);
                            var drawBaselineH = boxY + Math.max(0, (boxH - textHH) / 2) + fitH.ascent;
                            ctx.textBaseline = 'alphabetic';
                            ctx.fillStyle = getLineColor(i);
                            ctx.fillText(lineText, drawXH, drawBaselineH);
                        }

                        if (i === selectedLine) {
                            posLabel = 'Line ' + (i+1) + (
                                (b.x === -1 && b.y === -1) ? ': auto position' : (': X:' + b.x + ' Y:' + b.y)
                            ) + '  •  ' + b.w + '×' + b.h + ' box';
                        }
                        cumulativeY += boxHeights[i];
                    }

                    // Draw all box decorations (dashed border + resize handles) after every
                    // line's text so they're always on top and never hidden behind glyphs.
                    for (var di = 0; di < 4; di++) {
                        if (lineRects[di]) drawBoxDecoration(lineRects[di].x, lineRects[di].y, lineRects[di].w, lineRects[di].h, di);
                    }

                    var posEl = document.getElementById('pos_display');
                    if (posEl) posEl.textContent = posLabel;
                    updateBadges();
                }
                window.renderCanvasPreview = renderCanvasPreview;

                function hitTestLine(cx, cy) {
                    var PAD = 8;
                    for (var i = lineRects.length - 1; i >= 0; i--) {
                        var r = lineRects[i];
                        if (!r) continue;
                        if (cx >= r.x - PAD && cx <= r.x + r.w + PAD &&
                            cy >= r.y - PAD && cy <= r.y + r.h + PAD) { return i; }
                    }
                    return -1;
                }

                // canvas.width/height are the fixed bitmap resolution (see canvas.width = 640
                // above); the element itself is CSS-stretched to width:100% of its container.
                // Every hit-test and drawn coordinate downstream operates in bitmap space, so
                // clientX/Y must be scaled from CSS pixels into that space here -- otherwise
                // mouse position drifts further from the drawn handles the wider the container
                // is than 640px (which is any real page layout), making them unreliable to grab.
                function canvasXY(e) {
                    var rect = canvas.getBoundingClientRect();
                    var scaleX = canvas.width / rect.width;
                    var scaleY = canvas.height / rect.height;
                    return { cx: (e.clientX - rect.left) * scaleX, cy: (e.clientY - rect.top) * scaleY };
                }

                // Boxes are always freely movable + resizable in both dimensions now,
                // regardless of movement type. Any of the 8 handles (4 corners + 4 edges)
                // on the selected line's box can be grabbed - corners resize both width and
                // height together, edges resize just one dimension, like a normal image
                // resize in Word/PowerPoint.
                function hitTestHandle(cx, cy) {
                    if (selectedLine < 0) return null;
                    var r = lineRects[selectedLine];
                    if (!r) return null;
                    var pts = getHandlePoints(r);
                    var PAD = 9;
                    // Corners first so they win over edges on small boxes where zones overlap
                    var order = ['nw', 'ne', 'sw', 'se', 'n', 's', 'e', 'w'];
                    for (var idx = 0; idx < order.length; idx++) {
                        var key = order[idx], p = pts[key];
                        if (Math.abs(cx - p.x) <= PAD && Math.abs(cy - p.y) <= PAD) return key;
                    }
                    return null;
                }

                var resizing = false;
                var resizeHandle = null;
                var resizeFixed = null; // {left, right, top, bottom} in MODEL space, captured at drag start

                canvas.addEventListener('mousedown', function(e) {
                    var c = canvasXY(e);
                    var handle = hitTestHandle(c.cx, c.cy);
                    if (handle) {
                        var r = lineRects[selectedLine];
                        var b = window._lineBoxes[selectedLine];
                        // Resolve any -1 (auto) position to concrete model coords from the
                        // last render - resizing needs a real edge to anchor against.
                        var curX = b.x === -1 ? Math.round(r.x / modelScaleX) : b.x;
                        var curY = b.y === -1 ? Math.round(r.y / modelScaleY) : b.y;
                        b.x = curX; b.y = curY;
                        resizing = true;
                        resizeHandle = handle;
                        resizeFixed = {left: curX, right: curX + b.w, top: curY, bottom: curY + b.h};
                        canvas.style.cursor = getHandlePoints(r)[handle].cursor;
                        e.preventDefault();
                        return;
                    }
                    var hit = hitTestLine(c.cx, c.cy);
                    selectedLine = hit;
                    if (hit >= 0) {
                        dragging = true;
                        var r2 = lineRects[hit];
                        dragOffX = c.cx - r2.x;
                        dragOffY = c.cy - r2.y;
                        canvas.style.cursor = 'grabbing';
                    }
                    renderCanvasPreview();
                    e.preventDefault();
                });

                canvas.addEventListener('mousemove', function(e) {
                    var c   = canvasXY(e);
                    var MIN_SIZE = 10;
                    if (resizing && selectedLine >= 0) {
                        var b = window._lineBoxes[selectedLine];
                        // Clamp the raw mouse position in canvas-px to the model area first,
                        // then convert once to model units.
                        var cxClamped = Math.max(0, Math.min(canvas.width,  c.cx));
                        var cyClamped = Math.max(0, Math.min(canvas.height, c.cy));
                        var mx = cxClamped / modelScaleX, my = cyClamped / modelScaleY;
                        var hasW = resizeHandle.indexOf('w') >= 0, hasE = resizeHandle.indexOf('e') >= 0;
                        var hasN = resizeHandle.indexOf('n') >= 0, hasS = resizeHandle.indexOf('s') >= 0;
                        var newLeft   = hasW ? Math.min(mx, resizeFixed.right - MIN_SIZE)  : resizeFixed.left;
                        var newRight  = hasE ? Math.max(mx, resizeFixed.left + MIN_SIZE)   : resizeFixed.right;
                        var newTop    = hasN ? Math.min(my, resizeFixed.bottom - MIN_SIZE) : resizeFixed.top;
                        var newBottom = hasS ? Math.max(my, resizeFixed.top + MIN_SIZE)    : resizeFixed.bottom;
                        b.x = Math.round(newLeft);  if (b.x === -1) b.x = -2; // -1 is the auto-position sentinel
                        b.w = Math.round(newRight - newLeft);
                        b.y = Math.round(newTop);   if (b.y === -1) b.y = -2;
                        b.h = Math.round(newBottom - newTop);
                        renderCanvasPreview();
                    } else if (dragging && selectedLine >= 0) {
                        var r2 = lineRects[selectedLine] || {w: 20, h: 20};
                        var pxX = Math.max(0, Math.min(canvas.width - r2.w, c.cx - dragOffX));
                        var pxY = Math.max(0, Math.min(canvas.height - r2.h, c.cy - dragOffY));
                        var newX = Math.round(pxX / modelScaleX);
                        var newY = Math.round(pxY / modelScaleY);
                        if (newX === -1) newX = -2; // -1 is the auto-position sentinel
                        if (newY === -1) newY = -2;
                        window._lineBoxes[selectedLine].x = newX;
                        window._lineBoxes[selectedLine].y = newY;
                        renderCanvasPreview();
                    } else if (!dragging && !resizing) {
                        var overHandle = hitTestHandle(c.cx, c.cy);
                        var prev = hoveredLine;
                        hoveredLine = hitTestLine(c.cx, c.cy);
                        if (overHandle) {
                            canvas.style.cursor = getHandlePoints(lineRects[selectedLine])[overHandle].cursor;
                        } else {
                            canvas.style.cursor = hoveredLine >= 0 ? 'grab' : 'default';
                        }
                        if (hoveredLine !== prev) renderCanvasPreview();
                    }
                });

                window.addEventListener('mouseup', function() {
                    if (dragging || resizing) {
                        dragging = false; resizing = false; resizeHandle = null; resizeFixed = null;
                        canvas.style.cursor = hoveredLine >= 0 ? 'grab' : 'default';
                        saveConfig();
                    }
                });
                canvas.addEventListener('mouseleave', function() {
                    if (!dragging && !resizing) { hoveredLine = -1; canvas.style.cursor = 'default'; renderCanvasPreview(); }
                });

                // Arrow key nudging - moves selected line 1px per press, 10px with Shift
                // saveConfig is debounced so holding a key doesn't spam the server
                var _arrowSaveTimer = null;
                document.addEventListener('keydown', function(e) {
                    if (selectedLine < 0) return;
                    var arrows = {ArrowLeft:1, ArrowRight:1, ArrowUp:1, ArrowDown:1};
                    if (!arrows[e.key]) return;
                    e.preventDefault();
                    var mw2  = window._canvasModelW || 640;
                    var mh2  = window._canvasModelH || 360;
                    var b    = window._lineBoxes[selectedLine];
                    var step = e.shiftKey ? 10 : 1;
                    // Resolve auto (-1) positions from the rendered rect so the
                    // first keypress anchors from the visual position, not from 0
                    var curX = b.x, curY = b.y;
                    var r = lineRects[selectedLine];
                    if (curX === -1 && r) curX = Math.round(r.x / modelScaleX);
                    if (curY === -1 && r) curY = Math.round(r.y / modelScaleY);
                    if (curX === -1) curX = Math.round(mw2 / 2);
                    if (curY === -1) curY = Math.round(mh2 / 2);
                    if (e.key === 'ArrowLeft')  curX = Math.max(0, curX - step);
                    if (e.key === 'ArrowRight') curX = Math.min(mw2 - 1, curX + step);
                    if (e.key === 'ArrowUp')    curY = Math.max(0, curY - step);
                    if (e.key === 'ArrowDown')  curY = Math.min(mh2 - 1, curY + step);
                    if (curX === -1) curX = -2; // -1 is the auto-position sentinel
                    if (curY === -1) curY = -2;
                    b.x = curX; b.y = curY;
                    renderCanvasPreview();
                    clearTimeout(_arrowSaveTimer);
                    _arrowSaveTimer = setTimeout(saveConfig, 300);
                });

                window.resetLine = function(i) {
                    window._lineBoxes[i].x = -1;
                    window._lineBoxes[i].y = -1;
                    renderCanvasPreview(); saveConfig();
                };
                window.resetAllLines = function() {
                    window._lineBoxes.forEach(function(b) { b.x = -1; b.y = -1; });
                    selectedLine = -1;
                    renderCanvasPreview(); saveConfig();
                };

                // Re-render on text / color / font changes
                for (var li = 1; li <= 4; li++) {
                    (function(el) { if (el) el.addEventListener('input', renderCanvasPreview); })(document.getElementById('line_' + li));
                    (function(el) { if (el) el.addEventListener('input', renderCanvasPreview); })(document.getElementById('line_' + li + '_color'));
                }

                updateBadges();
                renderCanvasPreview();
            }

            // ---------------------------------------------------------------------------
            // Canvas background preview - supports FSEQ (.fseq), video (vid:), image (img:)
            // ---------------------------------------------------------------------------
            (function() {
                var _fseqMeta   = null;
                var _fseqSeq    = null;   // clean FSEQ name: no seq: prefix, no .fseq suffix
                var _contentType = null;  // 'seq', 'vid', or 'img'
                var _contentFile = null;  // filename (vid:/img:) or clean seq name (seq:)
                window._fseqBgImage = null;

                function fmtTime(ms) {
                    var s = Math.floor(ms / 1000);
                    var m = Math.floor(s / 60);
                    s = s % 60;
                    return m + ':' + (s < 10 ? '0' : '') + s;
                }

                // Returns {type, file} for the configured Names Display content, or null.
                function getConfiguredContent() {
                    // Background for the preview = the SELECTED names content item, or the
                    // waiting content when no names content is configured.
                    var val = '';
                    var lst = window._namesContentList || [];
                    var idx = window._namesSelectedIndex;
                    if (lst.length > 0 && idx != null && idx >= 0 && idx < lst.length) {
                        val = lst[idx].content || '';
                    }
                    if (!val) {
                        var defaultDp = document.getElementById('default_playlist');
                        val = defaultDp ? defaultDp.value : '';
                    }
                    if (!val) return null;
                    if (val.startsWith('seq:')) {
                        return { type: 'seq', file: val.replace(/^seq:/, '').replace(/\.fseq$/, '') };
                    }
                    if (val.startsWith('vid:')) {
                        return { type: 'vid', file: val.replace(/^vid:/, '') };
                    }
                    if (val.startsWith('img:')) {
                        return { type: 'img', file: val.replace(/^img:/, '') };
                    }
                    return null;  // plain playlist - no canvas preview
                }

                // ===================== Names Content List =====================
                window._namesContentList = Array.isArray(window._namesContentListInit) ? window._namesContentListInit : [];
                window._namesMode = window._namesContentModeInit || 'roundrobin';
                window._namesSelectedIndex = (window._namesContentList.length > 0) ? 0 : -1;

                function _blankLayout() {
                    return {
                        message_lines: ['', '', '', ''],
                        line_boxes: [{x:-1,y:-1,w:300,h:60},{x:-1,y:-1,w:300,h:60},{x:-1,y:-1,w:300,h:60},{x:-1,y:-1,w:300,h:60}],
                        line_colors: ['','','',''],
                        line_movements: ['Center','Center','Center','Center'],
                        line_speeds: [50,50,50,50],
                        line_fonts: ['FreeSans','FreeSans','FreeSans','FreeSans'],
                        line_orientations: ['horizontal','horizontal','horizontal','horizontal'],
                        display_duration: parseInt(window._flatDisplayDurationInit) || 30
                    };
                }

                // Read the current editor DOM + window buffers into a layout object.
                function collectEditorLayout() {
                    function gv(id){ var el=document.getElementById(id); return el?el.value:''; }
                    return {
                        message_lines: [gv('line_1'),gv('line_2'),gv('line_3'),gv('line_4')],
                        line_boxes: (window._lineBoxes||[]).slice(0,4).map(function(b){return {x:b.x,y:b.y,w:b.w,h:b.h};}),
                        line_colors: [1,2,3,4].map(function(n){var el=document.getElementById('line_'+n+'_color'); return el?el.value.toUpperCase():'';}),
                        line_movements: (window._lineMovements||['Center','Center','Center','Center']).slice(0,4),
                        line_speeds: (window._lineSpeeds||[50,50,50,50]).slice(0,4),
                        line_fonts: [1,2,3,4].map(function(n){var el=document.getElementById('line_'+n+'_font'); return (el&&el.value)?el.value:'FreeSans';}),
                        line_orientations: (window._lineOrientations||['horizontal','horizontal','horizontal','horizontal']).slice(0,4),
                        display_duration: parseInt(gv('content_duration'))||30
                    };
                }

                // Write a layout into the editor DOM + window buffers, then refresh preview.
                function applyLayoutToEditor(L) {
                    L = L || _blankLayout();
                    var lb = L.line_boxes || [];
                    window._lineBoxes = [];
                    for (var i=0;i<4;i++){ var b=lb[i]||{x:-1,y:-1,w:300,h:60}; window._lineBoxes.push({x:b.x,y:b.y,w:b.w,h:b.h}); }
                    window._lineMovements = (L.line_movements||[]).slice(0,4); while(window._lineMovements.length<4) window._lineMovements.push('Center');
                    window._lineSpeeds = (L.line_speeds||[]).slice(0,4); while(window._lineSpeeds.length<4) window._lineSpeeds.push(50);
                    window._lineOrientations = (L.line_orientations||[]).slice(0,4); while(window._lineOrientations.length<4) window._lineOrientations.push('horizontal');
                    var ml=L.message_lines||[], lc=L.line_colors||[], lf=L.line_fonts||[];
                    for (var n=0;n<4;n++){
                        var t=document.getElementById('line_'+(n+1)); if(t) t.value=ml[n]||'';
                        var c=document.getElementById('line_'+(n+1)+'_color'); if(c) c.value=(lc[n]||'#FF0000');
                        var mv=document.getElementById('line_'+(n+1)+'_movement'); if(mv) mv.value=window._lineMovements[n];
                        var fo=document.getElementById('line_'+(n+1)+'_font'); if(fo && lf[n]) fo.value=lf[n];
                        var oro=document.getElementById('line_'+(n+1)+'_orientation'); if(oro) oro.value=window._lineOrientations[n];
                        var sp=window._lineSpeeds[n];
                        var au=document.getElementById('line_'+(n+1)+'_speed_auto'); if(au) au.checked=(sp<=0);
                        var se=document.getElementById('line_'+(n+1)+'_speed'); if(se) se.value=(sp>0?sp:50);
                        var pa=document.getElementById('line_'+(n+1)+'_passes'); if(pa) pa.value=(sp<0?(-sp):1);
                        var sw=document.getElementById('line_'+(n+1)+'_speed_wrap'); if(sw) sw.style.display=(sp<=0)?'none':'';
                        var pw=document.getElementById('line_'+(n+1)+'_passes_wrap'); if(pw) pw.style.display=(sp<=0)?'':'none';
                        if (typeof updateLineSpeedRowVisibility==='function') updateLineSpeedRowVisibility(n);
                        if (typeof updateLineOrientationRowVisibility==='function') updateLineOrientationRowVisibility(n);
                    }
                    var d=document.getElementById('content_duration'); if(d) d.value=L.display_duration||30;
                    if (typeof window.renderCanvasPreview==='function') window.renderCanvasPreview();
                }

                // Flush the editor into the currently-selected item (mirror duration to the
                // hidden global field when there is no list).
                function flushEditorToSelected() {
                    var lst=window._namesContentList||[];
                    var idx=window._namesSelectedIndex;
                    if (lst.length>0 && idx>=0 && idx<lst.length) {
                        var L=collectEditorLayout(); var it=lst[idx];
                        it.message_lines=L.message_lines; it.line_boxes=L.line_boxes; it.line_colors=L.line_colors;
                        it.line_movements=L.line_movements; it.line_speeds=L.line_speeds;
                        it.line_orientations=L.line_orientations; it.display_duration=L.display_duration;
                        // Only capture fonts once the font dropdowns are populated, else an
                        // early autosave would overwrite real fonts with the FreeSans default.
                        if (window._fontsReady) it.line_fonts=L.line_fonts;
                    } else {
                        var d=document.getElementById('content_duration'); var hd=document.getElementById('display_duration');
                        if (d && hd) hd.value = parseInt(d.value)||30;
                    }
                }

                // Render the Name Display list + preview dropdown + mode toggle + none warning.
                function renderNamesList() {
                    var lst=window._namesContentList||[];
                    var box=document.getElementById('names_content_items');
                    if (box) {
                        box.innerHTML='';
                        if (lst.length===0) {
                            box.innerHTML='<div style="font-size:13px;color:#777;">No content added - names show over the Waiting content.</div>';
                        } else {
                            lst.forEach(function(it, i){
                                var row=document.createElement('div');
                                var isSel=(i===window._namesSelectedIndex);
                                row.style.cssText='display:flex;align-items:center;gap:8px;padding:5px 6px;border-bottom:1px solid #eee;border-radius:3px;'+(isSel?'background:#e3f2fd;':'');
                                var label=document.createElement('span');
                                label.style.cssText='flex:1;font-size:13px;color:#333;cursor:pointer;';
                                label.textContent=(i+1)+'. '+(it.content||'(none)');
                                label.title='Click to edit this content’s text on the Display tab';
                                label.onclick=function(){ selectNamesItem(i); };
                                var del=document.createElement('button');
                                del.type='button'; del.textContent='✕'; del.title='Remove';
                                del.style.cssText='background:#f44336;border:none;color:#fff;padding:2px 9px;border-radius:3px;cursor:pointer;font-size:12px;';
                                del.onclick=function(){ removeNamesItem(i); };
                                row.appendChild(label); row.appendChild(del);
                                box.appendChild(row);
                            });
                        }
                    }
                    var modeRow=document.getElementById('names_mode_row');
                    if (modeRow) modeRow.style.display=(lst.length>1)?'block':'none';
                    var rr=document.querySelector('input[name="names_mode"][value="roundrobin"]');
                    var rnd=document.querySelector('input[name="names_mode"][value="random"]');
                    if (rr) rr.checked=(window._namesMode!=='random');
                    if (rnd) rnd.checked=(window._namesMode==='random');
                    var warn=document.getElementById('name_display_none_warning');
                    if (warn) warn.style.display=(lst.length===0)?'block':'none';
                    var sel=document.getElementById('preview_content_select');
                    if (sel) { sel.innerHTML=''; lst.forEach(function(it,i){ sel.appendChild(new Option((i+1)+'. '+(it.content||'(none)'), i, false, i===window._namesSelectedIndex)); }); }
                    var prow=document.getElementById('preview_content_row');
                    if (prow) prow.style.display=(lst.length>1)?'block':'none';
                    var scope=document.getElementById('content_duration_scope');
                    if (scope) scope.textContent=(lst.length>0)?('- for content '+(window._namesSelectedIndex+1)):'- shown over waiting content';
                }

                // ---- Manage Content modal (Available <-> Names list, with arrows) ----
                function _mngAvailableOptions() {
                    // Hide content already in the Names list (right side).
                    var used={}; (window._namesContentList||[]).forEach(function(it){ used[it.content]=true; });
                    var out=[];
                    (window._fppSeqList||[]).forEach(function(s){ var v='seq:'+s; if(!used[v]) out.push({val:v, label:'🎬 '+s}); });
                    (window._fppImgList||[]).forEach(function(im){ var v='img:'+im; if(!used[v]) out.push({val:v, label:'🖼️ '+im}); });
                    return out;
                }
                function _mngRenderAvailable() {
                    var sel=document.getElementById('mng_available'); if(!sel) return;
                    sel.innerHTML='';
                    _mngAvailableOptions().forEach(function(o){ sel.appendChild(new Option(o.label, o.val)); });
                }
                function _mngRenderSelected(keepIdx) {
                    var sel=document.getElementById('mng_selected'); if(!sel) return;
                    sel.innerHTML='';
                    (window._namesContentList||[]).forEach(function(it,i){ sel.appendChild(new Option((i+1)+'. '+(it.content||'(none)'), i)); });
                    if (keepIdx!=null && keepIdx>=0 && keepIdx<sel.options.length) sel.options[keepIdx].selected=true;
                }
                function openManageContentModal() {
                    flushEditorToSelected();          // don't lose current edits
                    _mngRenderAvailable();
                    _mngRenderSelected();
                    // Scroll to top so the top-aligned fixed modal is in view inside the iframe.
                    try { window.scrollTo(0,0); window.parent.postMessage({type:'scrollTop'},'*'); } catch(e) {}
                    var m=document.getElementById('manage_content_modal'); if(m) m.style.display='flex';
                }
                function closeManageContentModal() {
                    var m=document.getElementById('manage_content_modal'); if(m) m.style.display='none';
                    var lst=window._namesContentList||[];
                    if (window._namesSelectedIndex>=lst.length) window._namesSelectedIndex=lst.length-1;
                    if (lst.length>0 && window._namesSelectedIndex<0) window._namesSelectedIndex=0;
                    renderNamesList();
                    if (window._namesSelectedIndex>=0) applyLayoutToEditor(lst[window._namesSelectedIndex]);
                    if (typeof window.toggleFseqPreview==='function') window.toggleFseqPreview();
                    saveConfig();
                }
                // A freshly-added content defaults to showing the texter's {name} on
                // line 1, centered (position + movement). Never clobber text the user
                // has already typed into any line.
                function _seedNamePlaceholder(item) {
                    var ml = item.message_lines || ['','','',''];
                    var hasText = ml.some(function(s){ return (s||'').trim() !== ''; });
                    if (!hasText) {
                        item.message_lines = ['{name}','','',''];
                        item.line_boxes = item.line_boxes || [];
                        item.line_boxes[0] = {x:-1,y:-1,w:300,h:60};   // -1,-1 = centered
                        item.line_movements = item.line_movements || ['Center','Center','Center','Center'];
                        item.line_movements[0] = 'Center';
                    }
                    return item;
                }
                function mngAdd() {
                    var av=document.getElementById('mng_available'); if(!av) return;
                    var chosen=Array.prototype.filter.call(av.options,function(o){return o.selected;}).map(function(o){return o.value;});
                    if (!chosen.length) return;
                    var lst=window._namesContentList;
                    chosen.forEach(function(val){
                        var item;
                        if (lst.length===0) { item=collectEditorLayout(); item.content=val; }  // seed first from current editor
                        else { item=_blankLayout(); item.content=val; }
                        _seedNamePlaceholder(item);
                        lst.push(item);
                    });
                    _mngRenderSelected(lst.length-1);
                    _mngRenderAvailable();   // hide the newly-added items from the left list
                }
                function mngRemove() {
                    var sel=document.getElementById('mng_selected'); if(!sel) return;
                    var idxs=Array.prototype.filter.call(sel.options,function(o){return o.selected;}).map(function(o){return parseInt(o.value);});
                    if (!idxs.length) return;
                    idxs.sort(function(a,b){return b-a;}).forEach(function(i){ window._namesContentList.splice(i,1); });
                    _mngRenderSelected();
                    _mngRenderAvailable();   // removed items become available again
                }
                function mngMoveUp() {
                    var sel=document.getElementById('mng_selected'); if(!sel) return;
                    var i=sel.selectedIndex; if(i<=0) return;
                    var lst=window._namesContentList;
                    var tmp=lst[i-1]; lst[i-1]=lst[i]; lst[i]=tmp;
                    _mngRenderSelected(i-1);
                }
                function mngMoveDown() {
                    var sel=document.getElementById('mng_selected'); if(!sel) return;
                    var i=sel.selectedIndex; var lst=window._namesContentList;
                    if(i<0||i>=lst.length-1) return;
                    var tmp=lst[i+1]; lst[i+1]=lst[i]; lst[i]=tmp;
                    _mngRenderSelected(i+1);
                }
                function removeNamesItem(i) {
                    var lst=window._namesContentList; if (i<0||i>=lst.length) return;
                    flushEditorToSelected();
                    lst.splice(i,1);
                    if (window._namesSelectedIndex>=lst.length) window._namesSelectedIndex=lst.length-1;
                    if (lst.length===0) window._namesSelectedIndex=-1;
                    renderNamesList();
                    if (window._namesSelectedIndex>=0) applyLayoutToEditor(lst[window._namesSelectedIndex]);
                    if (typeof window.toggleFseqPreview==='function') window.toggleFseqPreview();
                    saveConfig();
                }
                function selectNamesItem(i) {
                    var lst=window._namesContentList; if (i<0||i>=lst.length) return;
                    flushEditorToSelected();      // capture edits to the item we're leaving
                    window._namesSelectedIndex=i;
                    applyLayoutToEditor(lst[i]);
                    renderNamesList();
                    if (typeof window.toggleFseqPreview==='function') window.toggleFseqPreview();
                    if (typeof saveConfig==='function') saveConfig();  // persist the flushed edits
                }
                function onPreviewContentChange(){ var sel=document.getElementById('preview_content_select'); if(sel) selectNamesItem(parseInt(sel.value)); }
                function onNamesModeChange(mode){ window._namesMode=(mode==='random')?'random':'roundrobin'; saveConfig(); }
                function onContentDurationChange(){
                    flushEditorToSelected();
                    // Re-cap the background scrubber and re-fit "Fit to time" scroll lines to
                    // the new duration.
                    if (typeof window.toggleFseqPreview==='function') window.toggleFseqPreview();
                    else if (typeof window.renderCanvasPreview==='function') window.renderCanvasPreview();
                    saveConfig();
                }

                function initNamesUI() {
                    var lst=window._namesContentList||[];
                    // Only pick the initial selection / duration ONCE (loadFPPData may re-run
                    // on a list refresh; don't stomp the user's current editor selection then).
                    if (!window._namesUIInited) {
                        // Migrate a pre-list single Name content into the list on first load,
                        // seeded with the flat text layout the server just rendered, so an
                        // upgrading user's existing setup appears as content #1 and is editable.
                        if (lst.length===0 && window._flatNameContentInit) {
                            var seed = collectEditorLayout();
                            seed.content = window._flatNameContentInit;
                            seed.display_duration = parseInt(window._flatDisplayDurationInit)||30;
                            if (window._lineFontsInit && window._lineFontsInit.length) seed.line_fonts = window._lineFontsInit.slice(0,4);
                            lst.push(seed);
                            window._namesContentList = lst;
                        }
                        window._namesSelectedIndex=(lst.length>0)?0:-1;
                        var d=document.getElementById('content_duration');
                        if (d) d.value=(lst.length>0)?(lst[0].display_duration||30):(parseInt(window._flatDisplayDurationInit)||30);
                        window._namesUIInited=true;
                    }
                    if (window._namesSelectedIndex>=lst.length) window._namesSelectedIndex=lst.length-1;
                    renderNamesList();
                }

                window.openManageContentModal=openManageContentModal;
                window.closeManageContentModal=closeManageContentModal;
                window.mngAdd=mngAdd;
                window.mngRemove=mngRemove;
                window.mngMoveUp=mngMoveUp;
                window.mngMoveDown=mngMoveDown;
                window.removeNamesItem=removeNamesItem;
                window.selectNamesItem=selectNamesItem;
                window.onPreviewContentChange=onPreviewContentChange;
                window.onNamesModeChange=onNamesModeChange;
                window.onContentDurationChange=onContentDurationChange;
                window.initNamesUI=initNamesUI;
                window.renderNamesList=renderNamesList;
                window.flushEditorToSelected=flushEditorToSelected;
                window.collectEditorLayout=collectEditorLayout;
                window.applyLayoutToEditor=applyLayoutToEditor;

                // Remote only: copy the Master's text layout for the selected content, scaling
                // the box positions/sizes from the Master's overlay model to THIS projector's
                // model so it lands in the same relative spot. Auto-centered boxes (x/y = -1)
                // stay auto, so they adapt regardless of model size.
                function syncPositionFromMaster(btn) {
                    var lst = window._namesContentList || [];
                    var idx = window._namesSelectedIndex;
                    var content = (lst[idx] && lst[idx].content) || '';
                    var status = document.getElementById('sync_pos_status');
                    if (!content) { if(status){status.style.color='#f44336'; status.textContent='Pick a content first.';} return; }
                    if (btn) btn.disabled = true;
                    if (status){ status.style.color='#888'; status.textContent='Fetching from master…'; }
                    fetch('/api/plugin/master-layout?content=' + encodeURIComponent(content))
                      .then(function(r){ return r.json(); })
                      .then(function(d){
                        if (!d || !d.found) {
                            if(status){ status.style.color='#f44336'; status.textContent = (d && d.error) ? d.error : 'Master has no layout for this content.'; }
                            return;
                        }
                        var L = d.layout || {};
                        var rw = parseInt(document.getElementById('overlay_model_width').value)||0;
                        var rh = parseInt(document.getElementById('overlay_model_height').value)||0;
                        var sx = (d.model_w>0 && rw>0) ? (rw/d.model_w) : 1;
                        var sy = (d.model_h>0 && rh>0) ? (rh/d.model_h) : 1;
                        var boxes = (L.line_boxes||[]).map(function(b){
                            b = b || {x:-1,y:-1,w:300,h:60};
                            return { x: (b.x<0?b.x:Math.round(b.x*sx)),
                                     y: (b.y<0?b.y:Math.round(b.y*sy)),
                                     w: Math.max(1, Math.round((b.w||300)*sx)),
                                     h: Math.max(1, Math.round((b.h||60)*sy)) };
                        });
                        applyLayoutToEditor({
                            message_lines: L.message_lines, line_boxes: boxes, line_colors: L.line_colors,
                            line_movements: L.line_movements, line_speeds: L.line_speeds,
                            line_fonts: L.line_fonts, line_orientations: L.line_orientations,
                            display_duration: L.display_duration
                        });
                        flushEditorToSelected();
                        if (typeof saveConfig==='function') saveConfig();
                        if(status){ status.style.color='#4CAF50'; status.textContent = (sx===1&&sy===1) ? '✓ Synced from master' : '✓ Synced + scaled to this model'; }
                      })
                      .catch(function(){ if(status){ status.style.color='#f44336'; status.textContent='Could not reach master.'; } })
                      .finally(function(){ if (btn) btn.disabled = false; });
                }
                window.syncPositionFromMaster = syncPositionFromMaster;

                // ===================== Waiting Content Rotation List =====================
                window._waitingContentList = Array.isArray(window._waitingContentListInit) ? window._waitingContentListInit : [];
                window._waitingMode = window._waitingContentModeInit || 'roundrobin';

                // A content value is "missing" if it names a seq:/img: file FPP no longer has.
                function _waitingIsMissing(val) {
                    if (!val) return false;
                    if (val.indexOf('seq:')===0) return (window._fppSeqList||[]).indexOf(val.slice(4))<0;
                    if (val.indexOf('img:')===0) return (window._fppImgList||[]).indexOf(val.slice(4))<0;
                    return false;
                }

                // Render the Waiting list rows + mode toggle + none-warning, and keep the hidden
                // legacy default_playlist select synced to the first item (drives preview + save).
                function renderWaitingList() {
                    var lst=window._waitingContentList||[];
                    var box=document.getElementById('waiting_content_items');
                    if (box) {
                        box.innerHTML='';
                        if (lst.length===0) {
                            box.innerHTML='<div style="font-size:13px;color:#777;">No content added yet - click below to choose the sequence(s) that loop while waiting.</div>';
                        } else {
                            lst.forEach(function(it, i){
                                var row=document.createElement('div');
                                row.style.cssText='display:flex;align-items:center;gap:8px;padding:5px 6px;border-bottom:1px solid #eee;border-radius:3px;';
                                var label=document.createElement('span');
                                label.style.cssText='flex:1;font-size:13px;color:#333;';
                                var miss=_waitingIsMissing(it.content);
                                label.textContent=(i+1)+'. '+(it.content||'(none)')+(miss?'  ⚠ missing':'');
                                if (miss) label.style.color='#c62828';
                                row.appendChild(label);
                                // Duration control: sequences play their full length; images have
                                // no natural length, so expose an editable seconds field (default 30).
                                if ((it.content||'').indexOf('img:')===0) {
                                    var dwrap=document.createElement('span');
                                    dwrap.style.cssText='font-size:12px;color:#555;display:flex;align-items:center;gap:4px;';
                                    var dnum=document.createElement('input');
                                    dnum.type='number'; dnum.min='1'; dnum.max='3600';
                                    dnum.value=parseInt(it.display_duration)||30;
                                    dnum.style.cssText='width:56px;padding:2px 4px;font-size:12px;';
                                    dnum.title='How long this image shows before rotating';
                                    dnum.onchange=function(){
                                        var v=parseInt(dnum.value)||30; if(v<1)v=1;
                                        dnum.value=v; it.display_duration=v; saveConfig();
                                    };
                                    dwrap.appendChild(dnum);
                                    var secs=document.createElement('span'); secs.textContent='sec';
                                    dwrap.appendChild(secs);
                                    row.appendChild(dwrap);
                                } else if ((it.content||'').indexOf('seq:')===0) {
                                    var note=document.createElement('span');
                                    note.style.cssText='font-size:11px;color:#999;';
                                    note.textContent='full length';
                                    row.appendChild(note);
                                }
                                var del=document.createElement('button');
                                del.type='button'; del.textContent='✕'; del.title='Remove';
                                del.style.cssText='background:#f44336;border:none;color:#fff;padding:2px 9px;border-radius:3px;cursor:pointer;font-size:12px;';
                                del.onclick=function(){ removeWaitingItem(i); };
                                row.appendChild(del);
                                box.appendChild(row);
                            });
                        }
                    }
                    var modeRow=document.getElementById('waiting_mode_row');
                    if (modeRow) modeRow.style.display=(lst.length>1)?'block':'none';
                    var rr=document.querySelector('input[name="waiting_mode"][value="roundrobin"]');
                    var rnd=document.querySelector('input[name="waiting_mode"][value="random"]');
                    if (rr) rr.checked=(window._waitingMode!=='random');
                    if (rnd) rnd.checked=(window._waitingMode==='random');
                    var warn=document.getElementById('waiting_content_none_warning');
                    if (warn) warn.style.display=(lst.length===0)?'block':'none';
                    // Sync hidden legacy select to the first item so the preview background and
                    // the saved default_playlist both track the list.
                    var dp=document.getElementById('default_playlist');
                    if (dp) {
                        var first=(lst.length>0)?(lst[0].content||''):'';
                        var has=Array.prototype.some.call(dp.options,function(o){return o.value===first;});
                        if (!has && first) dp.add(new Option(first, first));
                        dp.value=first;
                    }
                    if (typeof window.toggleFseqPreview==='function') window.toggleFseqPreview();
                }

                function _wmngAvailableOptions() {
                    var used={}; (window._waitingContentList||[]).forEach(function(it){ used[it.content]=true; });
                    var out=[];
                    (window._fppSeqList||[]).forEach(function(s){ var v='seq:'+s; if(!used[v]) out.push({val:v, label:'🎬 '+s}); });
                    (window._fppImgList||[]).forEach(function(im){ var v='img:'+im; if(!used[v]) out.push({val:v, label:'🖼️ '+im}); });
                    return out;
                }
                function _wmngRenderAvailable() {
                    var sel=document.getElementById('wmng_available'); if(!sel) return;
                    sel.innerHTML='';
                    _wmngAvailableOptions().forEach(function(o){ sel.appendChild(new Option(o.label, o.val)); });
                }
                function _wmngRenderSelected(keepIdx) {
                    var sel=document.getElementById('wmng_selected'); if(!sel) return;
                    sel.innerHTML='';
                    (window._waitingContentList||[]).forEach(function(it,i){ sel.appendChild(new Option((i+1)+'. '+(it.content||'(none)'), i)); });
                    if (keepIdx!=null && keepIdx>=0 && keepIdx<sel.options.length) sel.options[keepIdx].selected=true;
                }
                function openManageWaitingModal() {
                    _wmngRenderAvailable();
                    _wmngRenderSelected();
                    try { window.scrollTo(0,0); window.parent.postMessage({type:'scrollTop'},'*'); } catch(e) {}
                    var m=document.getElementById('manage_waiting_modal'); if(m) m.style.display='flex';
                }
                function closeManageWaitingModal() {
                    var m=document.getElementById('manage_waiting_modal'); if(m) m.style.display='none';
                    renderWaitingList();
                    saveConfig();
                }
                function wmngAdd() {
                    var av=document.getElementById('wmng_available'); if(!av) return;
                    var chosen=Array.prototype.filter.call(av.options,function(o){return o.selected;}).map(function(o){return o.value;});
                    if (!chosen.length) return;
                    var lst=window._waitingContentList;
                    chosen.forEach(function(val){ lst.push({content:val, display_duration:30}); });
                    _wmngRenderSelected(lst.length-1);
                    _wmngRenderAvailable();
                }
                function wmngRemove() {
                    var sel=document.getElementById('wmng_selected'); if(!sel) return;
                    var idxs=Array.prototype.filter.call(sel.options,function(o){return o.selected;}).map(function(o){return parseInt(o.value);});
                    if (!idxs.length) return;
                    idxs.sort(function(a,b){return b-a;}).forEach(function(i){ window._waitingContentList.splice(i,1); });
                    _wmngRenderSelected();
                    _wmngRenderAvailable();
                }
                function wmngMoveUp() {
                    var sel=document.getElementById('wmng_selected'); if(!sel) return;
                    var i=sel.selectedIndex; if(i<=0) return;
                    var lst=window._waitingContentList;
                    var tmp=lst[i-1]; lst[i-1]=lst[i]; lst[i]=tmp;
                    _wmngRenderSelected(i-1);
                }
                function wmngMoveDown() {
                    var sel=document.getElementById('wmng_selected'); if(!sel) return;
                    var i=sel.selectedIndex; var lst=window._waitingContentList;
                    if(i<0||i>=lst.length-1) return;
                    var tmp=lst[i+1]; lst[i+1]=lst[i]; lst[i]=tmp;
                    _wmngRenderSelected(i+1);
                }
                function removeWaitingItem(i) {
                    var lst=window._waitingContentList; if (i<0||i>=lst.length) return;
                    lst.splice(i,1);
                    renderWaitingList();
                    saveConfig();
                }
                function onWaitingModeChange(mode){ window._waitingMode=(mode==='random')?'random':'roundrobin'; saveConfig(); }

                function initWaitingUI() {
                    var lst=window._waitingContentList||[];
                    if (!window._waitingUIInited) {
                        // Migrate a pre-list single Waiting content into the list on first load so
                        // an upgrading user's existing selection becomes item #1.
                        if (lst.length===0 && window._flatDefaultContentInit) {
                            lst.push({content:window._flatDefaultContentInit, display_duration:30});
                            window._waitingContentList=lst;
                        }
                        window._waitingUIInited=true;
                    }
                    renderWaitingList();
                }

                window.openManageWaitingModal=openManageWaitingModal;
                window.closeManageWaitingModal=closeManageWaitingModal;
                window.wmngAdd=wmngAdd;
                window.wmngRemove=wmngRemove;
                window.wmngMoveUp=wmngMoveUp;
                window.wmngMoveDown=wmngMoveDown;
                window.removeWaitingItem=removeWaitingItem;
                window.onWaitingModeChange=onWaitingModeChange;
                window.initWaitingUI=initWaitingUI;
                window.renderWaitingList=renderWaitingList;

                window.toggleFseqPreview = function() {
                    var ct = getConfiguredContent();
                    var loadEl = document.getElementById('fseq_load_status');
                    if (!ct) {
                        if (loadEl) {
                            loadEl.style.color = '#ff9800';
                            var isRemote = ((document.getElementById('plugin_role')||{}).value) === 'remote';
                            if (!isRemote) {
                                loadEl.textContent = '\u26a0 Select a .fseq, video, or image as Waiting or Names content for background preview.';
                                return;
                            }
                            // On a remote there is no local content picker - content is pushed by
                            // the chosen plugin master. Point the user at the missing step: either
                            // pick a master, or (if one is picked) set content on that master.
                            loadEl.textContent = '\u26a0 Checking master...';
                            fetch('/api/plugin/masters').then(function(r){ return r.json(); }).then(function(d){
                                if (((document.getElementById('plugin_role')||{}).value) !== 'remote') return;
                                if (getConfiguredContent()) { loadBgPreview(); return; }
                                loadEl.style.color = '#ff9800';
                                if (!d || !d.selected) {
                                    loadEl.textContent = '\u26a0 No Master selected. Select a Master above to configure content.';
                                } else {
                                    loadEl.textContent = '\u26a0 No content from the Master yet. Configure content on the Master.';
                                }
                            }).catch(function(){
                                loadEl.style.color = '#ff9800';
                                loadEl.textContent = '\u26a0 Select a Master above to configure content.';
                            });
                        }
                        return;
                    }
                    loadBgPreview();
                };

                function _clearState() {
                    window._fseqBgImage = null;
                    _fseqMeta = null;
                    _fseqSeq  = null;
                    _contentType = null;
                    _contentFile = null;
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                }

                // Seconds to cap the preview scrubber at: the SELECTED names content's own
                // display_duration (from the data model, which is correct from page load),
                // falling back to the DOM duration fields. Reading the data model avoids a
                // timing bug where loadBgPreview ran before content_duration was populated and
                // fell back to the global 30s.
                function _previewCapSeconds() {
                    var lst = window._namesContentList || [];
                    var idx = window._namesSelectedIndex;
                    if (lst.length > 0 && idx != null && idx >= 0 && idx < lst.length) {
                        var d = parseInt(lst[idx].display_duration);
                        if (d > 0) return d;
                    }
                    var el = document.getElementById('content_duration') || document.getElementById('display_duration');
                    return (el && parseInt(el.value)) || 30;
                }

                function loadBgPreview() {
                    var ct = getConfiguredContent();
                    if (!ct) return;
                    _contentType = ct.type;
                    _contentFile = ct.file;
                    _fseqSeq     = (ct.type === 'seq') ? ct.file : null;

                    var loadEl = document.getElementById('fseq_load_status');
                    var scrubHint = document.getElementById('fseq_scrub_hint');
                    loadEl.textContent = 'Loading\u2026';
                    loadEl.style.color = '#aaa';
                    if (scrubHint) scrubHint.style.display = (ct.type === 'img') ? 'none' : '';

                    if (ct.type === 'seq') {
                        // ---- FSEQ: fetch info then show scrubber ----
                        var model = document.getElementById('overlay_model_name').value || '';
                        fetch('/api/fseq/info?sequence=' + encodeURIComponent(ct.file)
                                              + '&model=' + encodeURIComponent(model))
                            .then(function(r) { return r.json(); })
                            .then(function(data) {
                                if (data.error) {
                                    loadEl.textContent = '\u2717 ' + data.error;
                                    loadEl.style.color = '#f44336';
                                    return;
                                }
                                _fseqMeta = data;
                                if (data.detected_start_channel) {
                                    loadEl.textContent = '';
                                } else {
                                    loadEl.textContent = '\u26a0 Overlay model not found \u2014 verify model name in settings';
                                    loadEl.style.color = '#ff9800';
                                }
                                // The background always restarts from 0 and is cut off after
                                // display_duration seconds each time a message shows (see
                                // send_to_fpp/display loop) -- anything past that point in the
                                // FSEQ is never actually seen behind a message, so cap the
                                // scrubber there instead of the file's full length.
                                var displayDur = _previewCapSeconds();
                                var totalSec = Math.min(displayDur, Math.max(1, Math.floor(data.duration_ms / 1000)));
                                var scrubber = document.getElementById('fseq_scrubber');
                                scrubber.max = totalSec;
                                scrubber.value = 0;
                                window._scrubSeconds = 0;
                                document.getElementById('fseq_scrubber_row').style.display = '';
                                document.getElementById('fseq_time_display').textContent =
                                    '0:00 / ' + fmtTime(totalSec * 1000);
                                doFseqFetch(0);
                                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                            })
                            .catch(function(e) {
                                loadEl.textContent = '\u2717 ' + e;
                                loadEl.style.color = '#f44336';
                            });

                    } else if (ct.type === 'vid') {
                        // ---- Video: show scrubber (time in seconds), fetch frames ----
                        loadEl.textContent = '';
                        var scrubber = document.getElementById('fseq_scrubber');
                        // Capped to display_duration, not the video's own length -- playback
                        // always restarts from 0 and is cut off after display_duration seconds
                        // each time a message shows, so nothing past that point is ever seen.
                        scrubber.max = Math.max(1, _previewCapSeconds());
                        scrubber.value = 0;
                        window._scrubSeconds = 0;
                        document.getElementById('fseq_scrubber_row').style.display = '';
                        document.getElementById('fseq_time_display').textContent = '0:00';
                        document.getElementById('fseq_status').textContent =
                            'Scrub to preview different parts of the video';
                        doMediaFetch(0);
                        if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();

                    } else {
                        // ---- Image: load once, no scrubber ----
                        loadEl.textContent = '';
                        document.getElementById('fseq_scrubber_row').style.display = 'none';
                        window._scrubSeconds = 0;
                        doMediaFetch(0);
                        if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                    }
                }

                // Alias so old callers still work
                window.loadFseqPreview = loadBgPreview;

                var _scrubTimer = null;
                var _pendingImg  = null;

                function doFseqFetch(seconds) {
                    if (!_fseqMeta || !_fseqSeq) return;
                    var sec      = parseInt(seconds);
                    var frameIdx = Math.min(
                        Math.round(sec * _fseqMeta.fps),
                        _fseqMeta.frame_count - 1
                    );
                    var mw    = document.getElementById('overlay_model_width').value  || 0;
                    var mh    = document.getElementById('overlay_model_height').value || 0;
                    var model = document.getElementById('overlay_model_name').value   || '';

                    var url = '/api/fseq/frame'
                        + '?sequence=' + encodeURIComponent(_fseqSeq)
                        + '&frame='    + frameIdx
                        + '&model='    + encodeURIComponent(model)
                        + '&width='    + mw
                        + '&height='   + mh;
                    if (_fseqMeta.detected_start_channel) {
                        url += '&start_channel=' + _fseqMeta.detected_start_channel;
                    }
                    if (_fseqMeta.detected_channel_count) {
                        url += '&channel_count=' + _fseqMeta.detected_channel_count;
                    }
                    _loadImageUrl(url);
                }

                function doMediaFetch(seconds) {
                    if (!_contentType || !_contentFile || _contentType === 'seq') return;
                    var mw = document.getElementById('overlay_model_width').value  || 0;
                    var mh = document.getElementById('overlay_model_height').value || 0;
                    var url = '/api/media/preview'
                        + '?type='   + _contentType
                        + '&file='   + encodeURIComponent(_contentFile)
                        + '&time='   + Math.floor(seconds)
                        + '&width='  + mw
                        + '&height=' + mh;
                    _loadImageUrl(url);
                }

                function _loadImageUrl(url) {
                    var statusEl = document.getElementById('fseq_status');
                    if (_pendingImg) { _pendingImg.onload = null; _pendingImg.onerror = null; _pendingImg.src = ''; }
                    var img = new Image();
                    _pendingImg = img;
                    img.onload = function() {
                        if (img !== _pendingImg) return;
                        window._fseqBgImage = img;
                        statusEl.textContent = '';
                        if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                    };
                    img.onerror = function() {
                        if (img !== _pendingImg) return;
                        fetch(url).then(function(r) { return r.json(); }).then(function(d) {
                            statusEl.textContent = '\u2717 ' + (d.error || 'Failed to load frame');
                            statusEl.style.color = '#f44336';
                        }).catch(function() {
                            statusEl.textContent = '\u2717 Failed to load preview';
                            statusEl.style.color = '#f44336';
                        });
                    };
                    img.src = url;
                }

                window.fseqScrub = function(seconds) {
                    // Drives the scrolling-text preview too (see renderCanvasPreview) --
                    // updated immediately, unlike the network-bound background frame fetch
                    // below which stays debounced.
                    window._scrubSeconds = parseFloat(seconds) || 0;
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                    var loadEl = document.getElementById('fseq_load_status');
                    if (_contentType === 'seq') {
                        if (!_fseqMeta) return;
                        document.getElementById('fseq_time_display').textContent =
                            fmtTime(parseInt(seconds) * 1000) + ' / ' + fmtTime(_fseqMeta.duration_ms);
                        clearTimeout(_scrubTimer);
                        _scrubTimer = setTimeout(function() { doFseqFetch(seconds); }, 150);
                    } else if (_contentType === 'vid') {
                        document.getElementById('fseq_time_display').textContent = fmtTime(parseInt(seconds) * 1000);
                        clearTimeout(_scrubTimer);
                        _scrubTimer = setTimeout(function() { doMediaFetch(seconds); }, 150);
                    }
                    // img: no scrubbing
                };

                window.clearFseqPreview = function() {
                    _clearState();
                    document.getElementById('fseq_scrubber_row').style.display = 'none';
                    document.getElementById('fseq_status').textContent = '';
                    document.getElementById('fseq_load_status').textContent = '';
                };

            })();

            function updateNameDisplayWarning() {
                // The names list drives the warning + list UI now.
                if (window.renderNamesList) { window.renderNamesList(); return; }
                var warn = document.getElementById('name_display_none_warning');
                if (warn) warn.style.display = ((window._namesContentList || []).length === 0) ? 'block' : 'none';
            }

            // All DOM elements are above this script block - call init functions directly.
            // Each step is isolated: a failure in one (e.g. the FSEQ preview) must not
            // abort the rest, or the credential-block toggle (setupAutoSave →
            // updateSourceUI) would never run and the page would show the wrong
            // provider's fields. Guard every call.
            function _init(label, fn) {
                try { fn(); } catch (e) { console.error(label + ' init error:', e); }
            }
            _init('canvas', function() { initCanvasPreview(); });
            // Load preview immediately using server-rendered dropdown value, then again after FPP data populates
            _init('fseqPreview', function() { if (window.toggleFseqPreview) window.toggleFseqPreview(); });
            _init('nameDisplayWarning', updateNameDisplayWarning);
            _init('fonts', loadFonts);
            _init('fppData', loadFPPData);
            _init('respRows', initRespRows);
            _init('whitelistResp', checkWhitelistResponseState);
            _init('rateLimitResp', checkRateLimitResponseState);
            _init('duplicateResp', checkDuplicateState);
            _init('wordsPreview', updateWordsPreview);
            _init('autoSave', setupAutoSave);
            // Keep the role toggle + remote view in sync with the LIVE role while the page is
            // open, so switching FPP player↔remote flips the UI without a manual refresh.
            function reconcileRole() {
                // Don't stomp a just-made manual selection while its save is in flight.
                if (window._roleManualUntil && Date.now() < window._roleManualUntil) return;
                fetch('/api/plugin_role').then(function(r){return r.json();}).then(function(d){
                    if (!d || !d.role) return;
                    if (window._roleManualUntil && Date.now() < window._roleManualUntil) return;
                    var sel = document.getElementById('plugin_role');
                    if (sel) sel.value = d.role;
                    // Always enforce visibility to match the LIVE role - not only when the select
                    // value changed - so the shown tabs can never drift out of sync with the role
                    // (e.g. a remote that somehow still shows the SMS/Testing tabs).
                    if (typeof window.applyRoleVisibility === 'function') window.applyRoleVisibility(d.role === 'remote');
                }).catch(function(){});
            }
            reconcileRole();
            setInterval(reconcileRole, 5000);
            // Remote: live-refresh the names content list when the master adds/removes content,
            // so the Display dropdown/list updates without a manual reload. Only acts on
            // MEMBERSHIP changes (ids added/removed/reordered) so it never stomps layout edits
            // to a surviving selected item.
            function refreshRemoteNamesContent() {
                var _rs = document.getElementById('plugin_role');
                if (!_rs || _rs.value !== 'remote') return;
                fetch('/api/plugin/names-content').then(function(r){return r.json();}).then(function(d){
                    if (!d || !d.is_remote || !Array.isArray(d.names_content_list)) return;
                    var serverIds = d.names_content_list.map(function(it){ return it.content || ''; });
                    var cur = window._namesContentList || [];
                    var localIds = cur.map(function(it){ return it.content || ''; });
                    if (JSON.stringify(serverIds) === JSON.stringify(localIds)) return;  // no membership change
                    var prevSelId = (cur[window._namesSelectedIndex] || {}).content || null;
                    var byId = {}; cur.forEach(function(it){ if (it.content) byId[it.content] = it; });
                    // Rebuild in server order, keeping the local item (with any in-progress
                    // layout edits) for surviving ids; use the server item for new ids.
                    window._namesContentList = d.names_content_list.map(function(it){ return byId[it.content] || it; });
                    var lst = window._namesContentList;
                    var newIdx = -1;
                    if (prevSelId) { for (var i = 0; i < lst.length; i++) { if (lst[i].content === prevSelId) { newIdx = i; break; } } }
                    if (newIdx < 0) newIdx = (lst.length > 0) ? 0 : -1;
                    var selectionChanged = (newIdx < 0) || !prevSelId || (lst[newIdx] && lst[newIdx].content !== prevSelId);
                    window._namesSelectedIndex = newIdx;
                    if (typeof window.renderNamesList === 'function') window.renderNamesList();
                    // Only reset the editor if the selected content actually changed (so edits
                    // to a surviving selected item are not stomped).
                    if (selectionChanged && newIdx >= 0 && typeof applyLayoutToEditor === 'function') applyLayoutToEditor(lst[newIdx]);
                    if (typeof window.toggleFseqPreview === 'function') window.toggleFseqPreview();
                }).catch(function(){});
            }
            setInterval(refreshRemoteNamesContent, 10000);
            _init('liveStatus', updateLiveStatus);
            setInterval(updateLiveStatus, 5000);
            for (var _li = 0; _li < 4; _li++) { updateLineSpeedRowVisibility(_li); updateLineOrientationRowVisibility(_li); }
            initValignButtons();
            (function() {
                var w = parseInt(document.getElementById('overlay_model_width').value) || 0;
                var h = parseInt(document.getElementById('overlay_model_height').value) || 0;
                if (w > 0 && h > 0) updateModelAspect(w, h);
            })();
            initCustomColors();

            function showTab(tabName, btn) {
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + tabName).classList.add('active');
                btn.classList.add('active');
                // Re-report height after layout settles so iframe resizes correctly
                requestAnimationFrame(function() {
                    window.parent.postMessage({ type: 'iframeHeight', height: document.body.scrollHeight }, '*');
                });
            }

            function refreshFPPLists(btn) {
                if (btn) { btn.disabled = true; btn.textContent = '...'; }
                fetch('/api/fpp/refresh', {method:'POST'})
                    .then(() => loadFPPData())
                    .finally(() => { if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh Lists'; } });
            }

            // Loads the actual font file into the browser via the CSS Font Loading API
            // so canvas ctx.font can render it for real. Without this, the preview
            // canvas silently falls back to generic sans-serif for every font, since
            // the browser never has any of these files installed as system fonts.
            window._loadedFonts = window._loadedFonts || {};
            function ensureFontLoaded(name) {
                if (!name || window._loadedFonts[name]) return window._loadedFonts[name] || Promise.resolve();
                var ff = new FontFace(name, 'url("/api/fonts/file/' + encodeURIComponent(name) + '")');
                var p = ff.load().then(function(loaded) {
                    document.fonts.add(loaded);
                    // Repaint once this font is actually available. The preview may have been
                    // drawn with a fallback before the font finished loading (e.g. right after
                    // a reboot / fresh page load); this makes it self-heal instead of sticking
                    // on the wrong font until the user toggles the dropdown. Runs once per font.
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                }).catch(function(err) {
                    console.warn('Font preview load failed for "' + name + '":', err);
                });
                window._loadedFonts[name] = p;
                return p;
            }

            function loadFonts() {
                var currentFonts = (window._lineFontsInit && Array.isArray(window._lineFontsInit))
                    ? window._lineFontsInit : ['FreeSans', 'FreeSans', 'FreeSans', 'FreeSans'];
                fetch('/api/fpp/fonts')
                .then(r => r.json())
                .then(function(fonts) {
                    for (var i = 1; i <= 4; i++) {
                        var sel = document.getElementById('line_' + i + '_font');
                        if (!sel) continue;
                        var current = currentFonts[i - 1] || 'FreeSans';
                        if (fonts && fonts.length > 0) {
                            sel.innerHTML = '<option value="">-- Select Font --</option>';
                            var groups = {};
                            fonts.forEach(function(font) {
                                var cat = font.category || 'System';
                                if (!groups[cat]) {
                                    groups[cat] = document.createElement('optgroup');
                                    groups[cat].label = cat;
                                    sel.appendChild(groups[cat]);
                                }
                                groups[cat].appendChild(new Option(font.name, font.name, false, font.name === current));
                            });
                        } else {
                            sel.innerHTML = '<option value="FreeSans">FreeSans (default)</option>';
                        }
                    }
                    // Font dropdowns are now populated - safe for autosave to read them.
                    window._fontsReady = !!(fonts && fonts.length > 0);
                    return Promise.all(currentFonts.map(ensureFontLoaded));
                })
                .then(function() {
                    if (typeof renderCanvasPreview === 'function') renderCanvasPreview();
                })
                .catch(function() {
                    for (var i = 1; i <= 4; i++) {
                        var sel = document.getElementById('line_' + i + '_font');
                        if (sel) sel.innerHTML = '<option value="FreeSans">FreeSans (default)</option>';
                    }
                });
            }

            function loadFPPData() {
                fetch('/api/fpp/data')
                .then(r => r.json())
                .then(data => {
                    if (data.error) console.warn('FPP data partial error:', data.error);
                    const defaultSelect = document.getElementById('default_playlist');
                    const currentDefault = "{{ config.get('default_playlist', '') }}";

                    defaultSelect.innerHTML = '<option value="">-- Select a sequence --</option>';

                    // Content types: sequences (.fseq, background FSEQ effect) and images
                    // (static overlay) are enabled for BOTH the Waiting dropdown and the
                    // Names content list (built from _fppSeqList/_fppImgList in the Manage
                    // Content modal). Playlists/videos remain disabled (foreground).
                    if (data.sequences && data.sequences.length > 0) {
                        const sg1 = document.createElement('optgroup');
                        sg1.label = '🎬 Sequences (.fseq)';
                        data.sequences.forEach(seq => {
                            const val = 'seq:' + seq;
                            sg1.appendChild(new Option(seq, val, false, val === currentDefault));
                        });
                        defaultSelect.add(sg1);
                    }

                    if (data.images && data.images.length > 0) {
                        const ig1 = document.createElement('optgroup');
                        ig1.label = '🖼️ Images';
                        data.images.forEach(img => {
                            const val = 'img:' + img;
                            ig1.appendChild(new Option(img, val, false, val === currentDefault));
                        });
                        defaultSelect.add(ig1);
                    }

                    // If the stored Waiting selection no longer exists in FPP (deleted in
                    // the file manager), revert it to None so we stop referencing a gone
                    // file. Guarded by !data.error so a partial fetch can't wipe a valid one.
                    if (!data.error) {
                        var _hasOpt = function(sel, val) {
                            if (!val) return true;
                            return Array.prototype.some.call(sel.options, function(o) { return o.value === val; });
                        };
                        if (!_hasOpt(defaultSelect, currentDefault)) { defaultSelect.value = ''; saveConfig(); }
                    }

                    window._fppSeqList = data.sequences || [];
                    window._fppImgList = data.images || [];

                    const modelSelect = document.getElementById('overlay_model_name');
                    const currentModel = "{{ config.get('overlay_model_name', 'Texting Matrix') }}";
                    modelSelect.innerHTML = '<option value="">-- None --</option>';
                    window.fppModels = data.models || [];

                    if (data.models && data.models.length > 0) {
                        data.models.forEach(model => {
                            const name = typeof model === 'object' ? model.name : model;
                            const opt = new Option(name, name, false, name === currentModel);
                            modelSelect.add(opt);
                        });
                        // Set aspect ratio and save dimensions for the currently selected model
                        const cur = data.models.find(m => (typeof m === 'object' ? m.name : m) === currentModel);
                        if (cur && cur.width && cur.height) {
                            updateModelAspect(cur.width, cur.height);
                            document.getElementById('overlay_model_width').value = cur.width;
                            document.getElementById('overlay_model_height').value = cur.height;
                            saveConfig();
                        }
                    }

                    modelSelect.addEventListener('change', function() {
                        const selected = this.value;
                        const m = (window.fppModels || []).find(m => (typeof m === 'object' ? m.name : m) === selected);
                        if (m && m.width && m.height) {
                            updateModelAspect(m.width, m.height);
                            document.getElementById('overlay_model_width').value = m.width;
                            document.getElementById('overlay_model_height').value = m.height;
                        }
                        saveConfig();
                    });

                    // Names list UI (and the modal picker) are ready - render them.
                    try { if (window.initNamesUI) window.initNamesUI(); } catch(e) { console.error('Names UI init error:', e); }
                    // Waiting content rotation list UI (needs _fppSeqList/_fppImgList populated).
                    try { if (window.initWaitingUI) window.initWaitingUI(); } catch(e) { console.error('Waiting UI init error:', e); }
                    // Load background preview now that dropdowns are populated
                    try { if (window.toggleFseqPreview) window.toggleFseqPreview(); } catch(e) { console.error('Preview error:', e); }
                    updateNameDisplayWarning();
                })
                .catch(function(e) {
                    console.error('FPP data load failed:', e);
                    try { if (window.toggleFseqPreview) window.toggleFseqPreview(); } catch(e2) {}
                });
            }


var _saveTimer = null;
            function saveConfig() {
                clearTimeout(_saveTimer);
                _saveTimer = setTimeout(_doSave, 300);
            }
            function _doSave() {
                var status = document.getElementById('autosave_status');
                status.style.color = '#888';
                status.textContent = 'Saving...';

                // Capture the current editor into the selected names item (or mirror the
                // duration to the hidden global field when there's no list) before saving.
                if (typeof window.flushEditorToSelected === 'function') window.flushEditorToSelected();

                const data = {
                    // NOTE: plugin_role is deliberately NOT sent here. The select shows the
                    // RESOLVED role (e.g. 'master' on a lone box), so sending it on every
                    // settings save would convert auto ('') into an explicit role and defeat
                    // auto-follow. Only onRoleChange() - an explicit user action - writes it.
                    message_source: document.getElementById('message_source').value,
                    twilio_account_sid: document.getElementById('account_sid').value,
                    twilio_auth_token: document.getElementById('auth_token').value,
                    twilio_phone_number: document.getElementById('phone_number').value,
                    gv_email: document.getElementById('gv_email').value,
                    gv_app_password: document.getElementById('gv_app_password').value,
                    poll_interval: parseInt(document.getElementById('poll_interval').value),
                    display_duration: parseInt(document.getElementById('display_duration').value),
                    max_messages_per_phone: parseInt(document.getElementById('max_messages').value),
                    allow_duplicate_names: document.getElementById('allow_duplicate_names').checked,
                    max_message_length: parseInt(document.getElementById('max_length').value),
                    one_word_only: document.getElementById('one_word_only')?.checked ?? false,
                    two_words_max: document.getElementById('two_words_max')?.checked ?? true,
                    profanity_filter: document.getElementById('profanity_filter').checked,
                    profanity_threshold: parseInt(document.getElementById('profanity_threshold').value) || 0,
                    use_whitelist: document.getElementById('use_whitelist').checked,
                    default_playlist: document.getElementById('default_playlist').value,
                    // Waiting content is a rotation list; default_playlist above is kept in
                    // sync with its first item server-side for the required-field/legacy paths.
                    default_content_list: window._waitingContentList || [],
                    default_content_mode: window._waitingMode || 'roundrobin',
                    // Names content is now a list; the flat key stays '' (only used as the
                    // fallback when the list is empty = names over the waiting content).
                    name_display_playlist: '',
                    names_content_list: window._namesContentList || [],
                    names_content_mode: window._namesMode || 'roundrobin',
                    overlay_model_name: document.getElementById('overlay_model_name').value,
                    overlay_model_width: parseInt(document.getElementById('overlay_model_width').value) || 0,
                    overlay_model_height: parseInt(document.getElementById('overlay_model_height').value) || 0,
                    message_lines: [
                        document.getElementById('line_1').value,
                        document.getElementById('line_2').value,
                        document.getElementById('line_3').value,
                        document.getElementById('line_4').value,
                    ],
                    line_boxes: window._lineBoxes || [{x:-1,y:-1,w:300,h:60},{x:-1,y:-1,w:300,h:60},{x:-1,y:-1,w:300,h:60},{x:-1,y:-1,w:300,h:60}],
                    line_colors: [0, 1, 2, 3].map(function(i) {
                        var el = document.getElementById('line_' + (i + 1) + '_color');
                        return el ? el.value.toUpperCase() : '#FF0000';
                    }),
                    line_movements: window._lineMovements || ['Center','Center','Center','Center'],
                    line_speeds: window._lineSpeeds || [50,50,50,50],
                    line_orientations: window._lineOrientations || ['horizontal','horizontal','horizontal','horizontal'],
                    custom_colors: window._customColors || [],
                    sms_response_show_not_live: document.getElementById('sms_response_show_not_live').checked,
                    sms_response_success: document.getElementById('sms_response_success').checked,
                    sms_response_profanity: document.getElementById('sms_response_profanity').checked,
                    sms_response_rate_limited: document.getElementById('sms_response_rate_limited').checked,
                    sms_response_duplicate: document.getElementById('sms_response_duplicate').checked,
                    sms_response_invalid_format: document.getElementById('sms_response_invalid_format').checked,
                    sms_response_too_long: document.getElementById('sms_response_too_long').checked,
                    sms_response_not_whitelisted: document.getElementById('sms_response_not_whitelisted').checked,
                    sms_response_blocked: document.getElementById('sms_response_blocked').checked,
                    response_success: document.getElementById('response_success').value,
                    response_profanity: document.getElementById('response_profanity').value,
                    response_rate_limited: document.getElementById('response_rate_limited').value,
                    response_duplicate: document.getElementById('response_duplicate').value,
                    response_invalid_format: document.getElementById('response_invalid_format').value,
                    response_too_long: document.getElementById('response_too_long').value,
                    response_not_whitelisted: document.getElementById('response_not_whitelisted').value,
                    response_blocked: document.getElementById('response_blocked').value,
                    response_show_not_live: document.getElementById('response_show_not_live').value
                };

                // Admin whitelist-approval fields exist only in Google Voice mode.
                // Include them ONLY when present so a Twilio-mode save never clears
                // the stored values (an absent key leaves the server's value intact).
                var _apEl = document.getElementById('admin_phone');
                if (_apEl) {
                    data.admin_phone = _apEl.value;
                    data.admin_approval_timeout_mins = parseInt((document.getElementById('admin_approval_timeout_mins')||{}).value) || 0;
                    data.admin_approval_prompt = (document.getElementById('admin_approval_prompt')||{}).value || '';
                    data.response_whitelist_pending = (document.getElementById('response_whitelist_pending')||{}).value || '';
                }

                // Only persist the per-line fonts once loadFonts() has actually
                // populated the font dropdowns. Otherwise an autosave that fires
                // during page init (e.g. model-dimension sync or the stale-content
                // reset, both inside loadFPPData) would read empty <select>s and
                // overwrite the saved/imported fonts with the FreeSans default -
                // which is exactly why imported fonts appeared to "not transfer".
                // Omitting the key leaves the server's stored line_fonts untouched.
                if (window._fontsReady) {
                    data.line_fonts = [0, 1, 2, 3].map(function(i) {
                        var el = document.getElementById('line_' + (i + 1) + '_font');
                        return el && el.value ? el.value : 'FreeSans';
                    });
                }

                fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                })
                .then(r => r.json())
                .then(function() {
                    status.style.color = '#4CAF50';
                    status.textContent = '✓ Saved';
                    setTimeout(function() { status.textContent = ''; }, 3000);
                })
                .catch(function() {
                    status.style.color = '#f44336';
                    status.textContent = '✗ Save failed';
                });
            }

            function setupAutoSave() {
                // The show's live state (config.enabled) is owned by the Start/Stop
                // scheduler commands (api_activate / api_deactivate) - there is no
                // manual enable toggle, so config saves here never touch it.

                // Turn on the auto-responses that can actually fire under Google
                // Voice (Twilio keeps them off / hidden). Skips rows locked by another
                // setting: rate-limited (unlimited), duplicate (dupes allowed),
                // invalid-format (whitelist on) - those stay off.
                function enableGvResponses() {
                    ['show_not_live','blocked','profanity','invalid_format','not_whitelisted','success'].forEach(function(id) {
                        var cb = document.getElementById('sms_response_' + id);
                        var row = document.getElementById('row_' + id);
                        if (cb && !cb.disabled && row && !row.classList.contains('locked')) {
                            cb.checked = true;
                            toggleResp(id);
                        }
                    });
                }
                // Message source selector - swap the visible credential block, apply
                // the source's rate-limit default, and save.
                var srcEl = document.getElementById('message_source');
                if (srcEl) srcEl.addEventListener('change', function() {
                    var isGV = this.value === 'google_voice';
                    // Google Voice: unlimited (0) + allow duplicate names.
                    // Twilio: rate limit 5 + disallow duplicates.
                    var mm = document.getElementById('max_messages');
                    if (mm) mm.value = isGV ? 0 : 5;
                    var dup = document.getElementById('allow_duplicate_names');
                    if (dup) dup.checked = isGV;
                    updateSourceUI();
                    checkDuplicateState();          // grey the duplicate response accordingly
                    checkRateLimitResponseState();  // grey the rate-limited response accordingly
                    if (isGV) enableGvResponses();  // Google Voice: turn on the usable responses
                    if (window.updateAdminApprovalUI) updateAdminApprovalUI();  // show/hide Live Name Approval
                    saveConfig();
                });
                // Google Voice credential fields - save on blur (like Twilio creds)
                ['gv_email','gv_app_password'].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('blur', saveConfig);
                });
                // Keep the Rate-Limited response lock in sync when the limit changes
                var mmEl = document.getElementById('max_messages');
                if (mmEl) mmEl.addEventListener('input', checkRateLimitResponseState);
                // Reflect the saved source on initial load
                updateSourceUI();

                // Checkboxes, selects, color picker - save immediately on change
                ['profanity_filter','use_whitelist','allow_duplicate_names',
                 'default_playlist','name_display_playlist','overlay_model_name',
                 'one_word_only','two_words_max',
                 'sms_response_show_not_live',
                 'sms_response_success','sms_response_profanity','sms_response_rate_limited',
                 'sms_response_duplicate','sms_response_invalid_format','sms_response_too_long',
                 'sms_response_not_whitelisted','sms_response_blocked'
                ].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('change', saveConfig);
                });
                // Reload background preview when Names Display content, Default Waiting
                // content (used as the fallback when Names content is None), or model changes
                ['name_display_playlist', 'default_playlist', 'overlay_model_name'].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('change', function() {
                        if (window.toggleFseqPreview) window.toggleFseqPreview();
                        updateNameDisplayWarning();
                    });
                });
                // The scrubber's range is capped to Display Duration (see loadBgPreview) -
                // reload it on change so that cap stays in sync with the field.
                var displayDurationEl = document.getElementById('display_duration');
                if (displayDurationEl) displayDurationEl.addEventListener('change', function() {
                    if (window.toggleFseqPreview) window.toggleFseqPreview();
                });

                // Text, number inputs - save when user clicks away
                ['account_sid','auth_token','phone_number',
                 'poll_interval','display_duration','max_messages','max_length',
                 'profanity_threshold',
                 'line_1','line_2','line_3','line_4',
                 'response_success','response_profanity','response_rate_limited',
                 'response_duplicate','response_invalid_format','response_too_long',
                 'response_not_whitelisted','response_blocked'
                ].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('blur', saveConfig);
                });
            }

            function testConnection() {
                const result = document.getElementById('twilio_test_result');
                result.innerHTML = '<span style="color:#555;">Testing...</span>';
                fetch('/api/test')
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        result.innerHTML = '<span style="color:#4CAF50;">✅ Twilio connection successful!</span>';
                    } else {
                        result.innerHTML = '<span style="color:#f44336;">❌ Connection failed: ' + data.error + '</span>';
                    }
                });
            }

            // Show the credential block for the selected message source, hide the other.
            // Also hide the SMS Responses tab for Twilio (untested there for now).
            function updateSourceUI() {
                var srcEl = document.getElementById('message_source');
                if (!srcEl) return;
                var isGV = srcEl.value === 'google_voice';
                var tw = document.getElementById('twilio_creds');
                var gv = document.getElementById('gv_creds');
                var appr = document.getElementById('gv_approval');
                if (tw) tw.style.display = isGV ? 'none' : '';
                if (gv) gv.style.display = isGV ? '' : 'none';
                if (appr) appr.style.display = isGV ? '' : 'none';

                // Point the help link at the selected provider's config section.
                // This page runs inside the plugin's own service (port 5000), so a
                // relative URL would resolve there instead of the FPP web server -
                // build an absolute URL to the FPP host (default port) explicitly.
                var helpLink = document.getElementById('provider_help_link');
                if (helpLink) {
                    helpLink.textContent = isGV ? 'View Google Voice Configuration' : 'View Twilio Configuration';
                    var fppBase = window.location.protocol + '//' + window.location.hostname;
                    helpLink.href = fppBase + '/plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=help.php#'
                        + (isGV ? 'google-voice' : 'twilio');
                }

                // SMS Responses are only exposed for Google Voice right now
                var smsBtn = document.getElementById('tabbtn-sms');
                if (smsBtn) {
                    smsBtn.style.display = isGV ? '' : 'none';
                    // If Twilio is selected while the SMS tab is open, jump to Settings
                    if (!isGV && smsBtn.classList.contains('active')) {
                        var setBtn = document.querySelector('.tab-btn[onclick*="settings"]');
                        if (setBtn) showTab('settings', setBtn);
                    }
                }
                // Twilio A2P/registration warnings are irrelevant for Google Voice
                var twWarn = document.getElementById('twilio_sms_warnings');
                if (twWarn) twWarn.style.display = isGV ? 'none' : '';
            }

            function testGoogleVoice() {
                var result = document.getElementById('gv_test_result');
                result.innerHTML = '<span style="color:#555;">Saving &amp; testing...</span>';
                // Save first so the server tests the latest credentials, then test.
                saveConfig();
                setTimeout(function() {
                    fetch('/api/test_gv')
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            var reply = data.reply_ready
                                ? ' &nbsp;·&nbsp; ✅ replies enabled'
                                : ' &nbsp;·&nbsp; ⚠️ replies unavailable (outbound SMTP blocked)';
                            result.innerHTML = '<span style="color:#4CAF50;">✅ Inbox connected!</span>' +
                                '<span style="color:' + (data.reply_ready ? '#4CAF50' : '#e65100') + ';">' + reply + '</span>';
                        } else {
                            result.innerHTML = '<span style="color:#f44336;">❌ ' + data.error + '</span>';
                        }
                    })
                    .catch(function() {
                        result.innerHTML = '<span style="color:#f44336;">❌ Test request failed.</span>';
                    });
                }, 600);
            }

            function viewMessages() {
                window.location.href = '/messages';
            }

            function submitTestMessage() {
                const testName = document.getElementById('test_name').value.trim();
                const resultDiv = document.getElementById('test_result');

                if (!testName) {
                    resultDiv.innerHTML = '<p class="error">❌ Please enter a name</p>';
                    return;
                }

                resultDiv.innerHTML = '<p>🧪 Submitting test message...</p>';

                fetch('/api/test/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: testName})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        resultDiv.innerHTML = '<p class="success">✅ ' + data.message + '</p>';
                        document.getElementById('test_name').value = '';
                        setTimeout(() => {
                            resultDiv.innerHTML += '<p><a href="/messages" style="color: #4CAF50;">📋 View Queue Status</a></p>';
                        }, 1000);
                    } else {
                        resultDiv.innerHTML = '<p class="error">❌ ' + data.error + '</p>';
                        if (data.reason) {
                            resultDiv.innerHTML += '<p style="font-size: 12px; color: #666;">Reason: ' + data.reason + '</p>';
                        }
                    }
                });
            }

            // ===== Color Picker =====
            function initCustomColors() {
                window._customColors = (window._customColorsInit && Array.isArray(window._customColorsInit))
                    ? window._customColorsInit.slice() : [];
            }

            // Per-line color swatch (next to each line's reset-to-center button)
            function onLineColorChange(i) {
                if (typeof renderCanvasPreview === 'function') renderCanvasPreview();
                if (typeof saveConfig === 'function') saveConfig();
            }

            // ===== Per-line saved-color palette popover =====
            // Lets you save the current swatch color, or recall a previously-saved one,
            // right where you pick a line's color - there's no separate global picker.
            function toggleColorPalette(i) {
                var pop = document.getElementById('line_' + (i + 1) + '_palette_popover');
                if (!pop) return;
                var opening = pop.style.display === 'none' || !pop.style.display;
                document.querySelectorAll('.color-palette-popover').forEach(function(p) { p.style.display = 'none'; });
                if (opening) {
                    renderColorPalettePopover(i);
                    pop.style.display = 'block';
                }
            }
            document.addEventListener('click', function(e) {
                if (e.target.closest && e.target.closest('.line-color-group')) return;
                document.querySelectorAll('.color-palette-popover').forEach(function(p) { p.style.display = 'none'; });
            });
            function renderColorPalettePopover(i) {
                var pop = document.getElementById('line_' + (i + 1) + '_palette_popover');
                if (!pop) return;
                pop.innerHTML = '';
                var swatches = document.createElement('div');
                swatches.className = 'color-palette-swatches';
                var colors = window._customColors || [];
                if (colors.length === 0) {
                    var empty = document.createElement('div');
                    empty.className = 'color-palette-empty';
                    empty.textContent = 'No saved colors yet';
                    swatches.appendChild(empty);
                } else {
                    colors.forEach(function(hex) {
                        var sw = document.createElement('button');
                        sw.type = 'button';
                        sw.className = 'color-palette-swatch';
                        sw.title = hex + ' (right-click to remove)';
                        sw.style.background = hex;
                        sw.onclick = function() { applyLineColor(i, hex); };
                        sw.oncontextmenu = function(e) { e.preventDefault(); removeCustomColor(i, hex); };
                        swatches.appendChild(sw);
                    });
                }
                pop.appendChild(swatches);
                var saveBtn = document.createElement('button');
                saveBtn.type = 'button';
                saveBtn.className = 'color-palette-save-btn';
                saveBtn.textContent = '+ Save current color';
                saveBtn.onclick = function() { saveCustomColor(i); };
                pop.appendChild(saveBtn);
            }
            function applyLineColor(i, hex) {
                var el = document.getElementById('line_' + (i + 1) + '_color');
                if (!el) return;
                el.value = hex;
                onLineColorChange(i);
                var pop = document.getElementById('line_' + (i + 1) + '_palette_popover');
                if (pop) pop.style.display = 'none';
            }
            function saveCustomColor(i) {
                var el = document.getElementById('line_' + (i + 1) + '_color');
                var hex = el ? el.value.toUpperCase() : '';
                if (!/^#[0-9A-F]{6}$/.test(hex)) return;
                window._customColors = window._customColors || [];
                if (window._customColors.indexOf(hex) === -1) {
                    window._customColors.push(hex);
                    if (window._customColors.length > 20) window._customColors.shift();
                    renderColorPalettePopover(i);
                    if (typeof saveConfig === 'function') saveConfig();
                }
            }
            function removeCustomColor(i, hex) {
                window._customColors = (window._customColors || []).filter(function(c) { return c !== hex; });
                renderColorPalettePopover(i);
                if (typeof saveConfig === 'function') saveConfig();
            }

            // Blacklist content-warning modal
            function showBlacklistWarning() {
                document.getElementById('blacklist-warning-modal').style.display = 'flex';
            }
            function hideBlacklistWarning() {
                document.getElementById('blacklist-warning-modal').style.display = 'none';
            }
            function proceedToBlacklist() {
                location.href = '/blacklist';
            }
        </script>

        <!-- Blacklist content-warning modal -->
        <div id="blacklist-warning-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
            <div style="background:#2a2a2a; color:#eee; border-radius:8px; padding:24px; max-width:420px; width:90%; box-shadow:0 4px 20px rgba(0,0,0,0.5);">
                <h3 style="margin-top:0; color:#ffc107;">⚠️ Warning</h3>
                <p style="margin-bottom:20px;">Blacklist contains profanity, and sexual related messaging. Viewer discretion is advised.</p>
                <div style="display:flex; gap:10px; justify-content:flex-end;">
                    <button onclick="hideBlacklistWarning()" style="background:#555; color:#fff; padding:10px 18px; border:none; border-radius:5px; cursor:pointer;">Return</button>
                    <button onclick="proceedToBlacklist()" style="background:#f44336; color:#fff; padding:10px 18px; border:none; border-radius:5px; cursor:pointer;">Proceed</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    _eff_role = get_plugin_role()
    # The true default response text, so the per-field "Reset to default" buttons
    # restore exactly what a fresh install ships (and track future default changes).
    _resp_defaults = {k: v for k, v in DEFAULT_CONFIG.items() if k.startswith('response_')}
    return render_template_string(html, config=config, secret_sentinel=SECRET_SENTINEL,
                                  effective_role=_eff_role, response_defaults=_resp_defaults,
                                  admin_ctx_seeded=admin_ctx_is_seeded(),
                                  admin_gv_linked=bool(config.get('gv_email') and config.get('gv_app_password')))

@app.route('/api/config', methods=['POST'])
def update_config():
    global config, twilio_client, polling_thread, stop_polling
    try:
        new_config = request.json or {}
        # Secret fields render with the masked SECRET_SENTINEL when a value is
        # stored. Interpret the submitted value:
        #   • unchanged sentinel → keep the stored secret (drop the key)
        #   • empty              → remove the stored secret (keep '' to clear it)
        #   • anything else      → update to the new value
        for _sk in SECRET_KEYS:
            if str(new_config.get(_sk, '')) == SECRET_SENTINEL:
                new_config.pop(_sk, None)
        config.update(new_config)

        # Profanity auto-block threshold: non-negative int, 0 = off. Clamp so a bad
        # client value can't disable the feature by accident or run away.
        if 'profanity_threshold' in new_config:
            try:
                _pt = int(new_config.get('profanity_threshold', 3) or 0)
            except (TypeError, ValueError):
                _pt = 3
            config['profanity_threshold'] = max(0, min(_pt, 100))

        # Sanitize the names content list - never trust client array shapes/lengths.
        if 'names_content_list' in new_config:
            raw_list = new_config.get('names_content_list')
            if not isinstance(raw_list, list):
                raw_list = []
            sanitized = [_sanitize_names_item(it) for it in raw_list]
            if is_remote():
                # On a REMOTE the master sync owns list MEMBERSHIP; the browser may only edit
                # per-content LAYOUTS. Merge the posted layouts into the existing items by
                # content id - never add or drop items here. This stops a remote's autosave
                # (which may run with a stale/empty list) from wiping the master-synced list.
                posted = {it.get('content'): it for it in sanitized if it.get('content')}
                config['names_content_list'] = [
                    posted.get(it.get('content'), it)
                    for it in (config.get('names_content_list') or [])
                ]
            else:
                config['names_content_list'] = sanitized
        if config.get('names_content_mode') not in ('roundrobin', 'random'):
            config['names_content_mode'] = 'roundrobin'
        # Keep the round-robin cursor valid if the list changed/shrank.
        _lst_len = len(config.get('names_content_list', []) or [])
        if _lst_len == 0 or int(config.get('names_content_rr_index', -1) or -1) >= _lst_len:
            config['names_content_rr_index'] = -1

        # Sanitize the WAITING content rotation list the same way.
        if 'default_content_list' in new_config:
            raw_wlist = new_config.get('default_content_list')
            if not isinstance(raw_wlist, list):
                raw_wlist = []
            # Drop items with no content so an empty picker row can't wedge the rotator.
            config['default_content_list'] = [d for d in (_sanitize_default_item(it) for it in raw_wlist) if d['content']]
        if config.get('default_content_mode') not in ('roundrobin', 'random'):
            config['default_content_mode'] = 'roundrobin'
        _wlst = config.get('default_content_list', []) or []
        if len(_wlst) == 0 or int(config.get('default_content_rr_index', -1) or -1) >= len(_wlst):
            config['default_content_rr_index'] = -1
        # Keep the single default_playlist in sync with the list's first item so the
        # required-field check, export/validation, and every legacy single-content code
        # path still resolve to a real value. Only mirror when a list is configured; an
        # empty list leaves the user's single default_playlist untouched.
        if _wlst:
            config['default_playlist'] = _wlst[0]['content']

        # Multi-instance role/discovery keys.
        if 'plugin_role' in new_config:
            _r = str(new_config.get('plugin_role') or '').strip().lower()
            config['plugin_role'] = _r if _r in ('master', 'remote') else ''
            global _resolved_role, _remotes_cache_time, _tml_peer_cache_time, polling_generation
            _resolved_role = None   # re-resolve the FPP-mode default next time if unset
            _remotes_cache_time = 0
            _tml_peer_cache_time = 0
            # Switched to remote: retire any running poller (a remote never polls/responds).
            # Switched to master: a poller will be (re)started by start_polling_if_needed below.
            if is_remote():
                polling_generation += 1

        # Normalize phone number to E.164 (strip spaces, dashes, parens - keep + and digits)
        if config.get('twilio_phone_number'):
            config['twilio_phone_number'] = re.sub(r'[^\d+]', '', config['twilio_phone_number'])

        # Admin approval (GV only): normalize the admin phone to digits and, if the
        # operator changed it, drop the seeded reply context so it no longer counts
        # as "connected" - the new number must text the GV number once to re-seed
        # (the bootstrap banner reappears until it does).
        if 'admin_phone' in new_config:
            config['admin_phone'] = _normalize_phone(config.get('admin_phone', ''))
            _ctx = load_admin_ctx()
            if _ctx and _normalize_phone(_ctx.get('phone', '')) != config['admin_phone']:
                clear_admin_ctx()
            # The number was just entered/changed - let the very next status poll run a
            # fresh mailbox scan instead of waiting out the seed/verify throttle windows.
            global _last_admin_seed_scan, _last_admin_verify_scan
            _last_admin_seed_scan = 0.0
            _last_admin_verify_scan = 0.0
        if 'admin_approval_timeout_mins' in new_config:
            try:
                _to = int(new_config.get('admin_approval_timeout_mins', 5) or 0)
            except (TypeError, ValueError):
                _to = 5
            config['admin_approval_timeout_mins'] = max(0, min(_to, 1440))

        save_config()

        # Keep the Twilio client in sync whenever credentials are present, so the
        # Twilio path works exactly as before regardless of the selected source.
        if config['twilio_account_sid'] and config['twilio_auth_token']:
            twilio_client = Client(
                config['twilio_account_sid'],
                config['twilio_auth_token']
            )

        # Start the poller for the selected source if not already running (e.g.
        # credentials entered after Text My Lights Start, or updated mid-show).
        start_polling_if_needed()

        return jsonify({"success": True})
    except Exception as e:
        return _client_error("update_config", e)

@app.route('/api/plugin/admin-approval-status')
def api_admin_approval_status():
    """Tiny status signal for the config page's bootstrap banner: whether the admin
    phone has texted the Google Voice number yet (so there is a reply context to
    text them). Polled only while the banner is showing."""
    seeded = admin_ctx_is_seeded()
    # If currently seeded, re-validate that the admin's email thread still exists.
    # verify_admin_ctx() searches directly for the stored Message-ID (throttled) and
    # clears the context if the thread was deleted, so the banner reverts on its own.
    if seeded:
        seeded = verify_admin_ctx()
    # Self-heal from existing texting history: if not seeded (never was, or the thread
    # was just cleared), scan the Gmail inbox for another Google Voice message from the
    # admin number (throttled internally).
    if not seeded and config.get('admin_phone', '').strip():
        seeded = seed_admin_ctx_from_inbox()
    return jsonify({
        "admin_phone": config.get('admin_phone', ''),
        "seeded": seeded,
        # Whether a Google Voice account is linked (credentials entered). Without it
        # there is no number to text "admin" to, so the UI shows a link-account
        # warning instead of the "text admin to connect" bootstrap banner.
        "gv_linked": bool(config.get('gv_email') and config.get('gv_app_password')),
    })

# ============================================================================
# Config Export / Import
# ----------------------------------------------------------------------------
# Export bundles the plugin settings, the block/whitelist/blacklist files, the
# FPP content the plugin references (the waiting + name-display playlists and
# the sequences/images/videos they use), and the FPP overlay-model definition
# into a single .zip, so another Pi can be brought up identically. Credentials
# (Twilio auth token, Google Voice app password) are DELIBERATELY excluded -
# plugin.json on disk never contains them (see save_config), and we re-scrub on
# import for good measure. Import restores everything and preserves the target
# Pi's own credentials.
# ============================================================================
BUNDLE_MARKER  = "textmylights-config"
BUNDLE_FORMAT  = 1

def _content_source_files(content_value, warnings):
    """Resolve a plugin content setting (default_playlist / name_display_playlist)
    to a list of (arc_subdir, absolute_path) files to include in the export.

    A content value is one of:
      • ''            -> nothing
      • 'seq:NAME'    -> a sequence file in the sequences dir
      • 'img:NAME'    -> an image file in the images dir
      • 'NAME'        -> an FPP playlist; its .json plus every sequence/media
                         item it references
    """
    files = []
    if not content_value:
        return files

    if content_value.startswith('seq:'):
        name = content_value[4:]
        if not name.endswith('.fseq'):
            name += '.fseq'
        files.append(('content/sequences', os.path.join(FSEQ_SEQUENCE_PATH, name)))
        return files

    if content_value.startswith('img:'):
        name = content_value[4:]
        files.append(('content/images', os.path.join(FPP_IMAGES_PATH, name)))
        return files

    # Otherwise it's a playlist name.
    pl_path = os.path.join(FPP_PLAYLISTS_PATH, content_value + '.json')
    if not os.path.isfile(pl_path):
        warnings.append(f"Playlist '{content_value}' not found on disk - skipped")
        return files
    files.append(('content/playlists', pl_path))

    # Walk the playlist for referenced sequences and media so the target Pi has
    # the actual files, not just the playlist that names them.
    try:
        with open(pl_path, 'r') as f:
            pl = json.load(f)
        sections = []
        for key in ('leadIn', 'mainPlaylist', 'leadOut'):
            if isinstance(pl.get(key), list):
                sections.extend(pl[key])
        for entry in sections:
            if not isinstance(entry, dict):
                continue
            seq = entry.get('sequenceName')
            if seq:
                files.append(('content/sequences', os.path.join(FSEQ_SEQUENCE_PATH, seq)))
            media = entry.get('mediaName')
            if media:
                # Media may be a video or an image; include whichever exists.
                vid = os.path.join(FPP_VIDEOS_PATH, media)
                img = os.path.join(FPP_IMAGES_PATH, media)
                if os.path.isfile(vid):
                    files.append(('content/videos', vid))
                elif os.path.isfile(img):
                    files.append(('content/images', img))
                else:
                    warnings.append(f"Media '{media}' (in playlist '{content_value}') not found - skipped")
    except Exception as e:
        warnings.append(f"Could not read playlist '{content_value}': {e}")

    return files

@app.route('/api/config/export')
def export_config():
    """Build and stream a .zip bundle of the selected plugin configuration.

    The export modal sends section flags as query params (settings/lists/content/
    overlay = 1|0). A section defaults to included when its param is absent, so a
    plain GET of this URL still exports everything."""
    tmp = None
    try:
        want = lambda k: request.args.get(k, '1') == '1'
        inc_settings = want('settings')
        inc_lists    = want('lists')
        inc_content  = want('content')
        inc_overlay  = want('overlay')

        warnings = []
        # Settings (plugin.json on disk is already secrets-free) and the block /
        # name lists are separate opt-in groups, but share the 'settings/' arc dir
        # (import maps them to their targets by basename).
        settings_files = []
        if inc_settings:
            settings_files.append(('settings', CONFIG_FILE))
        if inc_lists:
            settings_files += [
                ('settings', BLOCKLIST_FILE),
                ('settings', WHITELIST_FILE),
                ('settings', WHITELIST_ADDED_FILE),
                ('settings', WHITELIST_REMOVED_FILE),
                ('settings', BLACKLIST_FILE),
                ('settings', BLACKLIST_ADDED_FILE),
                ('settings', BLACKLIST_REMOVED_FILE),
            ]

        # Referenced content ONLY - the Waiting + Name Display content this plugin
        # is set to use and the files they reference. Never all of FPP's media.
        content_files = []
        if inc_content:
            _list_content = [it.get('content', '') for it in (config.get('names_content_list', []) or [])]
            _wait_content = [it.get('content', '') for it in (config.get('default_content_list', []) or [])]
            _seen_cv = set()
            for cv in [config.get('default_playlist', ''), config.get('name_display_playlist', ''), *_wait_content, *_list_content]:
                if cv and cv not in _seen_cv:
                    _seen_cv.add(cv)
                    content_files.extend(_content_source_files(cv, warnings))

        # Overlay model ("matrix") - just the selected model's entry, not the whole
        # channel-output config (written to the zip as a small JSON payload below).
        overlay_payload = _extract_overlay_model(warnings) if inc_overlay else None

        # Write to a temp file (FSEQ can be large; avoid holding the whole zip in
        # RAM on a Pi). ZIP_STORED since FSEQ is already compressed.
        fd, tmp = tempfile.mkstemp(suffix='.zip', prefix='tml_export_')
        os.close(fd)
        seen = set()
        included = []
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as zf:
            for arc_dir, path in settings_files + content_files:
                if not path or not os.path.isfile(path):
                    continue
                arcname = f"{arc_dir}/{os.path.basename(path)}"
                if arcname in seen:
                    continue
                seen.add(arcname)
                zf.write(path, arcname)
                included.append(arcname)

            # Single-model overlay payload (not a file on disk).
            if overlay_payload:
                zf.writestr('overlay/overlay-model.json', overlay_payload)
                included.append('overlay/overlay-model.json')

            manifest = {
                "bundle": BUNDLE_MARKER,
                "format": BUNDLE_FORMAT,
                "created": datetime.now(timezone.utc).isoformat(),
                "includes": {
                    "settings": inc_settings,
                    "lists": inc_lists,
                    "content": bool(content_files),
                    "overlay_model": bool(overlay_payload),
                    "credentials": False,
                },
                "overlay_model_name": config.get('overlay_model_name', ''),
                "default_playlist": config.get('default_playlist', ''),
                "name_display_playlist": config.get('name_display_playlist', ''),
                "files": included,
                "warnings": warnings,
            }
            zf.writestr('manifest.json', json.dumps(manifest, indent=2))

        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        download_name = f"textmylights-config-{stamp}.zip"
        # send_file streams the temp file; remove it once the response is sent.
        resp = send_file(tmp, mimetype='application/zip',
                         as_attachment=True, download_name=download_name)

        @resp.call_on_close
        def _cleanup():
            try:
                os.remove(tmp)
            except OSError:
                pass
        return resp
    except Exception as e:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        logging.error(f"export_config failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

def _overlay_items(data):
    """Return (container_key, list) for the array of outputs/models inside an FPP
    channel-output config. co-other.json uses 'channelOutputs'; some files use
    'models'; a few are a bare list. container_key is None for a bare list."""
    if isinstance(data, dict):
        for k in ('channelOutputs', 'models'):
            if isinstance(data.get(k), list):
                return k, data[k]
    if isinstance(data, list):
        return None, data
    return None, None

def _extract_overlay_model(warnings):
    """Build a minimal bundle payload containing ONLY the currently-selected
    overlay model's entry (plus the metadata needed to merge it back), so export
    never ships the whole channel-output config. Returns bytes, or None if the
    model can't be located. The matching entry is found by any string field that
    equals the configured model name (robust to FPP's field naming)."""
    model = (config.get('overlay_model_name', '') or '').strip()
    if not model:
        warnings.append("No overlay model selected - overlay model not exported")
        return None
    ovl_file = _find_overlay_config_file()
    if not ovl_file:
        warnings.append("Overlay model config not found - overlay model not exported")
        return None
    try:
        with open(ovl_file, 'r') as f:
            data = json.load(f)
    except Exception as e:
        warnings.append(f"Could not read overlay config: {e}")
        return None
    container_key, items = _overlay_items(data)
    if items is None:
        warnings.append("Overlay config had an unexpected shape - overlay model not exported")
        return None
    entry, name_field = None, None
    for it in items:
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            if isinstance(v, str) and v == model:
                entry, name_field = it, k
                break
        if entry is not None:
            break
    if entry is None:
        warnings.append(f"Overlay model '{model}' not found in the config - not exported")
        return None
    payload = {
        "__tml_overlay__": 1,
        "dest_file": os.path.basename(ovl_file),   # e.g. co-other.json
        "container_key": container_key,            # e.g. channelOutputs (or null=bare list)
        "name_field": name_field,                  # which field names the model
        "model": entry,
    }
    return json.dumps(payload, indent=2).encode('utf-8')

def _merge_overlay_model(payload_bytes, warnings):
    """Add (or update) just the one exported overlay model into the target Pi's
    channel-output config, leaving every other output on that Pi untouched. Backs
    up the target file first. Falls back to a full replace only for legacy bundles
    that shipped a whole config file."""
    try:
        payload = json.loads(payload_bytes)
    except Exception:
        warnings.append("Overlay model in bundle was not valid JSON - skipped")
        return

    # Legacy bundle (a whole config file, pre single-model export): full replace.
    if not (isinstance(payload, dict) and payload.get("__tml_overlay__")):
        dest_path = os.path.join(FPP_CONFIG_DIR, 'co-other.json')
        os.makedirs(FPP_CONFIG_DIR, exist_ok=True)
        if os.path.isfile(dest_path):
            try:
                shutil.copy2(dest_path, dest_path + '.tml-bak')
            except OSError:
                pass
        with open(dest_path, 'wb') as f:
            f.write(payload_bytes)
        warnings.append("Imported a legacy overlay bundle by full replace (previous kept as .tml-bak)")
        return

    dest_file  = os.path.basename(payload.get('dest_file') or 'co-other.json')
    dest_path  = os.path.join(FPP_CONFIG_DIR, dest_file)
    key        = payload.get('container_key')
    name_field = payload.get('name_field') or 'description'
    entry      = payload.get('model')
    if not isinstance(entry, dict):
        warnings.append("Overlay model entry missing from bundle - skipped")
        return
    model_name = entry.get(name_field)

    # Load the target config (or start fresh if it doesn't exist yet).
    dest = None
    if os.path.isfile(dest_path):
        try:
            with open(dest_path, 'r') as f:
                dest = json.load(f)
        except Exception:
            warnings.append(f"Target {dest_file} was unreadable - overlay model skipped")
            return

    if key:
        if not isinstance(dest, dict):
            dest = {}
        items = dest.get(key)
        if not isinstance(items, list):
            items = []
            dest[key] = items
    else:
        if not isinstance(dest, list):
            dest = []
        items = dest

    # Back up before writing.
    os.makedirs(FPP_CONFIG_DIR, exist_ok=True)
    if os.path.isfile(dest_path):
        try:
            shutil.copy2(dest_path, dest_path + '.tml-bak')
        except OSError:
            pass

    # Replace an existing entry of the same name (update), else append (add).
    items[:] = [it for it in items
                if not (isinstance(it, dict) and it.get(name_field) == model_name)]
    items.append(entry)

    with open(dest_path, 'w') as f:
        json.dump(dest, f, indent=2)

@app.route('/api/config/import', methods=['POST'])
def import_config():
    """Restore a configuration bundle produced by /api/config/export.
    Credentials are never taken from the bundle; the target Pi keeps its own."""
    global config, twilio_client, _blocklist_cache, _whitelist_cache, _blacklist_cache
    try:
        upload = request.files.get('file')
        if upload is None:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        # Whether to also adopt the bundle's Master/Remote mode. Default OFF: an import keeps
        # THIS Pi's role so restoring settings never silently flips a master into a remote.
        import_mode = str(request.form.get('import_mode', '')).strip().lower() in ('1', 'true', 'yes', 'on')

        data = upload.read()
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return jsonify({"success": False, "error": "Not a valid .zip file"}), 400

        names = set(zf.namelist())
        if 'manifest.json' not in names:
            return jsonify({"success": False, "error": "Missing manifest.json - not a Text My Lights bundle"}), 400
        try:
            manifest = json.loads(zf.read('manifest.json'))
        except Exception:
            return jsonify({"success": False, "error": "Corrupt manifest.json"}), 400
        if manifest.get('bundle') != BUNDLE_MARKER:
            return jsonify({"success": False, "error": "This .zip is not a Text My Lights config bundle"}), 400

        warnings = []
        summary = {"settings": False, "content": 0, "overlay_model": False}

        # Map each export subdir to its destination directory on this Pi. basename
        # is used for every write (zip-slip safe - no attacker-controlled paths).
        dest_dirs = {
            'content/playlists':  FPP_PLAYLISTS_PATH,
            'content/sequences':  FSEQ_SEQUENCE_PATH,
            'content/images':     FPP_IMAGES_PATH,
            'content/videos':     FPP_VIDEOS_PATH,
        }
        # settings files land at their known individual paths, keyed by basename.
        settings_targets = {
            os.path.basename(CONFIG_FILE):            None,  # handled specially below
            os.path.basename(BLOCKLIST_FILE):         BLOCKLIST_FILE,
            os.path.basename(WHITELIST_FILE):         WHITELIST_FILE,
            os.path.basename(WHITELIST_ADDED_FILE):   WHITELIST_ADDED_FILE,
            os.path.basename(WHITELIST_REMOVED_FILE): WHITELIST_REMOVED_FILE,
            os.path.basename(BLACKLIST_FILE):         BLACKLIST_FILE,
            os.path.basename(BLACKLIST_ADDED_FILE):   BLACKLIST_ADDED_FILE,
            os.path.basename(BLACKLIST_REMOVED_FILE): BLACKLIST_REMOVED_FILE,
        }

        # Reject a "zip bomb": a small archive that expands to a huge amount of data.
        total_uncompressed = sum(zi.file_size for zi in zf.infolist())
        if total_uncompressed > MAX_IMPORT_UNCOMPRESSED:
            return jsonify({"success": False,
                            "error": "Bundle contents are too large - refusing to import"}), 400

        # Accept ONLY bundles whose every entry is a file this plugin itself writes on
        # export (settings we know, our content subdirs, or the overlay payload). If the
        # archive contains anything else, reject the WHOLE import rather than partially
        # applying it - so a hand-built or tampered .zip can't smuggle stray files in.
        def _entry_allowed(entry):
            if entry.endswith('/') or entry == 'manifest.json':
                return True
            arc_dir = entry.rsplit('/', 1)[0] if '/' in entry else ''
            base = os.path.basename(entry)
            if arc_dir == 'settings':
                return base in settings_targets
            return arc_dir in dest_dirs or arc_dir == 'overlay'
        bad = [e for e in names if not _entry_allowed(e)]
        if bad:
            return jsonify({"success": False,
                            "error": "Bundle contains unexpected files - not a clean "
                                     "Text My Lights export; import cancelled.",
                            "unexpected": sorted(bad)[:10]}), 400

        for entry in names:
            if entry.endswith('/') or entry == 'manifest.json':
                continue
            base = os.path.basename(entry)
            if not base:
                continue
            arc_dir = entry.rsplit('/', 1)[0] if '/' in entry else ''

            if arc_dir == 'settings':
                if base == os.path.basename(CONFIG_FILE):
                    # Merge imported settings, but never import credentials and
                    # never overwrite this Pi's stored ones.
                    try:
                        imported = json.loads(zf.read(entry))
                    except Exception:
                        warnings.append("plugin.json in bundle was invalid - settings skipped")
                        continue
                    for sk in SECRET_KEYS:
                        imported.pop(sk, None)
                    # Mode/identity keys (this box's Master/Remote role, which master a remote
                    # follows, its advertised name) are box-specific. Keep THIS Pi's values
                    # unless the user explicitly opted to import them (import_mode checkbox).
                    if not import_mode:
                        for mk in MODE_KEYS:
                            imported.pop(mk, None)
                        logging.info("Import: kept this Pi's Master/Remote mode (import_mode off)")
                    config.update(imported)
                    save_config()
                    summary["settings"] = True
                elif base in settings_targets and settings_targets[base]:
                    target = settings_targets[base]
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, 'wb') as f:
                        f.write(zf.read(entry))
                else:
                    warnings.append(f"Unknown settings file '{base}' - skipped")

            elif arc_dir in dest_dirs:
                target_dir = dest_dirs[arc_dir]
                os.makedirs(target_dir, exist_ok=True)
                with open(os.path.join(target_dir, base), 'wb') as f:
                    f.write(zf.read(entry))
                summary["content"] += 1

            elif arc_dir == 'overlay':
                # Add/update just the one exported model in the target's channel-
                # output config, leaving the target's other outputs untouched.
                _merge_overlay_model(zf.read(entry), warnings)
                summary["overlay_model"] = True

            else:
                warnings.append(f"Unrecognized entry '{entry}' - skipped")

        # Refresh in-memory state: force cache reloads and re-sync Twilio client.
        _blocklist_cache = None
        _whitelist_cache = None
        _blacklist_cache = None
        try:
            if config.get('twilio_account_sid') and config.get('twilio_auth_token'):
                twilio_client = Client(config['twilio_account_sid'], config['twilio_auth_token'])
        except Exception:
            pass

        return jsonify({
            "success": True,
            "summary": summary,
            "warnings": warnings,
            "note": "Overlay model changes take effect after an FPPD restart.",
        })
    except Exception as e:
        logging.error(f"import_config failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/fpp/fonts')
def fpp_fonts_endpoint():
    try:
        return jsonify(get_fpp_fonts())
    except Exception:
        return jsonify([])

@app.route('/api/fonts/file/<name>')
def serve_font_file(name):
    """Serve raw font bytes so the browser can @font-face them for the config
    page's canvas preview - otherwise the preview silently falls back to a
    generic sans-serif for every font, since the browser never has the actual
    file. Only serves fonts found by _enumerate_fonts(); name is matched
    against that list, never used to build a filesystem path directly."""
    try:
        for f in _enumerate_fonts():
            if f['name'] == name:
                ext = os.path.splitext(f['path'])[1].lower()
                mimetype = {'.ttf': 'font/ttf', '.otf': 'font/otf'}.get(ext, 'application/octet-stream')
                with open(f['path'], 'rb') as fh:
                    data = fh.read()
                return Response(data, mimetype=mimetype)
        return Response(status=404)
    except Exception as e:
        logging.error(f"Error serving font file '{name}': {e}")
        return Response(status=500)

@app.route('/api/fpp/data')
def get_fpp_data():
    global _fpp_data_cache, _fpp_data_cache_time
    try:
        # Return cached result if still fresh
        if _fpp_data_cache and (time.time() - _fpp_data_cache_time) < _FPP_DATA_CACHE_TTL:
            return jsonify(_fpp_data_cache)

        # Fetch all in parallel instead of sequentially
        results = {}
        tasks = {
            'playlists': get_fpp_playlists,
            'sequences': get_fpp_sequences,
            'models':    get_fpp_models,
            'fonts':     get_fpp_fonts,
            'videos':    get_fpp_videos,
            'images':    get_fpp_images,
        }
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    logging.error(f"FPP data task '{key}' failed: {e}")
                    results[key] = []

        _fpp_data_cache = results
        _fpp_data_cache_time = time.time()
        return jsonify(results)
    except Exception as e:
        return _client_error("get_fpp_data", e)

@app.route('/api/fpp/refresh', methods=['POST'])
def refresh_fpp_data():
    global _fpp_data_cache, _fpp_data_cache_time
    _fpp_data_cache = None
    _fpp_data_cache_time = 0
    return jsonify({"success": True})

@app.route('/api/fpp/test', methods=['POST'])
def test_fpp_api():
    try:
        success, status = test_fpp_connection()
        return jsonify({"success": success, "status": status})
    except Exception as e:
        return _client_error("test_fpp_api", e)

@app.route('/api/fseq/debug')
def fseq_debug():
    """Diagnostic endpoint - returns JSON describing exactly how the FSEQ frame would be read.
    ?sequence=name&model=ModelName   (same params as /api/fseq/frame)
    Helps diagnose channel-offset and bpp issues without needing SSH."""
    seq        = request.args.get('sequence', '').strip()
    model_name = request.args.get('model', config.get('overlay_model_name', '')).strip()

    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400

    name     = seq.removeprefix('seq:').removesuffix('.fseq')
    name     = os.path.basename(name)   # no path traversal - keep filename only
    filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
    if not os.path.exists(filepath):
        return jsonify({'error': f'Sequence not found: {name}.fseq'}), 404

    result = {
        'sequence':      name,
        'model':         model_name,
        'zstd_available': ZSTD_AVAILABLE,
        'pil_available':  PIL_AVAILABLE,
    }

    try:
        hdr = parse_fseq_header(filepath)
        comp_names = {0: 'uncompressed', 1: 'zlib', 2: 'zstd'}
        result['fseq'] = {
            'channel_count':        hdr['channel_count'],
            'frame_count':          hdr['frame_count'],
            'fps':                  round(hdr['fps'], 2),
            'step_time_ms':         hdr['step_time_ms'],
            'compression_type':     hdr['compression_type'],
            'compression_name':     comp_names.get(hdr['compression_type'], 'unknown'),
            'raw_compression_type': hdr['raw_compression_type'],
            'raw_compression_name': comp_names.get(hdr['raw_compression_type'], 'unknown'),
            'chan_data_offset':     hdr['chan_data_offset'],
            'num_comp_blocks':      len(hdr['comp_blocks']),
            'header_num_comp_blocks':   hdr['num_comp_blocks'],
            'num_sparse_ranges':    hdr['num_sparse_ranges'],
            'sparse_ranges':        hdr['sparse_ranges'],
            'sparse_sum':           sum(sr['count'] for sr in hdr['sparse_ranges']),
            'comp_blocks_preview':  hdr['comp_blocks'][:6],
        }

        # ── Decisive raw bytes ──────────────────────────────────────────────
        # These let us tell (without SSH) whether the channel data is actually
        # zlib / zstd / raw, and whether the header's compression byte lies.
        with open(filepath, 'rb') as _f:
            _hdr_raw = _f.read(32)
            _f.seek(hdr['chan_data_offset'])
            _data_probe = _f.read(8)
        probe_guess = 'unknown'
        if _data_probe[:4] == b'\x28\xB5\x2F\xFD':
            probe_guess = 'zstd'
        elif _data_probe[:1] == b'\x78':
            # zlib stream: 0x78 followed by 0x01/0x9C/0xDA typically
            probe_guess = 'zlib'
        elif hdr['compression_type'] == 0:
            probe_guess = 'raw/uncompressed'
        result['raw'] = {
            'header_hex':          _hdr_raw.hex(),
            'byte18_step_time':    _hdr_raw[18] if len(_hdr_raw) > 18 else None,
            'byte19_compression':  _hdr_raw[19] if len(_hdr_raw) > 19 else None,
            'byte20_num_blocks':   _hdr_raw[20] if len(_hdr_raw) > 20 else None,
            'byte21_num_sparse':   _hdr_raw[21] if len(_hdr_raw) > 21 else None,
            'data_probe_hex':      _data_probe.hex(),
            'data_looks_like':     probe_guess,
        }
    except Exception as e:
        result['fseq_error'] = str(e)
        return jsonify(result)

    sc, cc = get_model_channel_info(model_name) if model_name else (None, None)
    mw = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    mh = int(request.args.get('height', config.get('overlay_model_height', 0)))
    num_pixels = mw * mh if mw > 0 and mh > 0 else 0
    ch_count   = cc if cc else (num_pixels * 3 if num_pixels else None)
    bpp        = (ch_count // num_pixels) if (num_pixels and ch_count) else None
    start_ch   = (sc - 1) if sc else 0   # 0-indexed

    resolved_frame_byte = _sparse_ch_to_frame_byte(hdr['sparse_ranges'], start_ch)
    result['model_info'] = {
        'start_channel_1idx':   sc,
        'channel_count':        cc,
        'width':                mw,
        'height':               mh,
        'num_pixels':           num_pixels,
        'effective_ch_count':   ch_count,
        'effective_bpp':        bpp,
        'start_ch_0idx':        start_ch,
        'resolved_frame_byte':  resolved_frame_byte,  # None = not in sparse ranges
    }

    # Try reading frame 0 and show first 5 pixel values
    if ch_count and num_pixels:
        try:
            raw = read_fseq_frame(hdr, 0, start_ch, ch_count)
            sample_pixels = []
            actual_bpp = bpp or 3
            for i in range(min(5, num_pixels)):
                b = i * actual_bpp
                if b + 2 < len(raw):
                    sample_pixels.append([raw[b], raw[b+1], raw[b+2]])
                else:
                    sample_pixels.append(None)
            result['frame0_sample'] = {
                'bytes_read':    len(raw),
                'bytes_expected': ch_count,
                'first_5_pixels_rgb': sample_pixels,
                'all_zero':      all(v == 0 for v in raw),
                'all_same':      len(set(raw)) == 1,
            }
        except Exception as e:
            result['frame0_error'] = str(e)

    return jsonify(result)

@app.route('/api/fseq/info')
def fseq_info():
    """Return FSEQ file metadata for the canvas scrubber (frame count, fps, duration).
    Also attempts to auto-detect the overlay model's start channel from FPP."""
    seq        = request.args.get('sequence', '').strip()
    model_name = request.args.get('model', config.get('overlay_model_name', '')).strip()
    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400
    name = seq.removeprefix('seq:').removesuffix('.fseq')
    name = os.path.basename(name)   # no path traversal - keep filename only
    filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
    if not os.path.exists(filepath):
        return jsonify({'error': f'Sequence not found: {name}.fseq'}), 404
    try:
        hdr = parse_fseq_header(filepath)
        detected_sc, detected_cc = (get_model_channel_info(model_name)
                                    if model_name else (None, None))
        return jsonify({
            'frame_count':             hdr['frame_count'],
            'fps':                     round(hdr['fps'], 3),
            'duration_ms':             hdr['duration_ms'],
            'channel_count':           hdr['channel_count'],
            'compression_type':        hdr['compression_type'],
            'step_time_ms':            hdr['step_time_ms'],
            'detected_start_channel':  detected_sc,
            'detected_channel_count':  detected_cc,
        })
    except Exception as e:
        return _client_error("fseq_info", e, 500)

@app.route('/api/fseq/frame')
def fseq_frame():
    """Return a single FSEQ frame as a PNG image for the canvas background preview."""
    if not PIL_AVAILABLE:
        return jsonify({'error': 'Pillow not installed - run fpp_install.sh'}), 503

    seq        = request.args.get('sequence', '').strip()
    frame_idx  = max(0, int(request.args.get('frame', 0)))
    model_name = request.args.get('model', config.get('overlay_model_name', ''))
    width      = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    height     = int(request.args.get('height', config.get('overlay_model_height', 0)))
    start_ch_override  = request.args.get('start_channel', '').strip()
    ch_count_override  = request.args.get('channel_count', '').strip()

    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400
    if width <= 0 or height <= 0:
        return jsonify({'error': 'Overlay model dimensions unknown - select a model first'}), 400

    name = seq.removeprefix('seq:').removesuffix('.fseq')
    name = os.path.basename(name)   # no path traversal - keep filename only
    filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
    if not os.path.exists(filepath):
        return jsonify({'error': f'Sequence not found: {name}.fseq'}), 404

    # Determine start channel and channel count from FPP model info
    if start_ch_override:
        start_ch_1 = int(start_ch_override)
        ch_count   = int(ch_count_override) if ch_count_override else width * height * 3
    else:
        start_ch_1, ch_count_fpp = (get_model_channel_info(model_name)
                                    if model_name else (None, None))
        ch_count = ch_count_fpp if ch_count_fpp else width * height * 3

    if not start_ch_1:
        return jsonify({
            'error': (
                f'Could not find start channel for model "{model_name}". '
                'Verify the overlay model name matches an FPP channel output model.'
            )
        }), 400

    try:
        hdr          = parse_fseq_header(filepath)
        frame_idx    = min(frame_idx, hdr['frame_count'] - 1)
        start_ch     = start_ch_1 - 1   # convert to 0-indexed
        num_pixels   = width * height
        # bytes_per_pixel: 3 for RGB, 4 for RGBW - derived from actual channel count
        bpp          = max(3, ch_count // num_pixels) if num_pixels > 0 else 3

        raw = read_fseq_frame(hdr, frame_idx, start_ch, ch_count)

        logging.info(
            f"FSEQ preview: model={model_name} start_ch={start_ch_1} "
            f"ch_count={ch_count} bpp={bpp} frame={frame_idx} "
            f"first_px=({raw[0] if raw else '?'},{raw[1] if len(raw)>1 else '?'},"
            f"{raw[2] if len(raw)>2 else '?'})"
        )

        img = Image.new('RGB', (width, height))
        pixels = []
        for i in range(num_pixels):
            b = i * bpp
            if b + 2 < len(raw):
                pixels.append((raw[b], raw[b + 1], raw[b + 2]))
            else:
                pixels.append((0, 0, 0))
        img.putdata(pixels)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.read(), mimetype='image/png',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        logging.error(f"FSEQ frame error: {e}")
        return _client_error("fseq_frame", e, 500)

@app.route('/api/media/preview')
def media_preview():
    """Return a canvas-preview PNG for an image or video file.
    ?type=img&file=filename.jpg - resize image to model dims and return as PNG.
    ?type=vid&file=filename.mp4&time=0 - extract a frame at `time` seconds via ffmpeg,
        resize to model dims, return as PNG.  Falls back to a black frame if ffmpeg
        is unavailable or extraction fails."""
    if not PIL_AVAILABLE:
        return jsonify({'error': 'Pillow not installed - run fpp_install.sh'}), 503

    media_type = request.args.get('type', '').strip()   # 'img' or 'vid'
    filename   = request.args.get('file', '').strip()
    time_sec   = max(0.0, float(request.args.get('time', 0)))
    width  = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    height = int(request.args.get('height', config.get('overlay_model_height', 0)))

    if not filename or media_type not in ('img', 'vid'):
        return jsonify({'error': 'Requires ?type=img|vid&file=filename'}), 400
    if width <= 0 or height <= 0:
        return jsonify({'error': 'Overlay model dimensions unknown - select a model first'}), 400

    # Security: no path traversal - strip all directory components
    filename = os.path.basename(filename)

    try:
        if media_type == 'img':
            img_path = os.path.join(FPP_IMAGES_PATH, filename)
            if not os.path.exists(img_path):
                return jsonify({'error': f'Image not found: {filename}'}), 404
            img = Image.open(img_path).convert('RGB')
            img = img.resize((width, height), Image.LANCZOS)

        else:  # vid
            vid_path = os.path.join(FPP_VIDEOS_PATH, filename)
            if not os.path.exists(vid_path):
                return jsonify({'error': f'Video not found: {filename}'}), 404
            # Try ffmpeg to extract a single frame at the requested timestamp
            import subprocess as _sp
            try:
                result = _sp.run(
                    [
                        'ffmpeg', '-ss', str(time_sec), '-i', vid_path,
                        '-vframes', '1', '-f', 'image2pipe',
                        '-vcodec', 'png', '-'
                    ],
                    capture_output=True, timeout=10
                )
                if result.returncode == 0 and result.stdout:
                    img = Image.open(io.BytesIO(result.stdout)).convert('RGB')
                    img = img.resize((width, height), Image.LANCZOS)
                else:
                    # ffmpeg failed - return a dark grey placeholder
                    img = Image.new('RGB', (width, height), (32, 32, 32))
            except (FileNotFoundError, _sp.TimeoutExpired):
                # ffmpeg not installed on this system - return placeholder
                img = Image.new('RGB', (width, height), (32, 32, 32))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.read(), mimetype='image/png',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        return _client_error("media_preview", e, 500)


@app.route('/api/test')
def test_twilio():
    try:
        if not twilio_client:
            return jsonify({"success": False, "error": "Twilio client not initialized"})

        account = twilio_client.api.accounts(config['twilio_account_sid']).fetch()
        return jsonify({"success": True, "account": account.friendly_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/test_gv')
def test_google_voice_conn():
    """Verify Google Voice connectivity: IMAP (inbound, required) and SMTP
    (outbound replies, optional). Used by the config UI."""
    email_addr = config.get('gv_email', '').strip()
    app_pw = config.get('gv_app_password', '').strip()
    if not email_addr or not app_pw:
        return jsonify({"success": False, "error": "Enter your Gmail address and app password first."})

    # IMAP - required for reading incoming texts
    try:
        imap = imaplib.IMAP4_SSL(config.get('gv_imap_host', 'imap.gmail.com'))
        try:
            imap.login(email_addr, app_pw)
            imap.select(config.get('gv_imap_folder', 'INBOX'))
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        return jsonify({"success": False,
                        "error": f"Login failed ({e}). Use a Google App Password (not your normal password), "
                                 "with 2-Step Verification enabled and IMAP turned on in Gmail."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

    # SMTP - only needed for outbound auto-responses; report but don't fail on it
    reply_ready = False
    reply_error = ""
    try:
        with smtplib.SMTP(config.get('gv_smtp_host', 'smtp.gmail.com'),
                          int(config.get('gv_smtp_port', 587)), timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(email_addr, app_pw)
        reply_ready = True
    except Exception as e:
        reply_error = str(e)

    return jsonify({"success": True, "reply_ready": reply_ready, "reply_error": reply_error})

@app.route('/api/messages')
def get_messages():
    try:
        with open(get_day_log_path(), 'r') as f:
            messages = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        messages = []
    today = datetime.now().date().isoformat()
    return jsonify(redact_messages(list(reversed(messages)), today))

@app.route('/api/messages/clear', methods=['POST'])
def clear_messages():
    try:
        with open(get_day_log_path(), 'w') as f:
            json.dump([], f)
        logging.info("Message history cleared (today's file)")
        return jsonify({"success": True})
    except Exception as e:
        return _client_error("clear_messages", e)

@app.route('/api/messages/<date_str>')
def get_messages_by_date(date_str):
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
    try:
        with open(get_day_log_path(target_date), 'r') as f:
            messages = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        messages = []
    except Exception as e:
        return _client_error("get_messages_by_date", e, 500)
    return jsonify(redact_messages(list(reversed(messages)), date_str))

@app.route('/api/plugin_role')
def api_plugin_role():
    """Current effective role (master/remote), so the config page can reflect the live value.
    `plugin_role` is the user's EXPLICIT choice ('' = auto, follow FPP mode); `fpp_mode` + detail
    show what FPP reports (diagnostic - explicit choice always wins over it)."""
    return jsonify({"role": get_plugin_role(),
                    "plugin_role": (config.get('plugin_role') or ''),
                    "fpp_mode": _last_fpp_mode,
                    "fpp_mode_detail": _fpp_mode_detail})


@app.route('/api/plugin/masters')
def api_plugin_masters():
    """Remote (browser-facing): the plugin masters discovered on the FPP network, each with a
    friendly name + source number, and which one this remote is currently pinned to. Drives the
    'Sync to Master' picker + its auto-refresh. Normal token auth."""
    if not is_remote():
        return jsonify({"masters": [], "selected": "", "is_remote": False})
    force = request.args.get('refresh') in ('1', 'true', 'yes')
    masters = discover_masters(force=force)
    # Auto-pin when there's exactly one master and nothing's been chosen yet, so the box shows
    # it checked without the user having to pick.
    _maybe_auto_select_single_master()
    sel = _selected_master_addr()
    return jsonify({
        "is_remote": True,
        "selected": sel,
        "selected_reachable": any(m['address'] == sel for m in masters) if sel else True,
        "masters": [{"address": m["address"], "name": m["name"], "phone": m["phone"],
                     "selected": (m["address"] == sel)} for m in masters],
    })


@app.route('/api/plugin/names-content')
def api_plugin_names_content():
    """Remote (browser-facing): the current master-synced names content list, so the open
    Display page can live-refresh its dropdown/list when the master adds/removes content
    without a manual reload. Normal token auth."""
    if not is_remote():
        return jsonify({"is_remote": False, "names_content_list": []})
    return jsonify({"is_remote": True,
                    "names_content_list": config.get('names_content_list', []) or []})


@app.route('/api/plugin/select-master', methods=['POST'])
def api_plugin_select_master():
    """Remote (browser-facing): pin this remote to ONE plugin master (by address), or clear the
    pin. An explicit clear stores 'none' (follow nobody) rather than '' so the single-master
    auto-select won't immediately re-pick it. Only one master at a time."""
    if not is_remote():
        return jsonify({"success": False, "error": "This instance is not a remote."}), 409
    data = request.json or {}
    addr = str(data.get('address', '') or '').strip()
    global _masters_cache_time
    config['selected_master'] = addr if addr else 'none'
    save_config()
    _masters_cache_time = 0   # force a fresh probe on the next read
    logging.info(f"🔗 Remote pinned to plugin master: {addr or '(none - follow nobody)'}")
    # Immediately re-mirror the newly selected master's name-content list.
    try:
        sync_names_content_from_master()
    except Exception as e:
        logging.debug(f"select-master resync failed: {e}")
    return jsonify({"success": True, "selected": addr})


@app.route('/api/plugin/master-layout')
def api_master_layout():
    """Remote (browser-facing): fetch the master's text layout for a content id so the Display
    tab's 'Sync Position from Master' can copy it. Normal token auth; proxies to the master."""
    if not is_remote():
        return jsonify({"found": False, "error": "This instance is not a remote."}), 409
    content = request.args.get('content', '')
    master = _find_master_base()
    if not master:
        return jsonify({"found": False, "error": "Master not found on the network."}), 404
    try:
        r = requests.get(f"{master}/api/tml/layout", params={"content": content}, timeout=3)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"found": False, "error": f"Could not reach master: {e}"}), 502


@app.route('/api/queue/status')
def queue_status():
    try:
        status = get_queue_status()
        return jsonify(status)
    except Exception as e:
        return _client_error("queue_status", e)

@app.route('/api/test/message', methods=['POST'])
def test_message_submission():
    try:
        data = request.json
        test_name = data.get('name', '').strip()
        test_phone = data.get('phone', 'Local Testing')
        
        if not config.get('enabled', False):
            return jsonify({"success": False, "error": "Show is not live - press Start or run the Text My Lights Start script first"})

        if not test_name:
            return jsonify({"success": False, "error": "Name is required"})

        test_name = extract_name(test_name)
        is_valid, reason = is_valid_name(test_name)

        if not is_valid and not config.get('use_whitelist', False):
            if reason == "too_long":
                return jsonify({"success": False, "error": "Message exceeds Max Message Length", "reason": "too_long"})
            return jsonify({"success": False, "error": "Invalid name format", "reason": "invalid_format"})

        if not is_on_whitelist(test_name):
            return jsonify({"success": False, "error": "Name not on whitelist", "reason": "not_on_whitelist"})

        if config['profanity_filter'] and contains_profanity(test_name):
            return jsonify({"success": False, "error": "Profanity detected", "reason": "profanity"})

        success = add_to_queue(test_name, test_phone, f"TEST: {test_name}")

        if success:
            logging.info(f"🧪 Queued: {test_name}")
            log_message(test_phone, f"TEST: {test_name}", test_name, "queued")
            return jsonify({"success": True, "message": f"Test message '{test_name}' queued successfully!"})
        else:
            logging.error(f"🧪 Queue error: {test_name}")
            return jsonify({"success": False, "error": "Failed to add to queue"})
            
    except Exception as e:
        import traceback
        logging.error(traceback.format_exc())
        return _client_error("test_message_submission", e)

@app.route('/api/phone/block', methods=['POST'])
def api_block_phone():
    try:
        data = request.json or {}
        phone = data.get('phone')
        # Preferred path: block by message reference (date + timestamp). The full
        # number is resolved from the on-disk log server-side, so the browser only
        # ever holds the masked value - never the real number.
        if not phone and data.get('ts'):
            phone = _phone_from_log_ref(data.get('date'), data.get('ts'))
        if phone:
            success = block_phone(phone)
            # Never echo the full number back to the client.
            return jsonify({"success": success, "phone": mask_phone(phone)})
        return jsonify({"success": False, "error": "Could not resolve the number to block"})
    except Exception as e:
        return _client_error("api_block_phone", e)

@app.route('/api/respond', methods=['POST'])
def api_respond():
    """Send a manual custom reply to a logged message from the queue page.

    Google Voice only - Twilio has no reply path in this plugin. The message is
    identified by (date, timestamp); its stored reply context is resolved
    server-side so the real reply-to address never touches the browser."""
    try:
        if config.get('message_source') != 'google_voice':
            return jsonify({"success": False,
                            "error": "Replies are only available when Google Voice is the active source."}), 400
        data = request.json or {}
        text = (data.get('text') or '').strip()
        if not text:
            return jsonify({"success": False, "error": "Message text is required."}), 400
        ctx = _reply_ctx_from_log_ref(data.get('date'), data.get('ts'))
        if not ctx or not ctx.get('to'):
            return jsonify({"success": False,
                            "error": "No reply context stored for this message - it can't be answered."}), 400
        if send_gv_reply(text, "manual_reply", ctx=ctx):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Send failed - see the plugin log for details."})
    except Exception as e:
        return _client_error("api_respond", e)

@app.route('/api/phone/unblock', methods=['POST'])
def api_unblock_phone():
    try:
        data = request.json
        phone = data.get('phone')
        if phone:
            success = unblock_phone(phone)
            return jsonify({"success": success, "phone": phone})
        return jsonify({"success": False, "error": "No phone number provided"})
    except Exception as e:
        return _client_error("api_unblock_phone", e)

@app.route('/api/blocklist')
def api_get_blocklist():
    try:
        blocklist = load_blocklist()
        return jsonify({"blocklist": blocklist})
    except Exception as e:
        return _client_error("api_get_blocklist", e)

@app.route('/api/blacklist')
def api_get_blacklist():
    try:
        words = load_blacklist_words()
        return jsonify({"blacklist": words})
    except Exception as e:
        return _client_error("api_get_blacklist", e)

@app.route('/api/blacklist/add', methods=['POST'])
def api_add_blacklist():
    global _blacklist_cache, _blacklist_mtime
    try:
        data = request.json
        word = data.get('word', '').strip().lower()
        if not word:
            return jsonify({"success": False, "error": "Word is required"})
        # Check if already in global list
        global_words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                global_words = {line.strip().lower() for line in f if line.strip()}
        if word in global_words:
            return jsonify({"success": False, "error": "Word already in blacklist"})
        # Add to user-added list
        added = load_blacklist_added()
        if word in added:
            return jsonify({"success": False, "error": "Word already in blacklist"})
        added.add(word)
        with open(BLACKLIST_ADDED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(added)) + '\n')
        # If word was previously removed from global, un-remove it
        removed = load_blacklist_removed()
        if word in removed:
            removed.discard(word)
            with open(BLACKLIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _blacklist_cache = None
        _blacklist_mtime = None
        logging.info(f"Added '{word}' to user blacklist")
        return jsonify({"success": True})
    except Exception as e:
        return _client_error("api_add_blacklist", e)

@app.route('/api/blacklist/remove', methods=['POST'])
def api_remove_blacklist():
    global _blacklist_cache, _blacklist_mtime
    try:
        data = request.json
        word = data.get('word', '').strip().lower()
        global_words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                global_words = {line.strip().lower() for line in f if line.strip()}
        # Remove from user-added if present
        added = load_blacklist_added()
        if word in added:
            added.discard(word)
            with open(BLACKLIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(added)) + '\n' if added else '')
        # If in global, track removal so git pull can't re-add it
        if word in global_words:
            removed = load_blacklist_removed()
            removed.add(word)
            with open(BLACKLIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _blacklist_cache = None
        _blacklist_mtime = None
        logging.info(f"Removed '{word}' from blacklist")
        return jsonify({"success": True})
    except Exception as e:
        return _client_error("api_remove_blacklist", e)

@app.route('/api/whitelist')
def api_get_whitelist():
    try:
        names = sorted(load_whitelist())
        return jsonify({"whitelist": names})
    except Exception as e:
        return _client_error("api_get_whitelist", e)

@app.route('/api/whitelist/add', methods=['POST'])
def api_add_whitelist():
    global _whitelist_cache, _whitelist_mtime
    try:
        data = request.json
        name = data.get('name', '').strip().lower()
        if not name:
            return jsonify({"success": False, "error": "Name is required"})
        global_names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                global_names = {line.strip().lower() for line in f if line.strip()}
        removed = load_removed_names()

        if name in global_names:
            if name in removed:
                # Name was blocked by user - un-remove it to make it active again
                removed.discard(name)
                with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(sorted(removed)) + '\n')
                _whitelist_cache = None
                _whitelist_mtime = None
                logging.info(f"Re-enabled '{name}' in whitelist")
                return jsonify({"success": True})
            else:
                return jsonify({"success": False, "error": "Name already in whitelist"})

        # Add to user-added list
        added = load_whitelist_added()
        if name in added:
            return jsonify({"success": False, "error": "Name already in whitelist"})
        added.add(name)
        with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(added)) + '\n')
        # If name was previously removed from global, un-remove it
        if name in removed:
            removed.discard(name)
            with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _whitelist_cache = None
        _whitelist_mtime = None
        logging.info(f"Added '{name}' to user whitelist")
        return jsonify({"success": True})
    except Exception as e:
        return _client_error("api_add_whitelist", e)

@app.route('/api/whitelist/remove', methods=['POST'])
def api_remove_whitelist():
    global _whitelist_cache, _whitelist_mtime
    try:
        data = request.json
        name = data.get('name', '').strip().lower()
        global_names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                global_names = {line.strip().lower() for line in f if line.strip()}
        # Remove from user-added if present
        added = load_whitelist_added()
        if name in added:
            added.discard(name)
            with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(added)) + '\n' if added else '')
        # If in global, track removal so git pull can't re-add it
        if name in global_names:
            removed = load_removed_names()
            removed.add(name)
            with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _whitelist_cache = None
        _whitelist_mtime = None
        logging.info(f"Removed '{name}' from whitelist")
        return jsonify({"success": True})
    except Exception as e:
        return _client_error("api_remove_whitelist", e)

@app.route('/whitelist')
def view_whitelist():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Name Whitelist</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #4CAF50; }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { border: 1px solid #ddd; padding: 8px 10px; text-align: left; }
            th { background: #4CAF50; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px 5px 5px 0; }
            button:hover { background: #45a049; }
            .remove-btn { background: #f44336; padding: 4px 10px; font-size: 12px; margin: 0; }
            .remove-btn:hover { background: #d32f2f; }
            .add-btn { background: #2196F3; }
            .add-btn:hover { background: #0b7dda; }
            .info { background: #e8f5e9; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #c8e6c9; }
            .add-row { display: flex; gap: 10px; margin: 12px 0; }
            .add-row input { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
            .search-row { display: flex; gap: 10px; margin: 12px 0; }
            .search-row input { flex: 1; padding: 10px; border: 2px solid #4CAF50; border-radius: 4px; font-size: 14px; }
            .hint { color: #888; font-size: 13px; margin: 6px 0; }
            .error { color: #f44336; font-size: 13px; }
            .success { color: #4CAF50; font-size: 13px; }
            .empty { background: #f5f5f5; padding: 30px; text-align: center; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>📋 Name Whitelist</h1>
        <div class="info">
            Only names on this list are accepted when the whitelist is enabled. &nbsp;|&nbsp; <strong id="count">Loading...</strong>
        </div>
        {% if not config.get('use_whitelist', False) %}
        <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>⚠️ <strong>Whitelist is not enabled</strong> - All names will be shown regardless of this list.</span>
            <button onclick="toggleSetting('use_whitelist', true)" style="background:#4CAF50; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✓ Enable Whitelist</button>
        </div>
        {% else %}
        <div style="background:#e8f5e9; border:1px solid #a5d6a7; color:#2e7d32; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>✅ <strong>Whitelist is enabled</strong></span>
            <button onclick="toggleSetting('use_whitelist', false)" style="background:#f44336; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✗ Disable Whitelist</button>
        </div>
        {% endif %}
        <button onclick="location.href='/'">← Back to Config</button>

        <script>
        function toggleSetting(key, value) {
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({[key]: value})
            }).then(() => location.reload());
        }
        </script>

        <h3>Add a Name</h3>
        <div class="add-row">
            <input type="text" id="add_name" placeholder="Type a name to approve and press Enter..." onkeydown="if(event.key==='Enter') addName()">
            <button class="add-btn" onclick="addName()">+ Add</button>
        </div>
        <div id="add_result"></div>

        <h3>Search / Browse</h3>
        <div class="search-row">
            <input type="text" id="search" placeholder="Search names... (e.g. John)" oninput="renderTable()">
        </div>
        <div id="hint" class="hint"></div>
        <div id="list_area"></div>
        <div id="scroll_sentinel" style="height:1px;"></div>

        <script>
            var allNames = [];

            function loadWhitelist() {
                document.getElementById('count').textContent = 'Loading...';
                fetch('/api/whitelist')
                .then(r => r.json())
                .then(data => {
                    allNames = data.whitelist || [];
                    document.getElementById('count').textContent = allNames.length.toLocaleString() + ' approved names';
                    renderTable();
                    window.scrollTo(0, 0);
                })
                .catch(() => {
                    document.getElementById('count').textContent = 'Error loading';
                });
            }

            var visibleCount = 100;
            var currentFiltered = [];
            var PAGE_SIZE = 100;
            var observer = null;

            function setupSentinel() {
                if (observer) observer.disconnect();
                observer = new IntersectionObserver(function(entries) {
                    if (entries[0].isIntersecting) appendRows();
                }, { rootMargin: '400px' });
                observer.observe(document.getElementById('scroll_sentinel'));
            }

            function renderTable() {
                const query = document.getElementById('search').value.trim().toLowerCase();
                const area = document.getElementById('list_area');
                const hint = document.getElementById('hint');

                visibleCount = PAGE_SIZE;

                if (allNames.length === 0) {
                    hint.textContent = '';
                    area.innerHTML = '<div class="empty"><h3>No names in whitelist yet</h3><p>Add names above to approve them.</p></div>';
                    return;
                }

                currentFiltered = query
                    ? allNames.filter(n => n.toLowerCase().includes(query))
                    : allNames;

                if (currentFiltered.length === 0) {
                    hint.textContent = 'No names match "' + query + '"';
                    area.innerHTML = '<div class="empty"><p>No names match your search.</p></div>';
                    return;
                }

                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                area.innerHTML = '<table id="names_table"><tr><th>Name</th><th></th><th>Name</th><th></th></tr>' + buildRows(showing) + '</table>';
                setupSentinel();
            }

            function appendRows() {
                if (visibleCount >= currentFiltered.length) return;
                visibleCount = Math.min(visibleCount + PAGE_SIZE, currentFiltered.length);
                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                const area = document.getElementById('list_area');
                area.innerHTML = '<table id="names_table"><tr><th>Name</th><th></th><th>Name</th><th></th></tr>' + buildRows(showing) + '</table>';
            }

            function buildRows(items) {
                let rows = '';
                for (let i = 0; i < items.length; i += 2) {
                    const a = items[i];
                    const b = items[i + 1];
                    const ae = a.replace(/'/g, "&#39;");
                    const be = b ? b.replace(/'/g, "&#39;") : '';
                    rows += `<tr>` +
                        `<td style="text-transform:capitalize">${a}</td>` +
                        `<td><button class="remove-btn" onclick="removeName('${ae}')">✕ Remove</button></td>` +
                        (b
                            ? `<td style="text-transform:capitalize">${b}</td><td><button class="remove-btn" onclick="removeName('${be}')">✕ Remove</button></td>`
                            : `<td></td><td></td>`) +
                        `</tr>`;
                }
                return rows;
            }

            function updateHint() {
                const hint = document.getElementById('hint');
                const query = document.getElementById('search').value.trim();
                const showing = Math.min(visibleCount, currentFiltered.length);
                if (query) {
                    hint.textContent = 'Showing ' + showing + ' of ' + currentFiltered.length + ' matches';
                } else {
                    hint.textContent = 'Showing ' + showing + ' of ' + allNames.length.toLocaleString() + ' names' +
                        (showing < allNames.length ? ' - scroll down to load more' : '');
                }
            }

            function addName() {
                const input = document.getElementById('add_name');
                const name = input.value.trim();
                const result = document.getElementById('add_result');
                if (!name) return;
                fetch('/api/whitelist/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        result.innerHTML = '<p class="success">✅ Added: ' + name + '</p>';
                        input.value = '';
                        loadWhitelist();
                    } else {
                        result.innerHTML = '<p class="error">❌ ' + data.error + '</p>';
                    }
                    setTimeout(() => result.innerHTML = '', 3000);
                });
            }

            function removeName(name) {
                if (!confirm('Remove "' + name + '" from the whitelist?')) return;
                fetch('/api/whitelist/remove', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                })
                .then(r => r.json())
                .then(() => loadWhitelist());
            }

            loadWhitelist();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config)

@app.route('/blacklist')
def view_blacklist_page():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Profanity Filter - Blacklist</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #f44336; }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { border: 1px solid #ddd; padding: 8px 10px; text-align: left; }
            th { background: #f44336; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px 5px 5px 0; }
            button:hover { background: #45a049; }
            .remove-btn { background: #f44336; padding: 4px 10px; font-size: 12px; margin: 0; }
            .remove-btn:hover { background: #d32f2f; }
            .add-btn { background: #2196F3; }
            .add-btn:hover { background: #0b7dda; }
            .info { background: #fce4e4; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #f5c6c6; }
            .add-row { display: flex; gap: 10px; margin: 12px 0; }
            .add-row input { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
            .search-row { display: flex; gap: 10px; margin: 12px 0; }
            .search-row input { flex: 1; padding: 10px; border: 2px solid #f44336; border-radius: 4px; font-size: 14px; }
            .hint { color: #888; font-size: 13px; margin: 6px 0; }
            .error { color: #f44336; font-size: 13px; }
            .success { color: #4CAF50; font-size: 13px; }
            .empty { background: #f5f5f5; padding: 30px; text-align: center; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>🚫 Profanity Blacklist</h1>
        <div class="info">
            ℹ️ Messages containing any word on this list are rejected by the profanity filter. &nbsp;|&nbsp; <strong id="count">Loading...</strong>
        </div>
        {% if not config.get('profanity_filter', True) %}
        <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>⚠️ <strong>Blacklist is not enabled</strong> - Words on this list will still be shown.</span>
            <button onclick="toggleSetting('profanity_filter', true)" style="background:#4CAF50; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✓ Enable Profanity Filter</button>
        </div>
        {% else %}
        <div style="background:#e8f5e9; border:1px solid #a5d6a7; color:#2e7d32; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>✅ <strong>Profanity Filter is enabled</strong></span>
            <button onclick="toggleSetting('profanity_filter', false)" style="background:#f44336; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✗ Disable Profanity Filter</button>
        </div>
        {% endif %}
        <button onclick="location.href='/'">← Back to Config</button>

        <script>
        function toggleSetting(key, value) {
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({[key]: value})
            }).then(() => location.reload());
        }
        </script>

        <h3>Add a Word</h3>
        <div class="add-row">
            <input type="text" id="add_word" placeholder="Type a word to block and press Enter..." onkeydown="if(event.key==='Enter') addWord()">
            <button class="add-btn" onclick="addWord()">+ Add</button>
        </div>
        <div id="add_result"></div>

        <h3>Search / Browse</h3>
        <div class="search-row">
            <input type="text" id="search" placeholder="Search words..." oninput="renderTable()">
        </div>
        <div id="hint" class="hint"></div>
        <div id="list_area"></div>
        <div id="scroll_sentinel" style="height:1px;"></div>

        <script>
            var allWords = [];

            function loadBlacklist() {
                document.getElementById('count').textContent = 'Loading...';
                fetch('/api/blacklist')
                .then(r => r.json())
                .then(data => {
                    allWords = data.blacklist || [];
                    document.getElementById('count').textContent = allWords.length.toLocaleString() + ' blocked words';
                    renderTable();
                    window.scrollTo(0, 0);
                })
                .catch(() => {
                    document.getElementById('count').textContent = 'Error loading';
                });
            }

            var visibleCount = 100;
            var currentFiltered = [];
            var PAGE_SIZE = 100;
            var observer = null;

            function setupSentinel() {
                if (observer) observer.disconnect();
                observer = new IntersectionObserver(function(entries) {
                    if (entries[0].isIntersecting) appendRows();
                }, { rootMargin: '400px' });
                observer.observe(document.getElementById('scroll_sentinel'));
            }

            function renderTable() {
                const query = document.getElementById('search').value.trim().toLowerCase();
                const area = document.getElementById('list_area');
                const hint = document.getElementById('hint');

                visibleCount = PAGE_SIZE;

                if (allWords.length === 0) {
                    hint.textContent = '';
                    area.innerHTML = '<div class="empty"><h3>No words in blacklist yet</h3><p>Add words above to block them.</p></div>';
                    return;
                }

                currentFiltered = query
                    ? allWords.filter(w => w.toLowerCase().includes(query))
                    : allWords;

                if (currentFiltered.length === 0) {
                    hint.textContent = 'No words match "' + query + '"';
                    area.innerHTML = '<div class="empty"><p>No words match your search.</p></div>';
                    return;
                }

                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                area.innerHTML = '<table id="words_table"><tr><th>Word</th><th></th><th>Word</th><th></th></tr>' + buildRows(showing) + '</table>';
                setupSentinel();
            }

            function appendRows() {
                if (visibleCount >= currentFiltered.length) return;
                visibleCount = Math.min(visibleCount + PAGE_SIZE, currentFiltered.length);
                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                const area = document.getElementById('list_area');
                area.innerHTML = '<table id="words_table"><tr><th>Word</th><th></th><th>Word</th><th></th></tr>' + buildRows(showing) + '</table>';
            }

            function buildRows(items) {
                let rows = '';
                for (let i = 0; i < items.length; i += 2) {
                    const a = items[i];
                    const b = items[i + 1];
                    const ae = a.replace(/'/g, "&#39;");
                    const be = b ? b.replace(/'/g, "&#39;") : '';
                    rows += `<tr>` +
                        `<td>${a}</td>` +
                        `<td><button class="remove-btn" onclick="removeWord('${ae}')">✕ Remove</button></td>` +
                        (b
                            ? `<td>${b}</td><td><button class="remove-btn" onclick="removeWord('${be}')">✕ Remove</button></td>`
                            : `<td></td><td></td>`) +
                        `</tr>`;
                }
                return rows;
            }

            function updateHint() {
                const hint = document.getElementById('hint');
                const query = document.getElementById('search').value.trim();
                const showing = Math.min(visibleCount, currentFiltered.length);
                if (query) {
                    hint.textContent = 'Showing ' + showing + ' of ' + currentFiltered.length + ' matches';
                } else {
                    hint.textContent = 'Showing ' + showing + ' of ' + allWords.length.toLocaleString() + ' words' +
                        (showing < allWords.length ? ' - scroll down to load more' : '');
                }
            }

            function addWord() {
                const input = document.getElementById('add_word');
                const word = input.value.trim();
                const result = document.getElementById('add_result');
                if (!word) return;
                fetch('/api/blacklist/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({word: word})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        result.innerHTML = '<p class="success">✅ Added: ' + word + '</p>';
                        input.value = '';
                        loadBlacklist();
                    } else {
                        result.innerHTML = '<p class="error">❌ ' + data.error + '</p>';
                    }
                    setTimeout(() => result.innerHTML = '', 3000);
                });
            }

            function removeWord(word) {
                if (!confirm('Remove "' + word + '" from the blacklist?')) return;
                fetch('/api/blacklist/remove', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({word: word})
                })
                .then(r => r.json())
                .then(() => loadBlacklist());
            }

            loadBlacklist();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config)

@app.route('/blocklist')
def view_blocklist():
    blocklist = load_blocklist()
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Blocked Phone Numbers</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #f44336; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background: #f44336; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 10px 5px 10px 0; }
            .unblock-btn { background: #4CAF50; padding: 5px 10px; font-size: 12px; }
            .info { background: #ffebee; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #ffcdd2; color: #333; }
            .no-blocked { background: #f5f5f5; padding: 40px; text-align: center; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>🚫 Blocked Phone Numbers</h1>
        <div class="info">
            ℹ️ Blocked numbers cannot send messages | Total Blocked: {{ blocklist|length }}
        </div>
        <button onclick="location.href='/'">← Back to Config</button>
        <button onclick="location.href='/messages'">📋 View Messages</button>
        
        {% if blocklist|length == 0 %}
        <div class="no-blocked">
            <h2>No blocked numbers</h2>
            <p>Block numbers from the Messages page.</p>
        </div>
        {% else %}
        <table>
            <tr>
                <th>Phone Number</th>
                <th>Action</th>
            </tr>
            {% for phone in blocklist %}
            <tr>
                <td>{{ phone }}</td>
                <td><button class="unblock-btn" onclick="unblockPhone('{{ phone }}')">✅ Unblock</button></td>
            </tr>
            {% endfor %}
        </table>
        {% endif %}
        
        <script>
            function unblockPhone(phone) {
                if (confirm('Unblock ' + phone + '?')) {
                    fetch('/api/phone/unblock', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({phone: phone})
                    })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            alert('✅ Phone number unblocked!');
                            location.reload();
                        }
                    });
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html, blocklist=blocklist)

@app.route('/status')
def status_page():
    status_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Plugin Status</title>
        <style>
            body {{ font-family: monospace; background: #ffffff; color: #333; padding: 20px; }}
            .section {{ background: #f5f5f5; padding: 15px; margin: 15px 0; border: 1px solid #ddd; }}
            .ok {{ color: #4CAF50; }}
            .error {{ color: #f44336; }}
            button {{ background: #4CAF50; color: white; padding: 10px; border: none; cursor: pointer; margin: 5px; }}
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){{window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{{window.parent.postMessage({{type:'scrollTop'}},'*');}}catch(e){{}}}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>🔧 Text My Lights - Status</h1>
        <button onclick="location.href='/'">← Back</button>
        <button onclick="location.reload()">🔄 Refresh</button>
        
        <div class="section">
            <h2>Plugin State</h2>
            <p>Enabled: <span class="{'ok' if config.get('enabled') else 'error'}">{config.get('enabled')}</span></p>
            <p>Display Worker: <span class="{'ok' if display_thread and display_thread.is_alive() else 'error'}">{display_thread and display_thread.is_alive()}</span></p>
            <p>Polling Worker: <span class="{'ok' if polling_thread and polling_thread.is_alive() else 'error'}">{polling_thread and polling_thread.is_alive()}</span></p>
        </div>
        
        <div class="section">
            <h2>Queue Status</h2>
            <p>Currently Displaying: {currently_displaying.get('name') if currently_displaying else 'Nothing'}</p>
            <p>Queue Length: {len(message_queue)}</p>
        </div>
    </body>
    </html>
    """
    return status_html

@app.route('/messages')
def view_messages():
    today = datetime.now().date()
    tabs = []
    for i in range(7):
        d = today - timedelta(days=i)
        if i == 0:
            label = f"Today ({d.strftime('%b %-d')})"
        else:
            label = d.strftime("%a %b %-d")
        tabs.append({"date": d.isoformat(), "label": label, "is_today": (i == 0)})
    try:
        with open(get_day_log_path(today), 'r') as f:
            today_messages = list(reversed(json.load(f)))
    except (FileNotFoundError, json.JSONDecodeError):
        today_messages = []

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Message History & Queue</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #4CAF50; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background: #4CAF50; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            .displaying { background: #4CAF50 !important; color: white; font-weight: bold; }
            .queued { color: #e65100; }
            .displayed { color: #4CAF50; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 10px 5px 10px 0; }
            .block-btn { background: #f44336; padding: 5px 10px; font-size: 12px; }
            .respond-btn { background: #1976d2; padding: 4px 10px; font-size: 12px; margin: 0 0 0 8px; }
            .clear-btn { background: #f44336; }
            .info { background: #e3f2fd; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #90caf9; color: #333; }
            .queue-box { background: #f3e5f5; padding: 20px; border-radius: 5px; margin: 20px 0; border: 1px solid #ce93d8; color: #333; }
            .current-display { background: #4CAF50; padding: 15px; border-radius: 5px; margin: 10px 0; font-size: 18px; font-weight: bold; color: white; }
            .queue-item { background: #f9f9f9; padding: 10px; border-radius: 5px; margin: 5px 0; border-left: 4px solid #FF9800; }
            .tab-bar { display: flex; flex-wrap: wrap; gap: 4px; margin: 16px 0 0; border-bottom: 2px solid #4CAF50; }
            .tab-btn { padding: 8px 14px; border: 1px solid #ddd; border-bottom: none; background: #f5f5f5; cursor: pointer; border-radius: 4px 4px 0 0; font-size: 13px; color: #555; }
            .tab-btn:hover { background: #e8f5e9; }
            .tab-btn.active { background: #4CAF50; color: white; border-color: #4CAF50; font-weight: bold; }
            .tab-panel { display: none; padding-top: 16px; }
            .tab-panel.active { display: block; }
            .history-note { background: #fff9c4; padding: 8px 12px; border-radius: 4px; font-size: 13px; color: #5d4037; margin-bottom: 12px; border: 1px solid #f9a825; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>Message History & Queue</h1>
        <button onclick="location.href='/'">Back to Config</button>
        <button class="clear-btn" onclick="clearHistory()">Clear Today's Messages</button>

        <div class="tab-bar">
            {% for tab in tabs %}
            <button class="tab-btn {% if tab.is_today %}active{% endif %}"
                    id="tab-btn-{{ tab.date }}"
                    onclick="switchTab('{{ tab.date }}', {{ tab.is_today | tojson }})">{{ tab.label }}</button>
            {% endfor %}
        </div>

        <!-- TODAY TAB -->
        <div class="tab-panel active" id="panel-{{ tabs[0].date }}">
            <div class="info">Auto-refreshes every 5 seconds | Messages today: <span id="msg-count">{{ today_messages | length }}</span></div>
            <div class="queue-box">
                <h2>Current Display Queue</h2>
                <div id="queue-box-content"><p style="color:#aaa;">Loading...</p></div>
            </div>
            <h2>Today's Messages</h2>
            <div id="today-messages-content"><p style="color:#aaa;">Loading...</p></div>
        </div>

        <!-- PAST DAY TAB PANELS -->
        {% for tab in tabs[1:] %}
        <div class="tab-panel" id="panel-{{ tab.date }}">
            <div class="history-note">Past day snapshot - no live queue.</div>
            <div id="history-content-{{ tab.date }}"><p style="color:#aaa;">Click tab to load.</p></div>
        </div>
        {% endfor %}

        <!-- Block modal -->
        <div id="block-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
            <div style="background:#fff; border-radius:8px; padding:28px; max-width:420px; width:90%; box-shadow:0 4px 20px rgba(0,0,0,0.3);">
                <h3 style="margin-top:0; color:#333;">Block Action</h3>
                <p style="color:#555; margin-bottom:6px;">Phone: <strong id="modal-phone"></strong></p>
                <p style="color:#555; margin-bottom:20px;">Name: <strong id="modal-name-text"></strong></p>
                <p style="color:#333; font-weight:bold; margin-bottom:16px;">What would you like to block?</p>
                <div style="display:flex; flex-direction:column; gap:10px;">
                    <button style="background:#f44336; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="blockPhone()">Block this number from texting again</button>
                    <button id="modal-block-name-btn" style="background:#FF9800; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="blockNameFromDisplay()">Block this name from being displayed</button>
                    <p id="whitelist-warning" style="color:#f44336; font-size:12px; margin:0; padding:4px 0; display:none;">
                        Whitelist is not enabled - this name may appear again
                    </p>
                    <button style="background:#aaa; color:white; padding:10px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="closeBlockModal()">Cancel</button>
                </div>
            </div>
        </div>

        <!-- Respond modal -->
        <div id="respond-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
            <div style="background:#fff; border-radius:8px; padding:28px; max-width:460px; width:90%; box-shadow:0 4px 20px rgba(0,0,0,0.3);">
                <h3 style="margin-top:0; color:#333;">Send a Reply</h3>
                <p style="color:#555; margin-bottom:12px;">To: <strong id="respond-to"></strong></p>
                <textarea id="respond-text" rows="4" maxlength="300"
                          style="width:100%; box-sizing:border-box; padding:10px; border:1px solid #ccc; border-radius:5px; font-size:14px; font-family:inherit; resize:vertical;"
                          placeholder="Type your reply..."></textarea>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px;">
                    <span id="respond-count" style="color:#999; font-size:12px;">0 / 300</span>
                    <span id="respond-status" style="font-size:13px;"></span>
                </div>
                <div style="display:flex; gap:10px; margin-top:16px;">
                    <button id="respond-send-btn" style="background:#1976d2; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer; flex:1;"
                            onclick="sendRespond()">Send Reply</button>
                    <button style="background:#aaa; color:white; padding:12px 18px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="closeRespondModal()">Cancel</button>
                </div>
            </div>
        </div>

        <script>
            var useWhitelist = {{ config.get('use_whitelist', False) | tojson }};
            // Manual replies only work over Google Voice (Twilio has no reply path).
            var canRespond = {{ (config.get('message_source') == 'google_voice') | tojson }};
            var modalOpen = false;
            var refreshTimer = null;
            var prevQueueJson = null;
            var prevTodayJson = null;
            var loadedTabs = {};

            function esc(s) {
                return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
            }

            function fmtTime(ts) {
                if (!ts) return '';
                try {
                    var d = new Date(ts);
                    var mo = String(d.getMonth()+1).padStart(2,'0');
                    var dy = String(d.getDate()).padStart(2,'0');
                    var yr = d.getFullYear();
                    var hr = d.getHours();
                    var mn = String(d.getMinutes()).padStart(2,'0');
                    var sc = String(d.getSeconds()).padStart(2,'0');
                    var ampm = hr >= 12 ? 'PM' : 'AM';
                    hr = hr % 12 || 12;
                    return mo+'-'+dy+'-'+yr+' '+hr+':'+mn+':'+sc+' '+ampm;
                } catch(e) { return ts; }
            }

            function switchTab(date, isToday) {
                document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
                document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
                document.getElementById('tab-btn-' + date).classList.add('active');
                document.getElementById('panel-' + date).classList.add('active');
                if (isToday) {
                    scheduleRefresh();
                } else {
                    clearTimeout(refreshTimer);
                    if (!loadedTabs[date]) { loadedTabs[date] = true; loadHistoryTab(date); }
                }
                // Class change doesn't trigger MutationObserver - report height explicitly
                requestAnimationFrame(function() {
                    window.parent.postMessage({ type: 'iframeHeight', height: document.body.scrollHeight }, '*');
                });
            }

            function loadHistoryTab(date) {
                var container = document.getElementById('history-content-' + date);
                container.innerHTML = '<p style="color:#aaa;">Loading...</p>';
                fetch('/api/messages/' + date)
                    .then(function(r) { return r.json(); })
                    .then(function(msgs) { container.innerHTML = renderTable(msgs, false); })
                    .catch(function(e) { container.innerHTML = '<p style="color:#f44336;">Failed to load.</p>'; });
            }

            function scheduleRefresh() {
                clearTimeout(refreshTimer);
                refreshTimer = setTimeout(function() {
                    if (!modalOpen) refreshData(); else scheduleRefresh();
                }, 5000);
            }

            function refreshData() {
                Promise.all([
                    fetch('/api/queue/status').then(function(r) { return r.json(); }),
                    fetch('/api/messages').then(function(r) { return r.json(); })
                ]).then(function(results) {
                    renderQueue(results[0]);
                    renderTodayMessages(results[1]);
                    scheduleRefresh();
                }).catch(scheduleRefresh);
            }

            function renderQueue(status) {
                var json = JSON.stringify(status);
                if (json === prevQueueJson) return;
                prevQueueJson = json;
                var html = '';
                if (status.currently_displaying) {
                    html += '<div class="current-display">NOW DISPLAYING: ' + esc(status.currently_displaying.name) +
                            ' (from ***' + esc(status.currently_displaying.phone_last4) + ')</div>';
                } else {
                    html += '<div class="current-display" style="background:#bdbdbd;color:#333;">Nothing currently displaying</div>';
                }
                if (status.queue_length > 0) {
                    html += '<h3 style="color:#FF9800;margin-top:20px;">Queue (' + status.queue_length + ' waiting):</h3>';
                    status.queue.forEach(function(item, i) {
                        html += '<div class="queue-item"><strong>Queue Position ' + (i+1) + ':</strong> ' +
                                esc(item.name) + ' (from ***' + esc(item.phone_last4) + ')</div>';
                    });
                } else {
                    html += '<p style="color:#aaa;font-style:italic;margin-top:15px;">Queue is empty</p>';
                }
                document.getElementById('queue-box-content').innerHTML = html;
            }

            function renderTodayMessages(messages) {
                var json = JSON.stringify(messages);
                if (json === prevTodayJson) return;
                prevTodayJson = json;
                document.getElementById('msg-count').textContent = messages.length;
                document.getElementById('today-messages-content').innerHTML = renderTable(messages, true);
            }

            function renderTable(messages, showBlock) {
                if (!messages || messages.length === 0) {
                    return '<div style="background:#f5f5f5;padding:40px;text-align:center;border-radius:5px;"><h3>No messages</h3></div>';
                }
                var statusLabel = {'displaying':'DISPLAYING NOW','queued':'Queued','displayed':'Displayed'};
                var rows = messages.map(function(msg) {
                    var label = statusLabel[msg.status] || esc(msg.status);
                    var btn = '';
                    if (showBlock && msg.phone_full !== 'Local Testing') {
                        // Block by reference (timestamp + log date) - the full number
                        // stays server-side; we only carry the masked value for display.
                        btn = '<button class="block-btn" data-ts="' + esc(msg.timestamp) + '" data-date="' + esc(msg._log_date || '') +
                              '" data-masked="' + esc(msg.phone) + '" data-name="' + esc(msg.extracted_name) +
                              '" onclick="showBlockModal(this.dataset.masked,this.dataset.name,this.dataset.ts,this.dataset.date)">Block</button>';
                    }
                    // Respond button sits next to the phone number. Google Voice only,
                    // and only when this message carries a stored reply context.
                    var respond = '';
                    if (canRespond && msg.can_respond) {
                        respond = '<button class="respond-btn" data-ts="' + esc(msg.timestamp) + '" data-date="' + esc(msg._log_date || '') +
                                  '" data-masked="' + esc(msg.phone) +
                                  '" onclick="showRespondModal(this.dataset.masked,this.dataset.ts,this.dataset.date)">Respond</button>';
                    }
                    return '<tr class="' + esc(msg.status) + '">' +
                        '<td>' + fmtTime(msg.timestamp) + '</td>' +
                        '<td>' + esc(msg.phone) + respond + '</td>' +
                        '<td>' + esc(msg.message) + '</td>' +
                        '<td>' + esc(msg.extracted_name) + '</td>' +
                        '<td class="' + esc(msg.status) + '">' + label + '</td>' +
                        '<td>' + btn + '</td></tr>';
                }).join('');
                return '<table><tr><th>Timestamp</th><th>Phone</th><th>Message</th><th>Name</th><th>Status</th><th>Action</th></tr>' + rows + '</table>';
            }

            function showBlockModal(masked, name, ts, date) {
                modalOpen = true;
                document.getElementById('modal-phone').textContent = masked;
                document.getElementById('modal-name-text').textContent = name || '(no name)';
                document.getElementById('modal-block-name-btn').disabled = !name;
                document.getElementById('modal-block-name-btn').style.opacity = name ? '1' : '0.4';
                document.getElementById('whitelist-warning').style.display = useWhitelist ? 'none' : 'block';
                var modal = document.getElementById('block-modal');
                modal.dataset.ts = ts || '';
                modal.dataset.date = date || '';
                modal.dataset.name = name || '';
                modal.style.display = 'flex';
            }

            function closeBlockModal() {
                modalOpen = false;
                document.getElementById('block-modal').style.display = 'none';
                scheduleRefresh();
            }

            function blockPhone() {
                var modal = document.getElementById('block-modal');
                var ts = modal.dataset.ts, date = modal.dataset.date;
                closeBlockModal();
                fetch('/api/phone/block', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ts:ts, date:date}) })
                    .then(function(r) { return r.json(); })
                    .then(function(data) { alert(data.success ? 'Phone number blocked!' : ('Could not block: ' + (data.error || 'unknown error'))); refreshData(); });
            }

            function blockNameFromDisplay() {
                var modal = document.getElementById('block-modal');
                var name = modal.dataset.name;
                closeBlockModal();
                fetch('/api/whitelist/remove', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name}) })
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        alert(data.success ? '"' + name + '" blocked from display!' : 'Error: ' + data.error);
                        refreshData();
                    });
            }

            function showRespondModal(masked, ts, date) {
                modalOpen = true;
                var modal = document.getElementById('respond-modal');
                document.getElementById('respond-to').textContent = masked;
                var ta = document.getElementById('respond-text');
                ta.value = '';
                document.getElementById('respond-count').textContent = '0 / 300';
                document.getElementById('respond-status').textContent = '';
                document.getElementById('respond-send-btn').disabled = false;
                modal.dataset.ts = ts || '';
                modal.dataset.date = date || '';
                modal.style.display = 'flex';
                ta.focus();
            }

            function closeRespondModal() {
                modalOpen = false;
                document.getElementById('respond-modal').style.display = 'none';
                scheduleRefresh();
            }

            function sendRespond() {
                var modal = document.getElementById('respond-modal');
                var text = document.getElementById('respond-text').value.trim();
                var status = document.getElementById('respond-status');
                if (!text) { status.style.color = '#f44336'; status.textContent = 'Enter a message first.'; return; }
                var btn = document.getElementById('respond-send-btn');
                btn.disabled = true;
                status.style.color = '#555'; status.textContent = 'Sending...';
                fetch('/api/respond', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({ ts: modal.dataset.ts, date: modal.dataset.date, text: text })
                })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.success) {
                        status.style.color = '#4CAF50'; status.textContent = '✓ Reply sent!';
                        setTimeout(closeRespondModal, 1000);
                    } else {
                        status.style.color = '#f44336'; status.textContent = '✗ ' + (data.error || 'Send failed');
                        btn.disabled = false;
                    }
                })
                .catch(function() {
                    status.style.color = '#f44336'; status.textContent = '✗ Send failed';
                    btn.disabled = false;
                });
            }

            document.addEventListener('input', function(e) {
                if (e.target && e.target.id === 'respond-text') {
                    document.getElementById('respond-count').textContent = e.target.value.length + ' / 300';
                }
            });

            function clearHistory() {
                if (confirm("Clear all of today's messages?")) {
                    fetch('/api/messages/clear', { method:'POST' })
                        .then(function(r) { return r.json(); })
                        .then(function(data) { if (data.success) alert("Today's messages cleared!"); refreshData(); });
                }
            }

            refreshData();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config, tabs=tabs, today_messages=today_messages)


# ── Multi-instance (master/remote) endpoints ────────────────────────────────
# Authenticated in _require_access_token by trusting known FPP MultiSync peers (no secret).

@app.route('/api/tml/ping', methods=['GET'])
def api_tml_ping():
    """Identify this instance to a discovering peer: plugin name + effective role, plus a
    friendly name and source number so a remote can tell multiple masters apart. A master
    pushes only to peers that answer here with role == 'remote'."""
    return jsonify({"plugin": "textmylights", "role": get_plugin_role(),
                    "name": _instance_label(), "phone": _instance_phone_label()})


@app.route('/api/tml/content-list', methods=['GET'])
def api_tml_content_list():
    """Master: the content ids in its Name Display list, so a remote can mirror them into its
    own Display-tab dropdown (and give each its own overlay layout). Content ids only - no
    layouts, no sequence data."""
    names = [it.get('content', '') for it in (config.get('names_content_list', []) or []) if it.get('content')]
    return jsonify({"names": names})


@app.route('/api/tml/layout', methods=['GET'])
def api_tml_layout():
    """Master: the saved text layout for a content id + this master's overlay model size, so a
    remote can copy the positioning ('Sync Position from Master'), scaled to its own model."""
    content = request.args.get('content', '')
    item = next((it for it in (config.get('names_content_list', []) or [])
                 if it.get('content') == content), None)
    mw, mh = _overlay_model_dims()
    if not item:
        return jsonify({"found": False, "model_w": mw, "model_h": mh})
    return jsonify({"found": True, "model_w": mw, "model_h": mh, "layout": {
        k: item.get(k) for k in ('message_lines', 'line_boxes', 'line_colors', 'line_movements',
                                 'line_speeds', 'line_fonts', 'line_orientations', 'display_duration')
    }})


def _apply_remote_waiting(content):
    """Remote: switch the base waiting/background layer to the master-pushed content - unless
    it's already showing that content (no-op, so the master's heartbeat re-push doesn't cause
    a visible restart), or this instance lacks that content (keep whatever is showing)."""
    content = content or ''
    if content == _active_waiting_content:
        return  # already on it - ignore repeat/heartbeat pushes
    if content and not _content_exists_locally(content):
        logging.info(f"ℹ️  Remote: pushed waiting content '{content}' not present - keeping current")
        return
    with rotator_lock:
        _switch_waiting_content(content, _active_waiting_content)


def _push_from_selected_master():
    """Remote: True only if the current request comes from the master this remote is pinned to.
    With no master selected the remote follows nobody, so ALL pushes are ignored - nothing is
    displayed until a master is explicitly picked in the 'Sync to Master' list."""
    sel = _selected_master_addr()
    if not sel:
        return False
    return request.remote_addr == sel


@app.route('/api/tml/state', methods=['POST'])
def api_tml_state():
    """Remote: apply the master's chosen display state. A name event ({name, content,
    duration}) shows that name over the content using THIS instance's own layout; a waiting
    event ({content}) switches the background. Only remotes act on it, and only when the push
    comes from the master this remote is pinned to (so several masters can coexist)."""
    if not is_remote():
        return jsonify({"success": False, "error": "not in remote mode"}), 409
    if not _push_from_selected_master():
        return jsonify({"success": True, "ignored": "not the selected master"})
    data = request.json or {}
    content = str(data.get('content', '') or '')
    name = str(data.get('name', '') or '').strip()
    duration = data.get('duration')
    global _remote_last_state, _remote_last_state_time, _remote_stop_requested
    _remote_last_state = {"name": name, "content": content}
    _remote_last_state_time = time.time()
    try:
        if name:
            add_to_queue(name, "REMOTE", name,
                         override={"content": content, "duration": duration})
        else:
            # A waiting push with real content means the master is live - clear any prior
            # stop request so this remote resumes returning to its waiting content between
            # names. (An empty-content push is a stopped/cleared master; don't re-arm on it.)
            if content:
                _remote_stop_requested = False
            _apply_remote_waiting(content)
        return jsonify({"success": True})
    except Exception as e:
        return _client_error("api_tml_state", e)


@app.route('/api/tml/stop', methods=['POST'])
def api_tml_stop():
    """Remote: the master was Stopped, so stop here too. Same graceful behavior as the local
    Stop - any names the master already pushed drain first, then the waiting content stops.
    Only remotes act on it (the peer-IP allowlist in before_request gates who may call it), and
    only from the master this remote is pinned to."""
    if not is_remote():
        return jsonify({"success": False, "error": "not in remote mode"}), 409
    if not _push_from_selected_master():
        return jsonify({"success": True, "ignored": "not the selected master"})
    global _remote_stop_requested
    _remote_stop_requested = True   # the display worker tears down once its queue drains
    try:
        return jsonify(_deactivate_local())
    except Exception as e:
        return _client_error("api_tml_stop", e)


@app.route('/api/activate', methods=['GET', 'POST'])
def api_activate():
    """FPP scheduler hook: enable the plugin, start SMS polling, and start the waiting playlist."""
    global polling_thread, stop_polling

    # A remote never polls/responds - it just needs to be enabled to render pushed names and
    # show its own waiting content as a fallback until the master pushes. Skip the
    # content-required check + polling for remotes.
    if is_remote():
        config['enabled'] = True
        save_config()
        result = start_waiting_content()
        logging.info("✅ Text My Lights Start (remote) - ready to receive names from the master")
        return jsonify({"success": True, "playlist_started": result, "role": "remote",
                        "message": "Text My Lights remote activated"})

    # Require waiting content - a single default_playlist or a rotation list. Without one
    # the show has no defined state.
    _has_list = len(config.get('default_content_list', []) or []) > 0
    if not config.get('default_playlist', '').strip() and not _has_list:
        msg = "ERROR: No Default Waiting Content configured. Set one in the plugin settings before running Text My Lights Start."
        logging.error(msg)
        return jsonify({"success": False, "error": msg}), 400

    config['enabled'] = True
    stop_polling = False
    save_config()

    # Start the poller for the selected message source if not already running
    if not start_polling_if_needed():
        logging.warning("⚠️  Activate: message source not configured, polling not started")

    # Start the waiting content (single, or the rotator for a 2+ item list)
    result = start_waiting_content()

    logging.info(f"✅ Text My Lights Start activated - playlist {'started' if result else 'FAILED to start'}")
    return jsonify({"success": True, "playlist_started": result,
                    "message": "Text My Lights plugin activated"})


def _deactivate_local():
    """Disable the plugin and stop the show on THIS instance. If names are still displaying
    or queued, let them finish first (the display worker stops the waiting content once the
    queue drains); only stop immediately when the queue is idle. Returns the JSON response
    body. Shared by the scheduler hook (/api/deactivate) and the master's stop broadcast
    (/api/tml/stop)."""
    config['enabled'] = False    # stop accepting new names right away
    save_config()

    # Is anything still on screen or waiting to be shown?
    with queue_lock:
        pending = len(message_queue) > 0
    draining = pending or (currently_displaying is not None)

    if draining:
        logging.info("🛑 Text My Lights Stop: draining - names still playing/queued; "
                     "waiting content will stop after they finish")
        return {"success": True, "draining": True,
                "message": "Stopping after current names finish"}

    # Nothing queued or displaying - stop the waiting content now.
    stop_show_playback()
    logging.info("🛑 Text My Lights Stop: disabled and playback stopped")
    return {"success": True, "message": "Text My Lights plugin deactivated"}


@app.route('/api/deactivate', methods=['GET', 'POST'])
def api_deactivate():
    """FPP scheduler hook: disable the plugin and stop the show. The polling thread keeps
    running to send show_not_live replies. When this is the master, the Stop is also
    broadcast to every remote so pressing Stop here takes the whole show down."""
    # Tell the remotes to stop too (no-op unless this instance is the master). Fire this
    # first so they begin draining in parallel with the master.
    broadcast_stop_to_remotes()
    return jsonify(_deactivate_local())


if __name__ == '__main__':
    # Migrate files from old scattered paths to the new plugin data directory
    _migrations = [
        ("/home/fpp/media/config/plugin.fpp-textmylights.json", CONFIG_FILE),
        ("/home/fpp/media/config/blocked_phones.json",         BLOCKLIST_FILE),
        ("/home/fpp/media/config/last_message_sid.txt",        LAST_SID_FILE),
        ("/home/fpp/media/config/queue_pending.json",          QUEUE_FILE),
    ]
    for _old, _new in _migrations:
        if not os.path.exists(_new) and os.path.exists(_old):
            try:
                import shutil
                shutil.copy2(_old, _new)
                logging.info(f"Migrated {_old} → {_new}")
            except Exception as _e:
                logging.error(f"Migration failed {_old}: {_e}")

    load_config()

    # Clean up log files older than 7 days
    cleanup_old_logs()

    # Restore any pending queue items saved before the last shutdown
    load_queue_from_file()

    # Pre-warm caches in background so first test/message isn't slow
    def _warm_caches():
        try:
            load_blacklist()
            load_whitelist()
            logging.info("Cache pre-warm complete")
        except Exception as e:
            logging.warning(f"Cache pre-warm failed: {e}")
    threading.Thread(target=_warm_caches, daemon=True).start()

    # Display worker always runs so Testing Tools work without Twilio credentials
    display_thread = threading.Thread(target=display_worker, daemon=True)
    display_thread.start()

    # Waiting-content rotator runs for the whole process, idling unless the show is enabled
    # and a 2+ item rotation list is configured.
    rotator_thread = threading.Thread(target=waiting_rotator, daemon=True)
    rotator_thread.start()

    # Master heartbeat: keeps remotes' waiting content in sync even if one joins late.
    threading.Thread(target=master_sync_heartbeat, daemon=True).start()

    # Follow FPP's own mode: switch the plugin role to match when FPP changes player↔remote.
    threading.Thread(target=fpp_mode_watcher, daemon=True).start()

    # Remote: mirror the master's Name-content list into this box's Display dropdown so each
    # content can be given this remote's own overlay layout.
    threading.Thread(target=remote_content_sync, daemon=True).start()

    # Polling thread starts if the selected source is configured - runs in
    # standby (show_not_live replies) when disabled, and processes names normally
    # when enabled. Picks Twilio or Google Voice based on message_source.
    start_polling_if_needed()

    # Start the waiting content on launch if the plugin is already enabled
    if config['enabled']:
        def _start_default():
            import time
            time.sleep(3)  # brief delay to let FPP settle before sending commands
            start_waiting_content()
        threading.Thread(target=_start_default, daemon=True).start()

    logging.info("Text My Lights plugin starting...")
    if os.path.exists(AUTH_DISABLE_FILE):
        logging.warning("⚠️  AUTH DISABLED at startup (.disable_auth present) - access "
                        "control is OFF for everyone on the network. This is for debugging "
                        f"only; delete {AUTH_DISABLE_FILE} before normal use.")
    # Bind 0.0.0.0 ON PURPOSE: the Master/Remote feature needs other FPP instances on the LAN
    # to reach this one (the master POSTs name/stop events to each remote's :5000, and remotes
    # fetch the master's content list), so 127.0.0.1 would break multi-instance. Access is still
    # gated: browser endpoints require the per-instance token, /api/tml/* only accept FPP
    # MultiSync peers, and the scheduler hooks are loopback-only.
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
