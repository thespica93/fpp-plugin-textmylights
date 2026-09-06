#!/usr/bin/env python3
"""
Text My Lights - FPP plugin: viewers text a name that appears on your display.
Supports Twilio and Google Voice as message sources.
"""

from flask import Flask, request, jsonify, render_template_string, Response, g
import logging
import json
import secrets as _secrets
import requests
from datetime import datetime, timedelta, timezone
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from twilio.rest import Client
from collections import deque
import os
import struct
import io
import imaplib
import smtplib
import email
import email.utils
from email.header import decode_header, make_header

# PIL/Pillow for pixel-accurate text rendering (optional — falls back to FPP text API if unavailable)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# zstandard for FSEQ zstd decompression (optional — install via fpp_install.sh)
try:
    import zstandard as _zstd_mod
    ZSTD_AVAILABLE = True
except ImportError:
    _zstd_mod = None
    ZSTD_AVAILABLE = False

_scroll_thread = None   # background PIL scroll animation thread

# Configuration
PLUGIN_DIR      = os.path.dirname(os.path.abspath(__file__))

# All runtime data lives under one plugin folder
PLUGIN_DATA_DIR = "/home/fpp/media/plugin.fpp-textmylights"
CONFIG_FILE     = os.path.join(PLUGIN_DATA_DIR, "plugin.json")
# Credentials live in their own owner-only directory — NOT in plugin.json, logs,
# or backups. (True at-rest secrecy isn't possible on this hardware: an
# unattended service must be able to read them on boot, so any key would sit on
# the same card. This keeps them out of the shared config and off casual view.)
SECRETS_DIR     = os.path.join(PLUGIN_DATA_DIR, "secrets")
SECRETS_FILE    = os.path.join(SECRETS_DIR, "credentials.json")
SECRET_KEYS     = ("twilio_auth_token", "gv_app_password")
LOG_FILE        = os.path.join(PLUGIN_DATA_DIR, "logs", "sms_plugin.log")
QUEUE_FILE      = os.path.join(PLUGIN_DATA_DIR, "queue_pending.json")
MESSAGES_DIR    = os.path.join(PLUGIN_DATA_DIR, "logs", "messages")
LAST_SID_FILE   = os.path.join(PLUGIN_DATA_DIR, "last_message_sid.txt")
LAST_GV_UID_FILE = os.path.join(PLUGIN_DATA_DIR, "last_gv_uid.txt")
BLOCKLIST_FILE  = os.path.join(PLUGIN_DATA_DIR, "blocked_phones.json")

FSEQ_SEQUENCE_PATH = '/home/fpp/media/sequences'
FPP_VIDEOS_PATH    = '/home/fpp/media/videos'
FPP_IMAGES_PATH    = '/home/fpp/media/images'

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

# Setup logging — ensure the log directory exists, then write to file + stderr
_log_handlers = [logging.StreamHandler()]  # stderr always available via nohup
try:
    _log_handlers.append(logging.FileHandler(LOG_FILE))
except Exception:
    pass  # directory may not exist on some FPP installs; stderr is the fallback
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_log_handlers
)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

import flask.cli
flask.cli.show_server_banner = lambda *args: None

app = Flask(__name__)

# ============================================================================
# NETWORK ACCESS CONTROL
# ----------------------------------------------------------------------------
# The service binds 0.0.0.0:5000 so the FPP web UI (running on a different
# machine — the user's browser) can iframe it. To keep anonymous LAN clients
# from reading credentials / controlling the show, every *network* request must
# carry an access token. The token is minted here and read by the FPP-served
# PHP pages (ui.php / messages.php), which are already behind FPP's own web
# server — so only someone who can load the FPP UI ever receives it.
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
    mint one on first run. The token file is world-readable on purpose — the FPP
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

@app.before_request
def _require_access_token():
    # Trust the loopback interface (scheduler scripts, on-box curl, the poller
    # never hits HTTP). remote_addr comes from the socket peer; we never trust
    # X-Forwarded-For, so it cannot be spoofed to look local.
    if request.remote_addr in ('127.0.0.1', '::1'):
        return None
    if os.path.exists(AUTH_DISABLE_FILE):
        return None
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

# FPP runs locally — always use localhost
FPP_HOST = 'http://127.0.0.1'

