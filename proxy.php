<?php
/**
 * Text My Lights - same-origin reverse proxy to the plugin daemon (127.0.0.1:5000).
 *
 * WHY: the settings UI used to be a CROSS-ORIGIN iframe to http://<host>:5000/, which
 *   - is blocked by FPP's Apache CSP (default-src 'self', no frame-src),
 *   - breaks under HTTPS (mixed content),
 *   - and, under FPP's /proxy/<ip>/ view, sent the per-instance token to the master.
 * Serving the UI through FPP (same origin) via this proxy fixes all three. The daemon
 * trusts the loopback interface (see _require_access_token in sms_plugin.py), so a request
 * that arrives here over FPP's own web server and is forwarded to 127.0.0.1:5000 is
 * authorized WITHOUT any token crossing to the browser.
 *
 * Reached as:  plugin.php?plugin=fpp-plugin-textmylights&page=proxy.php&nopage=1&p=<path>&<query>
 *   - nopage=1 so FPP emits our raw bytes (JSON / PNG / zip) without the page chrome.
 *   - p = the daemon path (e.g. api/config); remaining query params are forwarded as-is.
 *
 * NOTE: requires validation on a current FPP nightly (how plugin.php serves nopage=1
 * pages, and that no bytes are emitted before this runs). See FPP_REVIEW_ISSUES.md #14.
 */

// The browser-facing base the daemon should bake into the served HTML as window.TML_BASE,
// so every /api/... call in the SPA is rewritten to come back through this proxy. RELATIVE
// on purpose: under FPP's /proxy/<ip>/ view the browser resolves it against the current
// path, so each box talks to its OWN daemon.
$PROXY_BASE = 'plugin.php?plugin=fpp-plugin-textmylights&page=proxy.php&nopage=1&p=';

$DAEMON = 'http://127.0.0.1:5000/';

// Path on the daemon (strip any leading slash; p is already the decoded path).
$path = isset($_GET['p']) ? ltrim((string)$_GET['p'], '/') : '';

// Forward every query param EXCEPT the routing ones this proxy consumes.
$fwd = $_GET;
unset($fwd['plugin'], $fwd['page'], $fwd['nopage'], $fwd['p']);
$qs = http_build_query($fwd);

$target = $DAEMON . $path . ($qs !== '' ? ('?' . $qs) : '');

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

$ch = curl_init();
curl_setopt($ch, CURLOPT_URL, $target);
curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $method);
curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);
curl_setopt($ch, CURLOPT_TIMEOUT, 600);          // imports/large exports can be slow
curl_setopt($ch, CURLOPT_FOLLOWLOCATION, false);

// Request body for write methods.
$reqHeaders = array('X-TML-Proxy-Base: ' . $PROXY_BASE);
if (in_array($method, array('POST', 'PUT', 'PATCH', 'DELETE'), true)) {
    $ctype = $_SERVER['CONTENT_TYPE'] ?? '';
    if (stripos($ctype, 'multipart/form-data') !== false) {
        // PHP already parsed multipart into $_POST/$_FILES, so php://input is empty.
        // Rebuild the fields for cURL (it sets its own multipart Content-Type + boundary).
        $fields = array();
        foreach ($_POST as $k => $v) { $fields[$k] = $v; }
        foreach ($_FILES as $k => $f) {
            if (is_array($f) && !empty($f['tmp_name']) && is_uploaded_file($f['tmp_name'])) {
                $fields[$k] = new CURLFile($f['tmp_name'], $f['type'] ?: 'application/octet-stream', $f['name']);
            }
        }
        curl_setopt($ch, CURLOPT_POSTFIELDS, $fields);   // do NOT set Content-Type; cURL does
    } else {
        $body = file_get_contents('php://input');
        curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
        if ($ctype !== '') { $reqHeaders[] = 'Content-Type: ' . $ctype; }
    }
}
curl_setopt($ch, CURLOPT_HTTPHEADER, $reqHeaders);

// Relay the daemon's status + selected response headers as they arrive.
$statusSet = false;
curl_setopt($ch, CURLOPT_HEADERFUNCTION, function ($ch, $header) use (&$statusSet) {
    $len = strlen($header);
    $t = trim($header);
    if ($t === '') return $len;
    // Set the browser's status code HERE, from the daemon's "HTTP/x.y <code> ..." line,
    // which always arrives before any body byte. We cannot wait until after the body is
    // streamed: FPP runs PHP with output_buffering=1024 (FPP_Install.sh), so the moment the
    // body passes 1 KB the (default 200) header is flushed and a later http_response_code()
    // is silently lost AND logs "headers already sent" to apache2-error.log. Parsing it in
    // the header phase keeps the daemon's real 403/404/500 intact for a response of ANY size.
    // A 1xx line (e.g. 100 Continue) or a repeated status line just overwrites until the
    // final one - still before the body begins.
    if (stripos($t, 'HTTP/') === 0) {
        $parts = explode(' ', $t, 3);
        if (isset($parts[1]) && ctype_digit($parts[1])) {
            http_response_code((int)$parts[1]);
            $statusSet = true;
        }
        return $len;
    }
    $lower = strtolower($t);
    // Pass through only headers that matter for the browser; let Apache/PHP set the rest.
    // NOTE: deliberately NOT relaying Content-Length - if PHP/Apache re-chunks or gzips the
    // streamed body, a stale length would truncate/corrupt the download.
    foreach (array('content-type:', 'content-disposition:', 'cache-control:') as $pfx) {
        if (strpos($lower, $pfx) === 0) { header($t); break; }
    }
    return $len;
});

