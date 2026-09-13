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
- [Rate Limiting with Multiple Names](#rate-limiting-with-multiple-names)
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

The name is validated, queued, and displayed. One name = one queue entry = one auto-response (per your response settings).

---

## Sending Multiple Names (Lists)

A single text can add **several names at once**. This is detected automatically — no setting to turn on.

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

Both of the above add **6 names** to the display.

### Multi-word names stay together

Because names are split **only** on commas and line breaks (not spaces):

| Text | Result |
|------|--------|
| `Mary Jane, Alex` | **2 names** — `Mary Jane` and `Alex` |
| `Mary-Jane, Alex` | **2 names** — `Mary-Jane` and `Alex` |
| `Mary Jane, Sam` | **2 names** — `Mary Jane` and `Sam` |

> Note: multi-word names like `Mary Jane` require **Two Words Maximum** to be on. If **One Word Only** is set, each two-word entry in a list is rejected as Invalid Format while the single-word names still go through.

### How lists behave

- Each name becomes its **own queue entry** and displays **one at a time**, in order.
- A text is only treated as a list when it contains **two or more valid names**. A normal sentence that happens to contain a comma is **not** chopped into names.
- A single name (no commas or line breaks) always uses the normal single-name path — nothing changes.
- Up to **25 names** per text are processed (extra names beyond that are ignored).
- Each name is checked individually for duplicates, format, whitelist, and profanity — so some names in a list can be accepted while others are skipped.

---

## What Gets Ignored

Some replies are **not** name submissions and are silently dropped — **no auto-response is sent**:

- **Emoji-only replies** — e.g. 👍, ❤️, 😂 (any emoji or combination).
- **Phone reactions / "tapbacks"** — when someone reacts to your confirmation text, the phone sends it as text like `Loved "…"`, `Laughed at "…"`, `Emphasized "…"`, or `Reacted 😂 to "…"`.

These used to trigger an **Invalid Format** reply. They are now recognized as courtesy reactions and ignored, so texters aren't nagged for tapping a thumbs-up.

---

## Rate Limiting with Multiple Names

If **Max Messages Per Phone** is set (Configuration tab), **each name counts as one message** toward that daily limit — not the text as a whole.

Example — limit of **5**, sender has already sent **3** today, then texts `A, B, C, D`:

| Name | Result |
|------|--------|
| A | ✅ Queued (4 of 5 used) |
| B | ✅ Queued (5 of 5 used) |
| C | ⛔ Skipped — daily limit reached |
| D | ⛔ Skipped — daily limit reached |

Set **Max Messages Per Phone** to `0` to turn rate limiting off entirely.

---

## Auto-Responses

- **Single name** → the matching per-type reply is sent (Success, Duplicate, Invalid Format, etc.). See [SMS Auto-Responses](08-sms-responses.md).
- **Multiple names** → **one combined summary reply** is sent instead of one text per name, for example:

  ```
  ✅ Added 5: Alex, Sam, Jordan, Taylor, Casey. ⚠️ Skipped: Riley (already sent today)
  ```

  The summary is sent only when the **Success** auto-response is enabled. If you have auto-responses off, no summary is sent (names are still queued).

Skipped names are grouped by reason: *already sent today*, *not on the list*, *not allowed* (profanity), *not a valid name*, and *daily limit reached*.

---

## Twilio vs Google Voice

Both message sources handle single names, comma lists, and line-break lists **identically**. The plugin reads the sender's text the same way regardless of source, and replies are delivered automatically over whichever source is active (Twilio API or a Google Voice email reply).

---

## Related Pages

- [Plugin Configuration](03-plugin-configuration.md) — Name Format Rules, Max Message Length, Max Messages Per Phone
- [Message Queue](04-message-queue.md) — How queued names are displayed and monitored
- [SMS Auto-Responses](08-sms-responses.md) — Customize the replies texters receive
- [Whitelist](05-whitelist.md) — Restrict the display to pre-approved names
