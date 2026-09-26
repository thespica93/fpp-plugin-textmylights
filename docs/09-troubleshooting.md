# Troubleshooting

> Solutions to common problems with the FPP SMS Twilio Plugin.

---

## Table of Contents

- [Can't Access Web Interface](#cant-access-web-interface)
- [Plugin Won't Start](#plugin-wont-start)
- [Twilio Connection Fails](#twilio-connection-fails)
- [Messages Not Being Received](#messages-not-being-received)
- [Names Not Appearing on Display](#names-not-appearing-on-display)
- [Text Appears in Wrong Position](#text-appears-in-wrong-position)
- [Auto-Responses Not Sending](#auto-responses-not-sending)
- [Whitelist / Blacklist Issues](#whitelist--blacklist-issues)
- [Plugin Not Listed in FPP Plugin Manager](#plugin-not-listed-in-fpp-plugin-manager)
- [Checking Logs](#checking-logs)
- [Resetting the Plugin](#resetting-the-plugin)

---

## Can't Access Web Interface

**Symptom:** Browser can't connect to `http://YOUR_FPP_IP:5000`

**Check 1 — Is the plugin running?**
```bash
ps aux | grep sms_plugin
```
If no output, the plugin isn't running. See [Plugin Won't Start](#plugin-wont-start).

**Check 2 — Is port 5000 open?**
```bash
ss -tlnp | grep 5000
```
If nothing shows, the service stopped. Check the log:
```bash
tail -50 /home/fpp/media/logs/sms_plugin.log
```

**Check 3 — Firewall blocking port 5000?**
```bash
sudo ufw allow 5000
sudo ufw reload
```

**Check 4 — Are you using the right IP?**
- Use your FPP device's IP address, not `localhost` (unless you're on the device itself)
- Find FPP's IP in the FPP web interface under **Status/Control** → **Network**

---

## Plugin Won't Start

**Symptom:** Plugin appears disabled, or `ps aux | grep sms_plugin` shows nothing

**Check 1 — Is the plugin enabled in FPP?**
1. Go to your FPP web interface → **Content Setup** → **Plugin Manager**
2. Verify the plugin shows as installed and enabled

**Check 2 — Are Python dependencies installed?**
```bash
pip3 list | grep -E "flask|twilio|requests"
```
Expected output:
```
Flask           3.0.0
requests        2.31.0
twilio          8.10.0
```
If any are missing, reinstall:
```bash
cd /opt/fpp/plugins/fpp-plugin-textmylights
bash scripts/fpp_install.sh
```

**Check 3 — Is there a Python error on startup?**
```bash
cd /opt/fpp/plugins/fpp-plugin-textmylights
python3 sms_plugin.py
```
Run it manually and look for error output. Press `Ctrl+C` when done.

**Check 4 — Is port 5000 already in use by something else?**
```bash
ss -tlnp | grep 5000
```
If another process is using port 5000, you'll need to change that process or stop it.

---

## Twilio Connection Fails

**Symptom:** "Test Twilio Connection" button shows an error

**Check 1 — Are credentials correct?**
- Account SID must start with `AC`
- Auth Token is 32 characters of letters/numbers
- Copy-paste directly from [console.twilio.com](https://console.twilio.com/) to avoid typos

**Check 2 — Does FPP have internet access?**
```bash
curl -s https://api.twilio.com --head | head -1
```
Expected: `HTTP/2 200` or similar. If it fails, check your network connection.

**Check 3 — Is your Twilio account in good standing?**
- Log into [console.twilio.com](https://console.twilio.com/) and check for any account alerts
- Free trial accounts expire — upgrade to a paid account if needed

---

## Messages Not Being Received

**Symptom:** You text your Twilio number but nothing happens

**Check 1 — Is the plugin enabled?**
- In the config page, verify **Enable Plugin** is checked and saved

**Check 2 — Is the phone number correct?**
- In the config, verify the Twilio Phone Number is in E.164 format: `+1XXXXXXXXXX`
- This must be the number you **receive** on (not your personal number)

**Check 3 — Is Twilio delivering the message?**
1. Log into [console.twilio.com](https://console.twilio.com/)
2. Go to **Monitor** → **Logs** → **Messaging**
3. Find your incoming message — if it shows an error, the issue is on the Twilio side

**Check 4 — Check the plugin log**
```bash
tail -f /home/fpp/media/logs/sms_plugin.log
```
Send a test text and watch the log in real time. You should see it being received.

**Check 5 — Is your Twilio trial account limiting senders?**
Free trial Twilio accounts can only send SMS **to** verified numbers. They can still **receive** from anyone — but if you're testing auto-responses, only verified numbers receive replies.

---

## Names Not Appearing on Display

**Symptom:** Messages are received but nothing shows on the LED display

**Check 1 — Is FPP connected?**
- In the config page, verify FPP IP/Port and click **Test FPP Connection**
- FPP must be running on the same device or accessible at the configured IP

**Check 2 — Is the Overlay Model configured?**
- In **FPP Display Settings**, an overlay model must be selected
- Click **🔄 Refresh** to reload the model list from FPP
- The selected model must exist in FPP: **Content Setup** → **Channel Outputs** → **Pixel Overlay Models**

**Check 3 — Is the name being filtered out?**
- Go to `http://YOUR_FPP_IP:5000/messages` and check the message status
- Status `blocked_whitelist` means whitelist is enabled and the name isn't in it
- Status `blocked_profanity` means the name hit the profanity filter
- Status `invalid_format` means the name format wasn't recognized

**Check 4 — Is the display playlist running?**
- Check that FPP is running and the "Name Displaying" playlist exists
- FPP must be in a running state (not stopped) for playlist switching to work

---

## Text Appears in Wrong Position

**Symptom:** Text shows at center or an unexpected position despite setting it on the canvas

**Check 1 — Confirm PIL is active and dimensions are saved**

Check the log for the `📐 Overlay` line emitted on every display:
```bash
grep "📐 Overlay" /home/fpp/media/logs/sms_plugin.log | tail -5
```
The line should show non-zero `overlay_size` and end with `PIL=True`. Example of a healthy line:
```
📐 Overlay: model=Texting Matrix overlay_size=128x32 canvas_pos=(10,12) mode=Center PIL=True
```

- If `overlay_size=0x0` — the model dimensions weren't saved. Open the config page, switch the Overlay Model dropdown to a different model and back again, then click **Save Configuration**.
- If `PIL=False` — Pillow is not installed. Run `bash scripts/fpp_install.sh` to install it.

**Check 2 — Check for shared memory permission errors**

```bash
grep -E "Permission denied|render_to_shm|scroll_via_shm" /home/fpp/media/logs/sms_plugin.log | tail -10
```
If you see `[Errno 13] Permission denied: '/dev/shm/FPP-Model-Data-...'`, the sudoers rule that allows the plugin to fix shm permissions may not be installed. Run `bash scripts/fpp_install.sh` to install it, then restart FPP.

**Check 3 — Verify the canvas position was saved**

On the config page, open the canvas preview and check the position display below the canvas. If it says **Position: center (auto)**, your drag was not saved — click **Save Configuration** after repositioning.

---

## Auto-Responses Not Sending

**Symptom:** Messages are received and names display, but senders don't get a reply

**Check 1 — Are auto-responses enabled?**
- In the **📱 SMS Responses** tab, verify **Enable SMS Auto-Responses** is checked and saved

**Check 2 — Is your Twilio trial limiting outgoing SMS?**
- Free trial accounts can only send SMS to **verified** numbers
- Upgrade your Twilio account to send to any number

**Check 3 — Check Twilio message logs**
1. Go to [console.twilio.com](https://console.twilio.com/)
2. **Monitor** → **Logs** → **Messaging** → **Outgoing**
3. Look for error codes on failed messages

**Check 4 — Is the plugin log showing errors?**
```bash
grep -i "response\|sms send\|error" /home/fpp/media/logs/sms_plugin.log | tail -20
```

---

## Whitelist / Blacklist Issues

**Symptom:** Names you removed keep reappearing, or removed words still get blocked

**This is expected behavior** — the whitelist and blacklist use `merge=union` git merging to protect your additions during updates. Removals are tracked separately.

If removals aren't sticking:
```bash
cat /opt/fpp/plugins/fpp-plugin-textmylights/whitelist_removed.txt
cat /opt/fpp/plugins/fpp-plugin-textmylights/blacklist_removed.txt
```
These files should contain the names/words you've removed. If they're missing or empty, try removing the name/word again via the web UI.

**Symptom:** Whitelist shows 0 names after an update

```bash
cd /opt/fpp/plugins/fpp-plugin-textmylights
git checkout -- whitelist.txt
```
This restores the default whitelist from git.

---

## Plugin Not Listed in FPP Plugin Manager

**Symptom:** After install, plugin doesn't appear in Plugin Manager, or says "already installed"

This can happen if the install was interrupted or the plugin was manually cloned. Try:

```bash
# Check if plugin directory exists
ls /opt/fpp/plugins/fpp-plugin-textmylights

# If it exists but isn't showing in FPP, restart FPP
sudo systemctl restart fppd

# Or try reinstalling
cd /opt/fpp/plugins/fpp-plugin-textmylights
bash scripts/fpp_install.sh
```

Then refresh the FPP Plugin Manager page.

---

## Checking Logs

The plugin log is the first place to look for any issue:

```bash
# View last 50 lines
tail -50 /home/fpp/media/logs/sms_plugin.log

# Follow log in real time
tail -f /home/fpp/media/logs/sms_plugin.log

# Search for errors
grep -i error /home/fpp/media/logs/sms_plugin.log

# Search for a specific phone number
grep "+15551234567" /home/fpp/media/logs/sms_plugin.log
```

The message history JSON is also useful:
```bash
cat /home/fpp/media/logs/received_messages.json | python3 -m json.tool | tail -100
```

---

## Resetting the Plugin

If you need a clean slate:

**Reset configuration only:**
```bash
rm /home/fpp/media/config/plugin.fpp-textmylights.json
# Restart the plugin — it will generate default config
```

**Clear message history:**
```bash
rm /home/fpp/media/logs/received_messages.json
```

**Clear blocked phones:**
```bash
echo '{}' > /opt/fpp/plugins/fpp-plugin-textmylights/blocked_phones.json
```

**Full reinstall:**
```bash
cd /opt/fpp/plugins
rm -rf fpp-plugin-textmylights
git clone https://github.com/thespica93/fpp-plugin-textmylights.git
cd fpp-plugin-textmylights
bash scripts/fpp_install.sh
```

---

## Still Need Help?

- **GitHub Issues:** [github.com/thespica93/fpp-plugin-textmylights/issues](https://github.com/thespica93/fpp-plugin-textmylights/issues)
- **FPP Community Forums:** [FalconChristmas.com](https://falconchristmas.com/)

When reporting an issue, please include:
- Your FPP version
- The relevant section of `/home/fpp/media/logs/sms_plugin.log`
- What you expected to happen vs. what actually happened
