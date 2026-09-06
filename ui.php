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
</style>
<iframe id="sms-plugin-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>" scrolling="no"></iframe>
<script>
    document.getElementById('sms-plugin-frame').addEventListener('load', function() {
        window.scrollTo(0, 0);
    });
    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'iframeHeight') {
            document.getElementById('sms-plugin-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data && e.data.type === 'scrollTop') {
            window.scrollTo(0, 0);
        }
    });
</script>
