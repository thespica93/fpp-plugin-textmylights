<?php
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
// Pass the plugin's network access token to the iframe (see ui.php for details).
$tokenFile = "/home/fpp/media/plugin.fpp-textmylights/.access_token";
$token = is_readable($tokenFile) ? trim(file_get_contents($tokenFile)) : "";

// Match FPP's UI theme (read server-side; see ui.php) - 'light'/'dark', or '' for System.
function tml_fpp_theme_msg() {
    $raw = '';
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
$fppTheme  = tml_fpp_theme_msg();
$pluginUrl = "http://$host:5000/messages" . ($token !== "" ? "?token=" . urlencode($token) : "");
if ($fppTheme !== "") {
    $pluginUrl .= ($token !== "" ? "&" : "?") . "theme=" . $fppTheme;
}
?>
<style>
    #sms-messages-frame {
        width: 100%;
        border: none;
        display: block;
        overflow: hidden;
        min-height: 400px;
    }
</style>
<iframe id="sms-messages-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>" scrolling="no"></iframe>
<script>
    document.getElementById('sms-messages-frame').addEventListener('load', function() {
        window.scrollTo(0, 0);
        tmlSendMsgTheme();   // match the plugin page to FPP's theme on load
    });

    // Match the cross-origin plugin iframe to FPP's theme (same approach as ui.php): judge it
    // from FPP's page-background luminance and forward it to the iframe, which applies it.
    function tmlMsgLum(str) {
        var m = (str || '').match(/rgba?\(([^)]+)\)/i);
        if (!m) return null;
        var p = m[1].split(',').map(function (s) { return parseFloat(s); });
        var a = p.length > 3 ? p[3] : 1;
        if (!(a > 0.1)) return null;
        return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2];
    }
    var TML_FPP_THEME = <?php echo json_encode($fppTheme); ?>;
    function tmlMsgDetectTheme() {
        if (TML_FPP_THEME === 'dark' || TML_FPP_THEME === 'light') return TML_FPP_THEME;
        // FPP's CSS color-scheme is the resolved theme (covers System Default); its body bg is
        // dark even in light mode, so use color-scheme, then text color, then the OS preference.
        try {
            var cs = (getComputedStyle(document.documentElement).colorScheme || '') + ' ' +
                     (getComputedStyle(document.body).colorScheme || '');
            var d = /\bdark\b/.test(cs), l = /\blight\b/.test(cs);
            if (d && !l) return 'dark';
            if (l && !d) return 'light';
            var fg = tmlMsgLum(getComputedStyle(document.body).color);
            if (fg !== null) return fg > 150 ? 'dark' : 'light';
        } catch (e) {}
        return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    var _tmlMsgLastTheme = null;
    function tmlSendMsgTheme() {
        var t = tmlMsgDetectTheme();
        _tmlMsgLastTheme = t;
        try { document.getElementById('sms-messages-frame').contentWindow.postMessage({ type: 'tml_theme', theme: t }, '*'); } catch (e) {}
    }
    setInterval(function () {
        var t = tmlMsgDetectTheme();
        if (t !== _tmlMsgLastTheme) tmlSendMsgTheme();
    }, 1500);

    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'iframeHeight') {
            document.getElementById('sms-messages-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data && e.data.type === 'scrollTop') {
            window.scrollTo(0, 0);
        }
    });
</script>
