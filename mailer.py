"""Sending (SMTP) and reply / bounce detection (IMAP)."""
import email
import email.header
import html
import imaplib
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import db

FREE_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com",
                "aol.com", "proton.me", "protonmail.com", "gmx.com", "yandex.com", "zoho.com", "mail.com"}


def cfg():
    user = os.environ.get("SMTP_USER", "")
    return {
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "smtp_user": user,
        "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
        "from_email": os.environ.get("FROM_EMAIL", user),
        "reply_to": os.environ.get("REPLY_TO", ""),
        "imap_host": os.environ.get("IMAP_HOST", "imap.gmail.com"),
        "imap_port": int(os.environ.get("IMAP_PORT", "993")),
        "imap_user": os.environ.get("IMAP_USER", user),
        "imap_password": os.environ.get("IMAP_PASSWORD", os.environ.get("SMTP_PASSWORD", "")),
    }


def smtp_configured():
    c = cfg()
    return bool(c["smtp_host"] and c["smtp_user"] and c["smtp_password"])


def imap_configured():
    c = cfg()
    return bool(c["imap_host"] and c["imap_user"] and c["imap_password"])


def footer(settings, lead):
    lines = [settings.get("signature", "").strip()]
    addr = settings.get("postal_address", "").strip()
    if addr:
        lines.append(addr)
    opt = settings.get("opt_out_line", "").strip()
    if opt:
        lines.append(opt)
    return "\n".join(l for l in lines if l)


def strip_signature(body):
    """Remove a signature block / opt-out lines that came from the sheet so ours is used instead."""
    out = []
    for line in body.splitlines():
        s = line.strip()
        if re.match(r"^varad adhyapak\b", s, re.I) or s.startswith("{postal_address}"):
            break
        out.append(line)
    text = "\n".join(out).rstrip()
    text = re.sub(r"\n+\s*[—-]?\s*Varad\s*$", "", text)  # trailing "— Varad" / "Varad"
    text = re.sub(r"\s*[—-]\s*Varad\s*$", "", text)
    return text.rstrip()


# Links: write [text](https://url) anywhere in an email or the signature. In the signature/footer, plain web
# addresses (varad-tech.site) and email addresses also become clickable.
MD_LINK = re.compile(r"\[([^\]\n]+)\]\(((?:https?://|mailto:)[^\s)]+)\)")
URL_RE = re.compile(r"https?://[^\s<>()]+[^\s<>().,;:!?'\"]")
EMAIL_RE_LINK = re.compile(r"(?<![\w.+-])([\w.+-]+@[\w-]+(?:\.[\w-]+)+)")
DOMAIN_RE = re.compile(
    r"(?<![@/\w.-])((?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|site|ai|io|co|net|org|in|ae|uk|ca|dev|app|"
    r"tech|agency|me|xyz|info|biz|us|eu|online|store|studio|digital|solutions|consulting|services)"
    r"(?:/[^\s<]*)?)(?![\w-])", re.I)


def _norm_url(u):
    u = re.sub(r"^(https?://|mailto:)", "", u.strip().lower())
    return u.removeprefix("www.").rstrip("/")


def plain_links(text):
    """Markdown links -> readable plain text: 'text (url)', or just the text when it already is the address."""
    def rep(m):
        t, u = m.group(1), m.group(2)
        return t if _norm_url(t) == _norm_url(u) else f"{t} ({u.removeprefix('mailto:')})"
    return MD_LINK.sub(rep, text)


def _link_html(escaped, domains=False):
    """Turn links in already-HTML-escaped text into <a> tags (markdown links, URLs, and optionally domains/emails)."""
    keep = []

    def hold(html_snippet):
        keep.append(html_snippet)
        return f"\x00{len(keep) - 1}\x00"

    a = 'style="color:#1a56db;text-decoration:underline"'
    q = lambda u: u.replace('"', "%22")  # noqa: E731 - keep hrefs inside their quotes
    escaped = MD_LINK.sub(lambda m: hold(f'<a href="{q(m.group(2))}" {a}>{m.group(1)}</a>'), escaped)
    escaped = URL_RE.sub(lambda m: hold(f'<a href="{q(m.group(0))}" {a}>{m.group(0)}</a>'), escaped)
    if domains:
        escaped = EMAIL_RE_LINK.sub(lambda m: hold(f'<a href="mailto:{m.group(1)}" {a}>{m.group(1)}</a>'), escaped)
        escaped = DOMAIN_RE.sub(lambda m: hold(f'<a href="https://{m.group(1)}" {a}>{m.group(1)}</a>'), escaped)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], escaped)


