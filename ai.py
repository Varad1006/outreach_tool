"""Claude-powered drafting and lead research."""
import base64
import json
import os

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
USE_FALLBACKS = os.environ.get("CLAUDE_FALLBACKS", "1") == "1"
_client = None


class AIError(Exception):
    pass


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment / .env
    return _client


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _create(**kwargs):
    """messages.create with server-side refusal fallbacks; retries without them if the account rejects the beta."""
    global USE_FALLBACKS
    try:
        if USE_FALLBACKS:
            return client().beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        return client().messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        if USE_FALLBACKS and "fallback" in str(e).lower():
            USE_FALLBACKS = False
            return client().messages.create(**kwargs)
        raise AIError(f"Claude rejected the request: {e.message}") from e
    except anthropic.AuthenticationError as e:
        raise AIError("Claude API key is missing or invalid. Set ANTHROPIC_API_KEY in your .env file.") from e
    except anthropic.RateLimitError as e:
        raise AIError("Claude rate limit hit. Wait a minute and try again.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude API error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Couldn't reach the Claude API. Check your internet connection.") from e


def _text(resp):
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined this request.")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _json_call(system, prompt, schema, effort="medium", max_tokens=8000):
    resp = _create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "max_tokens":
        raise AIError("Claude's answer was cut off. Try again.")
    try:
        return json.loads(_text(resp))
    except ValueError as e:
        raise AIError("Claude returned an unexpected format. Try again.") from e


# ---------------------------------------------------------------- drafting

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["subject", "body", "why"],
    "additionalProperties": False,
}

STEP_GUIDE = {
    0: ("the FIRST cold email. 90-150 words. Open with one specific, verifiable observation about their business "
        "from the evidence, connect it to lost leads / time / revenue, say concretely what you'd build for them, "
        "and end with ONE low-friction call to action (a 15-minute call or a short demo on their own data)."),
    1: ("FOLLOW-UP 1, sent 3-4 days after the first email in the same thread. 30-60 words. Don't repeat the first "
        "email. Offer something tangible and free (e.g. a 2-minute Loom demo built on one of their listings / a "
        "typical enquiry) and ask a yes/no question."),
    2: ("FOLLOW-UP 2, the final 'closing the loop' email in the same thread. 25-50 words. Polite, no guilt, "
        "offer a free 15-minute audit of their enquiry / lead flow, and make it easy to say no."),
}


def lead_context(lead):
    fields = [
        ("Company", lead.get("company")), ("Website", lead.get("website")),
        ("Decision-maker", lead.get("contact_name")), ("Their title", lead.get("title")),
        ("First name", lead.get("first_name")), ("Sending to", lead.get("email")),
        ("Email type", lead.get("email_type")), ("Country", lead.get("country") or ("UAE" if lead.get("segment") == "dubai" else "")),
        ("City / region", lead.get("city")), ("Practice area / industry", lead.get("practice_area")),
        ("AI need score (0-100)", lead.get("score")),
        ("Why they need AI (evidence)", lead.get("evidence")),
        ("AI solution to pitch", lead.get("solution")),
        ("Services to pitch", lead.get("services")),
        ("Researched opener", lead.get("opener")),
        ("Caveat to respect", lead.get("check_note")),
    ]
    raw = lead.get("raw") or {}
    for k in ("Agents (PF)", "Live listings (PF)", "For sale", "For rent", "Median WhatsApp reply (PF)",
              "Ad tracking detected", "Online booking", "Site languages", "Client reviews (ReviewSolicitors, UK)",
              "Rating", "Free consult / No-win-no-fee", "Practice mgmt / CRM visible", "Chat / AI on website"):
        if raw.get(k):
            fields.append((k, raw[k]))
    return "\n".join(f"{k}: {v}" for k, v in fields if v not in (None, ""))


