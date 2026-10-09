<?php
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
// Pass the plugin's network access token to the iframe (see ui.php for details).
$tokenFile = "/home/fpp/media/plugin.fpp-textmylights/.access_token";
$token = is_readable($tokenFile) ? trim(file_get_contents($tokenFile)) : "";
$pluginUrl = "http://$host:5000/messages" . ($token !== "" ? "?token=" . urlencode($token) : "");
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
    function tmlMsgDetectTheme() {
        // Background (body→html) if opaque, else text color (light text => dark theme), else OS pref.
        try {
            var bg = tmlMsgLum(getComputedStyle(document.body).backgroundColor);
            if (bg === null) bg = tmlMsgLum(getComputedStyle(document.documentElement).backgroundColor);
            if (bg !== null) return bg < 128 ? 'dark' : 'light';
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