def _paras(text, domains=False):
    out = []
    for p in text.split("\n\n"):
        if p.strip():
            out.append(f'<p style="margin:0 0 12px">{_link_html(html.escape(p, quote=False), domains).replace(chr(10), "<br>")}</p>')
    return "".join(out)


def footer_html(settings, lead):
    """The signature block as HTML, for previews in the app."""
    return _link_html(html.escape(footer(settings, lead), quote=False), True).replace("\n", "<br>")


def compose_body(body, settings, lead):
    """Plain-text version of the email (used for the text/plain part and the app's history)."""
    return plain_links(strip_signature(body) + "\n\n" + footer(settings, lead))


def compose_html(body, settings, lead, pixel_url=None):
    pixel = f'<img src="{pixel_url}" width="1" height="1" alt="" style="display:block;border:0">' if pixel_url else ""
    sig = _paras(footer(settings, lead), domains=True)
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">'
            f"{_paras(strip_signature(body))}{sig}{pixel}</div>")


def send_email(em, lead, settings):
    """Send one email row. Returns the Message-ID. Raises on failure."""
    c = cfg()
    if not smtp_configured():
        raise RuntimeError("SMTP isn't configured. Fill in SMTP_* in your .env file.")
    from_email = c["from_email"]
    domain = from_email.split("@")[-1] if "@" in from_email else None

    full_body = compose_body(em["body"], settings, lead)
    public = settings.get("public_url", "").rstrip("/")
    pixel = f"{public}/t/{em['token']}.gif" if public and settings.get("track_opens") == "1" else None

    msg = EmailMessage()
    msg["From"] = formataddr((settings.get("sender_name") or "", from_email))
    msg["To"] = em["to_email"]
    msg["Subject"] = em["subject"]
    msg["Message-ID"] = make_msgid(domain=domain)
    if c["reply_to"]:
        msg["Reply-To"] = c["reply_to"]
    if em.get("in_reply_to"):
        msg["In-Reply-To"] = em["in_reply_to"]
        msg["References"] = em["in_reply_to"]
    unsub = [f"<mailto:{c['reply_to'] or from_email}?subject=unsubscribe>"]
    if public:
        unsub.insert(0, f"<{public}/u/{em['token']}>")
    msg["List-Unsubscribe"] = ", ".join(unsub)
    msg.set_content(full_body)
    msg.add_alternative(compose_html(em["body"], settings, lead, pixel), subtype="html")

    _smtp_deliver(c, msg)
    return msg["Message-ID"]


def _smtp_hint(host):
    h = host.lower()
    if "gmail" in h or "google" in h:
        return ("Gmail / Google Workspace needs an App Password (Google Account → Security → 2-Step Verification → "
                "App passwords), not your normal password.")
    if "zoho" in h:
        return "Zoho accounts in India use smtp.zoho.in (EU: smtp.zoho.eu); smtppro.zoho.in for paid org mailboxes."
    if "office365" in h or "outlook" in h:
        return "Microsoft 365 needs 'Authenticated SMTP' enabled for the mailbox; personal Outlook.com accounts often block SMTP."
    if "titan" in h or "hostinger" in h or "secureserver" in h:
        return "Hosting mailboxes usually want port 465 (SSL) and the full email address as SMTP_USER."
    return "Check SMTP_HOST and SMTP_PORT with your email provider's SMTP settings page."


