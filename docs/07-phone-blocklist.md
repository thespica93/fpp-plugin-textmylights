# Phone Blocklist

> Block specific phone numbers from submitting names to your display.

Access the Phone Blocklist at: **`http://YOUR_FPP_IP:5000/blocklist`**

---

## Table of Contents

- [What Is the Phone Blocklist?](#what-is-the-phone-blocklist)
- [How to Block a Phone Number](#how-to-block-a-phone-number)
- [Viewing Blocked Numbers](#viewing-blocked-numbers)
- [Unblocking a Number](#unblocking-a-number)
- [How Blocking Works](#how-blocking-works)
- [Blocklist File Location](#blocklist-file-location)

---

## What Is the Phone Blocklist?

The phone blocklist is a list of **phone numbers** that are permanently banned from submitting names. When a blocked number texts your Twilio number, their message is silently ignored — no name is queued and no auto-response is sent.

This is useful for:
- Repeat spammers
- People submitting inappropriate names repeatedly
- Nuisance texters who are trying to game the system

Unlike the profanity filter and whitelist, the phone blocklist is **always active** — there is no enable/disable toggle. If a number is on the list, it's blocked, period.

---

## How to Block a Phone Number

Phone numbers are blocked from the **[Message Queue](04-message-queue.md)** page (`http://YOUR_FPP_IP:5000/messages`).

1. Find the message from the phone number you want to block
2. Click the **🚫 Block** button next to that message
3. A modal appears with two options:

   ![Block modal](images/queue-block-modal.png)

4. Select **"Block this phone number from texting again"**
5. Click **Block Phone**

The phone number is immediately added to the blocklist and all future texts from that number are ignored.

---

## Viewing Blocked Numbers

Go to **`http://YOUR_FPP_IP:5000/blocklist`** to see all currently blocked phone numbers.

![Phone blocklist page](images/blocklist-page.png)

The page shows:
- Each blocked phone number (partially masked for display)
- The date/time the number was blocked
- An **Unblock** button for each entry

You can also reach this page from the [Plugin Configuration](03-plugin-configuration.md) page — look for the **View Blocklist** button in the **Filters** section under the Phone Blocklist heading.

---

## Unblocking a Number

To unblock a phone number:

1. Go to `http://YOUR_FPP_IP:5000/blocklist`
2. Find the number you want to unblock
3. Click **Unblock**
4. Confirm — the number is removed from the list immediately

After unblocking, the number can text in and submit names again (subject to all other filters).

---

## How Blocking Works

When a message arrives from a blocked number:

1. The plugin checks the blocklist **before** any other filter
2. If the number is found, the message is **immediately discarded**
3. No auto-response is sent (to avoid confirming the block to the sender)
4. The blocked attempt is logged

The block check uses the phone number's **full E.164 format** (e.g., `+15551234567`) for an exact match. A blocked number cannot bypass the block by texting from a different Twilio region — the number must match exactly.

---

## Blocklist File Location

Blocked phone numbers are stored in:

```
/opt/fpp/plugins/fpp-plugin-textmylights/blocked_phones.json
```

This is a JSON file, for example:
```json
{
    "+15551234567": {
        "blocked_at": "2024-12-15T19:42:00",
        "reason": "manual block"
    },
    "+15559876543": {
        "blocked_at": "2024-12-20T21:15:33",
        "reason": "manual block"
    }
}
```

You can manually edit this file via SSH if needed, though the web UI is the recommended way to add/remove entries.

---

## Related Pages

- [Message Queue](04-message-queue.md) — Where you block phone numbers from
- [Name Whitelist](05-whitelist.md) — Block specific names (not numbers)
- [Profanity Blacklist](06-blacklist.md) — Block messages containing specific words
