"""Lead scraper agent.

Crawls company websites (home + contact/about/team pages + the site's Google Tag Manager container) and
produces lead rows in the same shape as the lead workbook: contacts, published emails, chat / ad / booking /
CRM detection, languages, an AI Need Score with evidence, and (with Claude) the decision-maker, pitch,
personalised opener and a ready-to-send email sequence.

Also usable from the command line:
    python scraper.py https://site-one.com site-two.ae --out leads.xlsx
    python scraper.py --file urls.txt --ai --segment law --out leads.xlsx
"""
import argparse
import json
import re
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; LeadResearchBot/1.0; +https://varad-tech.site)"
TIMEOUT = 15
MAX_BYTES = 2_500_000

# ---------------------------------------------------------------- detection signatures
# name: (substrings to look for in HTML / scripts, is_ai_bot)
CHAT = {
    "Intercom": (["widget.intercom.io", "js.intercomcdn.com"], False),
    "Drift": (["js.driftt.com", "drift.com/include"], False),
    "Tawk.to": (["embed.tawk.to"], False),
    "LiveChat": (["cdn.livechatinc.com", "livechatinc.com/tracking"], False),
    "Zendesk Chat": (["static.zdassets.com/ekr", "zopim.com", "v2.zopim.com"], False),
    "Crisp": (["client.crisp.chat"], False),
    "Tidio": (["code.tidio.co"], False),
    "HubSpot Chat": (["js.usemessages.com"], False),
    "Freshchat": (["wchat.freshchat.com", "freshchat.com/js"], False),
    "Olark": (["static.olark.com"], False),
    "JivoChat": (["code.jivosite.com", "code.jivo.ru"], False),
    "Zoho SalesIQ": (["salesiq.zoho", "salesiq.zohopublic"], False),
    "Smartsupp": (["smartsuppchat.com"], False),
    "Userlike": (["userlike-cdn-widgets", "userlike.com"], False),
    "Pure Chat": (["purechat.com"], False),
    "Gorgias": (["gorgias.chat", "config.gorgias"], False),
    "HelpCrunch": (["widget.helpcrunch.com"], False),
    "Chaport": (["app.chaport.com"], False),
    "LiveAgent": (["ladesk.com"], False),
    "Comm100": (["comm100.com", "comm100.io"], False),
    "Trengo": (["static.widget.trengo.eu"], False),
    "Chatwoot": (["chatwoot"], False),
    "Facebook Messenger": (["xfbml.customerchat", "customerchat.js"], False),
    "Bitrix24 widget": (["bitrix24.", "crm/site_button"], False),
    "GoHighLevel chat": (["leadconnectorhq.com/chat-widget", "widgets.leadconnectorhq.com"], False),
    "Elfsight widget": (["elfsightcdn.com", "apps.elfsight.com"], False),
    "WhatsApp chat plugin": (["getbutton.io", "whatshelp.io", "wati.io/", "widget.wati", "aisensy"], False),
    "Chaty": (["chaty-front", "/chaty/"], False),
    "Joinchat": (["joinchat", "joinchat.min.js"], False),
    "Click to Chat": (["ht-ctc", "click-to-chat"], False),
    "respond.io": (["respond.io/webchat", "cdn.respond.io"], False),
    "Chatbase": (["chatbase.co"], True),
    "Botpress": (["botpress.cloud", "cdn.botpress"], True),
    "Voiceflow": (["voiceflow.com"], True),
    "Landbot": (["landbot.io"], True),
    "Ada": (["ada.support", "static.ada.support"], True),
    "Kommunicate": (["kommunicate.io"], True),
    "Yellow.ai": (["yellow.ai", "cdn.yellowmessenger"], True),
    "Haptik": (["haptik.ai", "toolassets.haptikapi"], True),
    "Engati": (["engati.com", "engatiapp"], True),
    "Verloop": (["verloop.io"], True),
    "Kore.ai": (["kore.ai"], True),
    "Chatling": (["chatling.ai"], True),
    "SiteGPT": (["sitegpt.ai"], True),
    "CustomGPT": (["customgpt.ai"], True),
    "Botsonic": (["botsonic", "writesonic.com"], True),
    "Dante AI": (["dante-ai.com"], True),
    "Wonderchat": (["wonderchat.io"], True),
    "ChatBot.com": (["cdn.chatbot.com"], True),
    "Collect.chat": (["collect.chat"], True),
    "Chatfuel": (["chatfuel.com"], True),
    "ManyChat": (["manychat.com"], True),
    "Botmaker": (["botmaker.com"], True),
    "Lyro / Tidio AI": (["tidio-lyro"], True),
    "Fin (Intercom AI)": (["intercom.com/fin"], True),
}
ADS = {
    "Google Ads": ["googleadservices.com", "googleads.g.doubleclick.net", r"re:['\"]AW-\d{6,}"],
    "Meta (FB/IG) ads": ["connect.facebook.net", "fbevents.js", "fbq("],
    "TikTok ads": ["analytics.tiktok.com", "ttq.load"],
    "Snapchat ads": ["sc-static.net/scevent", "snaptr("],
    "LinkedIn ads": ["snap.licdn.com", "_linkedin_partner_id"],
    "Microsoft ads": ["bat.bing.com"],
    "X (Twitter) ads": ["static.ads-twitter.com"],
}
BOOKING = {
    "Calendly": ["calendly.com"], "Acuity": ["acuityscheduling.com"], "Setmore": ["setmore.com"],
    "SimplyBook": ["simplybook."], "YouCanBookMe": ["youcanbook.me"], "Cal.com": ["cal.com/embed", "app.cal.com"],
    "TidyCal": ["tidycal.com"], "Zcal": ["zcal.co"], "HubSpot Meetings": ["meetings.hubspot.com"],
    "Booksy": ["booksy.com"], "Square Appointments": ["squareup.com/appointments", "book.squareup"],
    "OnceHub": ["oncehub.com"], "SavvyCal": ["savvycal.com"], "Google appointments": ["calendar.app.google"],
    "Appointy": ["appointy.com"], "Lawmatics": ["lawmatics.com"], "10to8": ["10to8.com"],
}
CRM = {
    "HubSpot": ["js.hs-scripts.com", "js.hsforms.net", "hs-analytics.net"], "Salesforce": ["force.com", "pardot.com"],
    "Zoho CRM": ["zohocrm", "crm.zoho"], "Pipedrive": ["pipedrive"], "GoHighLevel": ["leadconnectorhq", "msgsndr.com"],
    "Clio": ["clio.com", "app.clio"], "LEAP": ["leap.co.uk", "leapsoftware"], "Actionstep": ["actionstep"],
    "WhatConverts": ["whatconverts"], "CallRail": ["callrail"], "Bitrix24": ["bitrix24"],
    "Propertybase": ["propertybase"], "Mailchimp": ["chimpstatic.com", "list-manage.com"],
    "ActiveCampaign": ["activehosted.com", "trackcmp.net"], "Monday.com": ["monday.com/forms"],
}
PORTALS = {"Property Finder": "propertyfinder.ae", "Bayut": "bayut.com", "Dubizzle": "dubizzle.com",
           "Rightmove": "rightmove.co.uk", "Zoopla": "zoopla.co.uk", "ReviewSolicitors": "reviewsolicitors.co.uk",
           "Trustpilot": "trustpilot.com", "Google reviews": "g.page/"}
