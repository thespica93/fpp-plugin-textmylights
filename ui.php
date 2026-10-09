<?php
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
// Read the plugin's network access token (minted by sms_plugin.py) and pass it
// to the iframe so the service authorizes this browser. This page is served by
// FPP's own web server, so only someone who can reach the FPP UI gets the token.
$tokenFile = "/home/fpp/media/plugin.fpp-textmylights/.access_token";
$token = is_readable($tokenFile) ? trim(file_get_contents($tokenFile)) : "";
$pluginUrl = "http://$host:5000/" . ($token !== "" ? "?token=" . urlencode($token) : "");
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
        background: #fff; color: #333; width: 92%; max-width: 460px;
        border-radius: 8px; padding: 22px; box-shadow: 0 8px 30px rgba(0,0,0,0.35);
        max-height: 88vh; overflow-y: auto; box-sizing: border-box;
    }
    .tml-modal h3 { margin: 0 0 6px; color: #333; font-size: 20px; }
    .tml-modal .tml-sub { font-size: 13px; color: #555; margin: 0 0 14px; }
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
    .tml-modal .tml-opt .tml-desc { font-size: 12px; color: #666; }
    .tml-modal .tml-status { font-size: 13px; margin: 0 0 10px; min-height: 18px; }
    .tml-modal .tml-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 6px; align-items: center; }
    .tml-modal .tml-actions button {
        padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; color: #fff; font-size: 14px;
    }
    .tml-modal .tml-actions button:disabled { cursor: default; opacity: 0.6; }
    .tml-modal .tml-cancel { background: #9e9e9e; }
    .tml-modal .tml-go { background: #4CAF50; min-width: 110px; }
    #tml-import-modal .tml-go { background: #2196F3; }
</style>
<iframe id="sms-plugin-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>" scrolling="no"></iframe>

<!-- Export selection modal (owned by the parent so it is a true fixed overlay). -->
<div id="tml-export-modal" class="tml-modal" onclick="if(event.target===this)tmlHideExport()">
    <div class="tml-card">
        <h3>Export Config</h3>
        <p class="tml-sub">Choose what to include. Only the content <strong>this plugin is set to use</strong> is ever exported - never all of FPP's files. <strong>Credentials are never included.</strong></p>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-settings" checked>
            <span><strong>Plugin settings</strong><br><span class="tml-desc">Display lines, message rules, response text, filters, poll interval, selected content &amp; overlay model.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-lists" checked>
            <span><strong>Blocked numbers &amp; word lists</strong><br><span class="tml-desc">Blocked phone numbers and your whitelist / blacklist words.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-content">
            <span><strong>Content files</strong> (off by default)<br><span class="tml-desc">Copies the actual sequence / image / video files into the bundle - can be large. Leave OFF: xLights FPP Connect already distributes sequences to your Pis, so the export just records which content to use by name. Turn ON only for a fully self-contained copy.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-overlay" checked>
            <span><strong>Overlay model (matrix)</strong><br><span class="tml-desc">The FPP Pixel Overlay Model the names are drawn onto.</span></span></label>
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
        <p class="tml-sub">Import <strong id="tml-imp-name">the selected file</strong>? This <strong>overwrites</strong> the plugin settings, block / whitelist, the content this plugin references, and the overlay model on <strong>THIS Pi</strong>. Your saved credentials are kept.</p>
        <p class="tml-status" id="tml-imp-status"></p>
        <div class="tml-actions">
            <button type="button" id="tml-imp-cancel" class="tml-cancel" onclick="tmlHideImport()">Cancel</button>
            <button type="button" id="tml-imp-go" class="tml-go" onclick="tmlDoImport()">Import</button>
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
    function tmlDetectTheme() {
        // Judge FPP's theme without depending on its internal class names:
        //  1) a non-transparent page background (body, then <html>) - dark bg => dark theme;
        //  2) else the TEXT color, which the theme always sets even when backgrounds are
        //     transparent - light text => dark theme (inverted);
        //  3) else the OS/browser preference (what FPP's "system default" follows).
        try {
            var bg = tmlLum(getComputedStyle(document.body).backgroundColor);
            if (bg === null) bg = tmlLum(getComputedStyle(document.documentElement).backgroundColor);
            if (bg !== null) return bg < 128 ? 'dark' : 'light';
            var fg = tmlLum(getComputedStyle(document.body).color);
            if (fg !== null) return fg > 150 ? 'dark' : 'light';
        } catch (e) {}
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

    // The plugin service base + its access token (same token the iframe loads with).
    // Lets THIS parent page talk to :5000 directly - needed so the export download
    // is triggered by the real Export click here (user activation) instead of a
    // postMessage inside the cross-origin iframe, which browsers block.
    var TML_SVC   = <?php echo json_encode("http://$host:5000/"); ?>;
    var TML_TOKEN = <?php echo json_encode($token); ?>;

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
    var _tmlExporting = false, _tmlExportTimer = null;
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
        document.getElementById('tml-export-modal').style.display = 'none';
    }
    function tmlDoExport() {
        if (_tmlExporting) return;
        function ck(id) { return document.getElementById(id).checked ? 1 : 0; }
        var sel = { s: ck('tml-exp-settings'), l: ck('tml-exp-lists'),
                    c: ck('tml-exp-content'), o: ck('tml-exp-overlay') };
        if (!sel.s && !sel.l && !sel.c && !sel.o) { alert('Select at least one thing to export.'); return; }
        _tmlExporting = true;
        var go = document.getElementById('tml-exp-go');
        go.disabled = true; go.textContent = 'Exporting...';
        document.getElementById('tml-exp-cancel').disabled = true;

        // Download straight from this parent page via a hidden iframe. The browser
        // streams the (possibly very large) sequence zip to disk - it never buffers
        // the whole file in JS memory, so the tab can't freeze. Triggering it here,
        // from the real Export click, keeps the user activation browsers require and
        // sidesteps the cross-origin-iframe download block. Auth via ?token; the
        // server's Content-Disposition names the file.
        // Cache-buster (&_ts) guarantees the iframe sees a new URL and navigates
        // every time, even for two identical back-to-back exports.
        var url = TML_SVC + 'api/config/export?settings=' + sel.s + '&lists=' + sel.l
                + '&content=' + sel.c + '&overlay=' + sel.o
                + (TML_TOKEN ? '&token=' + encodeURIComponent(TML_TOKEN) : '')
                + '&_ts=' + Date.now();
        var dl = document.getElementById('tml-export-dl');
        if (!dl) {   // defensive: init IIFE should have made it already
            dl = document.createElement('iframe');
            dl.id = 'tml-export-dl';
            dl.style.display = 'none';
            document.body.appendChild(dl);
        }
        dl.src = url;

        // Keep the modal up through the hand-off so the user sees it start, then
        // close. The browser's own download manager shows the rest of the transfer.
        go.textContent = 'Downloading...';
        _tmlExportTimer = setTimeout(function() {
            document.getElementById('tml-export-modal').style.display = 'none';
            tmlResetExportBtn();
        }, 1800);
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
            if (_tmlImporting) { tmlResetImportBtn(); alert('Import timed out. Please try again.'); }
        }, 180000);
    }

    window.addEventListener('message', function(e) {
        if (!e.data) return;
        if (e.data.type === 'iframeHeight') {
            document.getElementById('sms-plugin-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data.type === 'scrollTop') { window.scrollTo(0, 0); }

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
                alert('Export failed: ' + (e.data.error || 'unknown error'));
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
                ist.style.color = '#2e7d32';
                ist.textContent = msg + ' Reloading...';
                setTimeout(function() {
                    document.getElementById('tml-import-modal').style.display = 'none';
                    tmlResetImportBtn();
                    tmlFrame().postMessage({ type: 'tml_reloadFrame' }, '*');
                }, 1800);
            } else {
                tmlResetImportBtn();
                ist.style.color = '#c62828';
                ist.textContent = 'Import failed: ' + (e.data.error || 'unknown error');
            }
        }
    });
</script>
