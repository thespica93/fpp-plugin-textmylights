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
    });
    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'iframeHeight') {
            document.getElementById('sms-messages-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data && e.data.type === 'scrollTop') {
            window.scrollTo(0, 0);
        }
    });
</script>