LANG_HINTS = {"Arabic": ["العربية", "/ar/", "lang=\"ar", "hreflang=\"ar"], "Russian": ["Русский", "/ru/", "hreflang=\"ru"],
              "French": ["Français", "/fr/", "hreflang=\"fr"], "Chinese": ["中文", "/zh/", "hreflang=\"zh"],
              "Spanish": ["Español", "/es/", "hreflang=\"es"], "Hindi": ["हिन्दी", "/hi/", "hreflang=\"hi"],
              "German": ["Deutsch", "/de/", "hreflang=\"de"], "Urdu": ["اردو", "/ur/", "hreflang=\"ur"],
              "Punjabi": ["ਪੰਜਾਬੀ", "hreflang=\"pa"], "Polish": ["Polski", "hreflang=\"pl"],
              "Portuguese": ["Português", "hreflang=\"pt"], "Italian": ["Italiano", "hreflang=\"it"]}
PAGE_KEYWORDS = ["contact", "about", "team", "people", "staff", "leadership", "management", "who-we-are",
                 "our-agents", "agents", "lawyers", "solicitors", "attorneys", "meet", "founder", "company",
                 "our-firm", "partners", "consult", "book", "appointment"]
TITLE_WORDS = ["founder", "co-founder", "ceo", "chief executive", "managing director", "managing partner", "owner",
               "director", "principal", "partner", "head of", "general manager", "president", "proprietor",
               "senior partner", "md"]