# Default configuration
DEFAULT_CONFIG = {
    "enabled": False,
    # Which inbound message source feeds the pipeline: "twilio" | "google_voice"
    "message_source": "twilio",
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
    "max_messages_per_phone": 5,
    "max_message_length": 30,
    "max_message_age_mins": 5,
    "one_word_only": False,
    "two_words_max": True,
    "use_whitelist": False,
    "profanity_filter": True,
    "fpp_host": "http://127.0.0.1",
    "default_playlist": "",
    "name_display_playlist": "",
    "overlay_model_name": "",
    "text_color": "#FF0000",
    "text_font": "FreeSans",
    "text_position": "Center",
    "message_template": "Merry Christmas {name}!",  # legacy — migrated to message_lines on load
    "message_lines": ["Merry Christmas", "{name}!", "", ""],
    # Each box is the MAX area a line can render into; font size auto-fits to it
    # (largest size where the actual message text fits both w and h), then is
    # centered within the box. x/y < 0 means auto-position (horizontally centered /
    # vertically stacked among the other auto-positioned lines) — w/h are always
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
    "sms_response_show_not_live": False,
    "sms_response_success": False,
    "sms_response_profanity": False,
    "sms_response_rate_limited": False,
    "allow_duplicate_names": False,
    "sms_response_duplicate": False,
    "sms_response_invalid_format": False,
    "sms_response_not_whitelisted": False,
    "sms_response_blocked": False,
    "response_show_not_live": "Ho, Ho, Ho, It looks like our show isn't running now. Try again later.",
    "response_success": "Merry Christmas! Your name will appear on our display soon! 🎄",
    "response_profanity": "Sorry, your message contains inappropriate content and cannot be displayed. Please keep within the Christmas spirit! 🎅",
    "response_blocked": "Sorry, Your phone number has been blocked from sending messages.",
    "response_rate_limited": "You've reached the maximum number of messages allowed. Please try again tomorrow!",
    "response_duplicate": "You've already sent this name today!",
    "response_invalid_format": "Please send only a name (1-2 words, no sentences).",
    "response_not_whitelisted": "Sorry, that name is not on our approved list and cannot be shown.",
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

        # Drop responses whose trigger can't fire (limit 0 / duplicates allowed)
        _apply_source_policy()
        save_config()

        logging.info("Configuration loaded successfully")
    except FileNotFoundError:
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


def _apply_source_policy():
    """Keep the saved config self-consistent with the selected message source.

    SMS auto-responses only have a working outbound path over Google Voice
    (reply-to-email); Twilio has no reply path in this plugin and the config
    page hides the whole SMS Responses tab under Twilio. So under Twilio we
    force every response OFF — otherwise a stale toggle left over from Google
    Voice would imply a reply that can never be sent.

    We do NOT force Max Messages Per Phone or Allow Duplicate Names to source
    defaults here. Those are the user's to set (the config page seeds sensible
    defaults when the source is switched), and clobbering them on every save is
    what previously made the Duplicate / Rate-Limited responses impossible to
    enable under Google Voice.

    We still drop the two responses whose trigger genuinely can't fire:
      - rate-limited → off when Max Messages Per Phone is 0 (nobody is limited)
      - duplicate    → off when duplicate names are allowed (never a duplicate)

    Invalid-format is intentionally not touched: the whitelist only greys it in
    the UI and the send path already skips it while the whitelist is on, so the
    user's on/off choice is preserved for when the whitelist is turned back off.

    Mutates `config` in place; caller is responsible for saving."""
    if config.get('message_source', 'twilio') != 'google_voice':
        for key in list(config.keys()):
            if key.startswith('sms_response_'):
                config[key] = False

    if config.get('max_messages_per_phone', 0) == 0:
        config['sms_response_rate_limited'] = False
    if config.get('allow_duplicate_names', False):
        config['sms_response_duplicate'] = False

_font_path_cache = {}

def _resolve_font_path(font_name):
    """Resolve a font name to its file path (fc-match, falling back to a manual
    directory search), cached per name for the process lifetime. Box-fit sizing
    calls this many times per line (once per binary-search step) at different
    sizes, so the expensive part — locating the file — only happens once."""
    if font_name in _font_path_cache:
        return _font_path_cache[font_name]

    path = None
    # Use fontconfig (fc-match) — same resolution FPP uses for its font names
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


def render_to_shm(line_items, model_name, width, height):
    """Render multiple text lines to FPP shared memory, each with its own box, color, font,
    and orientation.
    line_items: list of (text, box_x, box_y, box_w, box_h, color_hex, font_name, orientation)
    tuples. orientation is 'horizontal' (default), 'vertical_rotated', or 'vertical_stacked'
    — see _render_oriented_text_strip. Font size is auto-fit to (box_w, box_h), then the
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
            # FPP creates shm files as root after postStart.sh runs.
            # Use the sudoers rule added by fpp_install.sh to fix permissions once.
            logging.warning(f"render_to_shm: permission denied on {shm_path} — running sudo chmod")
            import subprocess
            result = subprocess.run(
                ['sudo', '-n', '/usr/bin/chmod', '666', shm_path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                _write()
            else:
                logging.error(f"render_to_shm: sudo chmod failed: {result.stderr.decode().strip()}")
                logging.error("render_to_shm: restart FPPD to apply shm permissions from postStart.sh")
                return False

        logging.info(f"render_to_shm: wrote {len(raw)} bytes to {shm_path} ({len(line_items)} lines)")
        return True
    except Exception as e:
        logging.error(f"render_to_shm failed: {e}")
        return False


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
                    img.paste(strip, (draw_x, draw_y))

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
            import subprocess
            result = subprocess.run(
                ['sudo', '-n', '/usr/bin/chmod', '666', shm_path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                _write()
            else:
                logging.error(f"render_image_to_shm: sudo chmod failed")
                return False

        logging.info(f"render_image_to_shm: wrote {image_path} → {shm_path}")
        return True
    except Exception as e:
        logging.error(f"render_image_to_shm failed: {e}")
        return False


def animate_lines_via_shm(items, model_name, width, height, duration):
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
            unconstrained — the text is expected to be wider than the box and travels
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
        fps = 30

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
            else:  # Center — fixed, centered in box
                entry['dx'] = resolved_bx + max(0, (box_w - tw) // 2)
                entry['dy'] = resolved_by + max(0, (box_h - th) // 2)
            prepared.append(entry)

        if not prepared:
            return False

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        if os.path.exists(shm_path) and not os.access(shm_path, os.W_OK):
            import subprocess
            subprocess.run(['sudo', '-n', '/usr/bin/chmod', '666', shm_path],
                           capture_output=True, timeout=5)

        logging.info(f"🎬 animate_lines_via_shm: model={model_name} size={width}x{height} "
                     f"lines={len(prepared)} duration={duration}s")

        def _clip_paste(frame, strip, src_x, src_y, dst_x, dst_y, vis_w, vis_h, clip):
            # Intersect the paste rect with the line's own box, so scrolling text is only
            # visible while inside it — entering/exiting at the box edges instead of the
            # full canvas edges.
            cx, cy, cw, ch = clip
            x0, y0 = max(dst_x, cx), max(dst_y, cy)
            x1, y1 = min(dst_x + vis_w, cx + cw), min(dst_y + vis_h, cy + ch)
            if x1 <= x0 or y1 <= y0:
                return
            crop_x0, crop_y0 = src_x + (x0 - dst_x), src_y + (y0 - dst_y)
            frame.paste(strip.crop((crop_x0, crop_y0, crop_x0 + (x1 - x0), crop_y0 + (y1 - y0))), (x0, y0))

        def _animate():
            import time as _time
            start = _time.time()
            while _time.time() - start < duration:
                frame = Image.new('RGB', (width, height), (0, 0, 0))
                for e in prepared:
                    if e['movement'] in ('L2R', 'R2L'):
                        ix = int(e['pos'])
                        src_x = max(0, -ix); dst_x = max(0, ix)
                        vis_w = min(e['tw'] - src_x, width - dst_x)
                        if vis_w > 0:
                            _clip_paste(frame, e['strip'], src_x, 0, dst_x, e['dy'], vis_w, e['th'], e['clip'])
                    elif e['movement'] in ('T2B', 'B2T'):
                        iy = int(e['pos'])
                        src_y = max(0, -iy); dst_y = max(0, iy)
                        vis_h = min(e['th'] - src_y, height - dst_y)
                        if vis_h > 0:
                            _clip_paste(frame, e['strip'], 0, src_y, e['dx'], dst_y, e['tw'], vis_h, e['clip'])
                    else:
                        frame.paste(e['strip'], (e['dx'], e['dy']))
                try:
                    with open(shm_path, 'r+b') as f: f.write(frame.tobytes())
                except Exception:
                    pass
                for e in prepared:
                    if e['movement'] in ('L2R', 'R2L', 'T2B', 'B2T'):
                        if e.get('done'):
                            continue  # fit passes all completed -- hold fully exited
                        e['pos'] += e['dir'] * e['step_px']
                        overshot = (e['dir'] < 0 and e['pos'] < e['loop_end']) or (e['dir'] > 0 and e['pos'] > e['loop_end'])
                        if overshot:
                            # A pass just completed. Fixed-speed loops forever (snap back
                            # and repeat for the rest of the window). Fit-to-time snaps back
                            # for passes 1..N-1, then on the Nth pass holds fully exited at
                            # loop_end (no jarring snap-back flash at the very end).
                            if e.get('fit'):
                                e['wraps'] += 1
                                if e['wraps'] >= e['fit_passes']:
                                    e['pos'] = e['loop_end']; e['done'] = True
                                else:
                                    e['pos'] = e['loop_start']
                            else:
                                e['pos'] = e['loop_start']
                _time.sleep(1.0 / fps)

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
    # Offset 20 and 21 are separate uint8 fields — NOT a single uint16
    num_comp_blocks   = raw[20]           # uint8
    num_sparse_ranges = raw[21]           # uint8

    # ── Auto-detect zstd compression ─────────────────────────────────────────
    # FSEQ v2.2 (minor_version >= 2) sometimes writes compression_type=0 in
    # byte 19 even though the data is zstd-compressed.  Probe the actual data
    # at chan_data_offset for the zstd frame magic (0xFD2FB528 little-endian).
    _ZSTD_MAGIC = b'\x28\xB5\x2F\xFD'
    with open(filepath, 'rb') as _f:
        _f.seek(chan_data_offset)
        _probe = _f.read(4)
    effective_ctype = compression_type
    if _probe == _ZSTD_MAGIC and compression_type == 0:
        effective_ctype = 2   # override: treat as zstd
        logging.info(
            "FSEQ: header says uncompressed (byte 19 = 0) but zstd magic detected "
            "at chan_data_offset — treating as zstd (FSEQ v2.2 quirk)"
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
    # we don't parse here — discard to ensure direct channel offset mapping.
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
        return logical_ch   # Dense FSEQ — direct 1:1 mapping

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
                f"FSEQ preview: start_ch {start_ch} not in sparse ranges — "
                f"falling back to frame byte 0 (model-specific FSEQ?)"
            )
        else:
            raise ValueError(
                f"Model channel count {ch_count} exceeds FSEQ channel count {total_ch}"
            )

    if ctype == 0:
        # Uncompressed: seek directly to frame + channel byte offset
        offset = header['chan_data_offset'] + frame_idx * total_ch + frame_byte
        with open(filepath, 'rb') as f:
            f.seek(offset)
            return f.read(ch_count)

    elif ctype in (1, 2):
        # zlib (1) or zstd (2) block compression — same block table layout
        if ctype == 2 and not ZSTD_AVAILABLE:
            raise ValueError(
                "FSEQ uses zstd compression — run fpp_install.sh to install the "
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
                # Fallback with an explicit size cap — allow up to 64 frames per
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
    # Video/Play Media support disabled — only .fseq, images, and playlists accepted
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
    never recurses into it — the endpoint then returns null. os.walk has no
    such bug.

    Returns a list of {'name', 'category', 'path'} dicts. Bundled fonts under
    this plugin's fonts/<category>/ (e.g. fonts/christmas/) are tagged with
    that category name; everything else found in the OS font directories is
    tagged "System". Names are deduped — a bundled font also gets copied into
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
    rendering. .ttf/.pfb names are derived the same way FPP does — filename
    minus a fixed 4-char extension — so they match what FPP's native overlay
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

def send_sms_response(to_phone, message_type):
    """Send an SMS response to the user based on message type.

    Twilio: sends via the Twilio API. Google Voice: sends by replying to the
    forwarding email (Google Voice converts an email reply into an outbound SMS),
    using the reply context captured by the poller for the current message."""
    if not config.get(f'sms_response_{message_type}', False):
        return False

    # Get the appropriate response message
    response_key = f"response_{message_type}"
    response_message = config.get(response_key, "")

    if not response_message:
        logging.warning(f"No response message configured for type: {message_type}")
        return False

    # Google Voice: reply-to-email path
    if config.get('message_source') == 'google_voice':
        return send_gv_reply(response_message, message_type)

    # Twilio: REST API path
    if not twilio_client:
        logging.warning("Cannot send SMS response: Twilio client not initialized")
        return False

    try:
        twilio_client.messages.create(
            body=response_message,
            from_=config['twilio_phone_number'],
            to=to_phone
        )
        logging.info(f"📤 Sent SMS response to {to_phone[-4:]}: {message_type}")
        return True
    except Exception as e:
        logging.error(f"Error sending SMS response: {e}")
        return False


def _sanitize_header(value):
    """Strip CR/LF (and stray control chars) from values that come from an inbound
    email before they go into outbound reply headers, so a crafted message can't
    inject extra headers (LOW-2 — email header injection)."""
    if value is None:
        return ''
    return re.sub(r'[\r\n\x00]+', ' ', str(value)).strip()

def send_gv_reply(text, message_type=""):
    """Send an outbound SMS via Google Voice by replying to the forwarding email.

    Replying to the notification email from the same Gmail account causes Google
    Voice to deliver the reply body as an SMS to the original sender. Uses the
    reply context (target address + threading headers) captured by the poller
    for the message currently being processed."""
    ctx = _gv_reply_ctx
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
    
    max_len = config.get('max_message_length', 30)
    return message[:max_len] if message else "Guest"

def is_valid_name(text):
    """Check if text is a valid name"""
    text = ' '.join(text.split())
    words = text.split()
    word_count = len(words)
    
    if config.get('one_word_only', False):
        if word_count != 1:
            return False, "Please send only a first name (one word)"
    elif config.get('two_words_max', True):
        if word_count > 2:
            return False, "Please send only a name (1-2 words, no sentences)"
    
    if len(text) > 50:
        return False, "Message too long - please send only a name"
    
    return True, ""

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
                # Single combined pattern — one regex pass instead of N passes
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
    """Redact a phone number to its last 4 digits for display/API responses —
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
                    logging.error(f"Deleted old log: {filename}")
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
            logging.error(f"Queue restore: {restored} item(s) loaded from disk")
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
                   if log.get('phone_full') == phone and _parse_log_date(log) == today)
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

def log_message(phone, message, name, status):
    """Log received message to today's daily log file."""
    try:
        log_path = get_day_log_path()
        try:
            with open(log_path, 'r') as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logs = []
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "phone": phone,
            "phone_full": phone,
            "message": message,
            "extracted_name": name,
            "status": status
        })
        with open(log_path, 'w') as f:
            json.dump(logs, f, indent=2)
        logging.info(f"✅ Message logged: {phone[-4:]} | {name} | {status}")
    except Exception as e:
        logging.error(f"Error logging message: {e}")

def update_message_status(phone, name, new_status):
    """Update the status of a message — searches today's file, then yesterday's if not found."""
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
                logging.info(f"Updated status: {phone[-4:]} | {name} | {new_status}")
            except Exception as e:
                logging.error(f"Error writing status update: {e}")
        return found

    try:
        today = datetime.now().date()
        if not _update_in_file(get_day_log_path(today)):
            _update_in_file(get_day_log_path(today - timedelta(days=1)))
    except Exception as e:
        logging.error(f"Error updating message status: {e}")

def add_to_queue(name, phone, message):
    """Add a message to the display queue"""
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

