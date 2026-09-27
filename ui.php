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
    /* Export modal lives HERE, in the parent (not the iframe), so position:fixed
       centers on the real browser viewport — it stays put while scrolling. */
    #tml-export-modal {
        display: none; position: fixed; inset: 0; z-index: 100000;
        background: rgba(0,0,0,0.5);
        align-items: center; justify-content: center;
        font-family: Arial, sans-serif;
    }
    #tml-export-modal .tml-card {
        background: #fff; color: #333; width: 92%; max-width: 460px;
        border-radius: 8px; padding: 22px; box-shadow: 0 8px 30px rgba(0,0,0,0.35);
        max-height: 88vh; overflow-y: auto; box-sizing: border-box;
    }
    #tml-export-modal h3 { margin: 0 0 6px; color: #333; font-size: 20px; }
    #tml-export-modal .tml-sub { font-size: 12px; color: #666; margin: 0 0 14px; }
    #tml-export-modal label.tml-opt {
        display: flex; gap: 10px; align-items: flex-start; margin: 0 0 14px; cursor: pointer;
    }
    /* FPP's global input styles squish checkboxes to slivers — force a real box. */
    #tml-export-modal label.tml-opt input[type="checkbox"] {
        -webkit-appearance: auto !important; appearance: auto !important;
        width: 18px !important; height: 18px !important; min-width: 18px !important;
        flex: 0 0 18px; margin: 2px 0 0 !important; padding: 0 !important;
        accent-color: #4CAF50; cursor: pointer; box-sizing: border-box;
    }
    #tml-export-modal .tml-opt strong { font-size: 14px; }
    #tml-export-modal .tml-opt .tml-desc { font-size: 12px; color: #666; }
    #tml-export-modal .tml-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 6px; align-items: center; }
    #tml-export-modal .tml-actions button {
        padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; color: #fff; font-size: 14px;
    }
    #tml-export-modal .tml-actions button:disabled { cursor: default; opacity: 0.6; }
    #tml-export-modal #tml-exp-cancel { background: #9e9e9e; }
    #tml-export-modal #tml-exp-go { background: #4CAF50; min-width: 110px; }
</style>
<iframe id="sms-plugin-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>" scrolling="no"></iframe>

<!-- Export selection modal (owned by the parent so it is a true fixed overlay). -->
<div id="tml-export-modal" onclick="if(event.target===this)tmlHideExport()">
    <div class="tml-card">
        <h3>Export Config</h3>
        <p class="tml-sub">Choose what to include. Only the content <strong>this plugin is set to use</strong> is exported &mdash; never all of FPP's files. <strong>Credentials are never included.</strong></p>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-settings" checked>
            <span><strong>Plugin settings</strong><br><span class="tml-desc">Display lines, message rules, response text, filters, poll interval, selected content &amp; overlay model.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-lists" checked>
            <span><strong>Blocked numbers &amp; word lists</strong><br><span class="tml-desc">Blocked phone numbers and your whitelist / blacklist words.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-content" checked>
            <span><strong>Content files</strong><br><span class="tml-desc">The Waiting &amp; Name Display sequences, images, and videos this plugin uses &mdash; copied file-for-file. Can be large.</span></span></label>
        <label class="tml-opt"><input type="checkbox" id="tml-exp-overlay" checked>
            <span><strong>Overlay model (matrix)</strong><br><span class="tml-desc">The FPP Pixel Overlay Model the names are drawn onto.</span></span></label>
        <div class="tml-actions">
            <button type="button" id="tml-exp-cancel" onclick="tmlHideExport()">Cancel</button>
            <button type="button" id="tml-exp-go" onclick="tmlDoExport()">Export</button>
        </div>
    </div>
</div>

<script>
    document.getElementById('sms-plugin-frame').addEventListener('load', function() {
        window.scrollTo(0, 0);
    });

    var _tmlExporting = false;      // true while a download is in flight
    var _tmlExportTimer = null;

    function tmlResetExportBtn() {
        var go = document.getElementById('tml-exp-go');
        go.disabled = false; go.textContent = 'Export';
        document.getElementById('tml-exp-cancel').disabled = false;
        _tmlExporting = false;
    }
    function tmlShowExport() {
        tmlResetExportBtn();
        document.getElementById('tml-export-modal').style.display = 'flex';
    }
    function tmlHideExport() {
        if (_tmlExporting) return;   // don't close while a download is running
        document.getElementById('tml-export-modal').style.display = 'none';
    }
    function tmlDoExport() {
        if (_tmlExporting) return;
        function ck(id) { return document.getElementById(id).checked ? 1 : 0; }
        var sel = { s: ck('tml-exp-settings'), l: ck('tml-exp-lists'),
                    c: ck('tml-exp-content'), o: ck('tml-exp-overlay') };
        if (!sel.s && !sel.l && !sel.c && !sel.o) {
            alert('Select at least one thing to export.');
            return;
        }
        // Show progress and lock the modal open until the iframe reports done.
        _tmlExporting = true;
        var go = document.getElementById('tml-exp-go');
        go.disabled = true; go.textContent = 'Exporting...';
        document.getElementById('tml-exp-cancel').disabled = true;
        // The iframe performs the actual fetch/download (same-origin auth cookie).
        document.getElementById('sms-plugin-frame').contentWindow
            .postMessage({ type: 'tml_export', sel: sel }, '*');
        // Safety net in case the iframe never answers.
        _tmlExportTimer = setTimeout(function() {
            if (_tmlExporting) { tmlResetExportBtn(); alert('Export timed out. Please try again.'); }
        }, 120000);
    }

    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'iframeHeight') {
            document.getElementById('sms-plugin-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data && e.data.type === 'scrollTop') {
            window.scrollTo(0, 0);
        }
        // The iframe asks us to show the (parent-owned) export modal.
        if (e.data && e.data.type === 'tml_openExport') {
            tmlShowExport();
        }
        // The iframe finished (or failed) the download.
        if (e.data && e.data.type === 'tml_exportDone') {
            clearTimeout(_tmlExportTimer);
            var go = document.getElementById('tml-exp-go');
            if (e.data.success) {
                go.textContent = 'Downloaded';
                _tmlExporting = false;                 // allow closing now
                setTimeout(function() { tmlHideExport(); tmlResetExportBtn(); }, 900);
            } else {
                tmlResetExportBtn();
                alert('Export failed: ' + (e.data.error || 'unknown error'));
            }
        }
    });
</script>