JUNK_EMAIL = re.compile(r"(\.(png|jpe?g|gif|svg|webp|css|js)$)|example\.|sentry|wixpress|@domain\.|@email\.|"
                        r"yourname|your@|name@|user@|@2x|godaddy|noreply|no-reply|privacy@|gdpr@|dpo@|webmaster@|"
                        r"abuse@|postmaster@", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
GENERIC_LOCAL = {"info", "contact", "hello", "enquiries", "enquiry", "inquiries", "sales", "office", "admin",
                 "mail", "support", "team", "reception", "leasing", "careers", "jobs", "hr", "marketing",
                 "accounts", "billing", "bookings", "booking", "general", "help", "media", "press", "dubai",
                 "london", "toronto", "intake", "newclients", "new.clients", "clients", "legal", "law"}


class Site:
    """Polite fetcher bound to one website."""

    def __init__(self, url, delay=0.4, respect_robots=True):
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url.strip().strip("/")
        self.start = url
        p = urlparse(url)
        self.root = f"{p.scheme}://{p.netloc}"
        self.domain = p.netloc.lower().removeprefix("www.")
        self.delay = delay
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
        self.robots = None
        if respect_robots:
            try:
                r = self.s.get(self.root + "/robots.txt", timeout=8)
                if r.ok and "html" not in r.headers.get("content-type", ""):
                    self.robots = urllib.robotparser.RobotFileParser()
                    self.robots.parse(r.text.splitlines())
            except requests.RequestException:
                pass

    def allowed(self, url):
        return not self.robots or self.robots.can_fetch(USER_AGENT, url)

    def get(self, url):
        if not self.allowed(url):
            return None, "blocked by robots.txt"
        time.sleep(self.delay)
        try:
            r = self.s.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
            content = r.raw.read(MAX_BYTES, decode_content=True)
            r._content = content
            if r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            if "html" not in r.headers.get("content-type", "html"):
                return None, "not HTML"
            return r, None
        except requests.RequestException as e:
            return None, type(e).__name__


def _decode_cfemail(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def _match(text, patterns):
    for p in patterns:
        if p.startswith("re:"):
            if re.search(p[3:], text):
                return True
        elif p.lower() in text:
            return True
    return False


def detect(blob_lower, raw_text):
    chat = [(n, ai) for n, (pats, ai) in CHAT.items() if _match(blob_lower, [x.lower() for x in pats])]
    ads = [n for n, pats in ADS.items() if _match(raw_text, pats) or _match(blob_lower, [p for p in pats if not p.startswith("re:")])]
    booking = [n for n, pats in BOOKING.items() if _match(blob_lower, pats)]
    crm = [n for n, pats in CRM.items() if _match(blob_lower, pats)]
    return chat, ads, booking, crm


def _people(soup):
    """Find (name, title) pairs on team/about pages: a short title string next to a name-like string."""
    strings = [s.strip() for s in soup.stripped_strings if 1 < len(s.strip()) < 80]
    name_re = re.compile(r"^(?:(?:Dr|Mr|Mrs|Ms)\.?\s)?[A-Z][a-zA-Z'’\-]+(?:\s[A-Z][a-zA-Z'’\-]+){1,3}$")
    found = []
    for i, s in enumerate(strings):
        low = s.lower()
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in TITLE_WORDS) and len(s) < 70:
            for j in (i - 1, i + 1, i - 2):
                if 0 <= j < len(strings) and name_re.match(strings[j]) and not any(w in strings[j].lower() for w in TITLE_WORDS):
                    found.append((strings[j], s))
                    break
        m = re.match(r"^([A-Z][a-zA-Z'’\-]+(?:\s[A-Z][a-zA-Z'’\-]+){1,3})\s*[,–\-|]\s*(.{3,60})$", s)
        if m and any(w in m.group(2).lower() for w in TITLE_WORDS):
            found.append((m.group(1), m.group(2)))
    seen, out = set(), []
    for n, t in found:
        if n not in seen:
            seen.add(n)
            out.append((n, t))
    return out[:15]


def _seniority(title):
    t = title.lower()
    for rank, words in enumerate([["founder", "owner", "ceo", "chief executive", "managing partner", "managing director",
                                   "proprietor", "president", "principal"], ["senior partner", "partner", "director",
                                                                              "general manager", "head of"]]):
        if any(w in t for w in words):
            return rank
    return 2


def crawl(url, max_pages=6, respect_robots=True, render_js=False):
    """Crawl one website and return raw facts (no AI)."""
    site = Site(url, respect_robots=respect_robots)
    facts = {"input": url, "website": site.start, "domain": site.domain, "pages": [], "errors": []}
    home, err = site.get(site.start)
    if not home:
        facts["errors"].append(f"home: {err}")
        facts["status"] = "failed"
        return facts
    facts["website"] = home.url
    site.root = f"{urlparse(home.url).scheme}://{urlparse(home.url).netloc}"
    site.domain = urlparse(home.url).netloc.lower().removeprefix("www.")
    facts["domain"] = site.domain
    pages = [(home.url, home.text)]

    soup = BeautifulSoup(home.text, "html.parser")
    if render_js and len(soup.get_text(" ", strip=True)) < 400:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch()
                pg = b.new_page(user_agent=USER_AGENT)
                pg.goto(home.url, timeout=30000, wait_until="networkidle")
                pages[0] = (home.url, pg.content())
                b.close()
            soup = BeautifulSoup(pages[0][1], "html.parser")
        except Exception as e:  # noqa: BLE001 - optional dependency
            facts["errors"].append(f"js render: {type(e).__name__}")

    # Pick the most useful internal pages.
    cands = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(home.url, a["href"].split("#")[0])
        p = urlparse(href)
        if p.netloc.lower().removeprefix("www.") != site.domain or not p.scheme.startswith("http"):
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?|mp4)$", p.path, re.I):
            continue
        key = (p.path.lower() + " " + a.get_text(" ", strip=True).lower())
        score = sum(10 - i // 3 for i, k in enumerate(PAGE_KEYWORDS) if k in key)
        if score and href.rstrip("/") != home.url.rstrip("/"):
            cands[href] = max(cands.get(href, 0), score)
    for href in sorted(cands, key=cands.get, reverse=True)[: max(0, max_pages - 1)]:
        r, err = site.get(href)
        if r:
            pages.append((r.url, r.text))
        else:
            facts["errors"].append(f"{href}: {err}")
    facts["pages"] = [u for u, _ in pages]

    # Google Tag Manager containers often inject chat widgets and ad pixels.
    all_html = "\n".join(h for _, h in pages)
    gtm_text = ""
    for gid in sorted(set(re.findall(r"GTM-[A-Z0-9]{4,9}", all_html)))[:2]:
        try:
            g = site.s.get(f"https://www.googletagmanager.com/gtm.js?id={gid}", timeout=TIMEOUT)
            if g.ok:
                gtm_text += g.text[:MAX_BYTES]
        except requests.RequestException:
            pass
    facts["gtm"] = bool(gtm_text)

    blob = (all_html + gtm_text)
    blob_lower = blob.lower()
    chat, ads, booking, crm = detect(blob_lower, blob)
    facts["chat"] = [n for n, _ in chat]
    facts["ai_chat"] = [n for n, ai in chat if ai]
    facts["ads"], facts["booking"], facts["crm"] = ads, booking, crm

    # Contacts.
    emails, phones, wa, linkedin_co, linkedin_people, socials, portals = {}, [], set(), set(), set(), {}, set()
    texts, people = [], []
    for page_url, html in pages:
        ps = BeautifulSoup(html, "html.parser")
        for el in ps.select("[data-cfemail]"):
            e = _decode_cfemail(el.get("data-cfemail", ""))
            if e:
                emails.setdefault(e.lower(), page_url)
        for a in ps.find_all("a", href=True):
            h = a["href"].strip()
            hl = h.lower()
            if hl.startswith("mailto:"):
                e = h[7:].split("?")[0].strip()
                if EMAIL_RE.fullmatch(e):
                    emails.setdefault(e.lower(), page_url)
            elif hl.startswith("tel:"):
                num = re.sub(r"[^\d+]", "", h[4:])
                if len(num) >= 8 and num not in phones:
                    phones.append(num)
            elif "wa.me/" in hl or "api.whatsapp.com" in hl or "web.whatsapp.com" in hl:
                m = re.search(r"(?:wa\.me/|phone=)(\+?\d{8,15})", hl)
                wa.add(m.group(1) if m else h)
            elif "linkedin.com/company/" in hl:
                linkedin_co.add(h.split("?")[0])
            elif "linkedin.com/in/" in hl:
                linkedin_people.add(h.split("?")[0])
            elif any(s in hl for s in ("instagram.com/", "facebook.com/", "x.com/", "twitter.com/", "youtube.com/", "tiktok.com/@")):
                for s in ("instagram", "facebook", "twitter", "youtube", "tiktok"):
                    if s in hl:
                        socials.setdefault(s.capitalize(), h.split("?")[0])
            for name, dom in PORTALS.items():
                if dom in hl:
                    portals.add(name)
        for s in ps(["script", "style", "noscript", "svg"]):
            s.decompose()
        text = re.sub(r"\s+", " ", ps.get_text(" ", strip=True))
        for e in EMAIL_RE.findall(text):
            emails.setdefault(e.lower().strip("."), page_url)
        if any(k in page_url.lower() for k in ("team", "about", "people", "staff", "leadership", "agents", "lawyers",
                                                 "solicitors", "attorneys", "partners", "founder", "management", "meet")) \
                or page_url == pages[0][0]:
            people += _people(ps)
        texts.append((page_url, text))

    clean = {e: src for e, src in emails.items() if not JUNK_EMAIL.search(e)}
    facts["emails"] = [{"email": e, "source": src, "same_domain": e.split("@")[-1].removeprefix("www.").endswith(site.domain.split(":")[0]),
                        "generic": e.split("@")[0] in GENERIC_LOCAL} for e, src in clean.items()]
    facts["phones"] = phones[:5]
    if not phones:
        m = re.findall(r"(?:\+|00)\d[\d\s().-]{7,16}\d", "\n".join(t for _, t in texts))
        facts["phones"] = list(dict.fromkeys(re.sub(r"[^\d+]", "", x) for x in m))[:3]
    facts["whatsapp"] = sorted(wa)[:3]
    facts["linkedin_company"] = sorted(linkedin_co)[:1]
    facts["linkedin_people"] = sorted(linkedin_people)[:10]
    facts["socials"] = socials
    facts["portals"] = sorted(portals)
    brand = (title.split("|")[0].split(" - ")[0].strip().lower() if (title := (soup.title.get_text(strip=True) if soup.title else "")) else "")
    people = [p for p in people if p[0].lower() != brand and not any(w in p[0].lower() for w in ("realty", "solicitors", "law", "properties", "real estate", "ltd", "llp"))]
    people.sort(key=lambda p: _seniority(p[1]))
    facts["people"] = [{"name": n, "title": t} for n, t in people]

    # Site meta, languages and claims.
    title = soup.title.get_text(strip=True) if soup.title else ""
    og = soup.find("meta", property="og:site_name")
    facts["company"] = (og.get("content", "").strip() if og else "") or re.split(r"\s[|\-–—:]\s", title)[0].strip() or site.domain
    desc = soup.find("meta", attrs={"name": "description"})
    facts["description"] = desc.get("content", "").strip()[:300] if desc else ""
    langs = {"English"}
    for lang, hints in LANG_HINTS.items():
        if any(h.lower() in blob_lower for h in hints):
            langs.add(lang)
    facts["languages"] = sorted(langs, key=lambda x: (x != "English", x))
    full_text = " ".join(t for _, t in texts).lower()
    facts["claims"] = [c for c, pat in [("Free consultation", r"free (initial )?consultation"),
                                         ("No win no fee", r"no[ -]win[ ,-]*no[ -]fee"),
                                         ("Promises 24/7", r"24/7|24 hours a day|24-hour"),
                                         ("Off-plan focus", r"off[ -]plan"),
                                         ("Contact form", r"<form")] if re.search(pat, full_text if c != "Contact form" else blob_lower)]
    m = re.search(r"([\d,]{2,7})\+?\s*(?:properties|listings|homes for sale|units)", full_text)
    facts["listings_hint"] = m.group(1) if m else ""
    m = re.search(r"([\d,]{2,7})\+?\s*(?:reviews|client reviews|google reviews|5[- ]star reviews)", full_text)
    facts["reviews_hint"] = m.group(1) if m else ""
    facts["page_text"] = "\n\n".join(f"[{u}]\n{t[:3500]}" for u, t in texts)[:14000]
    facts["status"] = "ok"
    return facts


def best_email(facts, people=None):
    """Pick the best address: a named person's address on the company domain, else a general inbox."""
    emails = facts.get("emails", [])
    if not emails:
        return "", "Not found", []
    same = [e for e in emails if e["same_domain"]] or emails
    first_names = [p["name"].split()[0].lower() for p in (people or facts.get("people", []))]
    named = [e for e in same if not e["generic"] and any(fn and fn in e["email"] for fn in first_names)]
    personal = [e for e in same if not e["generic"]]
    general = [e for e in same if e["generic"]]
    pick, kind = (named[0], "Direct: decision-maker") if named else \
        (general[0], "General inbox (address FAO decision-maker)") if general else \
        (personal[0], "Company contact, other staff (address FAO decision-maker)")
    others = [e["email"] for e in emails if e["email"] != pick["email"]]
    return pick["email"], kind, others


def score(facts):
    """AI Need Score (0-100) with the reasons behind it, modelled on the lead workbook's scoring."""
    pts, why = 20, []
    if facts.get("ai_chat"):
        pts -= 20
        why.append(f"Already runs an AI chatbot ({', '.join(facts['ai_chat'])})")
    elif not facts.get("chat") and not facts.get("whatsapp"):
        pts += 30
        why.append("No chatbot, live chat or WhatsApp button: visitors can only fill a form or call and wait")
    elif not facts.get("chat"):
        pts += 18
        why.append("Only a WhatsApp link, no instant answers on the site itself: replies depend on staff being free")
    else:
        pts += 10
        why.append(f"Live chat ({', '.join(facts['chat'])}) but no AI: needs a human online to answer, so after-hours enquiries wait")
    if facts.get("ads"):
        pts += 15 if len(facts["ads"]) > 1 else 12
        why.append(f"{' + '.join(facts['ads'])} tracking installed: they pay for traffic, and leads that aren't answered instantly go elsewhere")
    if not facts.get("booking"):
        pts += 8
        why.append("No online booking: every viewing / consultation needs staff time to schedule")
    if facts.get("languages") == ["English"]:
        pts += 6
        why.append("English-only website: misses Arabic / Russian / other-language enquiries")
    if "Promises 24/7" in facts.get("claims", []) and not facts.get("chat"):
        pts += 4
        why.append("Promises 24/7 service but has no instant-response channel")
    if {"Free consultation", "No win no fee"} & set(facts.get("claims", [])):
        pts += 6
        why.append("Offers free consultations / no-win-no-fee: high enquiry volume to qualify")
    if "Off-plan focus" in facts.get("claims", []):
        pts += 4
        why.append("Off-plan focus: lots of repetitive payment-plan and launch questions")
    if facts.get("listings_hint"):
        pts += 6
        why.append(f"~{facts['listings_hint']} listings mentioned on the site: heavy enquiry load to qualify manually")
    if facts.get("reviews_hint"):
        pts += 5
        why.append(f"~{facts['reviews_hint']} reviews mentioned: a high-volume practice")
    if facts.get("crm"):
        pts += 2
        why.append(f"Uses {', '.join(facts['crm'])}: an AI assistant can plug straight into it")
    return max(0, min(100, pts)), why


def priority(s):
    return "A: Hot" if s >= 70 else "B: Warm" if s >= 60 else "C: Nurture"


def base_row(facts):
    """A lead row built only from crawled facts (used when AI is off, and as the input/fallback for AI)."""
    sc, why = score(facts)
    people = facts.get("people", [])
    dm = people[0] if people else {"name": "", "title": ""}
    email, etype, others = best_email(facts)
    first = dm["name"].split()[0] if dm["name"] else ""
    chat_txt = ", ".join(facts.get("chat") or []) or ("WhatsApp link only" if facts.get("whatsapp") else "None")
    solution = []
    if not facts.get("chat") or not facts.get("ai_chat"):
        solution.append("• 24/7 AI website + WhatsApp assistant trained on their services/listings: answers questions instantly, "
                        "qualifies the enquiry and books appointments into the team's calendar")
    if facts.get("ads"):
        solution.append("• AI voice agent that calls every new ad / portal lead within 60 seconds, qualifies them and hands hot ones to the team")
    solution.append("• Automated follow-up agent that re-engages cold enquiries and logs everything in the CRM")
    hello = f"Hi {first}" if first else "Hi there"
    opener = (f"{hello}, I was looking at {facts['domain']}: "
              + (why[0][0].lower() + why[0][1:] if why else "your enquiry flow could be faster")
              + ". I build AI assistants that reply to every enquiry in seconds, qualify it and book the appointment "
                "automatically, so your team only talks to serious prospects. Open to a 15-minute call this week?")
    return {
        "company": facts.get("company", ""), "website": facts.get("website", ""), "domain": facts.get("domain", ""),
        "contact_name": dm["name"], "title": dm["title"], "dm_source": "Company website" if dm["name"] else "",
        "email": email, "email_type": etype, "other_emails": ", ".join(others[:6]),
        "email_source": next((e["source"] for e in facts.get("emails", []) if e["email"] == email), ""),
        "phone": ", ".join(facts.get("phones", [])[:2]), "whatsapp": ", ".join(facts.get("whatsapp", [])),
        "linkedin": (facts.get("linkedin_people") or [""])[0], "company_linkedin": (facts.get("linkedin_company") or [""])[0],
        "other_contacts": "; ".join(f"{p['name']} ({p['title']})" for p in people[1:5]),
        "industry": "", "city": "", "country": "",
        "chat": chat_txt, "ads": ", ".join(facts.get("ads", [])) or "None",
        "booking": ", ".join(facts.get("booking", [])) or "None", "crm": ", ".join(facts.get("crm", [])) or "None",
        "languages": ", ".join(facts.get("languages", [])), "claims": ", ".join(c for c in facts.get("claims", []) if c != "Contact form"),
        "portals": ", ".join(facts.get("portals", [])),
        "score": sc, "priority": priority(sc), "evidence": "\n".join(f"• {w}" for w in why),
        "solution": "\n".join(solution), "services": "Website Chat Assistant + AI Voice Calling Bot + AI Agents (lead follow-up)",
        "opener": opener, "subject": f"Quick idea for {facts.get('company', '')}'s enquiries",
        "email1": "", "fu1": "", "fu2": "",
        "check_note": "" if dm["name"] else "No decision-maker found on the site: check LinkedIn before sending.",
        "pages": "\n".join(facts.get("pages", [])), "ai_enriched": False,
    }


def build_row(facts, use_ai=False, settings=None, segment="other", deep=False):
    """Facts -> lead row. With use_ai, Claude adds the decision-maker, pitch and the email sequence."""
    row = base_row(facts)
    if facts.get("status") != "ok" or not use_ai:
        return row
    import ai  # local import so the CLI works without Flask
    out = ai.enrich_scraped(facts, row, settings or {}, segment)
    if deep and not out.get("contact_name"):
        try:
            dm = ai.find_decision_maker(out.get("company") or row["company"], row["website"], out.get("city", ""))
            if dm.get("name"):
                out["contact_name"], out["title"] = dm["name"], dm.get("title", "")
                out["dm_source"] = f"Web ({dm.get('source', 'search')})"
                if dm.get("linkedin"):
                    row["linkedin"] = dm["linkedin"]
        except Exception:  # noqa: BLE001 - deep search is best-effort
            pass
    for k, v in out.items():
        if v not in (None, "") and k in row:
            row[k] = v
    # Re-pick the email once we know the decision-maker's name.
    if out.get("contact_name"):
        email, etype, others = best_email(facts, [{"name": out["contact_name"], "title": out.get("title", "")}])
        if email:
            row["email"], row["email_type"], row["other_emails"] = email, etype, ", ".join(others[:6])
    row["priority"] = priority(row["score"])
    row["ai_enriched"] = True
    return row


# ---------------------------------------------------------------- Excel export (same layout as the lead workbook)

EXPORT_COLUMNS = [
    ("#", None), ("Priority", "priority"), ("AI Need Score", "score"), ("Company", "company"), ("Website", "website"),
    ("Decision-maker", "contact_name"), ("Title", "title"), ("Best email to use", "email"), ("Email type", "email_type"),
    ("Email source", "email_source"), ("Other emails", "other_emails"), ("Phone", "phone"),
    ("WhatsApp (from site)", "whatsapp"), ("DM LinkedIn", "linkedin"), ("Company LinkedIn", "company_linkedin"),
    ("Other key contacts", "other_contacts"), ("DM source", "dm_source"), ("Industry", "industry"),
    ("City / region", "city"), ("Country", "country"), ("Chat / AI on website", "chat"),
    ("Ad tracking detected", "ads"), ("Online booking", "booking"), ("CRM / tools visible", "crm"),
    ("Site languages", "languages"), ("Claims on site", "claims"), ("Listed on", "portals"),
    ("Why they need AI (evidence)", "evidence"), ("How: AI solution to pitch", "solution"),
    ("Your services to pitch", "services"), ("Personalised opener (email / LinkedIn)", "opener"),
    ("Outreach status", None), ("Notes", None), ("Send to", "email"), ("Subject", "subject"),
    ("Email 1 (ready to send)", "email1"), ("Follow-up 1 (day 3-4)", "fu1"), ("Follow-up 2 (day 8-10)", "fu2"),
    ("Check before sending", "check_note"), ("Pages crawled", "pages"),
]


def export_xlsx(rows, path, sheet_title="Scraped Leads"):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]
    ws.append([c for c, _ in EXPORT_COLUMNS])
    rows = sorted(rows, key=lambda r: -(r.get("score") or 0))
    for i, r in enumerate(rows, 1):
        vals = []
        for col, key in EXPORT_COLUMNS:
            if col == "#":
                vals.append(i)
            elif col == "Outreach status":
                vals.append(r.get("status") or "Not contacted")
            elif col == "Notes":
                vals.append(r.get("notes", ""))
            else:
                vals.append(r.get(key, ""))
        ws.append(vals)
    head = PatternFill("solid", fgColor="1F2A44")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head
        c.alignment = Alignment(wrap_text=True, vertical="center")
    wide = {"Why they need AI (evidence)", "How: AI solution to pitch", "Personalised opener (email / LinkedIn)",
            "Email 1 (ready to send)", "Follow-up 1 (day 3-4)", "Follow-up 2 (day 8-10)", "Pages crawled"}
    for idx, (col, _) in enumerate(EXPORT_COLUMNS, 1):
        ws.column_dimensions[get_column_letter(idx)].width = 60 if col in wide else 14 if len(col) < 12 else 24
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return path


