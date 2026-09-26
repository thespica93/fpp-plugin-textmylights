# Profanity Blacklist

> Prevent inappropriate words from appearing on your display.

Access the Blacklist Manager at: **`http://YOUR_FPP_IP:5000/blacklist`**

---

## Table of Contents

- [What Is the Blacklist?](#what-is-the-blacklist)
- [Enabling the Profanity Filter](#enabling-the-profanity-filter)
- [The Default Blacklist](#the-default-blacklist)
- [Blacklist Manager Page](#blacklist-manager-page)
- [Adding Words](#adding-words)
- [Removing Words](#removing-words)
- [Searching the Blacklist](#searching-the-blacklist)
- [How Word Matching Works](#how-word-matching-works)
- [How Removals Are Tracked](#how-removals-are-tracked)
- [Blacklist File Location](#blacklist-file-location)

---

## What Is the Blacklist?

The blacklist (also called the profanity filter) is a list of **blocked words**. When enabled, any incoming message that contains one of these words is rejected before it reaches the display.

The sender receives a polite rejection SMS (if auto-responses are enabled) and the name is never queued.

---

## Enabling the Profanity Filter

1. Go to the [Plugin Configuration](03-plugin-configuration.md) page (`http://YOUR_FPP_IP:5000`)
2. In the **Filters** section, check **Enable Profanity Filter**
3. Click **💾 Save Configuration**

You can also enable/disable the filter directly from the Blacklist Manager page using the toggle button at the top.

![Blacklist enable toggle](images/blacklist-enable-toggle.png)

| Banner | Meaning |
|--------|---------|
| 🟡 Yellow warning | Filter is **disabled** — all words pass through |
| 🟢 Green banner | Filter is **enabled** — blacklisted words are blocked |

---

## The Default Blacklist

The plugin ships with a default blacklist covering common profanity and offensive words in English. The exact list is stored in `blacklist.txt` and can be viewed and edited on the Blacklist Manager page.

You can add any additional words specific to your situation — slurs, local slang, competitor names, etc.

---

## Blacklist Manager Page

![Blacklist manager page](images/blacklist-manager.png)

The Blacklist Manager shows:

- **Total count** of words currently blocked
- **File path** on disk
- **Enable/Disable toggle** — change the filter setting without leaving the page
- **Search bar** — filter the list in real time
- **Add word field** — add a new blocked word
- **2-column list** — all blocked words with Remove buttons, loads more as you scroll

---

## Adding Words

To add a word to the blacklist:

1. Type the word in the **Add a word...** field
2. Click **➕ Add**
3. The word appears in the blocked list immediately

Words are matched **case-insensitively** — blocking "badword" also blocks "BadWord" and "BADWORD".

> 💡 **Tips for adding words:**
> - Add common misspellings or l33t-speak variants if needed (e.g., if blocking "bad", also consider "b4d")
> - Keep entries as whole words — see [How Word Matching Works](#how-word-matching-works) below

---

## Removing Words

Each word in the list has a **✕ Remove** button.

Clicking it asks for confirmation, then removes the word. Messages containing that word will no longer be blocked.

> ⚠️ Like the whitelist, removals are tracked locally so they survive plugin updates. See [How Removals Are Tracked](#how-removals-are-tracked).

---

## Searching the Blacklist

The search bar filters words in real time as you type.

- Search is **case-insensitive**
- Results appear instantly
- Hint text shows how many words match

---

## How Word Matching Works

The profanity filter uses **whole-word matching** with regex. This means:

| Blocked Word | "classic" | Blocked? |
|-------------|-----------|---------|
| `ass` | "classic" | ✅ No — "ass" is inside a word |
| `ass` | "ass" (by itself) | 🚫 Yes — whole word match |
| `ass` | "big ass hat" | 🚫 Yes — surrounded by spaces |

This prevents **false positives** where a blocked word appears as part of a legitimate word (e.g., "ass" inside "classic", "class", "assassin", etc.).

The matching is:
- Case-insensitive
- Whole-word boundary aware (uses `\b` regex word boundaries)
- Applied to the full original message text (before name extraction)

---

## How Removals Are Tracked

Like the whitelist, the blacklist uses the same protection mechanism:

- `blacklist.txt` — the main file, tracked by git with `merge=union`
- `blacklist_removed.txt` — local gitignored file tracking your removals

When you remove a word via the web UI:
1. Word is removed from `blacklist.txt`
2. Word is added to `blacklist_removed.txt`

When plugin updates via `git pull`:
- `blacklist.txt` may restore the word via merge
- `blacklist_removed.txt` causes it to be filtered out again automatically

When you re-add a removed word:
1. Word re-added to `blacklist.txt`
2. Word removed from `blacklist_removed.txt`

---

## Blacklist File Location

| File | Path | Purpose |
|------|------|---------|
| Main blacklist | `/opt/fpp/plugins/fpp-plugin-textmylights/blacklist.txt` | Blocked words, one per line |
| Removals tracking | `/opt/fpp/plugins/fpp-plugin-textmylights/blacklist_removed.txt` | Words you've removed (gitignored) |

You can also edit `blacklist.txt` directly via SSH:
```bash
nano /opt/fpp/plugins/fpp-plugin-textmylights/blacklist.txt
```
One word per line, case-insensitive.

---

## Related Pages

- [Name Whitelist](05-whitelist.md) — Approve specific names
- [Phone Blocklist](07-phone-blocklist.md) — Block specific phone numbers
- [SMS Auto-Responses](08-sms-responses.md) — Customize the profanity rejection reply
- [Plugin Configuration](03-plugin-configuration.md) — Enable/disable the profanity filter
