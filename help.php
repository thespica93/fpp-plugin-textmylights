<?php
// Text My Lights - Help / Documentation page
$pluginName = "fpp-plugin-textmylights";
$githubBase = "https://github.com/thespica93/fpp-plugin-textmylights";

// Render a setup screenshot from the plugin's own docs/images/ folder. The images
// ship with the plugin, so FPP serves them from the plugin directory via its own
// file route (plugin.php?...&file=docs/images/<name>&nopage=1) - same origin, no
// external hosting or internet needed. This replaces inlining ~1.3 MB of base64 on
// EVERY page load: the browser fetches each image once and caches it across visits.
// Returns '' if the image isn't present.
function tml_shot($file, $alt) {
    $name = basename($file);
    if (!is_file(__DIR__ . '/docs/images/' . $name)) {
        return '';
    }
    $src = 'plugin.php?plugin=fpp-plugin-textmylights&file=docs/images/'
         . rawurlencode($name) . '&nopage=1';
    return '<img class="shot" loading="lazy" src="' . htmlspecialchars($src, ENT_QUOTES)
         . '" alt="' . htmlspecialchars($alt, ENT_QUOTES) . '">';
}

// FPP's UI theme, read server-side (see ui.php) - 'light'/'dark' or '' for System/unknown.
function tml_fpp_theme_help() {
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
$fppTheme = tml_fpp_theme_help();
?>
<style>
    .sms-help { max-width: 1100px; margin: 0 auto; font-family: Arial, sans-serif; line-height: 1.5; }
    .sms-help h1 { color: #333; margin: 0 0 6px; }
    .sms-help h2 { color: #4CAF50; border-bottom: 2px solid #4CAF50; padding-bottom: 6px; margin-top: 0; }
    .sms-help h3 { color: #333; margin-top: 18px; }
    .sms-help h4 { color: #333; margin-top: 14px; margin-bottom: 4px; font-size: 15px; }
    .sms-help ol { margin: 10px 0 10px 4px; padding-left: 20px; }
    .sms-help ol li { margin-bottom: 7px; }
    .sms-help code { background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-size: 13px; }
    .sms-help .shot { display: block; max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 6px; margin: 10px 0 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
    .sms-help .step-note { color: #555; font-size: 13px; margin: 4px 0 6px; }
    .ui-link { display: inline-block; background: #4CAF50; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none; font-weight: bold; margin: 6px 6px 6px 0; }
    .ui-link:hover { background: #45a049; color: white; text-decoration: none; }
    .ui-link.secondary { background: #2196F3; }
    .ui-link.secondary:hover { background: #0b7dda; }
    .ui-link.danger { background: #f44336; }
    .ref table { width: 100%; border-collapse: collapse; margin: 10px 0; }
    .ref th { background: #4CAF50; color: white; padding: 8px 10px; text-align: left; font-size: 13px; }
    .ref td { padding: 7px 10px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }
    .ref tr:hover td { background: #f7f7f7; }
    .ref td:first-child { white-space: nowrap; font-weight: bold; }
    .note { background: #e3f2fd; border: 1px solid #90caf9; color: #0d47a1; border-radius: 5px; padding: 10px 14px; margin: 12px 0; font-size: 13px; }
    .warn { background: #fff3cd; border: 1px solid #ffc107; border-radius: 5px; padding: 10px 14px; margin: 12px 0; font-size: 13px; }

    /* Two-column layout: left menu + content */
    .sms-help .tml-layout { display: flex; gap: 24px; align-items: flex-start; }
    .sms-help .tml-nav { flex: 0 0 220px; position: sticky; top: 12px; }
    .sms-help .tml-nav ul { list-style: none; margin: 0; padding: 0; border: 1px solid #ddd; border-radius: 8px; overflow: hidden; background: #fafafa; }
    .sms-help .tml-nav li { margin: 0; }
    .sms-help .tml-nav a { display: block; padding: 12px 16px; text-decoration: none; color: #333; font-weight: bold; font-size: 14px; border-left: 4px solid transparent; border-bottom: 1px solid #eee; }
    .sms-help .tml-nav li:last-child a { border-bottom: none; }
    .sms-help .tml-nav a:hover { background: #f0f0f0; color: #333; }
    .sms-help .tml-nav a.active { background: #fff; color: #4CAF50; border-left-color: #4CAF50; }
    .sms-help .tml-content { flex: 1 1 auto; min-width: 0; }
    .sms-help .tml-panel { display: none; }
    .sms-help .tml-panel.active { display: block; }
    @media (max-width: 760px) {
        .sms-help .tml-layout { flex-direction: column; }
        .sms-help .tml-nav { position: static; flex-basis: auto; width: 100%; }
    }

    /* Dark theme - applied by matching FPP's theme (see the script at the bottom). Brand
       accent green (#4CAF50) stays in both modes. */
    .sms-help.tml-dark { color: #e6e6e6; }
    .sms-help.tml-dark h1, .sms-help.tml-dark h3, .sms-help.tml-dark h4 { color: #e6e6e6; }
    .sms-help.tml-dark p, .sms-help.tml-dark li, .sms-help.tml-dark strong, .sms-help.tml-dark em { color: #e6e6e6; }
    .sms-help.tml-dark code { background: #2d323a; color: #e6e6e6; }
    .sms-help.tml-dark .shot { border-color: #3a3f47; }
    .sms-help.tml-dark .step-note { color: #a0a6b0; }
    .sms-help.tml-dark .ref td { border-bottom-color: #3a3f47; color: #e6e6e6; }
    .sms-help.tml-dark .ref tr:hover td { background: #262a31; }
    .sms-help.tml-dark .note { background: #0e2a40; color: #9fd0ff; border-color: #2b5c82; }
    .sms-help.tml-dark .warn { background: #4a3c0a; color: #ffd98a; border-color: #8a6d00; }
    .sms-help.tml-dark .history-note { background: #3a3410; color: #e0d48a; border-color: #8a7d1a; }
    .sms-help.tml-dark .tml-nav ul { background: #23262d; border-color: #3a3f47; }
    .sms-help.tml-dark .tml-nav a { color: #e6e6e6; border-bottom-color: #3a3f47; }
    .sms-help.tml-dark .tml-nav a:hover { background: #2a2e35; color: #e6e6e6; }
    .sms-help.tml-dark .tml-nav a.active { background: #1e2127; color: #4CAF50; }
</style>

<div class="sms-help">

    <h1>📱 Text My Lights — Help</h1>
    <p>Visitors text their name to your number and it appears on your pixel display. Messages can come from <strong>Twilio</strong> or <strong>Google Voice</strong> — pick one under <em>Settings → Message Source</em>.</p>

    <div class="note"><strong>Runs as a background layer.</strong> Text My Lights plays its content as an <strong>FSEQ Effect</strong> with a Pixel Overlay on top, so it runs alongside your scheduled show without taking over the player. Waiting and Name Display content must be <strong>sequences (.fseq)</strong> (or images); <strong>videos and playlists are not supported</strong> as plugin content because they run in the foreground and would replace your show. Both are optional — see <a href="#waiting-content">Waiting &amp; Name Display Content</a> and <a href="#scheduled-show">Running with a Scheduled Show</a>.</div>

    <a href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=ui.php" target="_top" class="ui-link">🔧 Open Config UI</a>
    <a href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=messages.php" target="_top" class="ui-link secondary">📋 View Message Queue</a>

    <div class="tml-layout">

    <!-- ================= LEFT MENU ================= -->
    <nav class="tml-nav">
        <ul>
            <li><a href="#" class="tml-tab active" data-panel="twilio">📞 Twilio Configuration</a></li>
            <li><a href="#" class="tml-tab" data-panel="google-voice">🟢 Google Voice Configuration</a></li>
            <li><a href="#" class="tml-tab" data-panel="settings">⚙️ Plugin Settings</a></li>
            <li><a href="#" class="tml-tab" data-panel="emoji">😀 Emoji in Names</a></li>
            <li><a href="#" class="tml-tab" data-panel="multi-pi">🖥️ Master &amp; Remote Pis</a></li>
            <li><a href="#" class="tml-tab" data-panel="backup">💾 Backup &amp; Restore</a></li>
            <li><a href="#" class="tml-tab" data-panel="support">🆘 Support</a></li>
        </ul>
    </nav>

    <div class="tml-content">

    <!-- ================= TWILIO ================= -->
    <section class="tml-panel active" id="panel-twilio">
    <h2 id="twilio">📞 Configure Twilio</h2>
    <p>Twilio is a paid SMS service (~$1/month for a number, ~$0.01 per text). It supports automatic SMS replies to visitors.</p>
    <ol>
        <li>Create an account at <a href="https://www.twilio.com/try-twilio" target="_blank">twilio.com</a> and buy an <strong>SMS-capable phone number</strong>.</li>
        <li>On the Twilio <a href="https://console.twilio.com" target="_blank">Console dashboard</a>, copy your <strong>Account SID</strong> and <strong>Auth Token</strong>.</li>
        <li>In this plugin: <em>Settings → Message Source → Twilio</em>. Paste the Account SID, Auth Token, and your Twilio phone number in <code>+1XXXXXXXXXX</code> format.</li>
        <li>Click <strong>Test Twilio Connection</strong> — you should see a success message.</li>
    </ol>
    <div class="warn"><strong>US numbers:</strong> Twilio requires <a href="https://www.twilio.com/docs/messaging/compliance/a2p-10dlc" target="_blank">A2P 10DLC registration</a> before texts (including auto-responses) will actually deliver. Register your number in the Twilio Console.</div>
    </section>

    <!-- ================= GOOGLE VOICE ================= -->
    <section class="tml-panel" id="panel-google-voice">
    <h2 id="google-voice">🟢 Configure Google Voice</h2>
    <p>Google Voice is <strong>free</strong>. It has no API, so the plugin reads the Gmail inbox that Google Voice forwards texts to. Automatic replies are supported by emailing Google Voice back (best-effort; may be rate-limited).</p>
    <p><strong>How it works:</strong> Google Voice forwards every incoming text to your Gmail inbox, and the plugin logs into that Gmail account (over IMAP, using an App Password) to read them. Use the Google account you want dedicated to the show — its inbox receives all the texts.</p>

    <h3>1. Choose a phone number</h3>
    <p>Go to <a href="https://voice.google.com" target="_blank">voice.google.com</a>, sign in, and accept the suggested number or click <em>Pick a different number</em>. (A US-based mobile number is required to qualify.)</p>
    <?php echo tml_shot('gv-01-choose-number.png', 'Choose a phone number'); ?>

    <h3>2. Verify your identity</h3>
    <p>Google requires verifying an existing phone number <strong>and</strong> a government-issued ID before the number can send and receive texts. Work through the tasks: link an existing number and enter the 6-digit code, then submit an ID type (Driver's License, Passport, State ID, or Green Card).</p>
    <?php echo tml_shot('gv-02-verify-tasks.png', 'Verification tasks'); ?>
    <?php echo tml_shot('gv-03-link-number.png', 'Link an existing number'); ?>
    <?php echo tml_shot('gv-04-verify-identity.png', 'Verify your identity'); ?>
    <?php echo tml_shot('gv-05-provide-id.png', 'Provide an ID'); ?>
    <?php echo tml_shot('gv-06-verified.png', 'Verified'); ?>

    <h3>3. Turn on email forwarding <span style="color:#f44336;">(required)</span></h3>
    <p>This is the key step that lets the plugin read your texts. In Google Voice <em>Settings → Messages</em>, turn <strong>on</strong> <strong>Forward messages to email</strong>, and confirm the email shown is the Gmail account the plugin will use.</p>
    <?php echo tml_shot('gv-07-forward-to-email.png', 'Forward messages to email'); ?>
    <div class="warn">Without this turned on, Google Voice keeps texts only inside its own app and the plugin has nothing to read — no names will reach your display.</div>

    <h3>4. Turn off call answering &amp; forwarding</h3>
    <p>The number is a <strong>text line</strong> for the show. In <em>Settings → Calls</em>, turn <strong>off</strong> every device under <strong>My devices</strong>, and turn <strong>off</strong> your linked number under <strong>Call forwarding</strong>.</p>
    <?php echo tml_shot('gv-08-calls-off.png', 'Turn off devices and call forwarding'); ?>

    <h3>5. Set Receiving Calls to Do Not Disturb</h3>
    <p>At the top of Google Voice, open the <strong>Receiving calls</strong> dropdown and choose <strong>Do not disturb</strong> so incoming calls go straight to voicemail. Texts are unaffected.</p>
    <?php echo tml_shot('gv-11-do-not-disturb.png', 'Set Receiving Calls to Do Not Disturb'); ?>

    <h3>6. Turn off spam filtering</h3>
    <p>In <em>Settings → Security</em>, turn <strong>off</strong> <strong>Filter spam calls and texts</strong>. When lots of people text names at once, a burst of messages can look like spam and get diverted to a Spam folder the plugin doesn't read — so names would silently go missing.</p>
    <?php echo tml_shot('gv-09-spam-off.png', 'Turn off spam filtering'); ?>

    <h3>7. Enable 2-Step Verification</h3>
    <p>In your <strong>Google Account</strong> (not Google Voice) → <a href="https://myaccount.google.com/signinoptions/two-step-verification" target="_blank">Security → 2-Step Verification</a>, turn it on. This is required to create the App Password in the next step.</p>
    <?php echo tml_shot('gv-10-2step-verification.png', 'Enable 2-Step Verification'); ?>

    <h3>8. Connect it to the plugin</h3>
    <ol>
        <li><strong>Create an App Password.</strong> Go to <a href="https://myaccount.google.com/apppasswords" target="_blank">App Passwords</a>, create one (name it e.g. “FPP”), and copy the <strong>16-character</strong> password.</li>
        <li>In this plugin: <em>Settings → Message Source → Google Voice</em>. Enter your <strong>Gmail address</strong> and paste the <strong>app password</strong> (leave IMAP Host as <code>imap.gmail.com</code>).</li>
        <li>Click <strong>Test Google Voice Connection</strong> — you should see “inbox connected.”</li>
    </ol>
    <div class="note"><strong>Good to know:</strong> Use the <em>app password</em>, not your normal Google password. Texts from unsaved numbers show the sender's phone number; texts from saved contacts show the contact name. Delivery is a few seconds to ~a minute slower than Twilio.</div>
    </section>

    <!-- ================= SETTINGS ================= -->
    <section class="tml-panel" id="panel-settings">
    <h2>⚙️ Plugin Settings</h2>
    <div class="note"><strong>📱 Works on your phone.</strong> The Config page fits small screens: the tabs, <em>View Message Queue</em> and <em>Start / Stop</em> wrap onto extra rows, side-by-side sections stack, and the Display tab shows the Preview above the line editors. One limit for now: dragging or resizing a line's box in the Preview needs a mouse — on a phone, set text, colors, effects and movement, and do box positioning from a computer.</div>

    <h3>Settings tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Message Source</td><td>Twilio or Google Voice. Changing it swaps which credential fields are shown, the rate-limit default, and the allowed responses.</td></tr>
        <tr><td>Start / Stop</td><td>Controls whether the plugin accepts and displays names. Use the <strong>▶ Start / ■ Stop</strong> button in the Config UI, or schedule the <code>fpp-plugin-textmylights-Start.sh</code> / <code>fpp-plugin-textmylights-Stop.sh</code> scripts with the FPP Scheduler (<em>Command → Run Script</em>). See <a href="#scheduled-show">Running with a Scheduled Show</a>.</td></tr>
        <tr><td>Runs as a background layer</td><td>Plugin content always plays as an <strong>FSEQ Effect</strong> (plus the Pixel Overlay), never as a playlist, and <strong>Stop</strong> only stops the plugin's own content. The plugin can therefore drive the projector while a scheduled playlist drives the rest of your display, and it never stops that playlist. Waiting and Name Display content must be <strong>sequences (.fseq)</strong>; <strong>videos and playlists are not supported</strong> because they run in the foreground and would take over the player.</td></tr>
        <tr><td>Credentials</td><td>Twilio: Account SID, Auth Token, Phone Number. Google Voice: Gmail Address + App Password.</td></tr>
        <tr><td>Poll Interval</td><td>How often (seconds) to check for new messages. 2–5 is typical.</td></tr>
        <tr><td>Default “Waiting” Content</td><td><strong>Optional.</strong> One or more sequences (.fseq) or images looped while no name is displayed; add two or more to rotate between them. If left empty, the plugin outputs nothing between names and the projector shows your scheduled show, which must then consist of <strong>sequences (.fseq)</strong> — see <a href="#waiting-content">below</a>.</td></tr>
        <tr><td>Name Display Content</td><td><strong>Optional.</strong> Sequence(s) or image(s) played while a name is displayed. If empty, the name is rendered over the Waiting content, or over your scheduled show when no Waiting content is set.</td></tr>
        <tr><td>Overlay Model</td><td>The FPP overlay model the name text is drawn onto. Use a <strong>standard RGB matrix</strong> model — see the note below on supported layouts.</td></tr>
        <tr><td>Max messages / phone</td><td>Per-day rate limit per phone number (0 = unlimited).</td></tr>
        <tr><td>Max message length</td><td>Longest name accepted, in characters. Each emoji counts as one character.</td></tr>
        <tr><td>Allow Emoji in Names</td><td><strong>On by default.</strong> Keeps emoji in names (e.g. “Emma 🎄”) and draws them in color on the display. Off = emoji are removed and only the letters are shown. See <a href="#emoji">Emoji in Names</a>.</td></tr>
        <tr><td>Allow duplicate names</td><td>If off, the same name from the same number is only shown once per day.</td></tr>
        <tr><td>Profanity filter</td><td>Rejects names containing blacklisted words <strong>or blacklisted emoji</strong> (Filters → <em>Manage Blacklist</em> / <em>Manage Emoji Blacklist</em>).</td></tr>
        <tr><td>Use whitelist</td><td>Only show names on your approved list.</td></tr>
    </table></div>
    <div class="note"><strong>Supported overlay layouts.</strong> The plugin draws names by writing the picture straight into the overlay model's memory, top-left to bottom-right. That only comes out right on a <strong>standard RGB matrix</strong>: <strong>3 channels per pixel (RGB, not RGBW)</strong>, laid out as a <strong>progressive</strong> grid — <strong>not serpentine / zig-zag</strong> — starting at the <strong>top-left</strong>. On those, text reads correctly. On a zig-zag, vertical, bottom-start, or RGBW model the output would come out scrambled, so the plugin <strong>detects a non-matching model and skips drawing</strong> rather than show garbage (it logs a warning naming the model). If your matrix is wired zig-zag, define the Pixel Overlay Model in FPP as a plain progressive RGB grid for the plugin to use. <em>(FPP&nbsp;10 adds an API that maps the data for any layout; a future release will use it to lift this restriction.)</em></div>

    <h3 id="waiting-content">Waiting &amp; Name Display Content</h3>
    <p>Neither is required; the plugin can be started with both empty. The projector output depends on which are configured:</p>
    <div class="ref"><table>
        <tr><th>Waiting</th><th>Name Display</th><th>Between names</th><th>When a name is displayed</th></tr>
        <tr><td>Set</td><td>Set</td><td>Waiting content loops</td><td>Name Display content plays with the name overlaid</td></tr>
        <tr><td>Set</td><td>Empty</td><td>Waiting content loops</td><td>Name is overlaid on the Waiting content</td></tr>
        <tr><td>Empty</td><td>Set</td><td>Scheduled show (plugin outputs nothing)</td><td>Name Display content plays with the name overlaid</td></tr>
        <tr><td>Empty</td><td>Empty</td><td>Scheduled show (plugin outputs nothing)</td><td>Name is overlaid <strong>directly on the scheduled show</strong></td></tr>
    </table></div>
    <div class="note"><strong>Using your own sequences between names.</strong> To keep the projector on your own sequences — for example, sequences that already display your texting number — leave <strong>Waiting</strong> empty. The plugin outputs nothing until a name arrives, overlays the name for the configured Display Duration, then clears the overlay so your show is visible again. The Config UI displays a warning when Waiting (or both Waiting and Name Display) is empty as a reminder of this behavior.</div>
    <div class="warn"><strong>Scheduled content must be sequences (.fseq).</strong> When Waiting is empty, names are displayed only over scheduled <strong>sequences (.fseq)</strong>. If the Scheduler is playing a video (.mp4 or other media), <strong>names will not appear</strong>: FPP renders video frames directly to the output after the Pixel Overlay is applied, so the plugin cannot draw over them. To use video content, render it to a sequence in xLights (add the video to the projector/matrix model with a <em>Video</em> effect and export the .fseq), then schedule that sequence in place of the video.</div>
    <div class="warn">With both Waiting and Name Display empty, the name text is drawn directly over whatever the scheduled sequence is outputting on the overlay model. Choose text colors and positions on the <em>Display</em> tab that remain legible over your show.</div>

    <div class="warn"><strong>Sequence channel range.</strong> Plugin content plays as an FSEQ Effect, and FPP applies <em>every channel contained in the sequence file</em> on top of any running show. A sequence exported with only the overlay model affects just those channels; a full-channel sequence overwrites every light in its range while it plays and blanks them briefly when it stops. This is expected when the plugin runs your display on its own, but matters when a scheduled show runs at the same time. The Config UI shows a warning in the content picker and below the content lists when a selected sequence includes channels outside the overlay model. The warning is informational; the content is still allowed.</div>

    <h3 id="scheduled-show">Running with a Scheduled Show</h3>
    <p>The plugin runs independently of the FPP Scheduler. <strong>While the plugin is started, incoming names are displayed whenever they arrive — including during a synchronized show</strong> (e.g. a choreographed song sequence), where a name would be overlaid on your programmed effects.</p>
    <div class="warn"><strong>Stop the plugin before a synchronized show.</strong> To prevent names from appearing during a show, press <strong>■ Stop</strong> in the Config UI or run the <code>fpp-plugin-textmylights-Stop.sh</code> script before the show begins, and <strong>▶ Start</strong> (or <code>fpp-plugin-textmylights-Start.sh</code>) afterward. To automate this, add both scripts to the FPP Scheduler (<em>Command → Run Script</em>) at the show's start and end times.</div>
    <div class="note"><strong>Stop behavior.</strong> Names already queued or on screen when Stop is issued finish displaying before the plugin stops. While stopped, the plugin keeps checking for messages and replies to texters with the <strong>Show Not Live</strong> response (if enabled on the <em>SMS Responses</em> tab); those names are not queued for later. In a Master/Remote setup, Stop on the Master also stops all connected Remotes.</div>

    <h3>Display tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Message Lines</td><td>Up to 4 lines of text; use <code>{name}</code> where the visitor's name should appear.</td></tr>
        <tr><td>Line Box / Position</td><td>Area each line renders into. Font size auto-fits the box; position can be centered or fixed.</td></tr>
        <tr><td>Color &amp; Font</td><td>Per-line text color and font.</td></tr>
        <tr><td>Text effects</td><td>Per-line effects, each in its own color:
            <strong>Outline</strong> (Thin, Medium, Thick, or <strong>Bubble</strong> — a fat wrap-around border),
            <strong>2nd Outline</strong> (a second border outside the first),
            <strong>Shadow</strong> (down and to the right: Small, Medium, Large),
            <strong>Glow</strong> (a soft halo: Soft, Medium, Strong), and
            <strong>Gradient</strong> (the letters shade from the line color at the top to a second color at the bottom).
            <strong>Quick style</strong> applies a ready-made look in one click — Bubble, Sticker, Neon, Gold, Candy Cane, Frosty (some also set the text color); every effect stays editable afterwards. The menu always shows the line's current look: <strong>None</strong> when no effects are on (choose it to clear them all), the style's name while the effects match it, and <strong>Custom</strong> once you change any effect yourself.
            <strong>Save your own:</strong> pick <strong>＋ Save current look…</strong> in the Quick style menu, give it a name, and the line's current effects and text color are saved under <strong>My Styles</strong> in every line's menu — on any content. Saving with an existing name replaces that style. <strong>Manage my styles…</strong> lists them so you can delete ones you no longer need (lines already using a style keep their look). Saved styles are stored on this Pi and included in Config Export/Import.
            Effects scale with the text and are counted inside the line's box, so the text shrinks slightly to make room. Layered back to front: glow, shadow, 2nd outline, outline, letters. Pure black is drawn as near-black, because FPP treats black overlay pixels as see-through. Emoji keep their own colors and aren't outlined.</td></tr>
        <tr><td>Overlapping lines</td><td>Line boxes may overlap. Lines are layered in order (Line 1 at the back, Line 4 in front), exactly as the preview shows — no line blacks out another.</td></tr>
        <tr><td>Movement &amp; Speed</td><td>Center (static) or scroll, with a speed for scrolling lines.</td></tr>
        <tr><td>Display Duration</td><td>How many seconds each name stays on screen. Set on the <strong>Master</strong> per Name Display content; every Pi uses it (see <a href="#preview-pi">Master &amp; Remote Pis</a>).</td></tr>
    </table></div>
    <div class="note"><strong>Glow and soft edges on the matrix.</strong> FPP's overlay can't be partly see-through — a pixel either shows the sequence underneath (pure black) or is drawn solid. So over a <strong>sequence</strong>, the fading part of a <strong>glow</strong> and the smooth edges of letters are drawn as a fine dot pattern: each pixel is either fully lit in the effect's color or left see-through, in proportion to how faded it is. On an LED matrix this reads as a soft glow blending into your sequence (instead of a dark ring around the text). The Display-tab preview shows the smooth version. Over an <strong>image</strong> background the plugin blends the image itself, so glows fade smoothly there. Effects set to a dark color (e.g. the shadow in <em>Bubble</em> or the 2nd outline in <em>Sticker</em>) still show dark, as chosen.</div>
    <div class="note"><strong>Background preview on small boards.</strong> The Display tab shows your sequence or image <em>behind</em> the text so you can see how the names will land. Drawing that background frame uses extra memory, so on boards with <strong>less than 1&nbsp;GB of RAM</strong> (e.g. a Pi Zero or BeagleBone Black) it is turned off automatically — the preview area stays blank and the plugin protects your show from running out of memory mid-display. <strong>You can still position and style the text as normal:</strong> the text boxes, movement, color, and font all work exactly the same; only the picture behind them is hidden. On a 1&nbsp;GB+ board (Pi&nbsp;3/4/5) the background preview appears automatically.</div>

    <h3>SMS Responses tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Response toggles</td><td>Turn each automatic reply on/off: success, blocked, rate-limited, duplicate, invalid format, not whitelisted, show-not-live.</td></tr>
        <tr><td>Response text</td><td>The message sent back for each case. Customize to your show.</td></tr>
        <tr><td>👥 Multiple Names</td><td>Indented under <strong>Success</strong>. Sent <strong>instead of</strong> the Success reply when one text puts two or more names on the display (see <a href="#multi-name">Several names in one text</a>). Default: <em>“Merry Christmas! It looks like you sent more than 1 name. Your names will appear 1 at a time on our display! 🎄”</em>. It has no toggle of its own — it follows the Success toggle. It is greyed out, with a note naming the cause, while the <strong>Name Whitelist</strong> is on, <strong>Max Messages Per Phone</strong> is above 0, or <strong>Allow Duplicate Names</strong> is off, because multi-name texts are rejected in those cases.</td></tr>
    </table></div>
    <div class="note">Works with both sources. Twilio sends via its API; Google Voice sends by emailing a reply back through Google Voice (best-effort, may be rate-limited).</div>

    <h3>How a message gets approved</h3>
    <div class="ref"><table>
        <tr><th>Check</th><th>If it fails</th></tr>
        <tr><td>Phone not blocked</td><td>Reply: number blocked</td></tr>
        <tr><td>Under rate limit</td><td>Reply: rate limited</td></tr>
        <tr><td>Valid name (1–2 words, letters)</td><td>Reply: invalid format</td></tr>
        <tr><td>Not a duplicate today</td><td>Reply: duplicate</td></tr>
        <tr><td>Passes profanity filter, including the emoji blacklist <span style="color:#888;">(if on)</span></td><td>Reply: profanity</td></tr>
        <tr><td>On whitelist <span style="color:#888;">(if on)</span></td><td>Reply: not on list</td></tr>
        <tr><td>✅ Added to display queue</td><td>Reply: success</td></tr>
    </table></div>
    <div class="note">A text that is only emoji or a phone “reaction” (like 👍 or <em>Loved “…”</em>) is ignored — it is not a name, gets no reply, and is never shown.</div>

    <h3 id="multi-name">👥 Several names in one text</h3>
    <p>A visitor can send more than one name in a single text. Names are split on <strong>commas</strong>, <strong>line breaks</strong>, <strong>&amp;</strong>, and the word <strong>and</strong>:</p>
    <div class="ref"><table>
        <tr><th>Text</th><th>Names shown</th></tr>
        <tr><td>Maggie and Tess</td><td>Maggie, Tess</td></tr>
        <tr><td>Maggie, Tess</td><td>Maggie, Tess</td></tr>
        <tr><td>Maggie &amp; Tess</td><td>Maggie, Tess</td></tr>
        <tr><td>Wyatt, Theo, and Bill</td><td>Wyatt, Theo, Bill</td></tr>
        <tr><td>Andy Brandon</td><td>Andy Brandon <span style="color:#888;">(one name — “and” only splits as a whole word, and spaces never split)</span></td></tr>
    </table></div>
    <p>Each name is queued and shown <strong>one at a time</strong>, and the visitor gets the <strong>Multiple Names</strong> reply instead of the normal Success reply. At least two real names are needed before a text is treated as a list, so an ordinary sentence with a comma isn't chopped up. Up to 25 names per text are used.</p>
    <div class="warn"><strong>Multi-name texts are all-or-nothing.</strong> They are only accepted when the <strong>Name Whitelist is off</strong>, <strong>Max Messages Per Phone is 0</strong> (unlimited), and <strong>Allow Duplicate Names is on</strong> — the Google Voice defaults. If any of those is set differently, or any name fails the format rules, the whole text gets the <strong>Invalid Format</strong> reply (and it doesn't count toward the daily limit). Profanity anywhere in the text rejects the whole text with the Profanity reply.</div>

    <h3 id="unfiltered">🧪 Testing tab &amp; admin phone skip every filter</h3>
    <p>Names sent from the <strong>Testing</strong> tab, and names texted from the <strong>Admin Phone Number</strong> (Google Voice), go straight to the display queue. They skip <strong>all</strong> of the checks above: profanity and emoji blacklist, whitelist, phone blocklist, max length, word rules, duplicate check, and the per-phone daily limit. They don't count toward any limit or auto-block tally.</p>
    <div class="warn"><strong>🔴 Profanity is allowed from the Testing tab and the admin phone.</strong> Anything typed there will be displayed, so only use them yourself. Names are still cleaned up the same way (symbols removed, Proper Case), and emoji follow the <em>Allow Emoji in Names</em> setting. Admin replies of <strong>Y</strong>/<strong>N</strong> and the word <strong>admin</strong> are still handled as approvals/connect, never displayed. The admin texting several names (separated by commas, line breaks, <strong>&amp;</strong> or <strong>and</strong>) shows each one, and gets the Multiple Names reply — even while the whitelist or limits are on.</div>

    <h3 id="live-approval">🙋 Live Name Approval over text <span style="color:#888;font-weight:normal;">(Google Voice only)</span></h3>
    <p>With the whitelist <strong>on</strong>, a name that is not on the list is normally rejected. Live Name Approval lets you say "yes" from your phone instead: when a visitor texts a name you have not pre-approved, the plugin texts <strong>you</strong> the name and you reply <strong>Y</strong> to add it and show it, or <strong>N</strong> to deny it. It runs only while the show is live, and only for Google Voice. Leave the Admin Phone Number blank to turn it off.</p>

    <h4>Connecting your admin phone</h4>
    <p>Google Voice can only <em>send</em> a text by replying inside an existing email conversation. So before it can text you, your admin phone has to start one:</p>
    <ol>
        <li>In <em>Settings → Message Source → Google Voice</em>, enter your <strong>Admin Phone Number</strong>.</li>
        <li>From that phone, text the word <strong>admin</strong> to your Google Voice number.</li>
        <li>The yellow "Action needed" banner turns green ("Admin phone connected") within a few seconds.</li>
    </ol>
    <div class="note">The word <strong>admin</strong> is reserved — it is used only to connect and is <strong>never shown on the display</strong>. Connecting is always deliberate: texting names from the admin phone, or old texts already in Gmail, never connects it on their own — only the word <strong>admin</strong> does. You'll get a confirmation text back once it's connected. If you change the admin number, text <strong>admin</strong> again from the new phone.</div>

    <div class="warn"><strong>🔴 Keep the conversation thread.</strong> Approvals work only while that Google Voice conversation with your admin number is still in your Gmail <strong>inbox</strong>. If you delete it or archive it out of the inbox, the plugin notices (it re-checks that exact message periodically), the banner reverts to "Action needed," and approvals pause until you text <strong>admin</strong> again.</div>

    <h4>Approving names during the show</h4>
    <div class="ref"><table>
        <tr><th>You receive</th><th>You reply</th><th>What happens</th></tr>
        <tr><td>"Approve ‘Name’? Y/N"</td><td><strong>Y</strong></td><td>Name is added to the whitelist and shown; the visitor gets the success reply.</td></tr>
        <tr><td>(same)</td><td><strong>N</strong></td><td>Name is denied; the visitor gets the standard <strong>Not on Whitelist</strong> reply. There is no separate "denied" message.</td></tr>
        <tr><td>(no reply in time)</td><td>—</td><td>The request expires after the <strong>Approval Timeout</strong> and the visitor gets the Not on Whitelist reply. If you reply <strong>Y</strong> later, the name is still added to the whitelist for next time, but it is <strong>not</strong> shown now.</td></tr>
    </table></div>
    <p>The approval prompt and the visitor-facing "please wait" message are editable on the <em>SMS Responses</em> tab, under the <strong>Not on Whitelist</strong> response (which is also what a denial sends).</p>
    <div class="note">Names you text from the admin phone yourself are shown <strong>unfiltered</strong> — see <a href="#unfiltered">Testing tab &amp; admin phone skip every filter</a>.</div>
    </section>

    <!-- ================= EMOJI ================= -->
    <section class="tml-panel" id="panel-emoji">
    <h2 id="emoji">😀 Emoji in Names</h2>
    <p>Visitors can include emoji with their name — “Emma 🎄”, “The Smiths 👨‍👩‍👧”, “Leo 🇺🇸” — and they are drawn on the display in full color. Turn this on or off with <strong>Allow Emoji in Names</strong> under <em>Settings → Message Settings</em> (on by default). With it off, emoji are simply removed and only the letters are shown.</p>

    <h3>How emoji are counted</h3>
    <div class="ref"><table>
        <tr><th>Rule</th><th>Details</th></tr>
        <tr><td>Length</td><td>Each emoji counts as <strong>one character</strong> toward Max Message Length — including combined emoji like 👨‍👩‍👧, flags 🇺🇸 and skin tones 👋🏽.</td></tr>
        <tr><td>Word rules</td><td>An emoji on its own is not a word, so “Mary Jane 🎄” still passes <em>Two Words Maximum</em>.</td></tr>
        <tr><td>Whitelist</td><td>Names are matched <strong>without</strong> their emoji, so “Emma 🎄” matches <em>emma</em> on the list. Admin-approved names are added without emoji.</td></tr>
        <tr><td>Emoji only</td><td>A text with no letters (just 👍 or 🎄🎄) is ignored, the same as a phone reaction.</td></tr>
        <tr><td>Color</td><td>Emoji keep their own colors; the line color only applies to the letters.</td></tr>
    </table></div>

    <h3 id="emoji-blacklist">Emoji Blacklist</h3>
    <p>Some emoji are used as slang for sex, drugs, violence or hate. The <strong>Emoji Blacklist</strong> screens them as part of the <strong>profanity filter</strong>: a match gets the <strong>Profanity</strong> response and counts toward the daily auto-block tally, just like a blacklisted word. Open it from <em>Filters → 😶 Manage Emoji Blacklist</em>.</p>
    <div class="ref"><table>
        <tr><th>Entry type</th><th>Blocked when</th><th>Example</th></tr>
        <tr><td>Single emoji</td><td>That emoji appears <strong>anywhere</strong> in the text</td><td>🖕 🍆 🔫</td></tr>
        <tr><td>Combination</td><td><strong>All</strong> of its emoji appear in the same text, in any order</td><td>💦 👅 &nbsp;·&nbsp; ❄️ 👃 &nbsp;·&nbsp; 🍃 💨</td></tr>
        <tr><td>Repeated emoji</td><td>That emoji appears at least that many times</td><td>6️⃣ 6️⃣ 6️⃣</td></tr>
    </table></div>
    <div class="note"><strong>Skin tones don't matter.</strong> Blocking 🖕 also blocks 🖕🏻 🖕🏽 🖕🏿, so one entry covers every tone. Many emoji are innocent on their own — especially at Christmas (❄️ ⛄ 🌲 🍬 🍪) — so the built-in list mostly blocks them only in <em>combinations</em>.</div>
    <ul>
        <li><strong>Built-in list.</strong> The plugin ships with a list of ~450 entries (sexual slang, rude gestures, drug codes, weapons/violence, and hate symbols), drawn from the DEA's emoji drug-code chart, parent-safety emoji guides, and the ADL's hate-symbol database. It is updated with the plugin.</li>
        <li><strong>The full emoji list.</strong> The page shows every emoji iOS and Android support (the official Unicode list), searchable by name and grouped by category. Emoji already blocked are outlined in red; emoji that are part of a blocked combination have an orange dot.</li>
        <li><strong>Add an entry.</strong> Tap emoji in the list (or paste them) to build an entry: one emoji is blocked on its own, two or more are blocked as a combination. Then click <strong>+ Add to Blacklist</strong>.</li>
        <li><strong>Remove an entry.</strong> Click ✕ on it. Built-in entries you remove stay removed after plugin updates, and can be brought back from <em>Removed built-in entries</em> → <strong>Restore</strong>.</li>
    </ul>
    <div class="warn">The emoji blacklist only works while the <strong>Profanity Filter is on</strong>. It still rejects texts with blocked emoji when <em>Allow Emoji in Names</em> is off. Names from the Testing tab and the admin phone are <a href="#unfiltered">not screened</a>.</div>

    <h3>Display requirements</h3>
    <ul>
        <li>Emoji are drawn with the <strong>Noto Color Emoji</strong> font, which the plugin installs automatically. If it couldn't be installed, names still show — just without their emoji. Reinstall the plugin to retry.</li>
        <li>Emoji newer than the installed font are left off the display.</li>
        <li>On some systems, combined emoji are drawn as their main emoji: 👨‍👩‍👧 shows as 👨, 🎅🏽 as 🎅, and 1️⃣ as 1. Flags may show as two letters (🇺🇸 as “US”).</li>
        <li>Send “Emma 🎄” from the <strong>Testing</strong> tab to see how emoji look on your display.</li>
    </ul>
    </section>

    <!-- ================= MASTER & REMOTE ================= -->
    <section class="tml-panel" id="panel-multi-pi">
    <h2 id="multi-pi">🖥️ Master &amp; Remote Pis</h2>
    <p>With more than one projector, one Pi runs as the <strong>Master</strong> (it receives the texts and runs the show) and the others run as <strong>Remotes</strong> that display the same names at the same moment, each on its own overlay with its own text layout.</p>

    <h3>Each Pi needs its own copy of the content</h3>
    <p>The Master tells each Remote <em>which</em> content to play, but the sequence and image files themselves must already be on that Remote. Upload them in FPP's <strong>File Manager</strong> on each Pi (or let FPP MultiSync copy them). Config Export/Import does <strong>not</strong> carry sequences or images.</p>
    <div class="note"><strong>If a Remote is missing a Name Display content</strong>, the show keeps running: that Remote shows the name over whatever it is currently displaying (usually its Waiting content), using its own text layout, for the Master's display time. With several Name Display contents in round-robin, only the turns that land on the missing content are affected. A Remote that is missing Waiting content keeps showing its current background. Each case is logged on that Pi.</div>

    <h3 id="preview-pi">Previewing &amp; editing each Pi from the Master</h3>
    <p>On the Master's <em>Display</em> tab, the <strong>“Which Pi are you previewing and editing?”</strong> dropdown (under Background Preview) chooses which device the preview shows and where text changes are saved:</p>
    <div class="ref"><table>
        <tr><th>Choice</th><th>Preview shows</th><th>Text changes save to</th></tr>
        <tr><td><strong>This Pi (master)</strong></td><td>The Master's own overlay and background</td><td>The Master</td></tr>
        <tr><td><strong>Remote Pi: <em>name</em></strong></td><td>That Remote's overlay, at its real size, with its own background frames and text layout</td><td>That Remote</td></tr>
    </table></div>
    <ul>
        <li>The dropdown appears when Remotes are connected (or when this Pi has no overlay). The note under it says which Pi you're working on.</li>
        <li><strong>This Pi has no overlay model:</strong> “This Pi” is listed as <em>no overlay selected</em> and the preview starts on the largest Remote. Choosing This Pi shows a message to select an overlay model in FPP Display Settings, or switch to a Remote Pi to configure it; text editing is greyed out until then.</li>
        <li><strong>While previewing a Remote,</strong> the content dropdown lists only the content that Remote has. If the content you were on isn't there, the preview switches to one it has and the note says so.</li>
        <li><strong>The Remote has none of your Name Display content:</strong> the background and the text editor are greyed out — there's nothing on that Pi to preview or edit. Add the sequence on that Pi; the preview unlocks within a few seconds.</li>
        <li>Edits made on a Remote's own Config page show up in the Master's preview automatically.</li>
        <li><strong>Display Duration is always the Master's.</strong> The Master starts and stops every name on every Pi, so while previewing a Remote the Display Duration box shows — and changes — the Master's duration for that content, labelled <em>“set on this Master, applies to every Pi”</em>. A Remote's own Config page shows the duration as controlled by the Master.</li>
        <li>Text effects (outline, shadow, glow, gradient, quick styles) are saved per Pi, so each projector can have its own look; set them for a Remote by previewing it from the Master or on its own Config page.</li>
    </ul>
    </section>

    <!-- ================= BACKUP & RESTORE ================= -->
    <section class="tml-panel" id="panel-backup">
    <h2 id="backup">💾 Backup &amp; Restore</h2>
    <p>Export the plugin's settings to a single file, then import it on another Pi to reproduce this unit's configuration — no re-configuring by hand. Both buttons are at the bottom of the <em>Settings</em> tab in the Config UI.</p>

    <div class="warn"><strong>🔒 Credentials are never exported.</strong> Your <strong>Twilio Auth Token</strong> and <strong>Google Voice App Password</strong> are deliberately left out of the export file, so it is safe to store and copy between devices. After importing on a new Pi, re-enter those under <em>Settings → Message Source</em>.</p>

    <div class="note"><strong>Only this plugin's own data is exported.</strong> FPP's sequences, images, videos and the overlay model are <strong>not</strong> included — those belong to FPP. Distribute your sequences/media to each Pi with <strong>xLights FPP Connect</strong>, and set up the <strong>Pixel Overlay Model</strong> in FPP on each Pi. The export records the <em>names</em> of the content and overlay model you selected (in the settings), so after importing, just make sure those exist on the new Pi.</div>

    <h3>What's in the export</h3>
    <div class="ref"><table>
        <tr><th>Included</th><th>Details</th></tr>
        <tr><td>Plugin settings</td><td>Everything in <code>plugin.json</code> — display lines and text effects, message rules, response text, filters, poll interval, and the <em>names</em> of the selected content / overlay model. (Your Twilio Account SID, phone number, and Gmail address come along; the auth token / app password do <strong>not</strong>.)</td></tr>
        <tr><td>Saved text styles</td><td>Your Quick Styles (<em>My Styles</em>) and saved colors. On import these are <strong>merged</strong>: a style with the same name is replaced, and styles already on the other Pi are kept — handy for sharing a style pack between Pis.</td></tr>
        <tr><td>Phone blocklist</td><td>Blocked phone numbers. Only included when the list has numbers, so importing never wipes another Pi's blocklist with an empty one.</td></tr>
        <tr><td>Word lists</td><td>Your name whitelist and profanity (word) blacklist, including your additions and removals.</td></tr>
        <tr><td>Emoji blacklist</td><td>Emoji and combinations you added to, or removed from, the built-in emoji blacklist.</td></tr>
    </table></div>

    <h3>Export</h3>
    <ol>
        <li>Open the Config UI and go to the <em>Settings</em> tab.</li>
        <li>Under <strong>Backup &amp; Restore</strong>, click <strong>⬇️ Export Config</strong>.</li>
        <li>A dialog lists what to include — <em>Plugin settings</em>, <em>Saved text styles</em>, <em>Phone blocklist</em>, <em>Word lists</em> and <em>Emoji blacklist</em>. All are checked by default; untick any to leave it out (e.g. export only your styles to share them).</li>
        <li>Click <strong>Export</strong>. A <code>textmylights-config-*.zip</code> downloads to your computer. Keep it somewhere safe.</li>
    </ol>

    <h3>Import onto another Pi</h3>
    <ol>
        <li>Install this plugin on the new Pi and open its Config UI.</li>
        <li>On the <em>Settings</em> tab, under <strong>Backup &amp; Restore</strong>, click <strong>⬆️ Import Config</strong> and choose the <code>.zip</code>.</li>
        <li>Confirm the prompt. Whatever the file contains is restored — plugin settings, phone blocklist, word lists and emoji blacklist replace this Pi's; saved text styles are merged into this Pi's. The page reloads with everything in place.</li>
        <li>Re-enter your <strong>Auth Token</strong> / <strong>App Password</strong> under <em>Message Source</em>, then test the connection.</li>
    </ol>
    <div class="note"><strong>Good to know:</strong> Import keeps the target Pi's own saved credentials — it never clears them. It also never touches FPP's own data: push your sequences/media with <strong>xLights FPP Connect</strong> and create the matching <strong>Pixel Overlay Model</strong> in FPP on the new Pi, so the names the settings reference resolve to real content there.</div>
    </section>

    <!-- ================= SUPPORT ================= -->
    <section class="tml-panel" id="panel-support">
    <h2>🆘 Support</h2>
    <p>Found a bug or have a question? Open an issue on GitHub or browse the project source.</p>
    <a href="<?php echo $githubBase; ?>/issues" target="_blank" class="ui-link danger">🐛 Report a Bug</a>
    <a href="<?php echo $githubBase; ?>" target="_blank" class="ui-link secondary">📖 GitHub</a>
    </section>

    </div><!-- /tml-content -->
    </div><!-- /tml-layout -->

</div>

<script>
(function () {
    var tabs = document.querySelectorAll('.sms-help .tml-tab');
    var panels = document.querySelectorAll('.sms-help .tml-panel');
    function show(name) {
        tabs.forEach(function (t) {
            t.classList.toggle('active', t.getAttribute('data-panel') === name);
        });
        panels.forEach(function (p) {
            p.classList.toggle('active', p.id === 'panel-' + name);
        });
    }
    tabs.forEach(function (t) {
        t.addEventListener('click', function (e) {
            e.preventDefault();
            show(t.getAttribute('data-panel'));
        });
    });

    // Open the tab named in the URL hash (e.g. help.php#google-voice), so the
    // "View … Configuration" links in the config UI land on the right panel
    // instead of the default (Twilio) tab. Also respond to later hash changes.
    function showFromHash() {
        var name = (location.hash || '').replace('#', '');
        var match = document.getElementById('panel-' + name);
        if (match) { show(name); return; }
        // An anchor inside a panel (e.g. #waiting-content): open its panel, then scroll to it.
        var target = name && document.getElementById(name);
        var panel = target && target.closest('.tml-panel');
        if (panel) { show(panel.id.replace('panel-', '')); target.scrollIntoView(); }
    }
    showFromHash();
    window.addEventListener('hashchange', showFromHash);
})();

// Match FPP's theme. This page renders inside the FPP page, so judge the theme from the
// FPP page-background luminance (robust to whatever theme CSS FPP uses), falling back to the
// browser preference, and toggle the dark class on the help container. Re-check periodically
// so a live FPP theme toggle is picked up.
(function () {
    var el = document.querySelector('.sms-help');
    if (!el) return;
    function lumOf(str) {
        var m = (str || '').match(/rgba?\(([^)]+)\)/i);
        if (!m) return null;
        var p = m[1].split(',').map(function (s) { return parseFloat(s); });
        var a = p.length > 3 ? p[3] : 1;
        if (!(a > 0.1)) return null;
        return 0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2];
    }
    var TML_FPP_THEME = <?php echo json_encode($fppTheme); ?>;
    function detect() {
        if (TML_FPP_THEME === 'dark' || TML_FPP_THEME === 'light') return TML_FPP_THEME;
        // FPP's CSS color-scheme is the resolved theme (covers System Default); its body bg is
        // dark even in light mode, so use color-scheme, then text color, then the OS preference.
        try {
            var cs = (getComputedStyle(document.documentElement).colorScheme || '') + ' ' +
                     (getComputedStyle(document.body).colorScheme || '');
            var d = /\bdark\b/.test(cs), l = /\blight\b/.test(cs);
            if (d && !l) return 'dark';
            if (l && !d) return 'light';
            var fg = lumOf(getComputedStyle(document.body).color);
            if (fg !== null) return fg > 150 ? 'dark' : 'light';
        } catch (e) {}
        return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    function apply() { el.classList.toggle('tml-dark', detect() === 'dark'); }
    apply();
    setInterval(apply, 1500);
})();
</script>
