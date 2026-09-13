# Message Formatting Guide

> How texts are read by the plugin — single names, two-word names, sending several names at once, and what gets ignored.

This page explains exactly how an incoming text is turned into one or more names on your display, so you know what to expect and what to tell your visitors.

Name format rules are set in the **⚙️ Configuration** tab at **`http://YOUR_FPP_IP:5000`** under **Name Format Rules**.

---

## Table of Contents

- [How a Text Becomes a Name](#how-a-text-becomes-a-name)
- [Name Format Rules](#name-format-rules)
- [Sending a Single Name](#sending-a-single-name)
- [Sending Multiple Names (Lists)](#sending-multiple-names-lists)
- [What Gets Ignored](#what-gets-ignored)
- [Auto-Responses](#auto-responses)
- [Twilio vs Google Voice](#twilio-vs-google-voice)
- [Related Pages](#related-pages)

---

## How a Text Becomes a Name

Every incoming text goes through the same steps:

1. **Greetings are stripped** from the front — `hi`, `hello`, `hey`, `merry christmas`, `happy holidays`. So *"Hi Alex"* becomes *"Alex"*.
2. **Emoji, punctuation, and symbols are removed.** Only letters, spaces, and hyphens are kept.
3. **The name is converted to Proper Case** — `alex` and `ALEX` both become `Alex`.
4. **The result is validated** against your Name Format Rules (below).
5. **Long messages are trimmed** to your **Max Message Length** (default 30 characters).

Hyphenated names like **"Mary-Jane"** count as a **single word**.

---

## Name Format Rules

In the **Configuration** tab, the **Name Format Rules** section has two mutually-exclusive toggles. Turning one on automatically turns the other off.

| Setting | Default | What's Accepted | What's Rejected |
|---------|---------|-----------------|-----------------|
| **One Word Only** | Off | `Alex` ✓ | `Mary Jane` ✗, sentences ✗ |
| **Two Words Maximum** | **On** | `Alex` ✓, `Mary Jane` ✓ | Sentences (3+ words) ✗ |
| *(Neither enabled)* | — | Any message up to Max Message Length | *Nothing is rejected on format* ⚠️ |

> ⚠️ **Leaving both off is not recommended** — visitors could send whole sentences that end up on your display. The plugin shows a warning in the UI when neither is enabled.

**When a name doesn't pass**, it's counted as **Invalid Format** and (if enabled) the *Invalid Format* auto-response is sent back.

### Whitelist mode overrides format rules

If the **Whitelist** is active, Name Format Rules are **disabled** — names are checked against your approved list instead of against word-count rules. See [Whitelist](05-whitelist.md).

---

## Sending a Single Name

The normal case. The texter sends one name:

```
Alex
```
```
Mary Jane        (allowed only if "Two Words Maximum" is on)
```
```
Mary-Jane        (hyphen = one word, always fine)
```

The name is validated, queued, and displayed. One name = one queue entry = one auto-response (per your response settings). All of your usual rules apply: rate limiting, duplicate detection, whitelist, and profanity.

---

## Sending Multiple Names (Lists)

A single text can add **several names at once** — this is detected automatically, there's no setting to turn on.

### How to separate names

Names must be separated by **commas** or **line breaks** — **never by spaces alone**. This is what keeps multi-word names intact.

**Commas — all on one line:**
```
Alex, Sam, Jordan, Taylor, Riley, Casey
```

**Line breaks — one per line:**
```
Alex
Sam
Jordan
Taylor
Riley
Casey
```

Both of the above add **6 names** to the display. Multi-word names stay together, because names are split **only** on commas and line breaks (not spaces):

| Text | Result |
|------|--------|
| `Mary Jane, Alex` | **2 names** — `Mary Jane` and `Alex` |
| `Mary-Jane, Alex` | **2 names** — `Mary-Jane` and `Alex` |

> A text is only treated as a group when it contains **two or more valid names**. A normal sentence that happens to contain a comma is **not** chopped into names. Up to **25 names** per text are processed.

### When is a group accepted? (All-or-nothing)

To keep the texter's experience simple, a grouped text is **all-or-nothing**. It's only accepted when your box is **fully open**, meaning **all** of these are true:

| Requirement | Setting |
|-------------|---------|
| No rate limiting | **Max Messages Per Phone** = `0` |
| Duplicates allowed | **Allow Duplicate Names** = **on** |
| Every name is valid | Each name passes your **Name Format Rules** — *or*, if the **Whitelist** is on, **every** name is on the whitelist |
| No profanity | No blocked word anywhere in the text |

**If all requirements are met** → every name is queued and the sender gets **one Success reply**.

**If any requirement is *not* met** → the **whole text** is rejected:

- Rate limiting is on, **or** duplicates are off, **or** any name fails format rules, **or** any name isn't on the whitelist → the sender gets your **Invalid Format** reply. This rejection **does not count** toward their daily message limit.
- Any profanity in the text → the sender gets your **Profanity** reply and nothing is queued.

> **Why all-or-nothing?** Splitting partial credit across a list (some names accepted, some rate-limited, some duplicates) made the replies confusing and hard to word. Instead, group texts are a simple "power user" feature that only works when there are no restrictions to reconcile. If you want per-person limits, duplicate blocking, or whitelist filtering, keep those on — texters just send **one name per message**, which always works.

---

## What Gets Ignored

Some replies are **not** name submissions and are silently dropped — **no auto-response is sent**:

- **Emoji-only replies** — e.g. 👍, ❤️, 😂 (any emoji or combination).
- **Phone reactions / "tapbacks"** — when someone reacts to your confirmation text, the phone sends it as text like `Loved "…"`, `Laughed at "…"`, `Emphasized "…"`, or `Reacted 😂 to "…"`.

These used to trigger an **Invalid Format** reply. They are now recognized as courtesy reactions and ignored, so texters aren't nagged for tapping a thumbs-up.

---

## Auto-Responses

- **Single name** → the matching per-type reply is sent (Success, Duplicate, Invalid Format, Rate Limited, Not on Whitelist, or Profanity). See [SMS Auto-Responses](08-sms-responses.md).
- **Multiple names (accepted)** → **one Success reply**.
- **Multiple names (rejected)** → **one Invalid Format reply** (restriction in place) or **one Profanity reply** (blocked word). No per-name breakdown is sent.

So a texter always gets **exactly one** reply, whether they sent one name or twenty.

---

## Twilio vs Google Voice

Both message sources handle single names, comma lists, and line-break lists **identically**. The plugin reads the sender's text the same way regardless of source, and replies are delivered automatically over whichever source is active (Twilio API or a Google Voice email reply).

---

## Related Pages

- [Plugin Configuration](03-plugin-configuration.md) — Name Format Rules, Max Message Length, Max Messages Per Phone
- [Message Queue](04-message-queue.md) — How queued names are displayed and monitored
- [SMS Auto-Responses](08-sms-responses.md) — Customize the replies texters receive
- [Whitelist](05-whitelist.md) — Restrict the display to pre-approved names
