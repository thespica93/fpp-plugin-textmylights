<?php
// The plugin UI is served SAME-ORIGIN through FPP via proxy.php (which forwards to the
// daemon on 127.0.0.1:5000). No access token is passed to the browser: the daemon trusts
// the loopback interface, and the proxy reaches it over loopback. This avoids the blocked
// cross-origin iframe, HTTPS mixed content, and the token leaking under FPP's /proxy view.
// The proxy base is RELATIVE so it resolves correctly under /proxy/<ip>/ (each box talks
// to its own daemon).
$proxyBase = "plugin.php?plugin=fpp-plugin-textmylights&page=proxy.php&nopage=1&p=";

// Read FPP's own UI theme server-side (this PHP runs inside FPP) so the plugin, which
// lives in a cross-origin iframe and can't see FPP's CSS, matches it exactly - 'light',
// 'dark', or '' for System/unknown (then the iframe follows the OS preference). Checked
// via FPP's $settings global, then the settings file, then a theme cookie.
function tml_fpp_theme() {
    $raw = '';
    // FPP stores the user's UI choice as "themeOverride" (Light/Dark); older/other keys fall back.
    if (isset($GLOBALS['settings']['themeOverride'])) $raw = $GLOBALS['settings']['themeOverride'];
    if ($raw === '' && isset($GLOBALS['settings']['Theme'])) $raw = $GLOBALS['settings']['Theme'];
    if ($raw === '' && is_readable('/home/fpp/media/settings')) {
        $lines = file('/home/fpp/media/settings', FILE_IGNORE_NEW_LINES);
        foreach ($lines as $line) { if (preg_match('/^\s*themeOverride\s*=\s*"?([^"]*)"?\s*$/i', $line, $m)) { $raw = $m[1]; break; } }
        if ($raw === '') { foreach ($lines as $line) { if (preg_match('/^\s*Theme\s*=\s*"?([^"]*)"?\s*$/i', $line, $m)) { $raw = $m[1]; break; } } }
    }
    if ($raw === '' && isset($_COOKIE['fppTheme'])) $raw = $_COOKIE['fppTheme'];
    $r = strtolower(trim($raw));
    if (strpos($r, 'dark')  !== false) return 'dark';
    if (strpos($r, 'light') !== false) return 'light';
    return '';
}
$fppTheme  = tml_fpp_theme();
// p is empty -> proxy forwards to the daemon root ('/'); theme is forwarded as a query param.
$pluginUrl = $proxyBase . ($fppTheme !== "" ? "&theme=" . urlencode($fppTheme) : "");
?>
<style>
    #sms-plugin-frame {
        width: 100%;
        border: none;
        display: block;
        overflow: hidden;
        min-height: 400px;
    }
    /* Modals live HERE, in the parent (not the iframe), so position:fixed centers
       on the real browser viewport — they stay put while scrolling. */
    .tml-modal {
        display: none; position: fixed; inset: 0; z-index: 100000;
        background: rgba(0,0,0,0.5);
        align-items: center; justify-content: center;
        font-family: Arial, sans-serif;
    }
    .tml-modal .tml-card {
        background: var(--bs-body-bg, #fff); color: var(--bs-body-color, #333); width: 92%; max-width: 460px;
        border-radius: 8px; padding: 22px; box-shadow: 0 8px 30px rgba(0,0,0,0.35);
        max-height: 88vh; overflow-y: auto; box-sizing: border-box;
    }
    .tml-modal h3 { margin: 0 0 6px; color: var(--bs-body-color, #333); font-size: 20px; }
    .tml-modal .tml-sub { font-size: 13px; color: var(--bs-secondary-color, #555); margin: 0 0 14px; }
    /* Dark theme (set via the .tml-dark class from tmlDetectTheme) to match FPP. */
    .tml-modal.tml-dark .tml-card { background: #262a31; color: #e6e6e6; box-shadow: 0 8px 30px rgba(0,0,0,0.6); }
    .tml-modal.tml-dark h3 { color: #e6e6e6; }
    .tml-modal.tml-dark .tml-sub { color: #a0a6b0; }
    .tml-modal.tml-dark .tml-opt .tml-desc { color: #9098a4; }
    .tml-modal.tml-dark .tml-status { color: #a0a6b0; }
    .tml-modal label.tml-opt {
        display: flex; gap: 10px; align-items: flex-start; margin: 0 0 14px; cursor: pointer;
    }
    /* FPP's global input styles squish checkboxes to slivers — force a real box. */
    .tml-modal label.tml-opt input[type="checkbox"] {
        -webkit-appearance: auto !important; appearance: auto !important;
        width: 18px !important; height: 18px !important; min-width: 18px !important;
        flex: 0 0 18px; margin: 2px 0 0 !important; padding: 0 !important;
        accent-color: #4CAF50; cursor: pointer; box-sizing: border-box;
    }
    .tml-modal .tml-opt strong { font-size: 14px; }
    .tml-modal .tml-opt .tml-desc { font-size: 12px; color: var(--bs-secondary-color, #666); }
    .tml-modal .tml-status { font-size: 13px; margin: 0 0 10px; min-height: 18px; }
    .tml-modal .tml-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 6px; align-items: center; }
    .tml-modal .tml-actions button {
        padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; color: #fff; font-size: 14px;
    }
    .tml-modal .tml-actions button:disabled { cursor: default; opacity: 0.6; }
    .tml-modal .tml-cancel { background: var(--bs-secondary, #9e9e9e); }
    .tml-modal .tml-go { background: #4CAF50; min-width: 110px; }
    #tml-import-modal .tml-go { background: #2196F3; }
    /* Colour accents for the plugin's jGrowl toasts (tmlNotify from the iframe / tmlToast here). */
    .jGrowl-notification.tml-jg-success { background: #2e7d32; color: #fff; }
    .jGrowl-notification.tml-jg-danger  { background: #c62828; color: #fff; }
    .jGrowl-notification.tml-jg-warning { background: #e65100; color: #fff; }
    .jGrowl-notification.tml-jg-success .jGrowl-close,
    .jGrowl-notification.tml-jg-danger  .jGrowl-close,
    .jGrowl-notification.tml-jg-warning .jGrowl-close { color: #fff; }
</style>
<iframe id="sms-plugin-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>" scrolling="no"></iframe>

<!-- Export selection modal (owned by the parent so it is a true fixed overlay). -->
<div id="tml-export-modal" class="tml-modal" onclick="if(event.target===this)tmlHideExport()">
    <div class="tml-card">
        <h3>Export Config</h3>
        <p class="tml-sub">This exports only the plugin's <strong>own</strong> data. FPP's sequences, images, videos and the overlay model are not included (distribute sequences with xLights FPP Connect; set the overlay model in FPP). <strong>Credentials are never included.</strong></p>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-settings" checked>
            <span><strong>Plugin settings</strong><br><span class="tml-desc">Display lines, message rules, response text, filters, poll interval, and the selected content &amp; overlay model <em>names</em>.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-lists" checked>
            <span><strong>Blocked numbers &amp; word lists</strong><br><span class="tml-desc">Blocked phone numbers and your whitelist / blacklist words.</span></span></label>
        <p class="tml-status" id="tml-exp-status"></p>
        <div class="tml-actions">
            <button type="button" id="tml-exp-cancel" class="tml-cancel" onclick="tmlHideExport()">Cancel</button>
            <button type="button" id="tml-exp-go" class="tml-go" onclick="tmlDoExport()">Export</button>
        </div>
    </div>
</div>

<!-- Import confirmation modal (parent-owned; stays open until the import finishes). -->
<div id="tml-import-modal" class="tml-modal" onclick="if(event.target===this)tmlHideImport()">
    <div class="tml-card">
        <h3>Import Config</h3>
        <p class="tml-sub">Import <strong id="tml-imp-name">the selected file</strong>? This <strong>overwrites</strong> the plugin settings and block / whitelist on <strong>THIS Pi</strong>. FPP's sequences, media and overlay model are not touched. Your saved credentials are kept.</p>
        <p class="tml-status" id="tml-imp-status"></p>
        <div class="tml-actions">
            <button type="button" id="tml-imp-cancel" class="tml-cancel" onclick="tmlHideImport()">Cancel</button>
            <button type="button" id="tml-imp-go" class="tml-go" onclick="tmlDoImport()">Import</button>
        </div>
    </div>
</div>

<!-- Generic confirm modal (parent-owned so it centers on the real viewport), driven by a
     tml_confirm message from any plugin page in the iframe. Replaces the browser's confirm(). -->
<div id="tml-generic-modal" class="tml-modal" onclick="if(event.target===this)tmlGenericResolve(false)">
    <div class="tml-card">
        <h3 id="tml-gen-title">Please confirm</h3>
        <p class="tml-sub" id="tml-gen-msg"></p>
        <div class="tml-actions">
            <button type="button" id="tml-gen-cancel" class="tml-cancel" onclick="tmlGenericResolve(false)">Cancel</button>
            <button type="button" id="tml-gen-ok" class="tml-go" onclick="tmlGenericResolve(true)">OK</button>
        </div>
    </div>
</div>

<script>
    document.getElementById('sms-plugin-frame').addEventListener('load', function() {
        window.scrollTo(0, 0);
        tmlSendTheme();   // tell the plugin which theme to use as soon as it loads
    });
    function tmlFrame() { return document.getElementById('sms-plugin-frame').contentWindow; }

    // Match the plugin (served cross-origin in the iframe, so it can't read FPP's CSS) to FPP's
    // own theme. We judge the theme from the luminance of FPP's page background - robust to
    // whatever class names/CSS vars the installed FPP theme uses - and forward it to the iframe,
    // which applies it. Falls back to the browser preference if the background can't be read.
    function tmlLum(str) {
        // Relative luminance of a CSS color, or null if transparent/unreadable.
        var m = (str || '').match(/rgba?\(([^)]+)\)/i);
        if (!m) return null;
        var p = m[1].split(',').map(function (s) { return parseFloat(s); });
        var a = p.length > 3 ? p[3] : 1;
        if (!(a > 0.1)) return null;
        return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2];
    }
    // FPP's theme as read server-side ('light'/'dark', or '' for System/unknown).
    var TML_FPP_THEME = <?php echo json_encode($fppTheme); ?>;
    function tmlColorScheme() {
        // FPP sets the CSS 'color-scheme' to its RESOLVED theme (light/dark) - including for
        // "System Default" - so this is the reliable signal. Returns 'dark'/'light' or null.
        try {
            var cs = (getComputedStyle(document.documentElement).colorScheme || '') + ' ' +
                     (getComputedStyle(document.body).colorScheme || '');
            var d = /\bdark\b/.test(cs), l = /\blight\b/.test(cs);
            if (d && !l) return 'dark';
            if (l && !d) return 'light';
        } catch (e) {}
        return null;
    }
    function tmlDetectTheme() {
        // 1) FPP's explicit light/dark setting (read server-side).
        if (TML_FPP_THEME === 'dark' || TML_FPP_THEME === 'light') return TML_FPP_THEME;
        // 2) CSS color-scheme = FPP's resolved theme (covers System Default). FPP's body
        //    background is dark even in light mode, so bg luminance is NOT reliable; use
        //    color-scheme, then the TEXT color (dark text => light theme), then the OS preference.
        var cs = tmlColorScheme();
        if (cs) return cs;
        var fg = tmlLum(getComputedStyle(document.body).color);
        if (fg !== null) return fg > 150 ? 'dark' : 'light';
        return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    var _tmlLastTheme = null;
    function tmlSendTheme() {
        var t = tmlDetectTheme();
        _tmlLastTheme = t;
        try { tmlFrame().postMessage({ type: 'tml_theme', theme: t }, '*'); } catch (e) {}
    }
    // Re-forward if the user toggles FPP's theme while the page is open.
    setInterval(function () {
        var t = tmlDetectTheme();
        if (t !== _tmlLastTheme) tmlSendTheme();
    }, 1500);

    // Same-origin proxy base for THIS parent page. Lets the Export download be triggered by
    // the real Export click here (user activation) through the FPP proxy (loopback-trusted,
    // no token). Relative so it works under /proxy/<ip>/.
    var TML_PROXY = <?php echo json_encode($proxyBase); ?>;

    // Create the hidden download iframe ONCE, up front. Creating it and setting its
    // src in the same click handler made the browser skip that first navigation (the
    // export only started on a second click); a ready, already-attached iframe
    // navigates reliably the first time.
    (function() {
        if (!document.getElementById('tml-export-dl')) {
            var ifr = document.createElement('iframe');
            ifr.id = 'tml-export-dl';
            ifr.style.display = 'none';
            document.body.appendChild(ifr);
        }
    })();

    /* ---------------- Export ---------------- */
    var _tmlExporting = false, _tmlExportPoll = null;
    function tmlResetExportBtn() {
        var go = document.getElementById('tml-exp-go');
        go.disabled = false; go.textContent = 'Export';
        document.getElementById('tml-exp-cancel').disabled = false;
        _tmlExporting = false;
    }
    function tmlShowExport() {
        tmlResetExportBtn();
        var m = document.getElementById('tml-export-modal');
        m.classList.toggle('tml-dark', tmlDetectTheme() === 'dark');
        m.style.display = 'flex';
    }
    function tmlHideExport() {
        if (_tmlExporting) return;
        clearInterval(_tmlExportPoll);
        document.getElementById('tml-export-modal').style.display = 'none';
    }
    function tmlDoExport() {
        if (_tmlExporting) return;
        function ck(id) { return document.getElementById(id).checked ? 1 : 0; }
        var sel = { s: ck('tml-exp-settings'), l: ck('tml-exp-lists') };
        if (!sel.s && !sel.l) { tmlToast('Select at least one thing to export.', 'warning'); return; }
        _tmlExporting = true;
        var go = document.getElementById('tml-exp-go');
        go.disabled = true; go.textContent = 'Exporting...';
        document.getElementById('tml-exp-cancel').disabled = true;
        var st = document.getElementById('tml-exp-status');
        st.style.color = ''; st.textContent = '';

        // Download straight from this parent page via a hidden iframe. The browser streams the
        // (possibly very large) zip to disk - never buffered in JS - so the tab can't freeze.
        // A unique dl_token lets us POLL the service for whether the download actually started
        // and finished, because a hidden-iframe download exposes no HTTP status to JS: a 403,
        // 500, or an error after streaming began would otherwise close the dialog silently.
        var token = 'dl' + Date.now() + '-' + Math.random().toString(36).slice(2);
        var url = TML_PROXY + encodeURIComponent('api/config/export')
                + '&settings=' + sel.s + '&lists=' + sel.l
                + '&dl_token=' + encodeURIComponent(token)
                + '&_ts=' + Date.now();
        var dl = document.getElementById('tml-export-dl');
        if (!dl) {   // defensive: init IIFE should have made it already
            dl = document.createElement('iframe');
            dl.id = 'tml-export-dl';
            dl.style.display = 'none';
            document.body.appendChild(dl);
        }
        dl.src = url;
        go.textContent = 'Downloading...';

        // Poll export status; show an error instead of silently closing if it never starts or
        // fails. 'done' = the zip was fully written; 'error' = it failed (incl. mid-stream);
        // no response within 20s = the request never reached the service (e.g. 403/blocked).
        var statusUrl = TML_PROXY + encodeURIComponent('api/config/export/status')
                      + '&dl_token=' + encodeURIComponent(token);
        var startedAt = Date.now();
        clearInterval(_tmlExportPoll);
        _tmlExportPoll = setInterval(function() {
            function fail(msg) {
                clearInterval(_tmlExportPoll);
                tmlResetExportBtn();
                st.style.color = 'var(--bs-danger-text-emphasis, #c62828)';
                st.textContent = msg;
            }
            fetch(statusUrl, { cache: 'no-store' })
                .then(function(r) { return r.json(); })
                .then(function(s) {
                    if (s.state === 'done') {
                        clearInterval(_tmlExportPoll);
                        _tmlExporting = false;
                        go.textContent = 'Downloaded';
                        st.style.color = 'var(--bs-success-text-emphasis, #2e7d32)';
                        st.textContent = 'Export complete.';
                        setTimeout(function() { tmlHideExport(); tmlResetExportBtn(); }, 1200);
                    } else if (s.state === 'error') {
                        fail('Export failed: ' + (s.error || 'unknown error'));
                    } else if (Date.now() - startedAt > 20000) {
                        fail('Export did not start (no response from the plugin service).');
                    }
                })
                .catch(function() {
                    if (Date.now() - startedAt > 20000) fail('Export did not start (service unreachable).');
                });
        }, 600);
    }

    /* ---------------- Import ---------------- */
    var _tmlImporting = false, _tmlImportTimer = null;
    function tmlResetImportBtn() {
        var go = document.getElementById('tml-imp-go');
        go.disabled = false; go.textContent = 'Import';
        document.getElementById('tml-imp-cancel').disabled = false;
        _tmlImporting = false;
    }
    function tmlShowImport(name) {
        tmlResetImportBtn();
        document.getElementById('tml-imp-name').textContent = name || 'the selected file';
        document.getElementById('tml-imp-status').textContent = '';
        document.getElementById('tml-imp-status').style.color = '#555';
        var m = document.getElementById('tml-import-modal');
        m.classList.toggle('tml-dark', tmlDetectTheme() === 'dark');
        m.style.display = 'flex';
    }
    function tmlHideImport() {
        if (_tmlImporting) return;                 // don't close mid-import
        document.getElementById('tml-import-modal').style.display = 'none';
        tmlFrame().postMessage({ type: 'tml_cancelImport' }, '*');
    }
    function tmlDoImport() {
        if (_tmlImporting) return;
        _tmlImporting = true;
        var go = document.getElementById('tml-imp-go');
        go.disabled = true; go.textContent = 'Importing...';
        document.getElementById('tml-imp-cancel').disabled = true;
        var st = document.getElementById('tml-imp-status');
        st.style.color = '#555'; st.textContent = 'Importing... please wait.';
        tmlFrame().postMessage({ type: 'tml_doImport' }, '*');
        _tmlImportTimer = setTimeout(function() {
            if (_tmlImporting) { tmlResetImportBtn(); tmlToast('Import timed out. Please try again.', 'danger'); }
        }, 180000);
    }

    /* ---------------- Toasts + generic confirm (parent-owned, for the iframe) ---------------- */
    // FPP-native toast. Used by this parent page; the iframe pages reach jGrowl directly via
    // window.parent.jQuery.jGrowl (see tmlNotify injected into those pages).
    function tmlToast(msg, type) {
        try {
            if (window.jQuery && jQuery.jGrowl) {
                jQuery.jGrowl(String(msg), { life: (type === 'danger' ? 7000 : 4000),
                    theme: 'tml-jg' + (type ? ' tml-jg-' + type : '') });
                return;
            }
        } catch (e) {}
        alert(msg);
    }
    var _tmlGenId = null;
    function tmlGenericResolve(ok) {
        var id = _tmlGenId; _tmlGenId = null;
        document.getElementById('tml-generic-modal').style.display = 'none';
        if (id) { try { tmlFrame().postMessage({ type: 'tml_confirm_result', id: id, ok: !!ok }, '*'); } catch (e) {} }
    }
    function tmlShowConfirm(id, message, opts) {
        opts = opts || {};
        _tmlGenId = id;
        document.getElementById('tml-gen-title').textContent = opts.title || 'Please confirm';
        document.getElementById('tml-gen-msg').textContent = message;
        var ok = document.getElementById('tml-gen-ok'), cancel = document.getElementById('tml-gen-cancel');
        ok.textContent = opts.okText || 'OK';
        cancel.textContent = opts.cancelText || 'Cancel';
        ok.style.background = opts.danger ? '#f44336' : '#4CAF50';
        var m = document.getElementById('tml-generic-modal');
        m.classList.toggle('tml-dark', tmlDetectTheme() === 'dark');
        m.style.display = 'flex';
    }

    window.addEventListener('message', function(e) {
        if (!e.data) return;
        if (e.data.type === 'iframeHeight') {
            document.getElementById('sms-plugin-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data.type === 'scrollTop') { window.scrollTo(0, 0); }

        if (e.data.type === 'tml_confirm') { tmlShowConfirm(e.data.id, e.data.message, e.data.opts); }

        if (e.data.type === 'tml_openExport') { tmlShowExport(); }
        if (e.data.type === 'tml_exportDone') {
            clearTimeout(_tmlExportTimer);
            var go = document.getElementById('tml-exp-go');
            if (e.data.success) {
                go.textContent = 'Downloaded';
                _tmlExporting = false;
                setTimeout(function() { tmlHideExport(); tmlResetExportBtn(); }, 900);
            } else {
                tmlResetExportBtn();
                tmlToast('Export failed: ' + (e.data.error || 'unknown error'), 'danger');
            }
        }

        if (e.data.type === 'tml_openImport') { tmlShowImport(e.data.name); }
        if (e.data.type === 'tml_importDone') {
            clearTimeout(_tmlImportTimer);
            var ist = document.getElementById('tml-imp-status');
            if (e.data.success) {
                _tmlImporting = false;                 // allow closing now
                document.getElementById('tml-imp-go').textContent = 'Imported';
                var msg = 'Imported successfully.';
                if (e.data.warnings) { msg += ' (' + e.data.warnings + ' warning' + (e.data.warnings > 1 ? 's' : '') + ')'; }
                if (e.data.note) { msg += ' ' + e.data.note; }
                ist.style.color = 'var(--bs-success-text-emphasis, #2e7d32)';
                ist.textContent = msg + ' Reloading...';
                setTimeout(function() {
                    document.getElementById('tml-import-modal').style.display = 'none';
                    tmlResetImportBtn();
                    tmlFrame().postMessage({ type: 'tml_reloadFrame' }, '*');
                }, 1800);
            } else {
                tmlResetImportBtn();
                ist.style.color = 'var(--bs-danger-text-emphasis, #c62828)';
                ist.textContent = 'Import failed: ' + (e.data.error || 'unknown error');
            }
        }
    });
</script>