// Stream the body straight to the client (no full-file buffering in PHP memory).
curl_setopt($ch, CURLOPT_WRITEFUNCTION, function ($ch, $chunk) {
    echo $chunk;
    return strlen($chunk);
});

curl_setopt($ch, CURLOPT_RETURNTRANSFER, false);
$ok = curl_exec($ch);
$code = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
$err = curl_error($ch);
curl_close($ch);

if ($ok === false || $code === 0) {
    http_response_code(502);
    // Distinguish a full-page load (the browser navigating to the plugin UI) from an SPA
    // API/fetch call. A navigation sends "Accept: text/html"; fetch() defaults to "*/*".
    // The known page paths are a belt-and-suspenders fallback. On a page load, show a short
    // themed message telling the user what to do; API callers keep the machine-readable JSON.
    // This is the common sight on a FRESH install/update too: the FPP installer only restarts
    // the daemon if it was already running (fpp_install.sh), so the service isn't up until the
    // user restarts FPPD - which is exactly what the message asks for.
    $accept = $_SERVER['HTTP_ACCEPT'] ?? '';
    $isPage = ($path === '')
        || in_array($path, array('messages', 'whitelist', 'blacklist', 'blocklist'), true)
        || stripos($accept, 'text/html') !== false;
    if (!$isPage) {
        header('Content-Type: application/json');
        echo json_encode(array('error' => 'Plugin service unreachable on 127.0.0.1:5000', 'detail' => $err));
        exit;
    }
    header('Content-Type: text/html; charset=utf-8');
    // Honor FPP's ?theme=dark|light when present; otherwise follow the OS preference.
    $themeAttr = '';
    if (isset($_GET['theme']) && ($_GET['theme'] === 'dark' || $_GET['theme'] === 'light')) {
        $themeAttr = ' data-theme="' . $_GET['theme'] . '"';
    }
    // NOTE: we deliberately do NOT offer a restart button here. FPP plugin policy (§3.6) does
    // not allow plugins to call /api/system/fppd/restart; the installer already sets the
    // restartFlag, so we point the user at FPP's own restart banner instead.
    echo '<!doctype html><html lang="en"' . $themeAttr . '><head><meta charset="utf-8">'
       . '<meta name="viewport" content="width=device-width,initial-scale=1">'
       . '<title>Text My Lights - service not running</title><style>'
       . ':root{--bg:#f4f5f7;--card:#fff;--fg:#1b1b1f;--muted:#5a5f6a;--border:#dfe2e8;--accent:#c0392b}'
       . '@media (prefers-color-scheme:dark){:root{--bg:#1b1d21;--card:#24272c;--fg:#e8eaed;--muted:#a0a6b0;--border:#363a41;--accent:#ff6b5e}}'
       . ':root[data-theme="light"]{--bg:#f4f5f7;--card:#fff;--fg:#1b1b1f;--muted:#5a5f6a;--border:#dfe2e8;--accent:#c0392b}'
       . ':root[data-theme="dark"]{--bg:#1b1d21;--card:#24272c;--fg:#e8eaed;--muted:#a0a6b0;--border:#363a41;--accent:#ff6b5e}'
       . 'html,body{margin:0;background:var(--bg);color:var(--fg);'
       . 'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.55}'
       . '.wrap{max-width:560px;margin:8vh auto;padding:0 20px}'
       . '.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:28px 30px}'
       . 'h1{margin:0 0 4px;font-size:1.35rem}.lead{color:var(--muted);margin:0 0 18px}'
       . 'p{margin:0 0 14px}.muted{color:var(--muted);font-size:.92rem}'
       . 'code{background:rgba(127,127,127,.18);padding:1px 6px;border-radius:4px;font-size:.9em}'
       . '.bar{height:3px;background:var(--accent);border-radius:10px 10px 0 0;margin:-28px -30px 22px}'
       . '</style></head><body><div class="wrap"><div class="card"><div class="bar"></div>'
       . '<h1>Text My Lights isn&rsquo;t running</h1>'
       . '<p class="lead">The plugin service on <code>127.0.0.1:5000</code> didn&rsquo;t respond.</p>'
       . '<p>If you&rsquo;ve just <strong>installed or updated</strong> the plugin, restart FPPD using the '
       . '<strong>restart button in the banner at the top of the page</strong>. The service starts with FPPD.</p>'
       . '<p class="muted">If it still doesn&rsquo;t start, check <code>plugin-fpp-plugin-textmylights.log</code> '
       . 'under <strong>Content Setup &rarr; File Manager &rarr; Logs</strong>.</p>'
       . '</div></div></body></html>';
    exit;
}
// Status was already set from the daemon's status line during the header phase (see the
// HEADERFUNCTION and the output_buffering note there). Fall back to setting it here only if
// no status line was seen AND nothing has been flushed yet - so this can never be the call
// that triggers a "headers already sent" warning.
if (!$statusSet && !headers_sent()) {
    http_response_code($code);
}
