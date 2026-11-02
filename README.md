# AI Outreach Agent

A personal tool for running cold email outreach from your lead sheet. With it you can:

1. **Import** your leads from the `.xlsx` file or a Google Sheets link. It reads the Dubai Real Estate and Law Firms tabs (300 leads).
2. **See each client's data**: decision-maker, email, why they need AI, and how you can help them.
3. **Draft emails** using the ready-made sheet email, or have Claude write a fresh personalised one (✨ Write with AI). You can add instructions such as "shorter, mention Arabic support".
4. **Send**: send one email now, or queue many. The queue respects a daily limit and spaces emails out to protect deliverability.
5. **Track** emails: sent, opened (optional), replied, auto-replies, bounces and opt-outs. Replies are found by scanning your inbox.
6. **Follow up**: a 3-step sequence (first email, follow-up 1 on day 3, follow-up 2 about 5 days later) sent **in the same thread**. A lead that replies is taken out of the sequence automatically.
7. **Lead Scraper agent**: builds new leads in the same format as your Excel. Paste a list of websites, or describe the businesses you want (e.g. "immigration law firms in Manchester") and Claude finds them. See section 3.
8. **Export** your pipeline back into your original workbook, with the `Outreach status` and `Notes` columns filled in.

Nothing is sent without you clicking **Send now** or **Queue**.

---

## 1. Setup (one time, about 10 minutes)

You need **Python 3.10+** installed.

```bash
cd ai-outreach-agent
./run.sh            # Windows: run.bat
```

The first run creates a `.env` file. Open it and fill in the following:

| Setting | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |
| `SMTP_USER` / `FROM_EMAIL` | The mailbox you send from (ideally your business domain, not @gmail.com) |
| `SMTP_PASSWORD` | Google Workspace/Gmail: turn on 2-Step Verification, then create an **App Password** at https://myaccount.google.com/apppasswords. Use that 16-character password, not your normal one. |
| `IMAP_HOST` | Leave as `imap.gmail.com` for Gmail/Workspace (turn on IMAP in Gmail settings → Forwarding and POP/IMAP) |

Then run `./run.sh` again and open **http://127.0.0.1:5050**.

> On a Mac, don't use port 5000: macOS AirPlay Receiver uses it and answers "Access to 127.0.0.1 was denied (403)". The app uses 5050 by default; set `PORT=` in `.env` to change it.

The three dots in the top bar (AI / Send / Inbox) turn green when each connection is configured. Use **Settings → Send test email** to check that sending works.

## 2. Daily workflow

1. **Import** (first time only): upload `AI_Leads_Dubai_RealEstate_and_Law_Firms.xlsx`.
2. **Settings**: check your signature, postal address (required by CASL for Canadian leads) and "About you" text. Claude uses "About you" when it writes.
3. On **Leads**, filter by *Priority A*. Tick 20–30 leads, then click **Draft from sheet** or **✨ Draft with AI**.
4. Open each lead, read the draft, edit it, and confirm the person on LinkedIn. Leads with a ⚠ have a "check before sending" note.
5. Tick the leads you've reviewed and click **Queue drafts to send**. They go out one at a time with a random 90–240s gap, up to your daily limit (default 30).
6. Click **↻ Check replies** each morning. It finds replies (status becomes *Replied*, follow-ups stop), bounces, out-of-office messages and "unsubscribe" requests.
7. Open **Follow-ups due**. Select the leads, click **Draft from sheet** or **Draft with AI**, then **Queue**. Follow-ups are threaded as `Re:` replies to your first email.
8. When a call is booked or a proposal is sent, update the status on the lead page.
9. Use **Export .xlsx** whenever you want your sheet updated.

## 3. Lead Scraper agent

Open **Lead Scraper** in the sidebar. For each website it:

1. **Crawls** the home page plus the most useful contact / about / team pages (6 by default), and the site's Google Tag Manager container. It respects robots.txt and fetches one page at a time per site.
2. **Collects** the following:
   - published emails, including Cloudflare-protected ones (it never guesses addresses)
   - the best email to use and its type (direct / general inbox / other staff)
   - phone, WhatsApp, and company and personal LinkedIn
   - people listed with their titles
   - 55+ chat and AI-bot vendors
   - Google, Meta, TikTok, Snapchat, LinkedIn and Microsoft ad pixels
   - booking tools and CRM / practice software
   - site languages, "24/7", "free consultation" and "no win no fee" claims, and portal profiles
3. **Scores** each lead with an AI Need Score (0–100) and lists the evidence, using the same idea as your sheet: no instant reply, paid ads, no booking, English-only, high volume…
4. **With Claude** (AI enrichment on), it also identifies the decision-maker and writes "Why they need AI", "How you can help", a personalised opener, a subject line and the 3-email sequence. **Deep search** looks up the owner or CEO on LinkedIn and the web when the site names nobody.
5. **Skips** sites that already run an AI chatbot and companies already in your leads (optional).

Review the results, then click **Add to leads**, or download them as `.xlsx`. The download uses the same columns as your lead workbook and can be imported again.

Command line (no web app needed):

```bash
.venv/bin/python scraper.py site1.com site2.ae --out leads.xlsx
.venv/bin/python scraper.py --file urls.txt --ai --segment law --out law_leads.xlsx
```

Tips: get website lists from Google Maps, Property Finder or Bayut broker pages, or legal directories, or use "Find businesses with AI". JavaScript-only sites: `pip install playwright && playwright install chromium`, then turn on "Render JavaScript sites".

## 4. Open tracking (optional)

Open tracking adds an invisible image to each email. The recipient's mail app has to load that image from your app, so the app must be reachable on the internet. Two ways to do that:

- **Quick test:** run `cloudflared tunnel --url http://localhost:5050` and paste the https URL into Settings → Public URL.
- **Always on:** deploy to a small server (Render, Railway, a VPS) with `HOST=0.0.0.0`, set `APP_PASSWORD` so only you can log in, and use something like `https://outreach.varad-tech.site` as the public URL.

Then tick **Add an open-tracking pixel** in Settings. Opens are only approximate: Apple Mail pre-loads images, many clients block them, and tracking pixels slightly hurt cold-email deliverability. **Replies are what count.** Reply tracking works without any of this.

## 5. Deliverability and compliance

- Send from a **business domain** with SPF, DKIM and DMARC set up. Warm it up first: about 10/day in week 1, then 20–40/day.
- Every email automatically gets your signature, your postal address (if set), an opt-out line and a `List-Unsubscribe` header.
- Anyone who opts out or bounces is blocked from future sends.
- UK (PECR): fine for corporate addresses (Ltd/LLP). Check the firm type for sole practitioners.
- Canada (CASL): only use addresses published on the firm's own site, and always include your postal address.
- This is not legal advice. The README tab of your lead sheet has more detail.

## Files

```
app.py        web app, send queue, follow-up logic
ai.py         Claude: email drafting, scraper enrichment, business discovery
scraper.py    Lead Scraper agent: crawler, signal detection, scoring, .xlsx export (also a CLI)
mailer.py     SMTP sending, IMAP reply/bounce/opt-out detection
importer.py   xlsx / Google Sheets import, export back to xlsx
db.py         SQLite storage (data/outreach.db)
templates/    pages
```

Your data lives in `data/outreach.db`. Back it up if it matters to you.