def _smtp_deliver(c, msg):
    """Send via SMTP. If the configured port can't be reached (reset / refused / TLS error) before anything was
    sent, retry once on the other standard port: 587 (STARTTLS) <-> 465 (SSL)."""
    host, port = c["smtp_host"], c["smtp_port"]
    attempts = [(port, port == 465)]
    alt = (587, False) if port == 465 else (465, True)
    attempts.append(alt)
    context = ssl.create_default_context()
    errors = []
    for p, use_ssl in attempts:
        sending = False
        try:
            if use_ssl:
                s = smtplib.SMTP_SSL(host, p, context=context, timeout=30)
            else:
                s = smtplib.SMTP(host, p, timeout=30)
            with s:
                s.ehlo()
                if not use_ssl:
                    s.starttls(context=context)
                    s.ehlo()
                s.login(c["smtp_user"], c["smtp_password"])
                sending = True
                s.send_message(msg)
            return p
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(f"{host} rejected the login for {c['smtp_user']} ({e.smtp_code}). {_smtp_hint(host)}") from e
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
            raise RuntimeError(f"{host} refused the message: {e}") from e
        except (OSError, smtplib.SMTPException) as e:  # reset, refused, timeout, TLS errors, disconnects
            if sending:  # the server may already have the message: don't risk sending it twice
                raise RuntimeError(f"Connection dropped while sending via {host}:{p} ({e}). Check your Sent folder "
                                   "before retrying.") from e
            errors.append(f"port {p}{' SSL' if use_ssl else ' STARTTLS'}: {type(e).__name__}: {e}")
    raise RuntimeError(f"Couldn't connect to {host} — " + "; ".join(errors) + ". " + _smtp_hint(host) +
                       " A VPN, antivirus or office network that blocks outgoing mail ports can also cause this.")


def send_test(to_addr, settings):
    em = {"to_email": to_addr, "subject": "Test from your AI Outreach Agent", "token": "test",
          "body": "This is a test email. If you can read it, sending works."}
    return send_email(em, {"contact_name": ""}, settings)


# ------------------------------------------------------------------ IMAP reply / bounce checking

def _decode(value):
    if not value:
        return ""
    parts = email.header.decode_header(value)
    return "".join(p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p for p, enc in parts)


def _body_text(msg, limit=20000):
    chunks = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() in ("text/plain", "message/delivery-status", "text/rfc822-headers", "text/html"):
            try:
                payload = part.get_payload(decode=True) or b""
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                continue
    return "\n".join(chunks)[:limit]


def _snippet(msg):
    """The new text of a reply (quoted history removed). Prefers the text/plain part."""
    plain = html_part = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        try:
            text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html_part:
            html_part = text
    if not plain and html_part:
        h = re.split(r'<blockquote|<div class="gmail_quote|<div id="appendonsend|<hr', html_part, maxsplit=1, flags=re.I)[0]
        plain = html.unescape(re.sub(r"<br\s*/?>|</p>|</div>", "\n", re.sub(r"<(style|script)[^>]*>.*?</\1>", "", h, flags=re.S | re.I), flags=re.I))
        plain = re.sub(r"<[^>]+>", " ", plain)
    lines = []
    for line in plain.splitlines():
        s = line.strip()
        if s.startswith(">") or re.match(r"^(On .*(wrote:)?|From: .*|-{2,}\s*Original Message\s*-{2,}|_{5,})$", s):
            break
        if s:
            lines.append(s)
    return " ".join(lines)[:400]


