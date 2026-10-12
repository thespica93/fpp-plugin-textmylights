# Message Queue

> Monitor incoming messages, view history, and manage individual senders.

Open the Message Queue from the plugin UI (**Content Setup → Text My Lights → View Message Queue**).

---

## Table of Contents

- [How the Queue Works](#how-the-queue-works)
- [Queue Page Overview](#queue-page-overview)
- [Message Statuses](#message-statuses)
- [Name Validation Rules](#name-validation-rules)
- [Blocking a Sender](#blocking-a-sender)
- [Message History](#message-history)

---

## How the Queue Works

```
Incoming SMS
     │
     ▼
┌─────────────────────┐
│  Validation & Filters│  ← profanity, whitelist, rate limit, format
└─────────────────────┘
     │ approved
     ▼
┌─────────────────────┐
│     Message Queue    │  ← FIFO, one displayed at a time
└─────────────────────┘
     │ front of queue
     ▼
┌─────────────────────┐
│   FPP Display        │  ← switches playlist + shows overlay text
└─────────────────────┘
     │ after display duration
     ▼
  Next in queue (or return to waiting playlist)
```

**Key behaviors:**
- Messages display **one at a time** — no overlap, no interruptions
- The queue is **FIFO** (first in, first out)
- While a name is displaying, FPP switches to your "Name Displaying" playlist
- After the display duration, FPP returns to your "Waiting" playlist (or immediately shows the next queued name)
- The queue persists across page refreshes but is **cleared on plugin restart**

---

## Queue Page Overview

![Message queue page](images/queue-page-overview.png)

The queue page shows:

1. **Currently Displaying** — the name on screen right now (if any)
2. **Queue** — names waiting to be displayed, in order
3. **Recent Messages** — all received messages with their status

---

## Message Statuses

Each received message has a status indicating what happened to it:

| Status | Icon | Meaning |
|--------|------|---------|
| `queued` | 🕐 | Approved and waiting to display |
| `displaying` | ✨ | Currently on screen |
| `displayed` | ✅ | Successfully shown |
| `blocked_profanity` | 🚫 | Contained a blacklisted word |
| `blocked_whitelist` | 🚫 | Name not in the approved whitelist |
| `blocked_phone` | 🚫 | Phone number is on the blocklist |
| `rate_limited` | ⏱ | Sender hit the daily message limit |
| `duplicate` | 🔁 | Same name from same phone already shown today |
| `invalid_format` | ❌ | Name didn't pass format validation |
| `not_whitelisted` | ❌ | Name not in whitelist (when whitelist enabled) |

---

## Name Validation Rules

Before a name is accepted, it must pass these checks (in order):

1. **Phone is not blocked** — phone number not in blocklist
2. **Rate limit** — sender hasn't exceeded daily message limit
3. **Format validation** — message must contain a valid name
4. **Duplicate check** — same name from same phone not already received today
5. **Profanity filter** *(if enabled)* — no blacklisted words in the name
6. **Whitelist check** *(if enabled)* — name must be in the approved whitelist

### What counts as a valid name?

- Letters only (no numbers, symbols, or excessive punctuation)
- 1 word (if "One Word Only" is set) or up to 2 words (if "Two Words Max" is set)
- Common greeting words at the start are stripped (e.g., "Hi Sarah" → "Sarah")
- Must be between 2 and 30 characters

### Greeting Words Stripped Automatically

The plugin strips common lead-in words before extracting the name:

> "Hi my name is Sarah" → **Sarah**
> "Please display John" → **John**
> "Merry Christmas from Mike" → **Mike**

---

## Blocking a Sender

On the message queue page, each message has a **🚫 Block** button. Clicking it opens a modal with two options:

![Block modal](images/queue-block-modal.png)

### Option 1: Block Phone Number

Adds the sender's phone number to the **Phone Blocklist**. All future texts from this number will be rejected immediately.

Use this for: spammers, repeated inappropriate submissions, or anyone abusing the system.

### Option 2: Block Name from Display

Removes the name from the **Whitelist** and adds it to the removal tracking file. The name will not appear even if it is texted in again.

> ⚠️ **Warning:** If the whitelist is not enabled, blocking a name from display will remove it from the whitelist file, but the name will still be accepted (since the whitelist filter is not active). A warning is shown in the modal when this is the case.

Use this for: names that slipped through the profanity filter, or names you simply don't want shown.

---

## Message History

The queue page shows a history of all received messages during the current plugin session.

Each entry shows:
- Sender's phone number (partially masked for privacy)
- The extracted name
- Timestamp received
- Status (displayed, blocked, etc.)
- Block button

Message history is also saved to disk at:
```
/home/fpp/media/plugindata/fpp-plugin-textmylights/logs/messages/
```

This file persists between plugin restarts and can be viewed for a full history of the season.

---

## Related Pages

- [Phone Blocklist](07-phone-blocklist.md) — View and manage blocked phone numbers
- [Whitelist](05-whitelist.md) — Manage approved names
- [Plugin Configuration](03-plugin-configuration.md) — Adjust queue behavior settings
