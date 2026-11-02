"""Claude-powered drafting and lead research."""
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


# ---------------------------------------------------------------- research a new lead

LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "website": {"type": "string"},
        "industry": {"type": "string"},
        "country": {"type": "string"},
        "city": {"type": "string"},
        "contact_name": {"type": "string", "description": "Decision-maker's full name, or empty if not found"},
        "title": {"type": "string"},
        "email": {"type": "string", "description": "Best published email address, or empty if none was found"},
        "email_type": {"type": "string", "enum": ["Direct: decision-maker", "General inbox (address FAO decision-maker)",
                                                  "Company contact, other staff (address FAO decision-maker)", "Not found"]},
        "email_source": {"type": "string", "description": "URL where the email address was published"},
        "other_emails": {"type": "string"},
        "phone": {"type": "string"},
        "linkedin": {"type": "string"},
        "ai_need_score": {"type": "integer", "description": "0-100: how much they'd benefit from AI automation"},
        "evidence": {"type": "string", "description": "Bullet list (• ...) of concrete observations from their site"},
        "solution": {"type": "string", "description": "Bullet list (• ...) of specific AI solutions to pitch"},
        "services": {"type": "string", "description": "Which of the sender's services fit, joined with ' + '"},
        "opener": {"type": "string", "description": "2-3 sentence personalised email opener"},
        "check_note": {"type": "string", "description": "Anything the sender should verify before emailing"},
    },
    "required": ["company", "website", "industry", "country", "city", "contact_name", "title", "email", "email_type",
                 "email_source", "other_emails", "phone", "linkedin", "ai_need_score", "evidence", "solution",
                 "services", "opener", "check_note"],
    "additionalProperties": False,
}


def research_lead(query, settings):
    """Research a company on the web and return a structured lead. Two steps: web research, then extraction."""
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
    ]
    task = (
        f"Research this business as a potential client for AI automation services: {query}\n\n"
        f"About me (the seller):\n{settings.get('about_me', '')}\n\n"
        "Find and report:\n"
        "1. Company name, website, industry, city/country, size signals.\n"
        "2. The owner / CEO / managing partner (or the most senior relevant person) and their title.\n"
        "3. Email addresses that are PUBLISHED on their own website or a public business directory - give the page "
        "URL for each. Never guess or construct an address from a name pattern.\n"
        "4. Phone, LinkedIn.\n"
        "5. How they currently handle enquiries: live chat / chatbot / WhatsApp button / online booking / forms, "
        "languages, signs of paid ads, review volume, anything showing slow response or manual work.\n"
        "6. Concretely, which AI systems would save them time or win more leads, and why.\n"
        "If they already run an AI chatbot, say so clearly. Be factual; mark anything uncertain."
    )
    messages = [{"role": "user", "content": task}]
    resp = None
    for _ in range(5):  # resume server-side tool loops that pause
        resp = _create(model=MODEL, max_tokens=16000, output_config={"effort": "medium"}, tools=tools, messages=messages)
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": task}, {"role": "assistant", "content": resp.content}]
    report = _text(resp)
    if not report:
        raise AIError("Research returned nothing. Check the company name / URL and try again.")

    system = ("Convert a research report into a lead record. Use only what the report states. Leave a field as an "
              "empty string if it isn't in the report. Never invent an email address.")
    lead = _json_call(system, f"Seller profile:\n{settings.get('about_me', '')}\n\nResearch report:\n{report}",
                      LEAD_SCHEMA, effort="low")
    lead["report"] = report
    return lead
