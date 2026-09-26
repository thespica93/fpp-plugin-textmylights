# Name Whitelist

> Control exactly which names are allowed to appear on your display.

Access the Whitelist Manager at: **`http://YOUR_FPP_IP:5000/whitelist`**

---

## Table of Contents

- [What Is the Whitelist?](#what-is-the-whitelist)
- [Enabling the Whitelist](#enabling-the-whitelist)
- [The Default Whitelist](#the-default-whitelist)
- [Whitelist Manager Page](#whitelist-manager-page)
- [Adding Names](#adding-names)
- [Removing Names](#removing-names)
- [Searching the Whitelist](#searching-the-whitelist)
- [How Removals Are Tracked](#how-removals-are-tracked)
- [Whitelist File Location](#whitelist-file-location)

---

## What Is the Whitelist?

The whitelist is a list of **approved names**. When enabled, only names found in this list will be displayed. Any name texted in that is **not** in the whitelist is rejected and the sender receives a "not on whitelist" auto-response.

**Without whitelist enabled:** Any name that passes the profanity filter is accepted.

**With whitelist enabled:** Only pre-approved names are accepted — you have full control over what appears on your display.

---

## Enabling the Whitelist

1. Go to the [Plugin Configuration](03-plugin-configuration.md) page (`http://YOUR_FPP_IP:5000`)
2. In the **Filters** section, check **Enable Whitelist**
3. Click **💾 Save Configuration**

You can also enable/disable the whitelist directly from the Whitelist Manager page using the toggle button at the top.

![Whitelist enable toggle](images/whitelist-enable-toggle.png)

| Banner | Meaning |
|--------|---------|
| 🟡 Yellow warning | Whitelist is **disabled** — all names pass through |
| 🟢 Green banner | Whitelist is **enabled** — only listed names are shown |

---

## The Default Whitelist

The plugin ships with a whitelist of over **22,000 common first names** from English and many other languages, including international names with accented characters (é, ñ, ü, etc.).

This means most visitors can text their real first name and have it appear without any configuration — you only need to add unusual or uncommon names.

The default list covers names like: Aaron, Aaliyah, Abigail, Adam, Adriana, Ahmad, Aiden, Alan, Alexandra, ... and thousands more across many cultural backgrounds.

---

## Whitelist Manager Page

![Whitelist manager page](images/whitelist-manager.png)

The Whitelist Manager shows:

- **Total count** of names currently in the list
- **File path** on disk (with ✓ if the file exists)
- **Enable/Disable toggle** — change the whitelist setting without leaving the page
- **Search bar** — filter the list in real time
- **Add name field** — add a new name
- **2-column list** — all names with Remove buttons, loads more as you scroll

---

## Adding Names

To add a name that isn't in the default list:

1. Type the name in the **Add a name...** field at the top
2. Click **➕ Add**
3. The name appears in the list immediately

Names are stored case-insensitively — adding "sarah", "Sarah", or "SARAH" all result in the same entry.

> 💡 **When to add names:**
> - Unusual spellings (e.g., "Kaytlyn" instead of "Caitlin")
> - Nicknames not in the default list (e.g., "Bud", "Skip")
> - Non-English names not covered by the default list
> - Surnames if you want to allow "John Smith" as a two-word name

---

## Removing Names

Each name in the list has a **✕ Remove** button.

Clicking it asks for confirmation, then removes the name from the whitelist. The name will be rejected if texted in the future.

> ⚠️ **Removing from the web UI is permanent** (until you manually re-add the name). See [How Removals Are Tracked](#how-removals-are-tracked) below.

You can also remove a name directly from the [Message Queue](04-message-queue.md) page by clicking **🚫 Block** → **Block name from display**.

---

## Searching the Whitelist

The search bar at the top of the page filters names in real time as you type.

![Whitelist search](images/whitelist-search.png)

- Search is **case-insensitive**
- Results show instantly — no need to press Enter
- The hint text shows how many names match

This is especially useful with 22,000+ names — searching for "sarah" instantly finds all Sarah variants.

---

## How Removals Are Tracked

The whitelist is stored in a git-tracked file (`whitelist.txt`). Without special handling, a `git pull` (plugin update) could potentially restore names you removed.

To prevent this, **removals are tracked in a separate local file** (`whitelist_removed.txt`). This file is:
- Stored alongside `whitelist.txt` in the plugin directory
- **Not committed to git** (in `.gitignore`)
- Automatically applied every time the whitelist loads

When you remove a name via the web UI:
1. The name is removed from `whitelist.txt`
2. The name is added to `whitelist_removed.txt`

When a plugin update runs `git pull`:
- `whitelist.txt` may restore the name (due to `merge=union`)
- But `whitelist_removed.txt` still exists and the name is filtered out again

When you re-add a name via the web UI:
1. The name is added back to `whitelist.txt`
2. The name is removed from `whitelist_removed.txt` so it's no longer suppressed

---

## Whitelist File Location

| File | Path | Purpose |
|------|------|---------|
| Main whitelist | `/opt/fpp/plugins/fpp-plugin-textmylights/whitelist.txt` | List of approved names, one per line |
| Removals tracking | `/opt/fpp/plugins/fpp-plugin-textmylights/whitelist_removed.txt` | Names you've removed (gitignored) |

You can also edit `whitelist.txt` directly via SSH if needed:
```bash
nano /opt/fpp/plugins/fpp-plugin-textmylights/whitelist.txt
```
One name per line, case-insensitive.

---

## Related Pages

- [Profanity Blacklist](06-blacklist.md) — Block specific words
- [Phone Blocklist](07-phone-blocklist.md) — Block specific phone numbers
- [SMS Auto-Responses](08-sms-responses.md) — Customize the "not on whitelist" reply
- [Plugin Configuration](03-plugin-configuration.md) — Enable/disable whitelist