# ---------------------------------------------------------------- CLI

def _cli():
    ap = argparse.ArgumentParser(description="Scrape company websites into AI-ready lead rows (.xlsx).")
    ap.add_argument("urls", nargs="*", help="websites to scrape")
    ap.add_argument("--file", help="text file with one website per line")
    ap.add_argument("--out", default="scraped_leads.xlsx")
    ap.add_argument("--ai", action="store_true", help="use Claude to find the decision-maker and write the pitch + emails")
    ap.add_argument("--segment", default="other", help="dubai | law | other (guides the AI's pitch)")
    ap.add_argument("--pages", type=int, default=6, help="max pages per site")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--ignore-robots", action="store_true")
    a = ap.parse_args()
    urls = list(a.urls)
    if a.file:
        urls += [l.strip() for l in open(a.file, encoding="utf-8") if l.strip() and not l.startswith("#")]
    if not urls:
        ap.error("give at least one website")
    if a.ai:
        from dotenv import load_dotenv
        load_dotenv()

    def one(u):
        f = crawl(u, a.pages, not a.ignore_robots)
        r = build_row(f, a.ai, None, a.segment)
        print(f"  {'✓' if f['status'] == 'ok' else '✗'} {u}  score={r['score']}  email={r['email'] or '-'}  {'; '.join(f['errors'][:1])}")
        return r if f["status"] == "ok" else None

    print(f"Scraping {len(urls)} site(s)…")
    with ThreadPoolExecutor(a.workers) as ex:
        rows = [r for r in ex.map(one, urls) if r]
    export_xlsx(rows, a.out)
    print(f"Saved {len(rows)} lead(s) to {a.out}")


if __name__ == "__main__":
    _cli()