def draft_email(lead, step, settings, instructions="", previous=None):
    """Write a personalised email for a lead. Returns {subject, body, why}."""
    system = (
        "You write short, specific B2B cold emails for a solo AI automation engineer. You sound like a "
        "thoughtful human engineer, not a marketer: plain words, no hype, no buzzwords, no emojis, no "
        "exclamation marks, no 'I hope this finds you well'. Use only facts given in the lead data; never invent "
        "numbers, clients or results. Address the decision-maker by first name. If the email is going to a "
        "general inbox, start the body with an 'FAO <name>, <title>' line. Plain text only. Do NOT include a "
        "signature, sign-off name, unsubscribe line or postal address - those are appended automatically.\n\n"
        f"About the sender:\n{settings.get('about_me', '')}"
    )
    parts = [f"Write {STEP_GUIDE.get(step, STEP_GUIDE[0])}", "", "Lead data:", lead_context(lead)]
    reference = {0: lead.get("sheet_email1"), 1: lead.get("sheet_fu1"), 2: lead.get("sheet_fu2")}.get(step)
    if reference:
        parts += ["", "A pre-written draft for this step (improve on it, keep what works):", reference]
    if previous:
        parts += ["", "Emails already sent to them in this thread, oldest first:"]
        parts += [f"--- {p['subject']}\n{p['body']}" for p in previous]
    if step > 0:
        parts += ["", "This goes in the same thread, so for 'subject' just return the original subject."]
    else:
        parts += ["", "Subject: under 7 words, lowercase-ish, specific to them, no clickbait."]
    if instructions:
        parts += ["", f"Extra instructions from the sender: {instructions}"]
    parts += ["", "In 'why', give one sentence on the angle you chose."]
    return _json_call(system, "\n".join(parts), DRAFT_SCHEMA, effort="medium")


# ---------------------------------------------------------------- scraper agent

def _web_research(task, searches=5, fetches=6):
    """One Claude turn with web search + fetch; resumes server-side tool loops that pause."""
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches},
    ]
    messages = [{"role": "user", "content": task}]
    resp = None
    for _ in range(5):
        resp = _create(model=MODEL, max_tokens=16000, output_config={"effort": "medium"}, tools=tools, messages=messages)
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": task}, {"role": "assistant", "content": resp.content}]
    return _text(resp)


ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "description": "Clean company / firm name"},
        "industry": {"type": "string", "description": "e.g. Real estate brokerage, Immigration law, Personal injury law"},
        "city": {"type": "string"},
        "country": {"type": "string"},
        "contact_name": {"type": "string", "description": "Most senior decision-maker named in the pages, or empty"},
        "title": {"type": "string"},
        "dm_source": {"type": "string", "description": "Page URL where the decision-maker was found, or empty"},
        "other_contacts": {"type": "string", "description": "Other senior people as 'Name (Title); ...'"},
        "score": {"type": "integer", "description": "AI Need Score 0-100; start from the rule-based score and adjust by at most 10"},
        "evidence": {"type": "string", "description": "3-5 bullets starting with '• ', concrete facts from the crawl"},
        "solution": {"type": "string", "description": "2-4 bullets starting with '• ', specific AI systems to build for them"},
        "services": {"type": "string", "description": "Seller services that fit, joined with ' + '"},
        "opener": {"type": "string", "description": "2-3 sentence personalised opener with one specific observation and a question"},
        "subject": {"type": "string", "description": "Under 7 words, specific to them"},
        "email1": {"type": "string", "description": "Full first email body (90-150 words), no signature"},
        "fu1": {"type": "string", "description": "Follow-up 1 (30-60 words): offer a short free demo"},
        "fu2": {"type": "string", "description": "Follow-up 2 (25-50 words): close the loop, offer a free 15-minute audit"},
        "check_note": {"type": "string", "description": "Anything to verify before sending (uncertain name, already has AI, etc.), or empty"},
    },
    "required": ["company", "industry", "city", "country", "contact_name", "title", "dm_source", "other_contacts",
                 "score", "evidence", "solution", "services", "opener", "subject", "email1", "fu1", "fu2", "check_note"],
    "additionalProperties": False,
}

SEGMENT_HINT = {
    "dubai": "This is a Dubai real-estate brokerage. Typical pains: slow WhatsApp/portal lead replies, off-plan and "
             "payment-plan questions, multilingual buyers (EN/AR/RU), booking viewings.",
    "law": "This is a law firm (UK or Canada). Typical pains: after-hours enquiries, intake qualification, booking "
           "consultations, case status calls, document checklists. Keep claims modest and mention privacy.",
}


