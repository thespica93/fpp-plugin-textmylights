# Twilio Setup

> This guide walks you through creating a Twilio account, purchasing a phone number, and collecting the credentials needed for the plugin.

---

## Table of Contents

- [Step 1 — Create a Twilio Account](#step-1--create-a-twilio-account)
- [Step 2 — Get Your Credentials](#step-2--get-your-credentials)
- [Step 3 — Purchase a Phone Number](#step-3--purchase-a-phone-number)
- [Step 4 — Register Your Number (Required for SMS delivery)](#step-4--register-your-number-required-for-sms-delivery)
- [Step 5 — No Webhook Needed](#step-5--no-webhook-needed)
- [Twilio Free Trial Notes](#twilio-free-trial-notes)
- [Twilio Pricing](#twilio-pricing)

---

## Step 1 — Create a Twilio Account

1. Go to [twilio.com](https://www.twilio.com/) and click **Sign Up**
2. Enter your name, email, and password
3. Verify your email address
4. Verify your personal phone number (required by Twilio)
5. Answer the onboarding questions (select "SMS", "Receive messages", etc. — these don't affect functionality)

![Twilio signup page](images/twilio-signup.png)

---

## Step 2 — Get Your Credentials

Your **Account SID** and **Auth Token** are the two credentials the plugin uses to read your incoming messages.

1. After logging in, go to the **Twilio Console** home page: [console.twilio.com](https://console.twilio.com/)
2. You will see your credentials in the **Account Info** section on the dashboard

![Twilio dashboard with Account SID and Auth Token](images/twilio-dashboard-credentials.png)

| Field | Description |
|-------|-------------|
| **Account SID** | Starts with `AC...` — uniquely identifies your account |
| **Auth Token** | Secret key — keep this private, treat it like a password |

> ⚠️ **Never share your Auth Token publicly.** Anyone with your SID + Token can read your messages and send SMS from your number.

Copy both values — you will paste them into the plugin configuration.

---

## Step 3 — Purchase a Phone Number

### ⚠️ Important: US Carrier Registration Requirements

US carriers require all SMS sent from 10-digit local numbers to be registered under the **A2P 10DLC** program. Without registration, your outbound SMS replies will be blocked by carriers.

**We recommend purchasing a Toll-Free number instead.** Here's why:

| | Local Number | Toll-Free Number |
|---|---|---|
| Monthly cost | ~$1.00/month | ~$2.00/month |
| Registration fee | $4 one-time (brand) + ~$10/month (campaign) | **Free** |
| Approval time | 2–4 weeks | 1–3 business days |
| Best for | High-volume business use | ✅ Seasonal/event displays |

For a seasonal light show with low message volume, a toll-free number costs less overall and gets you up and running much faster.

### Buying a Toll-Free Number

1. In the Twilio Console, go to **Phone Numbers** → **Manage** → **Buy a Number**

   ![Twilio Buy a Number menu](images/twilio-buy-number-menu.png)

2. Click **Advanced Search** and check **Toll-free** under Number type (uncheck Local)

   ![Twilio number search](images/twilio-number-search.png)

3. Click **Search**, then click **Buy** next to your chosen number (~$2.00/month)

4. Once purchased, go to **Phone Numbers** → **Manage** → **Active Numbers** and click your number

   ![Twilio active number settings](images/twilio-active-number.png)

5. Note the phone number in **E.164 format**: `+1XXXXXXXXXX`

   You will enter this in the plugin settings as the **Twilio Phone Number**.

> 💡 **Tip:** The phone number field in the plugin accepts any common format (`+18885551234`, `+1 888 555 1234`, `+1-888-555-1234`) — it will be normalized automatically.

---

## Step 4 — Register Your Number (Required for SMS delivery)

### Toll-Free Verification (Recommended)

If you purchased a toll-free number, you must complete a one-time free verification before outbound SMS will be delivered:

1. In the Twilio Console, go to **Messaging** → **Regulatory Compliance** → **Toll-Free Verification**
2. Click **Submit Verification**
3. Complete the two-step form using the guidance below

---

#### Step 1/2 — Business and Contact Information

| Field | What to enter |
|-------|--------------|
| **Business profile** | Select your existing starter profile if shown |
| **Legal entity name** | Your full name (e.g. `John Smith`) — a formal business name is not required |
| **Website URL** | See options below |
| **Business type** | Select **Sole Proprietor** for a personal display |

**Website URL options** — Twilio requires a URL but it does not need to be a business website. Use whichever you have:

- A **Facebook profile or page** for your light show (e.g. `https://www.facebook.com/YourLightShow`)
- A **Nextdoor post** or neighborhood group page about your display
- A **YouTube video** of your display
- A **personal website or blog** mentioning the display
- The **plugin GitHub page**: `https://github.com/thespica93/fpp-plugin-textmylights` *(if you use this plugin as-is)*

Any public URL that references your display or the plugin is acceptable.

---

#### Step 2/2 — Messaging Use Case

| Field | What to enter |
|-------|--------------|
| **Opt-in type** | **Via Text** — visitors initiate the conversation by texting your number first, which constitutes consent |
| **Proof of consent URL** | Same URL you used above |
| **Use case description** | See suggested text below |
| **Sample message** | See suggested text below |

**Use case description** (copy and paste):
> Visitors at a seasonal holiday light display text their first name to this number. The name is then shown on an LED pixel display as part of the show. No marketing messages are sent. Participants opt in by initiating the text themselves.

**Sample message** (copy and paste):
> 🎄 Merry Christmas! Your name has been added to the display — watch for it on the lights!

---

4. Submit — approval typically takes **1–3 business days**

> **While you wait for approval:**
> - Inbound messages (people texting your number) work **immediately**
> - The display will show names as normal
> - Outbound SMS auto-replies back to texters will be blocked by carriers until verification is approved
>
> You can start testing your display right away — just disable SMS auto-responses in the plugin settings until verification completes.

### Local Number (10DLC) — Not Recommended

If you purchased a local (10-digit) number, you must complete A2P 10DLC registration before outbound SMS will work:

1. **Register a Brand** — Messaging → Senders → A2P 10DLC → Register a Brand (~$4 one-time)
2. **Register a Campaign** — describe your use case (~$10/month ongoing fee)
3. **Link your number** to the campaign

Approval takes 2–4 weeks and costs more over time. We recommend switching to a toll-free number instead.

---

## Step 5 — No Webhook Needed

Unlike many Twilio integrations, **this plugin does not use webhooks**. Instead, it polls the Twilio REST API at a regular interval to check for new messages.

This means:
- Your FPP device does **not** need to be publicly accessible from the internet
- You do **not** need to configure any webhook URL in Twilio
- The plugin simply reads your incoming message list on its own schedule

---

## Twilio Free Trial Notes

Twilio offers a free trial with ~$15 in credit. During the trial:

| Limitation | Details |
|------------|---------|
| Outgoing SMS | Can only be sent to **verified** phone numbers |
| Incoming SMS | Works from any number |
| "Powered by Twilio" prefix | May be added to outgoing messages |
| Credit expiry | Trial credit expires after ~30 days of inactivity |

For a public display, you will want to **upgrade to a paid account** so anyone can text in (not just verified numbers). Upgrading requires adding a credit card — typical cost is $2–5/month for a seasonal display.

---

## Twilio Pricing

All prices are approximate and subject to change. See [twilio.com/en-us/sms/pricing](https://www.twilio.com/en-us/sms/pricing) for current rates.

| Item | Toll-Free (Recommended) | Local (10DLC) |
|------|------------------------|---------------|
| Phone number rental | ~$2.00/month | ~$1.00/month |
| Registration | Free (one-time) | ~$4 one-time + ~$10/month |
| Incoming SMS (US) | $0.0075 per message | $0.0075 per message |
| Outgoing SMS (US) | $0.0079 per message | $0.0079 per message |

**Example seasonal display using toll-free (6 weeks, 200 texts/night, 42 nights):**
- 8,400 incoming SMS = $63.00
- 8,400 outgoing responses = $66.36 *(if auto-responses enabled)*
- Phone number = $3.00 (1.5 months at $2/month)
- Registration = $0
- **Total: ~$66–$132 for the season**

To reduce costs, you can **disable SMS auto-responses** in the plugin settings — visitors won't get a reply, but the display still works.

---

## Next Step

Once you have your **Account SID**, **Auth Token**, and **Phone Number**, proceed to:

[→ Installation](02-installation.md)
