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
curl_setopt($ch, CURLOPT_HEADERFUNCTION, function ($ch, $header) {
    $len = strlen($header);
    $t = trim($header);
    if ($t === '') return $len;
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
    header('Content-Type: application/json');
    echo json_encode(array('error' => 'Plugin service unreachable on 127.0.0.1:5000', 'detail' => $err));
    exit;
}
http_response_code($code);