def enrich_scraped(facts, base, settings, segment="other"):
    """Turn crawled facts into a full lead row: decision-maker, evidence, pitch and a 3-email sequence."""
    signals = {k: facts.get(k) for k in ("domain", "company", "description", "chat", "ai_chat", "whatsapp", "ads",
                                          "booking", "crm", "languages", "claims", "portals", "listings_hint",
                                          "reviews_hint", "people", "emails", "phones", "linkedin_company")}
    system = (
        "You turn website crawl data into an outreach lead for a solo AI automation engineer. Use only facts present "
        "in the data; never invent names, numbers or email addresses. Plain, specific, human writing: no hype, no "
        "emojis, no exclamation marks. Emails: address the decision-maker by first name (or 'Hi there' if unknown); "
        "if the best email is a general inbox, start email1 with 'FAO <name>, <title>'. No signature or "
        "unsubscribe line (added automatically).\n\n"
        f"About the seller:\n{settings.get('about_me', '')}"
    )
    prompt = "\n".join([
        SEGMENT_HINT.get(segment, "Work out the industry from the pages."),
        "",
        f"Rule-based AI Need Score: {base['score']} because:\n{base['evidence']}",
        f"Best email chosen: {base['email']} ({base['email_type']})",
        "",
        "Detected signals (JSON):",
        json.dumps(signals, ensure_ascii=False)[:6000],
        "",
        "Page text from the crawl:",
        facts.get("page_text", "")[:14000],
    ])
    return _json_call(system, prompt, ENRICH_SCHEMA, effort="medium", max_tokens=12000)


DM_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "title": {"type": "string"}, "linkedin": {"type": "string"},
                   "source": {"type": "string"}},
    "required": ["name", "title", "linkedin", "source"],
    "additionalProperties": False,
}


def find_decision_maker(company, website, city=""):
    """Web search for the owner / CEO / managing partner when the website doesn't name one."""
    report = _web_research(
        f"Who is the owner, founder, CEO, managing director or managing partner of {company} ({website}) {city}? "
        "Check LinkedIn, the company's own site, business directories and news. Give the name, exact title, "
        "LinkedIn profile URL if found, and the source URL. If you can't confirm a current person from two "
        "sources or the company's own site, say so.", searches=4, fetches=3)
    return _json_call("Extract the confirmed decision-maker from this report. Leave fields empty if not confirmed.",
                      report, DM_SCHEMA, effort="low", max_tokens=2000)


DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {"companies": {"type": "array", "items": {
        "type": "object",
        "properties": {"company": {"type": "string"}, "website": {"type": "string"}, "why": {"type": "string"}},
        "required": ["company", "website", "why"], "additionalProperties": False}}},
    "required": ["companies"],
    "additionalProperties": False,
}


def discover_companies(query, count, exclude_domains=(), context=""):
    """Find candidate small/mid-size businesses (with their own website) matching a description."""
    excl = ", ".join(sorted(exclude_domains)[:150])
    report = _web_research(
        f"Find up to {count} small or mid-size businesses matching: {query}.\n"
        + (f"Why they're a fit and what to look for:\n{context}\n" if context else "")
        + "Rules: each must have its own website domain (not just a directory listing); skip national chains, "
        "franchises, big brands and directories/listicles; prefer firms that look busy (many listings, reviews, "
        "or ads) and that do NOT already have an AI chatbot. For each, give the company name and its homepage URL.\n"
        + (f"Skip these domains, already in my list: {excl}" if excl else ""),
        searches=8, fetches=4)
    out = _json_call("Extract the businesses from this report as a list. Homepage URLs only. No directories.",
                     report, DISCOVER_SCHEMA, effort="low", max_tokens=6000)
    return out["companies"][:count]


