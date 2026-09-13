# SMS Auto-Responses

> Customize the automatic text messages sent back to your visitors.

Configure auto-responses in the **📱 SMS Responses** tab at: **`http://YOUR_FPP_IP:5000`**

---

## Table of Contents

- [Overview](#overview)
- [Enabling Auto-Responses](#enabling-auto-responses)
- [Response Types](#response-types)
- [Customizing Messages](#customizing-messages)
- [Available Placeholders](#available-placeholders)
- [Example Messages](#example-messages)
- [Twilio Cost Consideration](#twilio-cost-consideration)

---

## Overview

When a visitor texts your Twilio number, the plugin can automatically send them a reply SMS. This improves the experience — visitors know whether their name was accepted, why it was rejected, and what to do differently.

Auto-responses are **optional**. If disabled, visitors receive no reply but their name still goes through the normal queue process.

---

## Enabling Auto-Responses

1. Go to `http://YOUR_FPP_IP:5000`
2. Click the **📱 SMS Responses** tab
3. Check **Enable SMS Auto-Responses**
4. Click **💾 Save Configuration**

When enabled, a response is sent for **every** incoming message — success and failure alike.

---

## Response Types

There are **seven** response scenarios, each with its own customizable message:

| Response Type | When It's Sent |
|--------------|----------------|
| **Success** | Name was accepted and added to the display queue |
| **Blocked (Profanity)** | Message contained a word from the profanity blacklist |
| **Rate Limited** | Sender has already reached the daily message limit |
| **Duplicate Name** | Same name from the same phone was already submitted today |
| **Invalid Format** | Message didn't contain a recognizable name |
| **Not on Whitelist** | Name was not found in the approved whitelist |
| **Phone Blocked** | *(No response sent — blocked numbers are silently ignored)* |

---

## Customizing Messages

Each response type has a text box where you can write your custom message.

![SMS Responses tab](images/sms-responses-tab.png)

To edit a response:
1. Go to the **📱 SMS Responses** tab
2. Find the response type you want to change
3. Edit the text in the box
4. Click **💾 Save Configuration** when done

Messages can be up to ~160 characters for a single SMS segment (longer messages cost more with Twilio).

---

## Available Placeholders

Use these placeholders in your response messages — they are replaced automatically with real values:

| Placeholder | Replaced With | Available In |
|-------------|---------------|-------------|
| `{name}` | The extracted name from the message | Success, Duplicate, Blocked |
| `{phone}` | The sender's phone number | All types |
| `{limit}` | The daily message limit number | Rate Limited |
| `{queue_position}` | Position in the display queue | Success |
| `{words}` | Your current word limit — `1 word` or `2 words` | Invalid Format |

---

## Example Messages

Below are suggested messages for each response type. Feel free to personalize them for your display's theme!

### Success
```
🎄 Ho ho ho! "{name}" has been added to our display! Watch for it on the lights! Merry Christmas! 🎄
```

### Blocked (Profanity)
```
Sorry, your message contained inappropriate content and could not be displayed. Please try a different name!
```

### Rate Limited
```
You've reached today's limit of {limit} messages. Thanks for participating! Check back tomorrow! 🎄
```

### Duplicate Name
```
"{name}" has already been displayed today from your number. Try texting a different name!
```

### Invalid Format
```
Please send only 1 name ({words}, no sentences).
```
`{words}` automatically becomes **"1 word"** or **"2 words"** to match your current Name Format Rule, so the reply always tells texters the right limit.

### Not on Whitelist
```
Sorry, "{name}" isn't in our approved name list. Try a different name, or ask a helper at the display!
```

---

## Twilio Cost Consideration

Each auto-response costs approximately **$0.0079** per SMS (US rates). For a busy display:

| Volume | Auto-Response Cost |
|--------|-------------------|
| 100 messages/night | $0.79/night |
| 500 messages/night | $3.95/night |
| 200 messages × 42 nights | $66.36/season |

To reduce costs:
- **Disable auto-responses** — visitors won't know if their name was accepted, but the display still works
- **Selective responses** — if you only want success responses (not rejection messages), that's a future customization option

---

## Related Pages

- [Plugin Configuration](03-plugin-configuration.md) — Enable/disable auto-responses
- [Message Queue](04-message-queue.md) — Monitor message statuses
- [Troubleshooting](09-troubleshooting.md) — Responses not sending?
