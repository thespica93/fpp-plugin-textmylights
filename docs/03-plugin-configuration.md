# Plugin Configuration

> Full reference for every setting in the plugin web interface.

Access the configuration page at: **`http://YOUR_FPP_IP:5000`**

---

## Table of Contents

- [Settings Overview](#settings-overview)
- [Twilio Settings](#twilio-settings)
- [FPP Display Settings](#fpp-display-settings)
- [Message Settings](#message-settings)
- [FPP Connection](#fpp-connection)
- [Filters](#filters)
- [Text Display Options](#text-display-options)
- [Top Action Bar](#top-action-bar)

---

## Settings Overview

The configuration page is organized into two columns:

| Left Column | Right Column |
|-------------|--------------|
| Twilio Settings | FPP Connection |
| FPP Display Settings | Filters |
| Message Settings | Text Display Options |

There are also separate tabs for **SMS Responses** and **Testing**.

![Plugin configuration page overview](images/plugin-config-overview.png)

---

## Twilio Settings

![Twilio settings section](images/config-twilio-settings.png)

| Field | Description | Example |
|-------|-------------|---------|
| **Account SID** | From your Twilio console dashboard | `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| **Auth Token** | Secret key from your Twilio dashboard | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| **Twilio Phone Number** | Your Twilio number in E.164 format | `+18005551234` |
| **Poll Interval (seconds)** | How often the plugin checks Twilio for new messages | `10` |

### Test Twilio Connection

After entering your credentials, click **🔌 Test Twilio Connection** to verify the plugin can reach your Twilio account. A success or failure message will appear directly below the button.

> 💡 **Poll Interval:** Lower values (5–10 seconds) mean names appear faster after texting. Higher values (30–60 seconds) reduce Twilio API calls. For a busy display, 10 seconds is a good balance.

---

## FPP Display Settings

![FPP Display settings section](images/config-fpp-display.png)

| Field | Description |
|-------|-------------|
| **Default "Waiting" Playlist** | The playlist FPP plays when the queue is empty (your normal show). Select from the dropdown after clicking Refresh. |
| **"Name Displaying" Playlist** | The playlist FPP switches to when a name is being displayed. Can be the same as your waiting playlist, or a special one. |
| **Overlay Model** | The FPP Overlay Model (LED matrix) where the name text will appear. Select from dropdown after clicking Refresh. |
| **Display Duration (seconds)** | How long each name stays on screen before moving to the next in queue. |
| **Clear Overlay After Display** | Whether to clear the overlay model text when the name finishes displaying. |

### Refresh Playlists / Models / Fonts

Click **🔄 Refresh Playlists/Models/Fonts** in the top bar to reload the available options from your FPP device. Do this any time you add new playlists or overlay models in FPP.

---

## Message Settings

![Message settings section](images/config-message-settings.png)

| Field | Description | Default |
|-------|-------------|---------|
| **Message Template** | The text displayed on the LED. Use `{name}` as a placeholder for the sender's name. | `Merry Christmas {name}!` |
| **Enable Plugin** | Master on/off switch. When disabled, the plugin stops polling and displaying. | Off |
| **Max Messages Per Phone Per Day** | Rate limit — how many times the same phone number can submit a name per day. Set to `0` to disable rate limiting. | `3` |

### Message Template Examples

| Template | Displayed Result |
|----------|-----------------|
| `Merry Christmas {name}!` | `Merry Christmas Sarah!` |
| `Happy Holidays {name}` | `Happy Holidays Mike` |
| `{name} loves Christmas!` | `Emily loves Christmas!` |
| `Hi {name}! 🎄` | `Hi Jake! 🎄` |

---

## FPP Connection

![FPP Connection section](images/config-fpp-connection.png)

| Field | Description | Default |
|-------|-------------|---------|
| **FPP IP Address** | The IP address of your FPP device. Usually `localhost` or `127.0.0.1` if the plugin runs on the same device as FPP. | `localhost` |
| **FPP Port** | The port FPP's web API listens on. | `80` |

After entering these, use **🔌 Test FPP Connection** to verify the plugin can communicate with FPP.

---

## Filters

![Filters section](images/config-filters.png)

The Filters section has three sub-sections:

### Profanity Filter

| Setting | Description |
|---------|-------------|
| **Enable Profanity Filter** | When enabled, any message containing a blocked word is rejected and the sender receives a polite rejection SMS. |
| **Manage Blacklist** | Opens the [Profanity Blacklist](06-blacklist.md) management page where you can add or remove words. |

> If disabled, **all words will be shown** regardless of the blacklist. A warning banner is shown on the blacklist management page when this is off.

### Name Whitelist

| Setting | Description |
|---------|-------------|
| **Enable Whitelist** | When enabled, only names present in `whitelist.txt` are allowed. All other names are rejected. |
| **Manage Whitelist** | Opens the [Name Whitelist](05-whitelist.md) management page. |

> The plugin ships with a whitelist of **22,000+ common first names**. If disabled, **any name will be accepted** (subject to profanity filter and format rules).

### Phone Blocklist

| Setting | Description |
|---------|-------------|
| **View Blocklist** | Opens the [Phone Blocklist](07-phone-blocklist.md) management page where you can view and remove blocked phone numbers. |

> Phone numbers are added to the blocklist by clicking **Block** on the [Message Queue](04-message-queue.md) page.

---

## Text Display Options

![Text Display Options section](images/config-text-display.png)

| Setting | Description |
|---------|-------------|
| **Font** | The font used for overlay text. Select from available FPP fonts in the dropdown. |
| **Font Size** | Text size in pixels. |
| **Font Color** | Hex color for the text (e.g., `#FFFFFF` for white). |
| **Background Color** | Hex color for the text background (e.g., `#000000` for black, or transparent). |
| **Text Position** | How the text is displayed on the overlay. See modes below. |
| **Scroll Speed** | Pixels per second for scroll modes. Only shown when a scroll mode is selected. |
| **One Word Only** | If checked, only single-word names are accepted (e.g., "Sarah" but not "Sarah M"). |
| **Two Words Max** | If checked, names up to two words are accepted. *Mutually exclusive with One Word Only.* |

> 💡 **One Word Only** and **Two Words Max** are mutually exclusive — checking one unchecks the other automatically.

### Text Position Modes

| Mode | Description |
|------|-------------|
| **Static** | Text is rendered at a fixed position you set. Drag the canvas to place text anywhere on the overlay. |
| **Scroll Left to Right** | Text enters from off the left edge and scrolls right across the full width. Set the vertical (Y) position. |
| **Scroll Right to Left** | Text enters from off the right edge and scrolls left across the full width. Set the vertical (Y) position. |
| **Scroll Top to Bottom** | Text enters from above the top edge and scrolls down the full height. Set the horizontal (X) position. |
| **Scroll Bottom to Top** | Text enters from below the bottom edge and scrolls up the full height. Set the horizontal (X) position. |

> 💡 **Pixel-accurate rendering:** All modes use Pillow (PIL) to write text directly into FPP's shared memory at the exact pixel coordinates you set — bypassing the FPP text API and giving you full control over placement. Pillow is installed automatically by `fpp_install.sh`.

### Canvas Position Preview

Below the Text Position dropdown is a live canvas preview of your overlay model at its correct aspect ratio. Use it to visually position the text before displaying it.

| Control | Description |
|---------|-------------|
| **Canvas drag** | Click and drag the preview text to move it. The cursor changes to indicate which axes are available. |
| **Vertical scrollbar** (right of canvas) | Drag or click to set the Y position (up/down). Shown for **Static** and **Left/Right scroll** modes. |
| **Horizontal scrollbar** (below canvas) | Drag or click to set the X position (left/right). Shown for **Static** and **Top/Bottom scroll** modes. |
| **Reset to Auto** | Clears your custom position and returns to auto-centered placement. |
| **Preview as** | Type any name here to preview your full message template on the canvas. Defaults to `Santa`. |

**Which controls are active per mode:**

| Mode | Canvas drag | V scrollbar | H scrollbar |
|------|-------------|-------------|-------------|
| Static | X and Y free | Shown | Shown |
| Left to Right / Right to Left | Y only | Shown | Hidden |
| Top to Bottom / Bottom to Top | X only | Hidden | Shown |

> 💡 **Position settings persist through firmware and plugin updates.** X/Y coordinates are saved to `/home/fpp/media/config/plugin.fpp-textmylights.json`, which is never touched by FPP or plugin updates.

---

## Top Action Bar

![Top action bar](images/config-action-bar.png)

| Button | Action |
|--------|--------|
| **💾 Save Configuration** | Saves all current settings to disk. Always click this after making changes. |
| **🔄 Refresh Playlists/Models/Fonts** | Reloads playlist, overlay model, and font lists from FPP. |
| **📋 View Message Queue** | Opens the [Message Queue](04-message-queue.md) page. |

---

## SMS Responses Tab

The **📱 SMS Responses** tab lets you customize the automatic text replies sent back to visitors. See the full reference in [SMS Auto-Responses](08-sms-responses.md).

---

## Testing Tab

The **🧪 Testing** tab lets you simulate an incoming SMS without actually texting from a phone. Useful for testing your display setup without using Twilio credits.

| Field | Description |
|-------|-------------|
| **Test Name** | The name to inject into the queue |
| **Test Phone** | A fake phone number for rate-limit tracking |

Click **▶ Send Test Message** to add it to the queue immediately.

---

## Next Steps

- [Message Queue](04-message-queue.md) — Monitor and manage incoming names
- [Whitelist](05-whitelist.md) — Control which names are allowed
- [Profanity Blacklist](06-blacklist.md) — Block inappropriate words
