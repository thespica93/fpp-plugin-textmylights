<?php
// Text My Lights - Help / Documentation page
$pluginName = "fpp-plugin-textmylights";
$githubBase = "https://github.com/thespica93/fpp-plugin-textmylights";

// Inline a setup screenshot from the plugin's own docs/images/ folder as a
// base64 data URI. This renders the walkthrough images inside FPP with no
// dependency on external hosting or internet access — the files ship with the
// plugin. Returns '' if the image isn't present.
function tml_shot($file, $alt) {
    $path = __DIR__ . '/docs/images/' . basename($file);
    if (!is_file($path)) {
        return '';
    }
    $data = base64_encode(file_get_contents($path));
    return '<img class="shot" src="data:image/png;base64,' . $data . '" alt="'
         . htmlspecialchars($alt, ENT_QUOTES) . '">';
}
?>
<style>
    .sms-help { max-width: 1100px; margin: 0 auto; font-family: Arial, sans-serif; line-height: 1.5; }
    .sms-help h2 { color: #4CAF50; border-bottom: 2px solid #4CAF50; padding-bottom: 6px; margin-top: 0; }
    .sms-help h3 { color: #333; margin-top: 18px; }
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
</style>

<div class="sms-help">

    <h1 style="color:#333; margin:0 0 6px;">📱 Text My Lights — Help</h1>
    <p>Visitors text their name to your number and it appears on your pixel display. Messages can come from <strong>Twilio</strong> or <strong>Google Voice</strong> — pick one under <em>Settings → Message Source</em>.</p>

    <a href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=ui.php" target="_top" class="ui-link">🔧 Open Config UI</a>
    <a href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=messages.php" target="_top" class="ui-link secondary">📋 View Message Queue</a>

    <div class="tml-layout">

    <!-- ================= LEFT MENU ================= -->
    <nav class="tml-nav">
        <ul>
            <li><a href="#" class="tml-tab active" data-panel="twilio">📞 Twilio Configuration</a></li>
            <li><a href="#" class="tml-tab" data-panel="google-voice">🟢 Google Voice Configuration</a></li>
            <li><a href="#" class="tml-tab" data-panel="settings">⚙️ Plugin Settings</a></li>
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

    <h3>Settings tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Message Source</td><td>Twilio or Google Voice. Changing it swaps which credential fields are shown, the rate-limit default, and the allowed responses.</td></tr>
        <tr><td>Start / Stop</td><td>The show is started and stopped by the <code>Start</code> / <code>Stop</code> scheduler commands — no manual enable toggle.</td></tr>
        <tr><td>Coexistence mode</td><td>Turn on when the Pi is your <strong>main show controller</strong> and Text My Lights only runs during breaks. On <strong>Stop</strong> it stops just this plugin's content and returns the output to your main show's schedule (<em>Start Next Scheduled Item</em>) instead of stopping everything — so the projector can be shared with other sequences. Off (default): the plugin owns the Pi and Stop halts all playback.</td></tr>
        <tr><td>Credentials</td><td>Twilio: Account SID, Auth Token, Phone Number. Google Voice: Gmail Address + App Password.</td></tr>
        <tr><td>Poll Interval</td><td>How often (seconds) to check for new messages. 2–5 is typical.</td></tr>
        <tr><td>Default “Waiting” Content</td><td><strong>Required.</strong> The playlist/sequence that loops while waiting for texts.</td></tr>
        <tr><td>Name Display Content</td><td>Optional content to play while a name is on screen (defaults to the waiting content).</td></tr>
        <tr><td>Overlay Model</td><td>The FPP overlay model the name text is drawn onto.</td></tr>
        <tr><td>Max messages / phone</td><td>Per-day rate limit per phone number (0 = unlimited).</td></tr>
        <tr><td>Max message length</td><td>Longest name accepted, in characters.</td></tr>
        <tr><td>Allow duplicate names</td><td>If off, the same name from the same number is only shown once per day.</td></tr>
        <tr><td>Profanity filter</td><td>Rejects names containing blacklisted words.</td></tr>
        <tr><td>Use whitelist</td><td>Only show names on your approved list.</td></tr>
    </table></div>

    <h3>Display tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Message Lines</td><td>Up to 4 lines of text; use <code>{name}</code> where the visitor's name should appear.</td></tr>
        <tr><td>Line Box / Position</td><td>Area each line renders into. Font size auto-fits the box; position can be centered or fixed.</td></tr>
        <tr><td>Color &amp; Font</td><td>Per-line text color and font.</td></tr>
        <tr><td>Movement &amp; Speed</td><td>Center (static) or scroll, with a speed for scrolling lines.</td></tr>
        <tr><td>Display Duration</td><td>How many seconds each name stays on screen.</td></tr>
    </table></div>

    <h3>SMS Responses tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Response toggles</td><td>Turn each automatic reply on/off: success, blocked, rate-limited, duplicate, invalid format, not whitelisted, show-not-live.</td></tr>
        <tr><td>Response text</td><td>The message sent back for each case. Customize to your show.</td></tr>
    </table></div>
    <div class="note">Works with both sources. Twilio sends via its API; Google Voice sends by emailing a reply back through Google Voice (best-effort, may be rate-limited).</div>

    <h3>How a message gets approved</h3>
    <div class="ref"><table>
        <tr><th>Check</th><th>If it fails</th></tr>
        <tr><td>Phone not blocked</td><td>Reply: number blocked</td></tr>
        <tr><td>Under rate limit</td><td>Reply: rate limited</td></tr>
        <tr><td>Valid name (1–2 words, letters)</td><td>Reply: invalid format</td></tr>
        <tr><td>Not a duplicate today</td><td>Reply: duplicate</td></tr>
        <tr><td>Passes profanity filter <span style="color:#888;">(if on)</span></td><td>Reply: blocked</td></tr>
        <tr><td>On whitelist <span style="color:#888;">(if on)</span></td><td>Reply: not on list</td></tr>
        <tr><td>✅ Added to display queue</td><td>Reply: success</td></tr>
    </table></div>
    </section>

    <!-- ================= BACKUP & RESTORE ================= -->
    <section class="tml-panel" id="panel-backup">
    <h2 id="backup">💾 Backup &amp; Restore</h2>
    <p>Export your entire setup to a single file, then import it on another Pi to reproduce this unit — no re-configuring by hand. Both buttons are at the bottom of the <em>Settings</em> tab in the Config UI.</p>

    <div class="warn"><strong>🔒 Credentials are never exported.</strong> Your <strong>Twilio Auth Token</strong> and <strong>Google Voice App Password</strong> are deliberately left out of the export file, so it is safe to store and copy between devices. After importing on a new Pi, re-enter those under <em>Settings → Message Source</em>.</p>

    <h3>What's in the export</h3>
    <div class="ref"><table>
        <tr><th>Included</th><th>Details</th></tr>
        <tr><td>Plugin settings</td><td>Everything in <code>plugin.json</code> — display lines, message rules, response text, filters, poll interval, and the selected content/overlay model. (Your Twilio Account SID, phone number, and Gmail address come along; the auth token / app password do <strong>not</strong>.)</td></tr>
        <tr><td>Block &amp; name lists</td><td>Blocked phone numbers, and your whitelist / blacklist word files.</td></tr>
        <tr><td>Content files</td><td>The <strong>Waiting</strong> and <strong>Name Display</strong> content, plus the sequences, images, and videos those playlists actually use — copied file-for-file.</td></tr>
        <tr><td>Overlay model</td><td>The FPP Pixel Overlay Model definition (the "matrix" the names are drawn onto).</td></tr>
    </table></div>

    <h3>Export</h3>
    <ol>
        <li>Open the Config UI and go to the <em>Settings</em> tab.</li>
        <li>Under <strong>Backup &amp; Restore</strong>, click <strong>⬇️ Export Config</strong>.</li>
        <li>A dialog lists what to include — <em>Plugin settings</em>, <em>Blocked numbers &amp; word lists</em>, <em>Content files</em>, and <em>Overlay model</em>. All are checked by default; untick anything you want to leave out (e.g. skip <em>Content files</em> for a small settings-only backup).</li>
        <li>Click <strong>Export</strong>. A <code>textmylights-config-*.zip</code> downloads to your computer. Keep it somewhere safe.</li>
    </ol>
    <div class="note">Only the content <strong>this plugin is set to use</strong> (your Waiting and Name Display selections, and the files they reference) is exported — never all of FPP's sequences or media.</div>

    <h3>Import onto another Pi</h3>
    <ol>
        <li>Install this plugin on the new Pi and open its Config UI.</li>
        <li>On the <em>Settings</em> tab, under <strong>Backup &amp; Restore</strong>, click <strong>⬆️ Import Config</strong> and choose the <code>.zip</code>.</li>
        <li>Confirm the prompt. Settings, lists, content files, and the overlay model are restored; the page reloads with everything in place.</li>
        <li>Re-enter your <strong>Auth Token</strong> / <strong>App Password</strong> under <em>Message Source</em>, then test the connection.</li>
    </ol>
    <div class="note"><strong>Good to know:</strong> Import keeps the target Pi's own saved credentials — it never clears them. Only the <strong>one selected overlay model</strong> is exported and it's <strong>added</strong> to the target's channel-output config (<code>co-other.json</code>) without touching that Pi's other outputs (the previous file is kept as <code>co-other.json.tml-bak</code>). It takes effect after an <strong>FPPD restart</strong>.</div>
    <div class="warn"><strong>Matching hardware:</strong> The overlay model maps to channel ranges, but the export does <strong>not</strong> include FPP's channel-output/controller configuration. For the display to light correctly, the new Pi's outputs and wiring must already match — or use FPP's own <em>Backup</em> for a full hardware clone.</div>
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
        if (match) { show(name); }
    }
    showFromHash();
    window.addEventListener('hashchange', showFromHash);
})();
</script>