def send_to_fpp(name):
    """Send name to FPP - Start name sequence and display text overlay"""
    try:
        fpp_host = FPP_HOST
        name_playlist = config.get('name_display_playlist', '')
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')
        
        # Build per-line rendered items from message_lines config
        message_lines = config.get('message_lines', ['Merry Christmas', '{name}!', '', ''])
        line_boxes_cfg = config.get('line_boxes', [])
        line_colors_cfg = config.get('line_colors', [])
        line_movements_cfg = config.get('line_movements', [])
        line_speeds_cfg = config.get('line_speeds', [])
        line_fonts_cfg = config.get('line_fonts', [])
        line_orientations_cfg = config.get('line_orientations', [])

        global_text_color = config.get('text_color', '#FF0000')
        if not global_text_color.startswith('#'):
            global_text_color = '#' + global_text_color
        global_scroll_speed = config.get('scroll_speed', 5)
        global_font = config.get('text_font', 'FreeSans')
        default_box = {'x': -1, 'y': -1, 'w': 300, 'h': 60}

        # Collect non-empty rendered lines + their saved box/colors/movement/speed/font.
        # A line with no override for a given setting falls back to the matching global
        # default. Font size is not stored — it's auto-fit to the line's box at render time.
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

        # True if at least one line scrolls — decides whether the fast one-shot static
        # render is enough, or the animated per-line renderer is needed.
        any_moving = any(item[6] != 'Center' for item in all_items)

        # FPP API fallback: join lines with newline
        display_message = '\n'.join(item[0] for item in rendered_lines) if rendered_lines else name

        logging.info(f"🎄 ========== STARTING DISPLAY FOR: {name} ==========")
        logging.info(f"📺 FPP Host: {fpp_host}")
        logging.info(f"🎬 Name Display Playlist: {name_playlist}")
        logging.info(f"📝 Overlay Model: {overlay_model}")
        logging.info(f"📝 Message Lines: {message_lines}")
        logging.info(f"📝 Display Message: {display_message}")
        
        # Step 1: Start the name display playlist/sequence/video/image (background)
        if name_playlist:
            try:
                logging.info(f"⏸️  STEP 1: Stopping any running playlist...")
                requests.get(f"{fpp_host}/api/playlists/stop", timeout=3)
                # Note: background FSEQ Effect is NOT stopped here — the names sequence
                # (foreground) will automatically suppress it and it auto-resumes after.
                time.sleep(0.1)

                logging.info(f"▶️  STEP 2: Starting name display content: {name_playlist}")

                import urllib.parse
                if name_playlist.startswith('seq:'):
                    # FSEQ Effect (loop=true, background=true): plays as background so
                    # overlay model renders on top with correct text colors.
                    seq_name = name_playlist[4:].removesuffix('.fseq')
                    effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
                    start_response = requests.get(effect_url, timeout=3)
                    logging.info(f"   FSEQ Effect Start (names): {start_response.status_code} - {start_response.text}")

                elif name_playlist.startswith('img:'):
                    # Image background — will be composited with text in Step 2 below
                    logging.info(f"   Image background: will render in overlay step")

                else:
                    command = "Start Playlist"
                    encoded_playlist = urllib.parse.quote(name_playlist)
                    command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_playlist}/true/false"
                    start_response = requests.get(command_url, timeout=3)
                    logging.info(f"   Start playlist response: {start_response.status_code}")

                time.sleep(0.3)

            except Exception as e:
                logging.error(f"💥 ERROR starting name playlist: {e}")
        else:
            # No names content configured — seq:/playlist waiting content composites
            # correctly underneath the overlay and is left running.
            pass

        # Step 2: Display text ON TOP of the sequence
        if overlay_model:
            try:
                logging.info(f"📝 STEP 3: Displaying text on model: {overlay_model}")

                text_position = config.get('text_position', 'Center')  # used only by the non-PIL fallback below
                text_color = global_text_color
                text_font = config.get('text_font', 'FreeSans')
                # The non-PIL fallback below has no concept of per-line boxes — approximate
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

                # Order matters to avoid flash of previous name:
                # 1. Disable overlay (State 0) — hides it
                # 2. Write new frame (PIL shm or FPP text API)
                # 3. Enable overlay (State 3 Transparent RGB) — activates cleanly
                requests.put(state_url, json={"State": 0}, timeout=3)

                mw = config.get('overlay_model_width', 0)
                mh = config.get('overlay_model_height', 0)
                logging.info(f"📐 Overlay: model={overlay_model} overlay_size={mw}x{mh} "
                             f"lines={len(all_items)} moving={any_moving} PIL={PIL_AVAILABLE}")

                shm_rendered = False
                scroll_started = False

                # For img: names content, composite text onto the image (State 2 = Opaque).
                # When no Names content is configured, fall back to an img: Default Waiting
                # content so the name displays over it instead of wiping it with plain text.
                # Image backgrounds only work with the static (no per-line movement) path —
                # scrolling text has never supported compositing onto an image background.
                img_source = name_playlist if name_playlist else config.get('default_playlist', '')
                img_bg_path = None
                if img_source.startswith('img:'):
                    img_bg_path = os.path.join(FPP_IMAGES_PATH, img_source[4:])
                    if not os.path.exists(img_bg_path):
                        logging.warning(f"⚠️ Image not found: {img_bg_path}")
                        img_bg_path = None

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
                        duration = config.get('display_duration', 30)
                        if img_bg_path:
                            logging.warning("⚠️ Image background does not support per-line movement — "
                                            "animating over a black background instead.")
                        scroll_started = animate_lines_via_shm(
                            all_items, overlay_model, mw, mh, duration
                        )
                        if scroll_started:
                            time.sleep(0.05)  # let first frame land before enabling overlay
                elif mw == 0 or mh == 0:
                    logging.warning(f"⚠️ PIL skipped: overlay dimensions not saved ({mw}x{mh}). "
                                    f"Re-select the model in config to save dimensions.")
                elif not PIL_AVAILABLE:
                    logging.warning("⚠️ Pillow not installed — using FPP text API (no X/Y positioning). "
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
                    response = requests.put(text_url, json=text_payload, timeout=10)
                    logging.info(f"📡 FPP text API fallback: {response.status_code}")
                else:
                    logging.info(f"✅ PIL {'scroll' if scroll_started else 'static'} render active")

                # State 2 (Opaque) for image background so it covers the display fully.
                # State 3 (Transparent RGB) for normal/FSEQ background (black = transparent).
                overlay_state = 2 if (img_bg_path and shm_rendered) else 3
                state_resp = requests.put(state_url, json={"State": overlay_state}, timeout=3)
                logging.info(f"   Overlay state={overlay_state}: {state_resp.status_code}")

            except Exception as e:
                logging.error(f"💥 ERROR sending text command: {e}")
                import traceback
                logging.error(traceback.format_exc())
        
        logging.info(f"✅ ========== DISPLAY COMMANDS COMPLETED ==========")
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


def start_default_playlist():
    """Start the configured default waiting playlist/sequence.
    For sequences (seq:), uses FSEQ Effect (loop=true, background=true) so it loops
    seamlessly as a background effect."""
    import urllib.parse
    fpp_host = FPP_HOST
    default_playlist = config.get('default_playlist', '')

    if not default_playlist:
        logging.info("ℹ️  No default playlist configured — skipping auto-start")
        return False

    try:
        if default_playlist.startswith('seq:'):
            # FSEQ Effect Start uses the display name WITHOUT .fseq extension
            seq_name = default_playlist[4:]
            seq_name = seq_name.removesuffix('.fseq')

            # loop=true, background=true: loops natively, auto-suppressed by foreground
            # sequences, auto-resumes when foreground stops
            effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
            logging.info(f"▶️  Starting FSEQ Effect Start (loop+background): {seq_name}")
            logging.info(f"   URL: {effect_url}")
            response = requests.get(effect_url, timeout=3)
            logging.info(f"   Response: {response.status_code} - {response.text}")

            if response.status_code == 200:
                logging.info(f"✅ FSEQ Effect Start — looping in background")
                return True

            logging.error(f"❌ FSEQ Effect Start failed: {response.status_code} - {response.text}")
            return False

        elif default_playlist.startswith('img:'):
            # Static image — render to overlay model shared memory
            img_name = default_playlist[4:]
            img_path = os.path.join(FPP_IMAGES_PATH, img_name)
            overlay_model = config.get('overlay_model_name', '')
            mw = config.get('overlay_model_width', 0)
            mh = config.get('overlay_model_height', 0)
            if PIL_AVAILABLE and overlay_model and mw > 0 and mh > 0 and os.path.exists(img_path):
                ok = render_image_to_shm(img_path, overlay_model, mw, mh)
                if ok:
                    encoded = urllib.parse.quote(overlay_model)
                    state_url = f"{fpp_host}/api/overlays/model/{encoded}/state"
                    requests.put(state_url, json={"State": 2}, timeout=3)  # Opaque
                    logging.info(f"✅ Image background set: {img_name}")
                    return True
            logging.warning(f"⚠️  Image background failed: PIL={PIL_AVAILABLE} model={overlay_model} "
                            f"dims={mw}x{mh} exists={os.path.exists(img_path) if img_path else False}")
            return False

        else:
            command = "Start Playlist"
            command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{urllib.parse.quote(default_playlist)}/true/true"
            logging.info(f"▶️  Starting playlist: {default_playlist}")
            logging.info(f"   URL: {command_url}")
            response = requests.get(command_url, timeout=3)
            logging.info(f"   Response: {response.status_code} - {response.text}")

            if response.status_code == 200:
                logging.info(f"✅ Playlist started")
                return True
            else:
                logging.error(f"❌ Failed to start playlist: {response.status_code}")
                return False
    except Exception as e:
        logging.error(f"Error starting default playlist: {e}")
        return False


def return_to_default_playlist():
    """Clear text overlay and stop the names sequence/playlist.
    If the default is a seq: (FSEQ Effect background), the background auto-resumes.
    If the default is a playlist, restart it explicitly."""
    try:
        fpp_host = FPP_HOST
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')

        if overlay_model:
            try:
                logging.info(f"🧹 Clearing text from model: {overlay_model}")
                import urllib.parse
                encoded_model = urllib.parse.quote(overlay_model)
                # Disable the overlay model (State 0) to stop rendering text
                state_url = f"{fpp_host}/api/overlays/model/{encoded_model}/state"
                response = requests.put(state_url, json={"State": 0}, timeout=3)
                logging.info(f"   Disable overlay (State 0): {response.status_code} - {response.text}")
                if response.status_code == 200:
                    logging.info(f"✅ Text cleared")
                else:
                    logging.warning(f"⚠️  Could not clear text: {response.status_code}")
            except Exception as e:
                logging.warning(f"Could not clear text: {e}")

        with queue_lock:
            queue_length = len(message_queue)

        if queue_length > 0:
            logging.info(f"📋 Queue has {queue_length} more names — skipping return-to-default")
            return

        import urllib.parse
        name_playlist  = config.get('name_display_playlist', '')
        default_content = config.get('default_playlist', '')

        if not name_playlist:
            # No names content. img: content was paused/replaced for overlay display —
            # restart it now. seq:/playlist content was never stopped.
            _default = config.get('default_playlist', '')
            if _default.startswith('img:'):
                start_default_playlist()
                logging.info("ℹ️  No names playlist — restarted default img content after overlay")
            else:
                logging.info("ℹ️  No names playlist — waiting content unchanged, overlay cleared")
            return

        if name_playlist.startswith('seq:'):
            # Stop the names FSEQ Effect — waiting FSEQ keeps running underneath
            seq_name = name_playlist[4:].removesuffix('.fseq')
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}", timeout=3)
            logging.info(f"⏹️  FSEQ Effect Stop (names): {r.status_code} - {r.text}")

        elif name_playlist.startswith('img:'):
            # Image mode: overlay was used, nothing extra to stop.
            # Re-apply default img content (it won't auto-resume)
            if default_content.startswith('img:'):
                start_default_playlist()

        else:
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('Stop Now')}", timeout=3)
            logging.info(f"⏹️  Stop Now ({r.status_code})")
            if default_content.startswith('img:'):
                start_default_playlist()

    except Exception as e:
        logging.error(f"Error in return_to_default_playlist: {e}")


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

            save_queue()  # item popped — remove from persistent queue before display starts

            currently_displaying = _next_item
            
            name = currently_displaying['name']
            phone = currently_displaying['phone']
            
            logging.info(f"🎬 NOW DISPLAYING: {name} (from {phone[-4:]})")
            
            try:
                logging.info(f"📝 Updating status to 'displaying'...")
                update_message_status(phone, name, "displaying")
                logging.info(f"✅ Status updated to 'displaying'")
            except Exception as e:
                logging.error(f"💥 Error updating status to displaying: {e}")
            
            try:
                logging.info(f"📺 Sending to FPP display...")
                send_to_fpp(name)
                logging.info(f"✅ Sent to FPP display")
            except Exception as e:
                logging.error(f"💥 Error sending to FPP: {e}")
            
            display_duration = int(config.get('display_duration', 30))
            logging.info(f"⏱️  Displaying for {display_duration} seconds...")

            try:
                name_playlist_chk = config.get('name_display_playlist', '')
                overlay_model_chk = config.get('overlay_model_name', '')
                if not name_playlist_chk and overlay_model_chk:
                    # No names content — FPP can reset the overlay state while the waiting
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
                logging.info(f"⏱️  Display duration completed")
            except Exception as e:
                logging.error(f"💥 Error during display: {e}")
            
            try:
                logging.info(f"🔄 Returning to default playlist...")
                return_to_default_playlist()
            except Exception as e:
                logging.error(f"💥 Error returning to default: {e}")
            
            logging.info(f"✅ FINISHED DISPLAYING: {name}")
            
            try:
                update_message_status(phone, name, "displayed")
                logging.info(f"✅ Status updated to 'displayed'")
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

