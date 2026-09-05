<?php
// Text My Lights - Help / Documentation page
$pluginName = "fpp-plugin-textmylights";
$githubBase = "https://github.com/thespica93/fpp-plugin-textmylights";
?>
<style>
    .sms-help { max-width: 860px; margin: 0 auto; font-family: Arial, sans-serif; line-height: 1.5; }
    .sms-help h2 { color: #4CAF50; border-bottom: 2px solid #4CAF50; padding-bottom: 6px; margin-top: 30px; }
    .sms-help h3 { color: #333; margin-top: 18px; }
    .sms-help ol { margin: 10px 0 10px 4px; padding-left: 20px; }
    .sms-help ol li { margin-bottom: 7px; }
    .sms-help code { background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-size: 13px; }
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
</style>

<div class="sms-help">

    <h2>📱 Text My Lights — Help</h2>
    <p>Visitors text their name to your number and it appears on your pixel display. Messages can come from <strong>Twilio</strong> or <strong>Google Voice</strong> — pick one under <em>Settings → Message Source</em>.</p>

    <a href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=ui.php" target="_top" class="ui-link">🔧 Open Config UI</a>
    <a href="plugin.php?_menu=content&plugin=fpp-plugin-textmylights&page=messages.php" target="_top" class="ui-link secondary">📋 View Message Queue</a>

    <!-- ================= TWILIO ================= -->
    <h2 id="twilio">📞 Configure Twilio</h2>
    <p>Twilio is a paid SMS service (~$1/month for a number, ~$0.01 per text). It supports automatic SMS replies to visitors.</p>
    <ol>
        <li>Create an account at <a href="https://www.twilio.com/try-twilio" target="_blank">twilio.com</a> and buy an <strong>SMS-capable phone number</strong>.</li>
        <li>On the Twilio <a href="https://console.twilio.com" target="_blank">Console dashboard</a>, copy your <strong>Account SID</strong> and <strong>Auth Token</strong>.</li>
        <li>In this plugin: <em>Settings → Message Source → Twilio</em>. Paste the Account SID, Auth Token, and your Twilio phone number in <code>+1XXXXXXXXXX</code> format.</li>
        <li>Click <strong>Test Twilio Connection</strong> — you should see a success message.</li>
    </ol>
    <div class="warn"><strong>US numbers:</strong> Twilio requires <a href="https://www.twilio.com/docs/messaging/compliance/a2p-10dlc" target="_blank">A2P 10DLC registration</a> before texts (including auto-responses) will actually deliver. Register your number in the Twilio Console.</div>

    <!-- ================= GOOGLE VOICE ================= -->
    <h2 id="google-voice">🟢 Configure Google Voice</h2>
    <p>Google Voice is <strong>free</strong>. It has no API, so the plugin reads the Gmail inbox that Google Voice forwards texts to. Automatic replies are supported by emailing Google Voice back (best-effort; may be rate-limited).</p>
    <ol>
        <li><strong>Turn on email forwarding.</strong> In <a href="https://voice.google.com/settings" target="_blank">Google Voice → Settings → Messages</a>, enable <em>“Forward messages to email.”</em></li>
        <li><strong>Turn on 2-Step Verification.</strong> On your Google Account, open <a href="https://myaccount.google.com/signinoptions/two-step-verification" target="_blank">Security → 2-Step Verification</a> and enable it (required for the next step).</li>
        <li><strong>Create an App Password.</strong> Go to <a href="https://myaccount.google.com/apppasswords" target="_blank">App Passwords</a>, create one (name it e.g. “FPP”), and copy the <strong>16-character</strong> password.</li>
        <li>In this plugin: <em>Settings → Message Source → Google Voice</em>. Enter your <strong>Gmail address</strong> and paste the <strong>app password</strong> (leave IMAP Host as <code>imap.gmail.com</code>).</li>
        <li>Click <strong>Test Google Voice Connection</strong> — you should see “inbox connected.”</li>
    </ol>
    <div class="note"><strong>Good to know:</strong> Use the <em>app password</em>, not your normal Google password. Texts from unsaved numbers show the sender's phone number; texts from saved contacts show the contact name. Delivery is a few seconds to ~a minute slower than Twilio.</div>

    <!-- ================= SETTINGS ================= -->
    <h2>⚙️ Plugin Settings</h2>

    <h3>Settings tab</h3>
    <div class="ref"><table>
        <tr><th>Setting</th><th>What it does</th></tr>
        <tr><td>Message Source</td><td>Twilio or Google Voice. Changing it swaps which credential fields are shown, the rate-limit default, and the allowed responses.</td></tr>
        <tr><td>Start / Stop</td><td>The show is started and stopped by the <code>Start</code> / <code>Stop</code> scheduler commands — no manual enable toggle.</td></tr>
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

    <h2>🆘 Support</h2>
    <a href="<?php echo $githubBase; ?>/issues" target="_blank" class="ui-link danger">🐛 Report a Bug</a>
    <a href="<?php echo $githubBase; ?>" target="_blank" class="ui-link secondary">📖 GitHub</a>

</div>