def check_inbox(days=30):
    """Scan the inbox for replies, auto-replies, bounces and unsubscribe requests. Returns counts."""
    c = cfg()
    if not imap_configured():
        raise RuntimeError("IMAP isn't configured. Fill in IMAP_* (or SMTP_*) in your .env file.")

    with db.tx() as conn:
        sent = conn.execute(
            "SELECT e.id, e.lead_id, e.to_email, e.message_id, e.sent_at, l.email AS lead_email, l.website "
            "FROM emails e JOIN leads l ON l.id = e.lead_id WHERE e.state='sent'").fetchall()
    if not sent:
        return {"scanned": 0, "replies": 0, "bounces": 0, "auto": 0, "unsubscribes": 0}

    by_msgid = {r["message_id"].strip("<>"): r for r in sent if r["message_id"]}
    by_addr, by_domain = {}, {}
    for r in sent:
        for a in {r["to_email"].lower(), (r["lead_email"] or "").lower()} - {""}:
            by_addr.setdefault(a, r)
            d = a.split("@")[-1]
            if d not in FREE_DOMAINS:
                by_domain.setdefault(d, r)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    counts = {"scanned": 0, "replies": 0, "bounces": 0, "auto": 0, "unsubscribes": 0}
    imap = imaplib.IMAP4_SSL(c["imap_host"], c["imap_port"])
    try:
        imap.login(c["imap_user"], c["imap_password"])
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SINCE", since)
        ids = data[0].split()[-3000:] if typ == "OK" else []
        for num in ids:
            typ, hdr = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID IN-REPLY-TO REFERENCES AUTO-SUBMITTED X-AUTOREPLY DATE)])")
            if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                continue
            h = email.message_from_bytes(hdr[0][1])
            counts["scanned"] += 1
            sender = parseaddr(h.get("From", ""))[1].lower()
            subject = _decode(h.get("Subject", ""))
            ext_id = (h.get("Message-ID") or f"imap-{num.decode()}").strip()
            refs = re.findall(r"<([^>]+)>", (h.get("In-Reply-To", "") + " " + h.get("References", "")))

            is_bounce = bool(re.match(r"(mailer-daemon|postmaster|mail-daemon)@", sender)) or \
                re.search(r"undeliverable|delivery status notification|returned mail|delivery failure|failure notice", subject, re.I)
            if is_bounce:
                typ, full = imap.fetch(num, "(BODY.PEEK[])")
                if typ != "OK":
                    continue
                body = _body_text(email.message_from_bytes(full[0][1])).lower()
                hit = next((r for a, r in by_addr.items() if a in body), None)
                if hit:
                    with db.tx() as conn:
                        if db.add_event(conn, hit["lead_id"], "bounce", subject, hit["id"], ext_id):
                            db.set_status(conn, hit["lead_id"], "Bounced", "bounce received")
                            counts["bounces"] += 1
                continue

            match = next((by_msgid[r] for r in refs if r in by_msgid), None) or by_addr.get(sender)
            if not match and "@" in sender:
                match = by_domain.get(sender.split("@")[-1])
            if not match:
                continue
            typ, full = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1]) if typ == "OK" else h
            snippet = _snippet(msg)
            auto = (h.get("Auto-Submitted", "no").lower() not in ("", "no")) or h.get("X-Autoreply") or \
                re.search(r"out of (the )?office|automatic reply|auto.?reply|away from", subject, re.I)
            with db.tx() as conn:
                if auto:
                    if db.add_event(conn, match["lead_id"], "auto_reply", f"{subject}: {snippet}", match["id"], ext_id):
                        counts["auto"] += 1
                elif re.search(r"\b(unsubscribe|remove me|stop emailing|no thanks|not interested)\b",
                               subject + " " + snippet[:300], re.I):
                    if db.add_event(conn, match["lead_id"], "unsubscribe", f"From {sender}: {snippet}", match["id"], ext_id):
                        conn.execute("UPDATE leads SET unsubscribed=1 WHERE id=?", (match["lead_id"],))
                        db.set_status(conn, match["lead_id"], "Not interested", "asked to opt out")
                        counts["unsubscribes"] += 1
                else:
                    if db.add_event(conn, match["lead_id"], "reply", f"From {sender} — {subject}: {snippet}", match["id"], ext_id):
                        row = conn.execute("SELECT status FROM leads WHERE id=?", (match["lead_id"],)).fetchone()
                        if row and row["status"] in ("Not contacted", "Emailed", "Follow-up 1 sent", "Follow-up 2 sent"):
                            db.set_status(conn, match["lead_id"], "Replied", "reply detected")
                        conn.execute("UPDATE emails SET state='cancelled', updated_at=? WHERE lead_id=? AND state='queued'",
                                     (db.now_iso(), match["lead_id"]))
                        counts["replies"] += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return counts