def process_incoming_message(from_number, body):
    """Run one inbound message through the full pipeline: show-live check →
    blocked → name extraction → rate limit → duplicate → whitelist → profanity
    → queue, sending the appropriate auto-response along the way.

    Source-agnostic — used by both poll_twilio() and poll_google_voice(). Only
    needs the sender identity and message text; per-source dedup bookkeeping
    (SID / IMAP UID) stays in the caller. Behavior is identical to the logic
    that previously lived inline in poll_twilio()."""
    if not config.get('enabled', False):
        # Show not live — reply if enabled, then discard
        if not is_blocked(from_number):
            send_sms_response(from_number, "show_not_live")
            log_message(from_number, body, "", "show_not_live")
            logging.info(f"🔴 Show not live reply sent to {from_number[-4:]}")
        return

    # Exactly one branch fires — only one SMS response is ever sent per message
    if is_blocked(from_number):
        logging.info(f"🚫 Blocked: {from_number[-4:]}")
        log_message(from_number, body, "", "blocked")
        send_sms_response(from_number, "blocked")

    else:
        name = extract_name(body)
        logging.debug(f"👤 Extracted name: '{name}'")
        max_msgs = config.get('max_messages_per_phone', 0)
        msg_count = get_message_count(from_number) if max_msgs > 0 else 0
        is_valid, _ = is_valid_name(name)

        if max_msgs > 0 and msg_count >= max_msgs:
            logging.info(f"⛔ Rate limited: {from_number[-4:]}")
            log_message(from_number, body, "", "rate_limited")
            send_sms_response(from_number, "rate_limited")

        elif not config.get('allow_duplicate_names', False) and has_sent_name_today(from_number, name):
            logging.info(f"🔄 Duplicate name: {name}")
            log_message(from_number, body, name, "duplicate_name_today")
            send_sms_response(from_number, "duplicate")

        elif not is_valid and not config.get('use_whitelist', False):
            logging.info(f"❌ Invalid format: '{body[:20]}'")
            log_message(from_number, body, name, "invalid_format")
            send_sms_response(from_number, "invalid_format")

        elif not is_on_whitelist(name):
            logging.info(f"❌ Not on whitelist: {name}")
            log_message(from_number, body, name, "not_on_whitelist")
            send_sms_response(from_number, "not_whitelisted")

        elif config['profanity_filter'] and contains_profanity(body):
            logging.info(f"❌ Profanity rejected")
            log_message(from_number, body, name, "profanity")
            send_sms_response(from_number, "profanity")

        else:
            success = add_to_queue(name, from_number, body)
            if success:
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
            # Midnight cleanup — delete daily log files older than 7 days
            today = datetime.now().date()
            if today != _current_day:
                _current_day = today
                try:
                    cleanup_old_logs()
                    logging.error(f"🌙 Midnight: old daily logs cleaned up for {today}")
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

                logging.info(f"📱 SMS from {from_number[-4:]}: '{body[:30]}'")  # keep at INFO — new message is significant

                try:
                    process_incoming_message(from_number, body)
                    # Advance the dedup marker only on success — an exception
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
# must be strings that only ever appear in the footer — NOT the bare
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
    any bare <URL> logo/link lines, and join what's left."""
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
    return ' '.join(message_lines).strip()


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
    # 1) Subject line — contains only the sender's number
    subj = _gv_decode_header(str(msg.get('Subject', '')))
    num = _gv_normalize_phone(subj)
    if num:
        return num

    # 2) From address local-part — segments are <GVnum>.<sendernum>.<token>;
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

    Sender identity: the From display name is the saved contact name, or — for an
    unknown sender — the raw phone number. When it looks like a number we
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
        # falling back to the display name — so blocklist / rate-limit / display
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
            # Midnight cleanup — delete daily log files older than 7 days
            today = datetime.now().date()
            if today != _current_day:
                _current_day = today
                try:
                    cleanup_old_logs()
                    logging.error(f"🌙 Midnight: old daily logs cleaned up for {today}")
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
    so it exits on its next loop, and a fresh poller is started — this lets the
    provider be switched from the UI without a service restart."""
    global polling_thread, polling_source, polling_generation

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

    # Correct poller already running — nothing to do
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
        <title>Text My Lights — Configuration</title>
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

        <!-- Tab navigation -->
        <div class="tabs" style="display:flex; align-items:center; gap:2px;">
            <button class="tab-btn active" onclick="showTab('settings', this)">⚙️ Settings</button>
            <button class="tab-btn" onclick="showTab('display', this)">🖥️ Display</button>
            <button class="tab-btn" id="tabbtn-sms" onclick="showTab('sms', this)">📱 SMS Responses</button>
            <button class="tab-btn" onclick="showTab('testing', this)">🧪 Testing</button>
            <button class="view-btn" onclick="viewMessages()" style="margin:0 0 0 10px; padding:7px 14px; font-size:13px;">📋 View Message Queue</button>
            <span id="autosave_status" style="font-size:13px; margin-left:8px;"></span>
            <div style="margin-left:auto; display:flex; gap:4px; align-items:center;">
                <button id="btn_twilio_start" onclick="twilioStart()" style="background:#2e7d32; color:#fff; border:none; padding:6px 12px; border-radius:4px; font-size:12px; font-weight:bold; cursor:pointer;">▶ TwilioStart</button>
                <button id="btn_twilio_stop" onclick="twilioStop()" style="background:#c62828; color:#fff; border:none; padding:6px 12px; border-radius:4px; font-size:12px; font-weight:bold; cursor:pointer;">■ TwilioStop</button>
            </div>
        </div>

        <!-- Plugin Live Banner -->
        <div id="plugin_live_banner" style="display:none; background:#1b5e20; color:#fff; padding:10px 16px; border-radius:5px; margin-top:10px; font-size:14px; font-weight:bold; align-items:center; gap:10px;">
            <span style="display:inline-block; width:12px; height:12px; background:#69f0ae; border-radius:50%; box-shadow:0 0 6px #69f0ae;"></span>
            Plugin is Live
        </div>

        <!-- Plugin Not Live Banner -->
        <div id="plugin_not_live_banner" style="display:none; background:#b71c1c; color:#fff; padding:10px 16px; border-radius:5px; margin-top:10px; font-size:14px; font-weight:bold; align-items:center; gap:10px;">
            <span style="display:inline-block; width:12px; height:12px; background:#ff8a80; border-radius:50%; box-shadow:0 0 6px #ff8a80;"></span>
            <span>Plugin is Not Live &mdash; Start Twilio to display incoming messages.<br>
            <span style="font-weight:normal; font-size:12px;">Note: Viewers can still send messages, messaging rates will apply, but no messages will be displayed.</span></span>
        </div>

        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content active">
            <div class="columns">

                <!-- LEFT COLUMN: Twilio + FPP Display + Message Settings -->
                <div class="column">
                    <div class="section">
                        <h2>Message Source</h2>
                        <label>SMS Provider:</label>
                        <select id="message_source">
                            <option value="twilio" {{ 'selected' if config.get('message_source','twilio') != 'google_voice' else '' }}>Twilio</option>
                            <option value="google_voice" {{ 'selected' if config.get('message_source','twilio') == 'google_voice' else '' }}>Google Voice (Gmail)</option>
                        </select>
                        <p class="help-text"><a id="provider_help_link" href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=help.php#twilio" target="_top">View Twilio Configuration</a></p>

                        <!-- Twilio credentials — shown when Message Source = Twilio -->
                        <div id="twilio_creds">
                            <h3 style="margin:14px 0 6px;">Twilio Settings</h3>
                            <label>Twilio Account SID:</label>
                            <input type="text" id="account_sid" value="{{ config.twilio_account_sid }}" placeholder="Starts with AC...">

                            <label>Twilio Auth Token:</label>
                            <input type="password" id="auth_token" value="" autocomplete="new-password"
                                   placeholder="{{ '•••••••• saved — leave blank to keep' if config.twilio_auth_token else 'Twilio Auth Token' }}">

                            <label>Twilio Phone Number:</label>
                            <input type="text" id="phone_number" value="{{ config.twilio_phone_number }}" placeholder="+1234567890">

                            <button class="test-btn" onclick="testConnection()">🔌 Test Twilio Connection</button>
                            <div id="twilio_test_result" style="margin-top: 8px; font-size: 14px;"></div>
                        </div>

                        <!-- Google Voice credentials — shown when Message Source = Google Voice -->
                        <div id="gv_creds" style="display:none;">
                            <h3 style="margin:14px 0 6px;">Google Voice Settings</h3>
                            <label>Gmail Address:</label>
                            <input type="text" id="gv_email" value="{{ config.get('gv_email','') }}" placeholder="you@gmail.com">

                            <label>App Password:</label>
                            <input type="password" id="gv_app_password" value="" autocomplete="new-password"
                                   placeholder="{{ '•••••••• saved — leave blank to keep' if config.get('gv_app_password') else '16-character app password' }}">

                            <button class="test-btn" onclick="testGoogleVoice()">🔌 Test Google Voice Connection</button>
                            <div id="gv_test_result" style="margin-top: 8px; font-size: 14px;"></div>
                        </div>

                        <label>Poll Interval (seconds):</label>
                        <input type="number" id="poll_interval" value="{{ config.poll_interval }}" min="1" max="60">

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">FPP Display Settings</h2>

                        <div id="fpp_content_live_warning" style="display:none; background:#b71c1c; color:#fff; border-radius:5px; padding:8px 12px; margin-bottom:10px; font-size:13px;">
                            🔴 <strong>Plugin is Live</strong> — run TwilioStop to edit
                        </div>
                        <div id="fpp_content_inputs">
                            <label>Default "Waiting" Content: <span style="color:#f44336;font-size:12px;">* required</span></label>
                            <select id="default_playlist">
                                <option value="">-- Select content --</option>
                            </select>
                            <p class="help-text">📺 This content loops while waiting for text messages</p>

                            <label>Name Display Content:</label>
                            <select id="name_display_playlist">
                                <option value="">-- None (Same as "Waiting" Content) --</option>
                                {% set _np = config.get('name_display_playlist', '') %}
                                {% if _np %}<option value="{{ _np }}" selected>{{ _np }}</option>{% endif %}
                            </select>
                            <p class="help-text">🎬 This content plays when displaying a name</p>
                            <div id="name_display_none_warning" style="display:none; background:#3a2f00; border:1px solid #ffc107; color:#ffc107; border-radius:5px; padding:8px 12px; margin-top:6px; font-size:13px;">
                                ⚠️ Left as None — names will appear directly over the Waiting content.
                            </div>

                            <label>Overlay Model Name: <button type="button" onclick="refreshFPPLists(this)" style="font-size:11px;padding:2px 7px;margin-left:8px;cursor:pointer;">↻ Refresh Lists</button></label>
                            <select id="overlay_model_name">
                                <option value="">-- None --</option>
                            </select>
                            <p class="help-text">📝 The pixel overlay model for text (e.g., "Texting Matrix")</p>
                        </div>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">Message Settings</h2>

                        <label>Display Duration (seconds):</label>
                        <input type="number" id="display_duration" value="{{ config.display_duration }}" min="5" max="300" onchange="if(window.renderCanvasPreview)window.renderCanvasPreview();">
                        <p class="help-text">⏱️ Each message displays for this many seconds before moving to the next</p>
                        <p class="help-text">💡 Scrolling lines set to "Fit to time" use this as their scroll window.</p>

                        <label>Max Messages Per Phone (0 = unlimited):</label>
                        <input type="number" id="max_messages" value="{{ config.max_messages_per_phone }}" min="0" max="100">

                        <div id="max_length_section">
                            <label>Max Message Length:</label>
                            <input type="number" id="max_length" value="{{ config.max_message_length }}" min="10" max="200">
                        </div>
                        <div id="max_length_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:6px; font-size:13px;">
                            ⚠️ <strong>Max Message Length is disabled</strong> — whitelist is enabled. Names are validated against the approved list, not by length.
                        </div>

                    </div>
                </div>

            </div>

            <!-- Filters — full width -->
            <div class="section" style="margin-top:12px;">
                <h2>Filters</h2>
                <div style="display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap;">

                    <!-- Sub-col 1: Profanity + Whitelist -->
                    <div style="flex:1; min-width:220px;">
                        <div id="blacklist_section">
                            <label class="toggle-switch"><input type="checkbox" id="profanity_filter" {{ 'checked' if config.profanity_filter else '' }} onchange="checkFiltersState(); saveConfig();"><span class="toggle-slider"></span></label>
                            <label class="checkbox-label">Enable Profanity Filter</label><br>
                            <button class="view-btn" onclick="showBlacklistWarning()" style="margin-top:6px;">🚫 Manage Blacklist</button>
                        </div>
                        <div id="profanity_disabled_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Profanity filter is disabled</strong> — this is not recommended. Re-enable it to filter names against the Blacklist, or enable the Whitelist instead.
                        </div>
                        <div id="blacklist_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Blacklist inactive</strong> — whitelist is enabled. All names are validated against the whitelist.
                        </div>

                        <hr style="border:none; border-top:1px solid #444; margin:15px 0;">

                        <label class="toggle-switch"><input type="checkbox" id="use_whitelist" {{ 'checked' if config.get('use_whitelist', False) else '' }} onchange="updateFormatRules(); checkFiltersState(); checkWhitelistResponseState(); saveConfig();"><span class="toggle-slider"></span></label>
                        <label class="checkbox-label">Enable Name Whitelist — only allow approved names</label><br>
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
                                <label class="toggle-switch"><input type="checkbox" id="one_word_only" {{ 'checked' if config.get('one_word_only', False) and not config.get('use_whitelist', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('two_words_max').checked = false; checkFormatWarning(); saveConfig();"><span class="toggle-slider"></span></label>
                                <label class="checkbox-label">One Word Only (e.g., "John" ✓, "John Smith" ✗)</label><br>

                                <label class="toggle-switch"><input type="checkbox" id="two_words_max" {{ 'checked' if config.get('two_words_max', True) and not config.get('use_whitelist', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('one_word_only').checked = false; checkFormatWarning(); saveConfig();"><span class="toggle-slider"></span></label>
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
                        <label class="checkbox-label">Allow Duplicate Names — same phone number can submit the same name multiple times per day</label>
                    </div>

                </div>
            </div>
            <script>
                // Remember the last active format-rule choice so enabling the
                // whitelist (which clears the rules) and then disabling it restores
                // exactly what was set before — rather than forcing "Two Words Max".
                var _savedFormatState = {
                    one_word_only: {{ 'true' if config.get('one_word_only', False) else 'false' }},
                    two_words_max: {{ 'true' if config.get('two_words_max', True) else 'false' }}
                };
                var _prevWhitelistOn = null;
                function updateFormatRules() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var one = document.getElementById('one_word_only');
                    var two = document.getElementById('two_words_max');
                    var inputs = document.getElementById('format_rules_inputs');
                    var note = document.getElementById('format_rules_disabled_note');
                    inputs.style.opacity = whitelistOn ? '0.4' : '1';
                    inputs.style.pointerEvents = whitelistOn ? 'none' : '';
                    note.style.display = whitelistOn ? 'block' : 'none';
                    if (whitelistOn) {
                        // Snapshot the current choice only when coming from the
                        // non-whitelist state (when already whitelisted the boxes are
                        // cleared and no longer reflect a real choice).
                        if (_prevWhitelistOn === false) {
                            _savedFormatState.one_word_only = one.checked;
                            _savedFormatState.two_words_max = two.checked;
                        }
                        one.checked = false;
                        two.checked = false;
                    } else {
                        one.checked = _savedFormatState.one_word_only;
                        two.checked = _savedFormatState.two_words_max;
                    }
                    _prevWhitelistOn = whitelistOn;
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
                    if (allowDupes) {
                        row.classList.add('locked');
                        row.classList.remove('enabled');
                        if (cb) cb.checked = false;
                    } else {
                        row.classList.remove('locked');
                        toggleResp('duplicate');
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
                    // Only disable/grey the row — never change the checkbox's own state,
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
                // (unlimited) — no one is ever rate limited. Lock the row live.
                function checkRateLimitResponseState() {
                    var mmEl = document.getElementById('max_messages');
                    var unlimited = !mmEl || parseInt(mmEl.value || '0', 10) === 0;
                    var row = document.getElementById('row_rate_limited');
                    var cb = document.getElementById('sms_response_rate_limited');
                    var warn = document.getElementById('rate_limited_disabled_warning');
                    if (!row) return;  // SMS-responses tab not parsed yet (init runs later)
                    if (unlimited) {
                        row.classList.add('locked');
                        row.classList.remove('enabled');
                        if (cb) { cb.checked = false; cb.disabled = true; }
                    } else {
                        row.classList.remove('locked');
                        if (cb) cb.disabled = false;
                        toggleResp('rate_limited');
                    }
                    if (warn) warn.style.display = unlimited ? '' : 'none';
                }
                updateFormatRules();
                checkFiltersState();
                checkDuplicateState();
            </script>

        </div>

        <!-- Display Settings Tab -->
        <div id="tab-display" class="tab-content">
            <div class="columns">

                <!-- LEFT COLUMN: Message Lines editor -->
                <div class="column">
                    <div class="section">
                        <h2>Message Lines</h2>

                        <label>Message Lines: <span style="font-size:11px; color:#888; font-weight:normal;">Use {name} in any line. Empty lines are skipped.</span></label>
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
                        {% set ml = config.get('message_lines') or ['Merry Christmas', '{name}!', '', ''] %}
                        {% set lc = config.get('line_colors') or ['', '', '', ''] %}
                        {% set lm = config.get('line_movements') or ['Center', 'Center', 'Center', 'Center'] %}
                        {% set ls = config.get('line_speeds') or [50, 50, 50, 50] %}
                        {# Per-line speed value with a safe fallback. speed <= 0 encodes
                           fit-to-time: 0/-1 = 1 pass, -N = N passes. #}
                        {% set s0 = ls[0] if ls|length > 0 else 50 %}
                        {% set s1 = ls[1] if ls|length > 1 else 50 %}
                        {% set s2 = ls[2] if ls|length > 2 else 50 %}
                        {% set s3 = ls[3] if ls|length > 3 else 50 %}
                        {% set lf = config.get('line_fonts') or ['FreeSans', 'FreeSans', 'FreeSans', 'FreeSans'] %}
                        {% set lo = config.get('line_orientations') or ['horizontal', 'horizontal', 'horizontal', 'horizontal'] %}
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
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration — the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_1_speed_auto" {{ 'checked' if s0 <= 0 else '' }} onchange="onLineSpeedAutoChange(0)"> Fit to time</label>
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
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration — the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_2_speed_auto" {{ 'checked' if s1 <= 0 else '' }} onchange="onLineSpeedAutoChange(1)"> Fit to time</label>
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
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration — the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_3_speed_auto" {{ 'checked' if s2 <= 0 else '' }} onchange="onLineSpeedAutoChange(2)"> Fit to time</label>
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
                                            <label class="line-speed-auto" title="Time the scroll to the whole display duration — the text enters at the start and finishes right at the end, whatever its length. Set how many full passes to make in that window."><input type="checkbox" id="line_4_speed_auto" {{ 'checked' if s3 <= 0 else '' }} onchange="onLineSpeedAutoChange(3)"> Fit to time</label>
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
                            <label>Position Preview:</label>
                            <p id="canvas_hint" style="font-weight:bold; font-size:13px; color:#4fc3f7; margin:4px 0 8px;">🖱️ Click a line to select it, then drag inside its box to move it, or drag an edge/corner to resize. Text auto-sizes to fill the box — the box is the MAX size text can be.</p>
                            <p class="help-text" style="margin:-4px 0 8px;">↔️ For scrolling text (Left/Right/Top/Bottom movement), the box is also where the text is allowed to show — it enters and exits at the box's own edges, not the display's, and always starts fully off-page before scrolling in.</p>
                            <canvas id="matrix_canvas" style="width:100%; display:block; background:#000; border:2px solid #555; border-radius:4px; cursor:default;"></canvas>
                            <div style="display:flex; gap:8px; margin-top:6px; align-items:center;">
                                <button type="button" onclick="resetAllLines()" style="background:#555; padding:6px 12px; font-size:12px;">Reset All to Center</button>
                                <span id="pos_display" style="font-size:12px; color:#888;"></span>
                            </div>

                            <!-- Canvas background preview (FSEQ / video / image) -->
                            <div style="margin-top:10px; padding:10px; background:#616161; border:1px solid #777; border-radius:4px;">
                                <span style="font-size:13px; font-weight:bold; color:#eee;">Background Preview</span>
                                <span id="fseq_scrub_hint" style="font-weight:normal; font-size:11px; color:#bbb; margin-left:6px;">Use scroll bar to move preview.</span>
                                <div id="fseq_preview_controls" style="margin-top:8px;">
                                    <div style="margin-bottom:6px;">
                                        <span id="fseq_seq_label" style="font-size:12px; color:#ccc;">Sequence: —</span>
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
                            window._lineBoxesInit = {{ config.get('line_boxes', [{'x':-1,'y':-1,'w':300,'h':60},{'x':-1,'y':-1,'w':300,'h':60},{'x':-1,'y':-1,'w':300,'h':60},{'x':-1,'y':-1,'w':300,'h':60}]) | tojson }};
                            window._lineMovementsInit = {{ config.get('line_movements', ['Center', 'Center', 'Center', 'Center']) | tojson }};
                            window._lineSpeedsInit = {{ config.get('line_speeds', [50, 50, 50, 50]) | tojson }};
                            window._lineFontsInit = {{ config.get('line_fonts', ['FreeSans', 'FreeSans', 'FreeSans', 'FreeSans']) | tojson }};
                            window._lineOrientationsInit = {{ config.get('line_orientations', ['horizontal', 'horizontal', 'horizontal', 'horizontal']) | tojson }};
                            window._customColorsInit = {{ config.get('custom_colors', []) | tojson }};
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
                <!-- Twilio-specific delivery warnings — hidden when Google Voice is the source -->
                <div id="twilio_sms_warnings">
                    <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:10px 14px; margin:10px 0; font-size:13px;">
                        ⚠️ <strong>Message &amp; data rates may apply.</strong>
                    </div>
                    <div style="background:#f8d7da; border:2px solid #f5c6cb; color:#721c24; border-radius:6px; padding:12px 16px; margin:10px 0; font-size:14px; font-weight:bold;">
                        ⛔ SMS responses will NOT be delivered unless your Twilio number is registered:<br>
                        <span style="font-weight:normal; font-size:13px; display:block; margin-top:6px;">
                            • <strong>Local 10-digit number</strong> — requires a valid A2P 10DLC brand &amp; campaign approval<br>
                            • <strong>Toll-free number</strong> — requires a completed toll-free verification (recommended)
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
                    .resp-locked-note { font-size: 13px; color: #856404; background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 7px 10px; margin: 4px 0 6px; }
                </style>

                <script>
                function toggleResp(id) {
                    var row = document.getElementById('row_' + id);
                    row.classList.toggle('enabled', document.getElementById('sms_response_' + id).checked);
                }
                function initRespRows() {
                    ['show_not_live','blocked','profanity','duplicate','invalid_format','rate_limited','not_whitelisted','success'].forEach(function(id) {
                        toggleResp(id);
                    });
                }
                </script>

                <div id="row_show_not_live" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_show_not_live" {{ 'checked' if config.get('sms_response_show_not_live', False) else '' }} onchange="toggleResp('show_not_live')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_show_not_live" style="margin-left:10px;vertical-align:middle;">🔴 Show Not Live — Send Response</label>
                    </div>
                    <p class="help-text" style="margin:4px 0 6px;">Sent to anyone who texts while the show is not active (TwilioStop has been called).</p>
                    <textarea id="response_show_not_live" rows="2">{{ config.get('response_show_not_live', "Ho, Ho, Ho, It looks like our show isn't running now. Try again later.") }}</textarea>
                </div>

                <div id="row_blocked" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_blocked" {{ 'checked' if config.get('sms_response_blocked', False) else '' }} onchange="toggleResp('blocked')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_blocked" style="margin-left:10px;vertical-align:middle;">🚫 Blocked Number — Send Response</label>
                    </div>
                    <textarea id="response_blocked" rows="2">{{ config.get('response_blocked', 'You have been blocked from sending messages.') }}</textarea>
                </div>

                <div id="row_profanity" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_profanity" {{ 'checked' if config.get('sms_response_profanity', False) else '' }} onchange="toggleResp('profanity')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_profanity" style="margin-left:10px;vertical-align:middle;">🤬 Profanity Detected — Send Response</label>
                    </div>
                    <textarea id="response_profanity" rows="2">{{ config.get('response_profanity', 'Sorry, your message contains inappropriate content and cannot be displayed.') }}</textarea>
                </div>

                <div id="row_duplicate" class="resp-row{% if config.get('allow_duplicate_names', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_duplicate"
                               {{ 'checked' if config.get('sms_response_duplicate', False) and not config.get('allow_duplicate_names', False) else '' }}
                               onchange="toggleResp('duplicate')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_duplicate" style="margin-left:10px;vertical-align:middle;">🔄 Duplicate Name — Send Response</label>
                    </div>
                    <p id="duplicate_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('allow_duplicate_names', False) else 'display:none;' }}">⚠️ <strong>Duplicate response is disabled</strong> — Allow Duplicate Names is on, so this response will never send.</p>
                    <textarea id="response_duplicate" rows="2">{{ config.get('response_duplicate', "You've already sent this name today!") }}</textarea>
                </div>

                <div id="row_invalid_format" class="resp-row{% if config.get('use_whitelist', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_invalid_format"
                               {{ 'checked' if config.get('sms_response_invalid_format', False) else '' }}
                               {{ 'disabled' if config.get('use_whitelist', False) else '' }}
                               onchange="toggleResp('invalid_format')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_invalid_format" style="margin-left:10px;vertical-align:middle;">❌ Invalid Format — Send Response</label>
                    </div>
                    <p id="invalid_format_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('use_whitelist', False) else 'display:none;' }}">⚠️ Invalid Format responses are disabled when the whitelist is active — all names are validated against the whitelist instead of format rules.</p>
                    <textarea id="response_invalid_format" rows="2">{{ config.get('response_invalid_format', 'Please send only a name (1-2 words, no sentences).') }}</textarea>
                </div>

                <div id="row_rate_limited" class="resp-row{% if config.get('max_messages_per_phone', 0) == 0 %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_rate_limited"
                               {{ 'checked' if config.get('sms_response_rate_limited', False) and config.get('max_messages_per_phone', 0) != 0 else '' }}
                               {{ 'disabled' if config.get('max_messages_per_phone', 0) == 0 else '' }}
                               onchange="toggleResp('rate_limited')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_rate_limited" style="margin-left:10px;vertical-align:middle;">⛔ Rate Limited — Send Response</label>
                    </div>
                    <p id="rate_limited_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('max_messages_per_phone', 0) == 0 else 'display:none;' }}">⚠️ Rate-Limited responses are disabled when Max Messages Per Phone is 0 (unlimited) — no one is ever rate limited.</p>
                    <textarea id="response_rate_limited" rows="2">{{ config.get('response_rate_limited', "You've reached the maximum number of messages allowed. Please try again tomorrow!") }}</textarea>
                </div>

                <div id="row_not_whitelisted" class="resp-row{% if not config.get('use_whitelist', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_not_whitelisted"
                               {{ 'checked' if config.get('sms_response_not_whitelisted', False) else '' }}
                               {{ 'disabled' if not config.get('use_whitelist', False) else '' }}
                               onchange="toggleResp('not_whitelisted')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_not_whitelisted" style="margin-left:10px;vertical-align:middle;">📋 Not on Whitelist — Send Response</label>
                    </div>
                    <p id="not_whitelisted_disabled_warning" class="resp-locked-note" style="{{ '' if not config.get('use_whitelist', False) else 'display:none;' }}">⚠️ Not-on-Whitelist responses only apply when the Name Whitelist is enabled.</p>
                    <textarea id="response_not_whitelisted" rows="2">{{ config.get('response_not_whitelisted', 'Sorry, that name is not on our approved list.') }}</textarea>
                </div>

                <div id="row_success" class="resp-row">
                    <div class="resp-toggle">
                        <label class="toggle-switch"><input type="checkbox" id="sms_response_success" {{ 'checked' if config.get('sms_response_success', False) else '' }} onchange="toggleResp('success')"><span class="toggle-slider"></span></label>
                        <label for="sms_response_success" style="margin-left:10px;vertical-align:middle;">✅ Success — Send Response</label>
                    </div>
                    <textarea id="response_success" rows="2">{{ config.get('response_success', 'Thanks! Your name will appear on our display soon! 🎄') }}</textarea>
                </div>

            </div>
        </div>

        <!-- Testing Tab -->
        <div id="tab-testing" class="tab-content">

            <div id="test_message_section" class="section" style="border: 2px solid #FF9800; margin-top: 20px;">
                <h2>🧪 Message Testing</h2>

                <div id="show_not_live_banner" style="display:none; background:#ffecb3; border:1px solid #FF9800; border-radius:6px; padding:10px 14px; margin-bottom:14px; color:#7a4f00; font-size:14px;">
                    🔴 Show is not live — run <strong>TwilioStart</strong> from the FPP scheduler to activate the display before testing.
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
            function twilioStart() {
                var btn = document.getElementById('btn_twilio_start');
                btn.disabled = true; btn.textContent = '...';
                fetch('/api/activate', {method:'POST'})
                .then(r => r.json())
                .then(function(d) {
                    if (d.success === false) { alert('TwilioStart failed: ' + (d.error || 'Unknown error')); }
                    updateLiveStatus();
                })
                .catch(function() { alert('TwilioStart request failed.'); })
                .finally(function() { btn.disabled = false; btn.textContent = '▶ TwilioStart'; });
            }

            function twilioStop() {
                var btn = document.getElementById('btn_twilio_stop');
                btn.disabled = true; btn.textContent = '...';
                fetch('/api/deactivate', {method:'POST'})
                .then(r => r.json())
                .then(function() { updateLiveStatus(); })
                .catch(function() { alert('TwilioStop request failed.'); })
                .finally(function() { btn.disabled = false; btn.textContent = '■ TwilioStop'; });
            }

            function updateLiveStatus() {
                fetch('/api/queue/status').then(r => r.json()).then(data => {
                    const live = data.show_live === true;

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
                // Can't call getLineFont(i) here — it's local to initCanvasPreview()'s
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
                // attribute quote issues). Each box is the MAX area a line renders into —
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
                    var el = document.getElementById('display_duration');
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
                            // area — matches the runtime, where a box extending past the model
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
                // on the selected line's box can be grabbed — corners resize both width and
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
                        // last render — resizing needs a real edge to anchor against.
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
                        var pxX = Math.max(0, Math.min(canvas.width  - r2.w, c.cx - dragOffX));
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

                // Arrow key nudging — moves selected line 1px per press, 10px with Shift
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
            // Canvas background preview — supports FSEQ (.fseq), video (vid:), image (img:)
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
                    var dp = document.getElementById('name_display_playlist');
                    var defaultDp = document.getElementById('default_playlist');
                    // Fall back to waiting content when names content is "None"
                    var val = (dp && dp.value) ? dp.value : (defaultDp ? defaultDp.value : '');
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
                    return null;  // plain playlist — no canvas preview
                }

                window.toggleFseqPreview = function() {
                    var ct = getConfiguredContent();
                    var label = document.getElementById('fseq_seq_label');
                    if (!ct) {
                        label.textContent = '\u26a0 Select a .fseq, video, or image as Waiting or Names content for background preview.';
                        label.style.color = '#ff9800';
                        return;
                    }
                    var icon = ct.type === 'seq' ? '🎬 ' : ct.type === 'vid' ? '🎥 ' : '🖼️ ';
                    label.textContent = icon + ct.file;
                    label.style.color = '#ccc';
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
                                var displayDur = parseInt(document.getElementById('display_duration').value) || 30;
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
                        scrubber.max = parseInt(document.getElementById('display_duration').value) || 30;
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
                var el = document.getElementById('name_display_playlist');
                var warn = document.getElementById('name_display_none_warning');
                if (el && warn) warn.style.display = el.value ? 'none' : 'block';
            }

            // All DOM elements are above this script block — call init functions directly.
            try { initCanvasPreview(); } catch(e) { console.error('Canvas init error:', e); }
            // Load preview immediately using server-rendered dropdown value, then again after FPP data populates
            if (window.toggleFseqPreview) window.toggleFseqPreview();
            updateNameDisplayWarning();
            loadFonts();
            loadFPPData();
            initRespRows();
            checkWhitelistResponseState();
            checkRateLimitResponseState();
            checkDuplicateState();
            setupAutoSave();
            updateLiveStatus();
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
                    const nameSelect = document.getElementById('name_display_playlist');
                    const currentDefault = "{{ config.get('default_playlist', '') }}";
                    const currentName = "{{ config.get('name_display_playlist', '') }}";

                    defaultSelect.innerHTML = '<option value="">-- Select a playlist --</option>';
                    nameSelect.innerHTML = '<option value="">-- None (No Playlist Change) --</option>';

                    if (data.playlists && data.playlists.length > 0) {
                        const pg1 = document.createElement('optgroup');
                        pg1.label = '📋 Playlists';
                        const pg2 = document.createElement('optgroup');
                        pg2.label = '📋 Playlists';
                        data.playlists.forEach(playlist => {
                            pg1.appendChild(new Option(playlist, playlist, false, playlist === currentDefault));
                            pg2.appendChild(new Option(playlist, playlist, false, playlist === currentName));
                        });
                        defaultSelect.add(pg1);
                        nameSelect.add(pg2);
                    }

                    if (data.sequences && data.sequences.length > 0) {
                        const sg1 = document.createElement('optgroup');
                        sg1.label = '🎬 Sequences (.fseq)';
                        const sg2 = document.createElement('optgroup');
                        sg2.label = '🎬 Sequences (.fseq)';
                        data.sequences.forEach(seq => {
                            const val = 'seq:' + seq;
                            sg1.appendChild(new Option(seq, val, false, val === currentDefault));
                            sg2.appendChild(new Option(seq, val, false, val === currentName));
                        });
                        defaultSelect.add(sg1);
                        nameSelect.add(sg2);
                    }

                    if (data.videos && data.videos.length > 0) {
                        const vg1 = document.createElement('optgroup');
                        vg1.label = '🎥 Videos';
                        const vg2 = document.createElement('optgroup');
                        vg2.label = '🎥 Videos';
                        data.videos.forEach(vid => {
                            const val = 'vid:' + vid;
                            vg1.appendChild(new Option(vid, val, false, val === currentDefault));
                            vg2.appendChild(new Option(vid, val, false, val === currentName));
                        });
                        defaultSelect.add(vg1);
                        nameSelect.add(vg2);
                    }

                    if (data.images && data.images.length > 0) {
                        const ig1 = document.createElement('optgroup');
                        ig1.label = '🖼️ Images';
                        const ig2 = document.createElement('optgroup');
                        ig2.label = '🖼️ Images';
                        data.images.forEach(img => {
                            const val = 'img:' + img;
                            ig1.appendChild(new Option(img, val, false, val === currentDefault));
                            ig2.appendChild(new Option(img, val, false, val === currentName));
                        });
                        defaultSelect.add(ig1);
                        nameSelect.add(ig2);
                    }

                    window._fppSeqList = data.sequences || [];

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

                const data = {
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
                    use_whitelist: document.getElementById('use_whitelist').checked,
                    default_playlist: document.getElementById('default_playlist').value,
                    name_display_playlist: document.getElementById('name_display_playlist').value,
                    overlay_model_name: document.getElementById('overlay_model_name').value,
                    line_fonts: [0, 1, 2, 3].map(function(i) {
                        var el = document.getElementById('line_' + (i + 1) + '_font');
                        return el && el.value ? el.value : 'FreeSans';
                    }),
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
                    sms_response_not_whitelisted: document.getElementById('sms_response_not_whitelisted').checked,
                    sms_response_blocked: document.getElementById('sms_response_blocked').checked,
                    response_success: document.getElementById('response_success').value,
                    response_profanity: document.getElementById('response_profanity').value,
                    response_rate_limited: document.getElementById('response_rate_limited').value,
                    response_duplicate: document.getElementById('response_duplicate').value,
                    response_invalid_format: document.getElementById('response_invalid_format').value,
                    response_not_whitelisted: document.getElementById('response_not_whitelisted').value,
                    response_blocked: document.getElementById('response_blocked').value,
                    response_show_not_live: document.getElementById('response_show_not_live').value
                };

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
                // scheduler commands (api_activate / api_deactivate) — there is no
                // manual enable toggle, so config saves here never touch it.

                // Turn on the auto-responses that can actually fire under Google
                // Voice (Twilio keeps them off / hidden). Skips rows locked by another
                // setting: rate-limited (unlimited), duplicate (dupes allowed),
                // invalid-format (whitelist on) — those stay off.
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
                // Message source selector — swap the visible credential block, apply
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
                    saveConfig();
                });
                // Google Voice credential fields — save on blur (like Twilio creds)
                ['gv_email','gv_app_password'].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('blur', saveConfig);
                });
                // Keep the Rate-Limited response lock in sync when the limit changes
                var mmEl = document.getElementById('max_messages');
                if (mmEl) mmEl.addEventListener('input', checkRateLimitResponseState);
                // Reflect the saved source on initial load
                updateSourceUI();

                // Checkboxes, selects, color picker — save immediately on change
                ['profanity_filter','use_whitelist','allow_duplicate_names',
                 'default_playlist','name_display_playlist','overlay_model_name',
                 'one_word_only','two_words_max',
                 'sms_response_show_not_live',
                 'sms_response_success','sms_response_profanity','sms_response_rate_limited',
                 'sms_response_duplicate','sms_response_invalid_format',
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
                // The scrubber's range is capped to Display Duration (see loadBgPreview) —
                // reload it on change so that cap stays in sync with the field.
                var displayDurationEl = document.getElementById('display_duration');
                if (displayDurationEl) displayDurationEl.addEventListener('change', function() {
                    if (window.toggleFseqPreview) window.toggleFseqPreview();
                });

                // Text, number inputs — save when user clicks away
                ['account_sid','auth_token','phone_number',
                 'poll_interval','display_duration','max_messages','max_length',
                 'line_1','line_2','line_3','line_4',
                 'response_success','response_profanity','response_rate_limited',
                 'response_duplicate','response_invalid_format',
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
                if (tw) tw.style.display = isGV ? 'none' : '';
                if (gv) gv.style.display = isGV ? '' : 'none';

                // Point the help link at the selected provider's config section.
                // This page runs inside the plugin's own service (port 5000), so a
                // relative URL would resolve there instead of the FPP web server —
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
            // right where you pick a line's color — there's no separate global picker.
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

    return render_template_string(html, config=config)

@app.route('/api/config', methods=['POST'])
def update_config():
    global config, twilio_client, polling_thread, stop_polling
    try:
        new_config = request.json or {}
        # Secrets are never rendered back into the config page (the fields render
        # blank with a "saved" placeholder). A blank value from the client therefore
        # means "keep the stored secret" — not "clear it" — so we drop blank secret
        # keys before merging rather than wiping the saved credential.
        for _sk in ('twilio_auth_token', 'gv_app_password'):
            if not str(new_config.get(_sk, '')).strip():
                new_config.pop(_sk, None)
        config.update(new_config)

        # Normalize phone number to E.164 (strip spaces, dashes, parens — keep + and digits)
        if config.get('twilio_phone_number'):
            config['twilio_phone_number'] = re.sub(r'[^\d+]', '', config['twilio_phone_number'])

        # Drop responses whose trigger can't fire (limit 0 / duplicates allowed)
        _apply_source_policy()

        save_config()

        # Keep the Twilio client in sync whenever credentials are present, so the
        # Twilio path works exactly as before regardless of the selected source.
        if config['twilio_account_sid'] and config['twilio_auth_token']:
            twilio_client = Client(
                config['twilio_account_sid'],
                config['twilio_auth_token']
            )

        # Start the poller for the selected source if not already running (e.g.
        # credentials entered after TwilioStart, or updated mid-show).
        start_polling_if_needed()

        return jsonify({"success": True})
    except Exception as e:
        return _client_error("update_config", e)

@app.route('/api/fpp/fonts')
def fpp_fonts_endpoint():
    try:
        return jsonify(get_fpp_fonts())
    except Exception:
        return jsonify([])

@app.route('/api/fonts/file/<name>')
def serve_font_file(name):
    """Serve raw font bytes so the browser can @font-face them for the config
    page's canvas preview — otherwise the preview silently falls back to a
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
    """Diagnostic endpoint — returns JSON describing exactly how the FSEQ frame would be read.
    ?sequence=name&model=ModelName   (same params as /api/fseq/frame)
    Helps diagnose channel-offset and bpp issues without needing SSH."""
    seq        = request.args.get('sequence', '').strip()
    model_name = request.args.get('model', config.get('overlay_model_name', '')).strip()

    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400

    name     = seq.removeprefix('seq:').removesuffix('.fseq')
    name     = os.path.basename(name)   # no path traversal — keep filename only
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
            'channel_count':     hdr['channel_count'],
            'frame_count':       hdr['frame_count'],
            'fps':               round(hdr['fps'], 2),
            'step_time_ms':      hdr['step_time_ms'],
            'compression_type':  hdr['compression_type'],
            'compression_name':  comp_names.get(hdr['compression_type'], 'unknown'),
            'chan_data_offset':   hdr['chan_data_offset'],
            'num_comp_blocks':   len(hdr['comp_blocks']),
            'num_sparse_ranges': hdr['num_sparse_ranges'],
            'sparse_ranges':     hdr['sparse_ranges'],
            'sparse_sum':        sum(sr['count'] for sr in hdr['sparse_ranges']),
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
    name = os.path.basename(name)   # no path traversal — keep filename only
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
        return jsonify({'error': 'Pillow not installed — run fpp_install.sh'}), 503

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
        return jsonify({'error': 'Overlay model dimensions unknown — select a model first'}), 400

    name = seq.removeprefix('seq:').removesuffix('.fseq')
    name = os.path.basename(name)   # no path traversal — keep filename only
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
        # bytes_per_pixel: 3 for RGB, 4 for RGBW — derived from actual channel count
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
    ?type=img&file=filename.jpg  — resize image to model dims and return as PNG.
    ?type=vid&file=filename.mp4&time=0 — extract a frame at `time` seconds via ffmpeg,
        resize to model dims, return as PNG.  Falls back to a black frame if ffmpeg
        is unavailable or extraction fails."""
    if not PIL_AVAILABLE:
        return jsonify({'error': 'Pillow not installed — run fpp_install.sh'}), 503

    media_type = request.args.get('type', '').strip()   # 'img' or 'vid'
    filename   = request.args.get('file', '').strip()
    time_sec   = max(0.0, float(request.args.get('time', 0)))
    width  = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    height = int(request.args.get('height', config.get('overlay_model_height', 0)))

    if not filename or media_type not in ('img', 'vid'):
        return jsonify({'error': 'Requires ?type=img|vid&file=filename'}), 400
    if width <= 0 or height <= 0:
        return jsonify({'error': 'Overlay model dimensions unknown — select a model first'}), 400

    # Security: no path traversal — strip all directory components
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
                    # ffmpeg failed — return a dark grey placeholder
                    img = Image.new('RGB', (width, height), (32, 32, 32))
            except (FileNotFoundError, _sp.TimeoutExpired):
                # ffmpeg not installed on this system — return placeholder
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

    # IMAP — required for reading incoming texts
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

    # SMTP — only needed for outbound auto-responses; report but don't fail on it
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
            return jsonify({"success": False, "error": "Show is not live — run TwilioStart first"})

        if not test_name:
            return jsonify({"success": False, "error": "Name is required"})

        test_name = extract_name(test_name)
        is_valid, validation_msg = is_valid_name(test_name)

        if not is_valid:
            return jsonify({"success": False, "error": validation_msg, "reason": "invalid_format"})

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
        # ever holds the masked value — never the real number.
        if not phone and data.get('ts'):
            phone = _phone_from_log_ref(data.get('date'), data.get('ts'))
        if phone:
            success = block_phone(phone)
            # Never echo the full number back to the client.
            return jsonify({"success": success, "phone": mask_phone(phone)})
        return jsonify({"success": False, "error": "Could not resolve the number to block"})
    except Exception as e:
        return _client_error("api_block_phone", e)

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
                # Name was blocked by user — un-remove it to make it active again
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
            <span>⚠️ <strong>Whitelist is not enabled</strong> — All names will be shown regardless of this list.</span>
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
                        (showing < allNames.length ? ' — scroll down to load more' : '');
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
        <title>Profanity Filter — Blacklist</title>
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
            <span>⚠️ <strong>Blacklist is not enabled</strong> — Words on this list will still be shown.</span>
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
                        (showing < allWords.length ? ' — scroll down to load more' : '');
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
        <h1>🔧 Text My Lights — Status</h1>
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
            <div class="history-note">Past day snapshot — no live queue.</div>
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
                        Whitelist is not enabled — this name may appear again
                    </p>
                    <button style="background:#aaa; color:white; padding:10px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="closeBlockModal()">Cancel</button>
                </div>
            </div>
        </div>

        <script>
            var useWhitelist = {{ config.get('use_whitelist', False) | tojson }};
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
                // Class change doesn't trigger MutationObserver — report height explicitly
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
                        // Block by reference (timestamp + log date) — the full number
                        // stays server-side; we only carry the masked value for display.
                        btn = '<button class="block-btn" data-ts="' + esc(msg.timestamp) + '" data-date="' + esc(msg._log_date || '') +
                              '" data-masked="' + esc(msg.phone) + '" data-name="' + esc(msg.extracted_name) +
                              '" onclick="showBlockModal(this.dataset.masked,this.dataset.name,this.dataset.ts,this.dataset.date)">Block</button>';
                    }
                    return '<tr class="' + esc(msg.status) + '">' +
                        '<td>' + fmtTime(msg.timestamp) + '</td>' +
                        '<td>' + esc(msg.phone) + '</td>' +
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


@app.route('/api/activate', methods=['GET', 'POST'])
def api_activate():
    """FPP scheduler hook: enable the plugin, start SMS polling, and start the waiting playlist."""
    global polling_thread, stop_polling

    # Require a default waiting playlist — without one the show has no defined state
    if not config.get('default_playlist', '').strip():
        msg = "ERROR: No Default Waiting Playlist configured. Set one in the plugin settings before running TwilioStart."
        logging.error(msg)
        return jsonify({"success": False, "error": msg}), 400

    config['enabled'] = True
    stop_polling = False
    save_config()

    # Start the poller for the selected message source if not already running
    if not start_polling_if_needed():
        logging.warning("⚠️  Activate: message source not configured, polling not started")

    # Start the default waiting playlist
    result = start_default_playlist()

    logging.info(f"✅ TwilioStart activated — playlist {'started' if result else 'FAILED to start'}")
    return jsonify({"success": True, "playlist_started": result,
                    "message": "Twilio SMS plugin activated"})


@app.route('/api/deactivate', methods=['GET', 'POST'])
def api_deactivate():
    """FPP scheduler hook: disable plugin and stop the current playlist/sequence.
    Polling thread keeps running in standby to send show_not_live replies."""
    config['enabled'] = False
    save_config()

    # Stop the current sequence/playlist and any background FSEQ effect
    try:
        import urllib.parse

        default = config.get('default_playlist', '')
        if default.startswith('seq:'):
            # FSEQ Effect Stop also uses display name without .fseq
            seq_name = default[4:].removesuffix('.fseq')
            effect_stop_url = f"{FPP_HOST}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}"
            r = requests.get(effect_stop_url, timeout=3)
            logging.info(f"🛑 FSEQ Effect Stop: {r.status_code} - {r.text}")

        # Stop Now catches playlists, videos, and foreground sequences
        command_url = f"{FPP_HOST}/api/command/{urllib.parse.quote('Stop Now')}"
        r2 = requests.get(command_url, timeout=3)
        logging.info(f"🛑 Stop Now: {r2.status_code} - {r2.text}")

        # Image content renders via overlay model (State 2 Opaque) — must clear explicitly
        overlay_model = config.get('overlay_model_name', '')
        if default.startswith('img:') and overlay_model:
            encoded = urllib.parse.quote(overlay_model)
            state_url = f"{FPP_HOST}/api/overlays/model/{encoded}/state"
            requests.put(state_url, json={"State": 0}, timeout=3)
            logging.info(f"🛑 Overlay cleared (img content stopped)")
    except Exception as e:
        logging.warning(f"Could not stop FPP playback: {e}")

    logging.info("🛑 TwilioStop: disabled, polling stopped, playlist stopped")
    return jsonify({"success": True, "message": "Twilio SMS plugin deactivated"})


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
                logging.error(f"Migrated {_old} → {_new}")
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

    # Polling thread starts if the selected source is configured — runs in
    # standby (show_not_live replies) when disabled, and processes names normally
    # when enabled. Picks Twilio or Google Voice based on message_source.
    start_polling_if_needed()

    # Start the default waiting playlist on launch if the plugin is already enabled
    if config['enabled']:
        def _start_default():
            import time
            time.sleep(3)  # brief delay to let FPP settle before sending commands
            start_default_playlist()
        threading.Thread(target=_start_default, daemon=True).start()

    logging.info("Text My Lights plugin starting...")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