# ---------------------------------------------------------------- profile-driven targeting

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One line: who the seller is and what they build"},
        "summary": {"type": "string", "description": "3-4 sentences in the seller's voice, usable as 'About me' for emails"},
        "services": {"type": "array", "items": {"type": "string"}, "description": "Concrete services they can sell"},
        "skills": {"type": "array", "items": {"type": "string"}, "description": "Key technical skills / tools"},
        "proof_points": {"type": "array", "items": {"type": "string"}, "description": "Projects, results, credentials"},
        "best_fit_clients": {"type": "array", "items": {"type": "string"}, "description": "Kinds of businesses that need these skills most"},
    },
    "required": ["headline", "summary", "services", "skills", "proof_points", "best_fit_clients"],
    "additionalProperties": False,
}


def analyze_profile(website_text, resume_text, resume_pdf, skills, about_me):
    """Turn the seller's website, resume and skills into a structured profile."""
    content = []
    if resume_pdf:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                        "data": base64.b64encode(resume_pdf).decode()}})
    text = "\n\n".join(part for part in [
        "Build a sales profile for this freelancer / small agency selling AI automation services. Use only what "
        "the sources say; don't invent clients or results.",
        f"Skills they listed:\n{skills}" if skills else "",
        f"Their current 'About me':\n{about_me}" if about_me else "",
        f"Resume text:\n{resume_text[:15000]}" if resume_text else ("Resume: attached PDF." if resume_pdf else ""),
        f"Their website (crawled pages):\n{website_text[:15000]}" if website_text else "",
    ] if part)
    content.append({"type": "text", "text": text})
    resp = _create(model=MODEL, max_tokens=6000, output_config={"effort": "low", "format": {"type": "json_schema", "schema": PROFILE_SCHEMA}},
                   messages=[{"role": "user", "content": content}])
    try:
        return json.loads(_text(resp))
    except ValueError as e:
        raise AIError("Claude returned an unexpected format. Try again.") from e


TARGETING_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy": {"type": "string", "description": "2-3 sentences: where to focus next and why, citing the lead data"},
        "segments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short label, e.g. 'Abu Dhabi off-plan brokerages'"},
                "search_query": {"type": "string", "description": "What to search for: business type + size + place"},
                "location": {"type": "string"},
                "industry_key": {"type": "string", "enum": ["dubai", "law", "other"],
                                 "description": "dubai = real estate in the UAE, law = law firms, other = anything else"},
                "source": {"type": "string", "enum": ["proven", "adjacent", "new"],
                           "description": "proven = already in the lead data, adjacent = close to it, new = fresh idea from the profile"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "why_fit": {"type": "string", "description": "Why this segment needs the seller's skills, 1-2 sentences"},
                "signals": {"type": "array", "items": {"type": "string"}, "description": "Website signs that a business needs help"},
                "pitch_angle": {"type": "string", "description": "The one offer to lead with"},
            },
            "required": ["name", "search_query", "location", "industry_key", "source", "priority", "why_fit",
                         "signals", "pitch_angle"],
            "additionalProperties": False}},
    },
    "required": ["strategy", "segments"],
    "additionalProperties": False,
}


def build_targeting(profile, lead_stats, best_examples, n=8):
    """Suggest target segments from the seller's profile and what their past leads show."""
    system = ("You are a B2B prospecting strategist for a solo AI automation engineer. Recommend specific, "
              "searchable customer segments (business type + size + place) where small and mid-size businesses "
              "visibly need the seller's skills. Ground every recommendation in the profile and lead data given. "
              "Rank by expected reply rate and deal value. Prefer segments with many independent businesses that "
              "have their own websites.")
    prompt = "\n".join([
        f"Suggest {n} target segments: include the best proven segments from the lead data (if any did well), "
        "adjacent segments (same pain, nearby city or similar industry) and a few new ones that fit the profile.",
        "", "Seller profile (JSON):", json.dumps(profile, ensure_ascii=False)[:6000],
        "", "Past leads by segment (JSON: leads, contacted, replied, avg score):", json.dumps(lead_stats, ensure_ascii=False)[:6000],
        "", "Examples of the best past leads (replied, or highest AI Need Score):", json.dumps(best_examples, ensure_ascii=False)[:5000],
    ])
    return _json_call(system, prompt, TARGETING_SCHEMA, effort="medium", max_tokens=12000)
