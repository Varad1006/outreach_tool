"""AI Outreach Agent: draft, send and track personalised cold emails from your lead sheet."""
import base64
import json
import os
import queue
import random
import re
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from markupsafe import Markup
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file,
                   url_for)

import ai
import db
import importer
import mailer
import scraper

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(16))
app.jinja_env.finalize = lambda v: "" if v is None else v  # show blanks, not "None"
db.init()

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
JOBS = {"total": 0, "done": 0, "errors": [], "running": False}
AI_QUEUE = queue.Queue()
SENDER_STATE = {"next_at": 0.0, "last_error": ""}


# ------------------------------------------------------------------ auth (only if APP_PASSWORD is set)

@app.before_request
def basic_auth():
    pw = os.environ.get("APP_PASSWORD")
    if not pw or request.path.startswith(("/t/", "/u/", "/static/")):
        return None
    auth = request.authorization
    if auth and secrets.compare_digest(auth.password or "", pw):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Outreach Agent"'})


# ------------------------------------------------------------------ helpers

def parse_ts(s):
    return datetime.fromisoformat(s) if s else None


def get_lead(conn, lead_id):
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        abort(404)
    return db.lead_dict(row)


def sent_emails(conn, lead_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM emails WHERE lead_id=? AND state='sent' ORDER BY step, sent_at", (lead_id,)).fetchall()]


def next_step(conn, lead_id):
    row = conn.execute("SELECT MAX(step) AS s FROM emails WHERE lead_id=? AND state IN ('sent','queued','sending')",
                       (lead_id,)).fetchone()
    return 0 if row["s"] is None else min(row["s"] + 1, 3)


def first_subject(conn, lead_id):
    row = conn.execute("SELECT subject FROM emails WHERE lead_id=? AND state IN ('sent','queued') ORDER BY step LIMIT 1",
                       (lead_id,)).fetchone()
    return row["subject"] if row else ""


def thread_subject(conn, lead, step, subject):
    if step == 0:
        return subject
    base = first_subject(conn, lead["id"]) or subject or f"Quick idea for {lead['company']}"
    return base if base.lower().startswith("re:") else f"Re: {base}"


def sheet_draft(conn, lead, step):
    if step == 0:
        subject = lead.get("sheet_subject") or f"Quick idea for {lead['company']}'s enquiries"
        body = lead.get("sheet_email1") or lead.get("opener") or ""
    elif step == 1:
        subject, body = "", lead.get("sheet_fu1") or ""
    else:
        subject, body = "", lead.get("sheet_fu2") or ""
    return thread_subject(conn, lead, step, subject), mailer.strip_signature(body)


def save_draft(conn, lead, step, subject, body, source, email_id=None):
    """Create or update the single editable draft for (lead, step)."""
    now = db.now_iso()
    subject = thread_subject(conn, lead, step, subject)
    existing = None
    if email_id:
        existing = conn.execute("SELECT * FROM emails WHERE id=? AND lead_id=? AND state IN ('draft','failed')",
                                (email_id, lead["id"])).fetchone()
    if not existing:
        existing = conn.execute("SELECT * FROM emails WHERE lead_id=? AND step=? AND state IN ('draft','failed') "
                                "ORDER BY id DESC LIMIT 1", (lead["id"], step)).fetchone()
    if existing:
        conn.execute("UPDATE emails SET subject=?, body=?, source=?, to_email=?, state='draft', error=NULL, updated_at=? "
                     "WHERE id=?", (subject, body, source, lead["email"], now, existing["id"]))
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO emails (lead_id, step, to_email, subject, body, state, source, token, created_at, updated_at) "
        "VALUES (?,?,?,?,?,'draft',?,?,?,?)",
        (lead["id"], step, lead["email"], subject, body, source, secrets.token_urlsafe(16), now, now))
    return cur.lastrowid


def send_block_reason(conn, lead, step):
    if lead["unsubscribed"]:
        return "this lead opted out"
    if lead["status"] in ("Bounced", "Not interested"):
        return f"status is '{lead['status']}'"
    if step > 0 and lead["status"] in db.STOP_STATUSES:
        return f"they're at '{lead['status']}' — reply personally instead of a follow-up"
    if step > 2:
        return "the 3-email sequence is already complete"
    if conn.execute("SELECT 1 FROM emails WHERE lead_id=? AND step=? AND state IN ('sent','queued','sending')",
                    (lead["id"], step)).fetchone():
        return "this step was already sent or queued"
    if step > 0 and not conn.execute("SELECT 1 FROM emails WHERE lead_id=? AND step=? AND state IN ('sent','queued','sending')",
                                     (lead["id"], step - 1)).fetchone():
        return "send the previous step first"
    if not lead["email"] or "@" not in lead["email"]:
        return "no valid email address"
    return None


def previous_message_id(conn, lead_id, step):
    row = conn.execute("SELECT message_id FROM emails WHERE lead_id=? AND step<? AND state='sent' AND message_id IS NOT NULL "
                       "ORDER BY step DESC LIMIT 1", (lead_id, step)).fetchone()
    return row["message_id"] if row else None


def deliver(email_id):
    """Send one email now. Returns (ok, message)."""
    settings = db.get_settings()
    with db.tx() as conn:
        em = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        if not em or em["state"] not in ("draft", "queued", "failed"):
            return False, "Email isn't sendable."
        em = dict(em)
        lead = get_lead(conn, em["lead_id"])
        # When sending a queued email, it already passed the check; re-check the opt-out / stop states only.
        reason = send_block_reason(conn, lead, em["step"]) if em["state"] != "queued" else (
            "lead opted out or stopped" if lead["unsubscribed"] or (em["step"] > 0 and lead["status"] in db.STOP_STATUSES)
            or lead["status"] in ("Bounced", "Not interested") else None)
        if reason:
            conn.execute("UPDATE emails SET state='cancelled', error=?, updated_at=? WHERE id=? AND state='queued'",
                         (reason, db.now_iso(), email_id))
            return False, f"Not sent: {reason}."
        em["in_reply_to"] = previous_message_id(conn, lead["id"], em["step"])
        em["to_email"] = lead["email"]
        conn.execute("UPDATE emails SET state='sending', updated_at=? WHERE id=?", (db.now_iso(), email_id))
    try:
        msg_id = mailer.send_email(em, lead, settings)
    except Exception as e:  # noqa: BLE001 - surface any SMTP problem to the UI
        with db.tx() as conn:
            conn.execute("UPDATE emails SET state='failed', error=?, updated_at=? WHERE id=?", (str(e), db.now_iso(), email_id))
            db.add_event(conn, lead["id"], "error", f"Send failed: {e}", email_id)
        return False, f"Send failed: {e}"
    with db.tx() as conn:
        conn.execute("UPDATE emails SET state='sent', sent_at=?, message_id=?, in_reply_to=?, to_email=?, "
                     "body=?, error=NULL, updated_at=? WHERE id=?",
                     (db.now_iso(), msg_id, em["in_reply_to"], em["to_email"],
                      mailer.compose_body(em["body"], settings, lead), db.now_iso(), email_id))
        if lead["status"] not in db.STOP_STATUSES:
            db.set_status(conn, lead["id"], db.STEP_STATUS[em["step"]], "sent")
        copy = mailer.SENT_COPY.get("last")
        if copy and copy[0] == "error":
            db.add_event(conn, lead["id"], "error", f"Sent, but no copy was saved to your Sent folder: {copy[1]}", email_id)
    if copy and copy[0] == "error":
        return True, f"Sent to {em['to_email']}, but no copy was saved to your Sent folder: {copy[1]}"
    return True, f"Sent to {em['to_email']}."


def sent_today(conn):
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return conn.execute("SELECT COUNT(*) AS n FROM emails WHERE state='sent' AND sent_at>=?", (start,)).fetchone()["n"]


def due_followups(conn, settings):
    """Leads whose next follow-up is due and not yet drafted/queued."""
    d1, d2 = int(settings["followup1_days"]), int(settings["followup2_days"])
    rows = conn.execute(
        "SELECT l.*, MAX(e.step) AS last_step, MAX(e.sent_at) AS last_sent FROM leads l "
        "JOIN emails e ON e.lead_id=l.id AND e.state='sent' "
        "WHERE l.unsubscribed=0 AND l.status IN ('Emailed','Follow-up 1 sent') GROUP BY l.id").fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        step = r["last_step"] + 1
        if step > 2:
            continue
        wait = d1 if step == 1 else d2
        due_at = parse_ts(r["last_sent"]) + timedelta(days=wait)
        if now < due_at:
            continue
        busy = conn.execute("SELECT 1 FROM emails WHERE lead_id=? AND step=? AND state IN ('queued','sending','sent')",
                            (r["id"], step)).fetchone()
        if not busy:
            d = dict(r)
            d["due_step"] = step
            d["overdue_days"] = (now - due_at).days
            out.append(d)
    return out


# ------------------------------------------------------------------ background workers

def sender_loop():
    while True:
        time.sleep(10)
        try:
            s = db.get_settings()
            if s.get("queue_paused") == "1" or time.time() < SENDER_STATE["next_at"]:
                continue
            with db.tx() as conn:
                if sent_today(conn) >= int(s["daily_limit"]):
                    continue
                row = conn.execute("SELECT id FROM emails WHERE state='queued' ORDER BY queued_at, id LIMIT 1").fetchone()
            if not row:
                continue
            ok, msg = deliver(row["id"])
            SENDER_STATE["last_error"] = "" if ok else msg
            gap = random.randint(int(s["gap_min_seconds"]), max(int(s["gap_min_seconds"]), int(s["gap_max_seconds"])))
            SENDER_STATE["next_at"] = time.time() + gap
        except Exception:  # keep the worker alive
            SENDER_STATE["last_error"] = traceback.format_exc(limit=1)


def ai_loop():
    while True:
        item = AI_QUEUE.get()
        JOBS["running"] = True
        try:
            settings = db.get_settings()
            if item.get("kind") == "revise":
                with db.tx() as conn:
                    em = conn.execute("SELECT * FROM emails WHERE id=? AND state IN ('draft','failed')", (item["email_id"],)).fetchone()
                    if not em:
                        continue  # sent, queued or deleted since it was picked: leave it alone
                    lead = get_lead(conn, em["lead_id"])
                    prev = sent_emails(conn, em["lead_id"])
                out = ai.revise_email(lead, em["step"], settings, em["subject"], em["body"], prev)
                with db.tx() as conn:
                    save_draft(conn, lead, em["step"], out["subject"], out["body"], "ai", em["id"])
            else:
                with db.tx() as conn:
                    lead = get_lead(conn, item["lead_id"])
                    prev = sent_emails(conn, item["lead_id"])
                out = ai.draft_email(lead, item["step"], settings, item.get("instructions", ""), prev)
                with db.tx() as conn:
                    save_draft(conn, lead, item["step"], out["subject"], out["body"], "ai")
        except Exception as e:  # noqa: BLE001
            JOBS["errors"].append(f"{item.get('label', 'Email')}: {e}")
        finally:
            JOBS["done"] += 1
            JOBS["running"] = not AI_QUEUE.empty()


def queue_ai(item):
    if JOBS["done"] >= JOBS["total"]:
        JOBS.update(total=0, done=0, errors=[])
    JOBS["total"] += 1
    AI_QUEUE.put(item)


def start_workers():
    for fn in (sender_loop, ai_loop, scraper_loop):
        threading.Thread(target=fn, daemon=True).start()


# ------------------------------------------------------------------ pages

@app.context_processor
def inject_globals():
    nav = {}
    if request.endpoint and not request.endpoint.startswith(("track_open", "unsubscribe", "static")):
        with db.tx() as conn:
            nav = {
                "leads": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                "drafts": conn.execute("SELECT COUNT(DISTINCT lead_id) FROM emails WHERE state IN ('draft','failed')").fetchone()[0],
                "queued": conn.execute("SELECT COUNT(*) FROM emails WHERE state='queued'").fetchone()[0],
                "jobs": conn.execute("SELECT COUNT(*) FROM scrape_jobs WHERE status IN ('pending','running','discovering')").fetchone()[0],
                "due": len(due_followups(conn, db.get_settings())),
            }
    return {"STATUSES": db.STATUSES, "STEP_NAMES": db.STEP_NAMES, "JOBS": JOBS, "NAV": nav,
            "ai_ok": ai.configured(), "smtp_ok": mailer.smtp_configured(), "imap_ok": mailer.imap_configured()}


@app.template_filter("ago")
def ago(ts):
    if not ts:
        return ""
    d = datetime.now(timezone.utc) - parse_ts(ts)
    if d.days >= 1:
        return f"{d.days}d ago"
    h = d.seconds // 3600
    return f"{h}h ago" if h else f"{max(1, d.seconds // 60)}m ago"


@app.route("/")
def dashboard():
    s = db.get_settings()
    with db.tx() as conn:
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
        stats = {
            "leads": one("SELECT COUNT(*) FROM leads"),
            "hot": one("SELECT COUNT(*) FROM leads WHERE priority LIKE 'A%'"),
            "contacted": one("SELECT COUNT(DISTINCT lead_id) FROM emails WHERE state='sent'"),
            "sent": one("SELECT COUNT(*) FROM emails WHERE state='sent'"),
            "opened": one("SELECT COUNT(DISTINCT lead_id) FROM emails WHERE open_count>0"),
            "replied": one("SELECT COUNT(*) FROM leads WHERE status IN ('Replied','Call booked','Proposal sent','Won')"),
            "bounced": one("SELECT COUNT(*) FROM leads WHERE status='Bounced'"),
            "queued": one("SELECT COUNT(*) FROM emails WHERE state='queued'"),
            "today": sent_today(conn),
        }
        funnel = [(st, one("SELECT COUNT(*) FROM leads WHERE status=?", st)) for st in db.STATUSES]
        due = due_followups(conn, s)
        hot = conn.execute("SELECT * FROM leads WHERE status='Not contacted' AND unsubscribed=0 AND email<>'' "
                           "AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.lead_id=leads.id AND e.state IN ('queued','sent')) "
                           "ORDER BY score DESC LIMIT 8").fetchall()
        activity = conn.execute("SELECT ev.*, l.company FROM events ev JOIN leads l ON l.id=ev.lead_id "
                                "WHERE ev.kind IN ('reply','bounce','unsubscribe','open','auto_reply') "
                                "ORDER BY ev.at DESC LIMIT 8").fetchall()
        jobs = conn.execute("SELECT * FROM scrape_jobs ORDER BY id DESC LIMIT 3").fetchall()
    stats["reply_rate"] = round(100 * stats["replied"] / stats["contacted"]) if stats["contacted"] else 0
    hour = datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return render_template("dashboard.html", stats=stats, funnel=funnel, due=due, hot=hot, activity=activity,
                           jobs=jobs, s=s, greet=greet, paused=s.get("queue_paused") == "1",
                           wait=max(0, int(SENDER_STATE["next_at"] - time.time())), sender=SENDER_STATE)


@app.route("/leads")
def leads_page():
    s = db.get_settings()
    f = {k: request.args.get(k, "") for k in ("seg", "pri", "status", "q", "view")}
    where, args = ["1=1"], []
    if f["seg"] == "scraped":
        where.append("l.sheet_name='Scraped'")
    elif f["seg"]:
        where.append("l.segment=? AND l.sheet_name<>'Scraped'"); args.append(f["seg"])
    if f["pri"]:
        where.append("l.priority LIKE ?"); args.append(f["pri"] + "%")
    if f["status"]:
        where.append("l.status=?"); args.append(f["status"])
    if f["q"]:
        where.append("(l.company LIKE ? OR l.contact_name LIKE ? OR l.email LIKE ? OR l.city LIKE ? OR l.practice_area LIKE ?)")
        args += [f"%{f['q']}%"] * 5
    if f["view"] == "drafts":
        where.append("EXISTS (SELECT 1 FROM emails e WHERE e.lead_id=l.id AND e.state IN ('draft','failed'))")
    elif f["view"] == "queued":
        where.append("EXISTS (SELECT 1 FROM emails e WHERE e.lead_id=l.id AND e.state='queued')")
    elif f["view"] == "opened":
        where.append("EXISTS (SELECT 1 FROM emails e WHERE e.lead_id=l.id AND e.open_count>0)")
    with db.tx() as conn:
        leads = conn.execute(
            "SELECT l.*, "
            "(SELECT COUNT(*) FROM emails e WHERE e.lead_id=l.id AND e.state='sent') AS n_sent, "
            "(SELECT SUM(open_count) FROM emails e WHERE e.lead_id=l.id) AS opens, "
            "(SELECT MAX(sent_at) FROM emails e WHERE e.lead_id=l.id AND e.state='sent') AS last_sent, "
            "(SELECT GROUP_CONCAT(state) FROM emails e WHERE e.lead_id=l.id AND e.state IN ('draft','queued','failed')) AS pending "
            f"FROM leads l WHERE {' AND '.join(where)} ORDER BY l.score DESC, l.company", args).fetchall()
        counts = {
            "": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
            "dubai": conn.execute("SELECT COUNT(*) FROM leads WHERE segment='dubai' AND sheet_name<>'Scraped'").fetchone()[0],
            "law": conn.execute("SELECT COUNT(*) FROM leads WHERE segment='law' AND sheet_name<>'Scraped'").fetchone()[0],
            "scraped": conn.execute("SELECT COUNT(*) FROM leads WHERE sheet_name='Scraped'").fetchone()[0],
        }
        due = due_followups(conn, s) if f["view"] == "due" else []
    if f["view"] == "due":
        due_ids = {d["id"] for d in due}
        leads = [l for l in leads if l["id"] in due_ids]
    titles = {"drafts": "Drafts to review", "due": "Follow-ups due", "queued": "Queued", "opened": "Opened"}
    return render_template("leads.html", leads=leads, f=f, counts=counts, s=s, title=titles.get(f["view"], "Leads"))


@app.route("/lead/<int:lead_id>")
def lead_page(lead_id):
    with db.tx() as conn:
        lead = get_lead(conn, lead_id)
        emails = [dict(r) for r in conn.execute("SELECT * FROM emails WHERE lead_id=? ORDER BY step, id", (lead_id,)).fetchall()]
        events = conn.execute("SELECT * FROM events WHERE lead_id=? ORDER BY at DESC LIMIT 100", (lead_id,)).fetchall()
        step = int(request.args.get("step", next_step(conn, lead_id)))
        draft = conn.execute("SELECT * FROM emails WHERE lead_id=? AND step=? AND state IN ('draft','failed') ORDER BY id DESC LIMIT 1",
                             (lead_id, step)).fetchone()
        if draft:
            draft = dict(draft)
        elif step <= 2:
            subj, body = sheet_draft(conn, lead, step)
            draft = {"id": None, "subject": subj, "body": body, "source": "sheet", "state": "new"}
        block = send_block_reason(conn, lead, step)
        ids = [r["id"] for r in conn.execute("SELECT id FROM leads ORDER BY score DESC, company").fetchall()]
    pos = ids.index(lead_id) if lead_id in ids else 0
    nav = {"prev": ids[pos - 1] if pos > 0 else None, "next": ids[pos + 1] if pos + 1 < len(ids) else None}
    s = db.get_settings()
    preview_footer = Markup(mailer.footer_html(s, lead))
    return render_template("lead.html", lead=lead, emails=emails, events=events, step=step, draft=draft,
                           block=block, nav=nav, preview_footer=preview_footer, s=s)


@app.post("/lead/<int:lead_id>/update")
def lead_update(lead_id):
    with db.tx() as conn:
        lead = get_lead(conn, lead_id)
        status = request.form.get("status")
        if status in db.STATUSES:
            db.set_status(conn, lead_id, status, "manual")
        new_email = request.form.get("email", "").strip()
        if new_email and new_email != lead["email"]:
            conn.execute("UPDATE leads SET email=? WHERE id=?", (new_email, lead_id))
            db.add_event(conn, lead_id, "note", f"Email changed {lead['email']} → {new_email}")
        conn.execute("UPDATE leads SET notes=?, unsubscribed=?, updated_at=? WHERE id=?",
                     (request.form.get("notes", ""), 1 if request.form.get("unsubscribed") else 0, db.now_iso(), lead_id))
    flash("Lead updated.")
    return redirect(url_for("lead_page", lead_id=lead_id))


@app.post("/lead/<int:lead_id>/compose")
def lead_compose(lead_id):
    action = request.form.get("action")
    step = int(request.form.get("step", 0))
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    email_id = request.form.get("email_id") or None
    with db.tx() as conn:
        lead = get_lead(conn, lead_id)
        if action == "sheet":
            subj, b = sheet_draft(conn, lead, step)
            save_draft(conn, lead, step, subj, b, "sheet", email_id)
            flash("Loaded the draft from your sheet.")
        elif action == "save":
            save_draft(conn, lead, step, subject, body, "manual", email_id)
            flash("Draft saved.")
        elif action == "discard" and email_id:
            conn.execute("DELETE FROM emails WHERE id=? AND lead_id=? AND state IN ('draft','failed')", (email_id, lead_id))
            flash("Draft discarded.")
    if action == "ai":
        if not ai.configured():
            flash("Add ANTHROPIC_API_KEY to .env to use AI drafting.", "error")
        else:
            try:
                with db.tx() as conn:
                    prev = sent_emails(conn, lead_id)
                out = ai.draft_email(lead, step, db.get_settings(), request.form.get("instructions", ""), prev)
                with db.tx() as conn:
                    save_draft(conn, lead, step, out["subject"], out["body"], "ai", email_id)
                flash(f"AI draft ready. Angle: {out.get('why', '')}")
            except ai.AIError as e:
                flash(str(e), "error")
    elif action in ("send", "queue"):
        with db.tx() as conn:
            reason = send_block_reason(conn, lead, step)
            if reason:
                flash(f"Can't send: {reason}.", "error")
                return redirect(url_for("lead_page", lead_id=lead_id, step=step))
            if not body or not subject:
                flash("Subject and body are required.", "error")
                return redirect(url_for("lead_page", lead_id=lead_id, step=step))
            eid = save_draft(conn, lead, step, subject, body, request.form.get("source") or "manual", email_id)
            if action == "queue":
                conn.execute("UPDATE emails SET state='queued', queued_at=?, updated_at=? WHERE id=?",
                             (db.now_iso(), db.now_iso(), eid))
        if action == "send":
            ok, msg = deliver(eid)
            flash(msg, None if ok else "error")
        else:
            flash("Added to the send queue. It'll go out within your daily limit, spaced out.")
        return redirect(url_for("lead_page", lead_id=lead_id))
    return redirect(url_for("lead_page", lead_id=lead_id, step=step))


@app.post("/bulk")
def bulk():
    ids = [int(i) for i in request.form.getlist("ids")]
    action = request.form.get("action") or request.form.get("set_status")
    if not ids:
        flash("Select some leads first.", "error")
        return redirect(request.referrer or url_for("leads_page"))
    done, skipped = 0, []
    if action in ("draft_sheet", "draft_ai"):
        if action == "draft_ai" and not ai.configured():
            flash("Add ANTHROPIC_API_KEY to .env to use AI drafting.", "error")
            return redirect(request.referrer or url_for("leads_page"))
        with db.tx() as conn:
            for lid in ids:
                lead = get_lead(conn, lid)
                step = next_step(conn, lid)
                reason = send_block_reason(conn, lead, step)
                if reason:
                    skipped.append(f"{lead['company']} ({reason})")
                    continue
                if action == "draft_sheet":
                    subj, b = sheet_draft(conn, lead, step)
                    save_draft(conn, lead, step, subj, b, "sheet")
                else:
                    queue_ai({"kind": "draft", "lead_id": lid, "step": step, "label": lead["company"],
                              "instructions": request.form.get("instructions", "")})
                done += 1
        flash(f"{'Drafted' if action == 'draft_sheet' else 'Writing AI drafts for'} {done} lead(s)." +
              (" AI drafts appear as they finish (refresh)." if action == "draft_ai" else ""))
    elif action == "queue":
        with db.tx() as conn:
            for lid in ids:
                lead = get_lead(conn, lid)
                d = conn.execute("SELECT * FROM emails WHERE lead_id=? AND state IN ('draft','failed') ORDER BY step LIMIT 1",
                                 (lid,)).fetchone()
                if not d:
                    skipped.append(f"{lead['company']} (no draft)")
                    continue
                reason = send_block_reason(conn, lead, d["step"])
                if reason:
                    skipped.append(f"{lead['company']} ({reason})")
                    continue
                conn.execute("UPDATE emails SET state='queued', queued_at=?, to_email=?, updated_at=? WHERE id=?",
                             (db.now_iso(), lead["email"], db.now_iso(), d["id"]))
                done += 1
        flash(f"Queued {done} email(s). They go out spaced {db.get_settings()['gap_min_seconds']}s+ apart, within your daily limit.")
    elif action in db.STATUSES:
        with db.tx() as conn:
            for lid in ids:
                db.set_status(conn, lid, action, "bulk")
                done += 1
        flash(f"Set {done} lead(s) to '{action}'.")
    if skipped:
        flash("Skipped: " + "; ".join(skipped[:15]) + (" …" if len(skipped) > 15 else ""), "error")
    return redirect(request.referrer or url_for("leads_page"))


@app.post("/check-inbox")
def check_inbox():
    try:
        c = mailer.check_inbox()
        flash(f"Scanned {c['scanned']} messages: {c['replies']} new replies, {c['auto']} auto-replies, "
              f"{c['bounces']} bounces, {c['unsubscribes']} opt-outs.")
    except Exception as e:  # noqa: BLE001
        flash(f"Inbox check failed: {e}", "error")
    return redirect(request.referrer or url_for("leads_page"))


@app.route("/queue")
def queue_page():
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT e.*, l.company, l.contact_name FROM emails e JOIN leads l ON l.id=e.lead_id "
            "WHERE e.state IN ('queued','sending','failed') OR (e.state='sent' AND e.sent_at>=?) "
            "ORDER BY CASE e.state WHEN 'sending' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, "
            "e.queued_at, e.sent_at DESC",
            ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),)).fetchall()
        today = sent_today(conn)
    s = db.get_settings()
    wait = max(0, int(SENDER_STATE["next_at"] - time.time()))
    return render_template("queue.html", rows=rows, today=today, s=s, wait=wait, sender=SENDER_STATE,
                           paused=s.get("queue_paused") == "1")


@app.post("/queue/toggle")
def queue_toggle():
    s = db.get_settings()
    db.set_setting("queue_paused", "0" if s.get("queue_paused") == "1" else "1")
    return redirect(url_for("queue_page"))


@app.post("/email/<int:email_id>/cancel")
def email_cancel(email_id):
    with db.tx() as conn:
        conn.execute("UPDATE emails SET state='draft', queued_at=NULL, updated_at=? WHERE id=? AND state IN ('queued','failed')",
                     (db.now_iso(), email_id))
    flash("Moved back to drafts.")
    return redirect(request.referrer or url_for("queue_page"))


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "POST":
        dest = os.path.join(db.DATA_DIR, "upload.xlsx")
        try:
            if request.files.get("file") and request.files["file"].filename:
                request.files["file"].save(dest)
            elif request.form.get("sheet_url"):
                importer.download_google_sheet(request.form["sheet_url"].strip(), dest)
                db.set_setting("sheet_url", request.form["sheet_url"].strip())
            else:
                flash("Choose a file or paste a Google Sheets link.", "error")
                return redirect(url_for("import_page"))
            r = importer.import_workbook(dest)
            flash(f"Imported {', '.join(r['sheets']) or 'nothing'}: {r['added']} new, {r['updated']} updated, "
                  f"{r['skipped']} skipped (no email).")
            return redirect(url_for("leads_page"))
        except Exception as e:  # noqa: BLE001
            flash(f"Import failed: {e}", "error")
    return render_template("import.html", s=db.get_settings())


@app.route("/research")
def research_page():
    return redirect(url_for("scraper_page"))


# ------------------------------------------------------------------ scraper agent

def known_domains(conn):
    out = set()
    for r in conn.execute("SELECT website, email FROM leads").fetchall():
        if r["website"]:
            out.add(urlparse(r["website"] if "//" in r["website"] else "//" + r["website"]).netloc.lower().removeprefix("www."))
        if r["email"] and "@" in r["email"]:
            out.add(r["email"].split("@")[-1].lower())
    out.discard("")
    return out


def add_scraped_lead(conn, row, segment):
    """Insert one scraped row into leads. Returns the lead id, or None if it has no email."""
    if not row.get("email"):
        return None
    raw = {"Chat / AI on website": row.get("chat"), "Ad tracking detected": row.get("ads"),
           "Online booking": row.get("booking"), "Practice mgmt / CRM visible": row.get("crm"),
           "Site languages": row.get("languages"), "WhatsApp (from site)": row.get("whatsapp"),
           "Company LinkedIn": row.get("company_linkedin"), "Other key contacts": row.get("other_contacts"),
           "DM source": row.get("dm_source"), "Email source": row.get("email_source"),
           "Claims on site": row.get("claims"), "Listed on": row.get("portals"), "Pages crawled": row.get("pages")}
    now = db.now_iso()
    vals = dict(company=row["company"] or row["domain"], website=row["website"], contact_name=row["contact_name"],
                first_name=importer._first_name(row["contact_name"]), title=row["title"], email=row["email"],
                email_type=row["email_type"], other_emails=row["other_emails"], phone=row["phone"],
                linkedin=row["linkedin"], country=row["country"], city=row["city"], practice_area=row["industry"],
                score=row["score"], priority=row["priority"], evidence=row["evidence"], solution=row["solution"],
                services=row["services"], opener=row["opener"], check_note=row["check_note"],
                sheet_subject=row["subject"], sheet_email1=row["email1"] or row["opener"], sheet_fu1=row["fu1"],
                sheet_fu2=row["fu2"], raw=json.dumps({k: v for k, v in raw.items() if v}, ensure_ascii=False))
    existing = conn.execute("SELECT id FROM leads WHERE sheet_name='Scraped' AND company=? AND email=?",
                            (vals["company"], vals["email"])).fetchone()
    if existing:
        conn.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in vals)}, updated_at=? WHERE id=?",
                     list(vals.values()) + [now, existing["id"]])
        return existing["id"]
    cur = conn.execute(
        f"INSERT INTO leads (sheet_name, segment, status, created_at, updated_at, {', '.join(vals)}) "
        f"VALUES ('Scraped', ?, 'Not contacted', ?, ?, {', '.join('?' for _ in vals)})",
        [segment, now, now] + list(vals.values()))
    return cur.lastrowid


def _job_cancelled(job_id):
    with db.tx() as conn:
        return conn.execute("SELECT status FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()["status"] == "cancelled"


def run_scrape_job(job_id):
    with db.tx() as conn:
        job = dict(conn.execute("SELECT * FROM scrape_jobs WHERE id=?", (job_id,)).fetchone())
    opts = json.loads(job["options"] or "{}")
    settings = db.get_settings()
    use_ai = opts.get("use_ai") and ai.configured()

    if job["mode"] == "discover":
        with db.tx() as conn:
            conn.execute("UPDATE scrape_jobs SET status='discovering' WHERE id=?", (job_id,))
            skip = known_domains(conn)
        try:
            found = ai.discover_companies(job["query"], int(opts.get("count", 15)), skip, opts.get("context", ""))
        except Exception as e:  # noqa: BLE001
            with db.tx() as conn:
                conn.execute("UPDATE scrape_jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                             (str(e), db.now_iso(), job_id))
            return
        with db.tx() as conn:
            for c in found:
                conn.execute("INSERT INTO scrape_results (job_id, url, status, created_at) VALUES (?,?,'pending',?)",
                             (job_id, c["website"], db.now_iso()))

    with db.tx() as conn:
        results = [dict(r) for r in conn.execute("SELECT * FROM scrape_results WHERE job_id=? AND status='pending'", (job_id,))]
        conn.execute("UPDATE scrape_jobs SET status='running', total=(SELECT COUNT(*) FROM scrape_results WHERE job_id=?) "
                     "WHERE id=?", (job_id, job_id))
        skip = known_domains(conn) if opts.get("skip_known") else set()

    seen = set()
    seen_lock = threading.Lock()

    def work(res):
        if _job_cancelled(job_id):
            return
        status, reason, row, lead_id = "ok", "", None, None
        try:
            with db.tx() as conn:
                conn.execute("UPDATE scrape_results SET status='running' WHERE id=?", (res["id"],))
            facts = scraper.crawl(res["url"], int(opts.get("pages", 6)), not opts.get("ignore_robots"), opts.get("render_js"))
            domain = facts.get("domain", "")
            with seen_lock:
                dup = domain in seen
                seen.add(domain)
            if facts["status"] != "ok":
                status, reason = "failed", "; ".join(facts["errors"][:2])
            elif dup or domain in skip:
                status, reason = "skipped", "already in your leads" if domain in skip else "duplicate in this job"
            elif facts.get("ai_chat") and opts.get("exclude_ai"):
                status, reason = "skipped", f"already runs an AI chatbot ({', '.join(facts['ai_chat'])})"
            else:
                row = scraper.build_row(facts, use_ai, settings, job["segment"], opts.get("deep"))
                if not row["email"]:
                    reason = "no published email found"
                if opts.get("auto_add") and row["email"]:
                    with db.tx() as conn:
                        lead_id = add_scraped_lead(conn, row, job["segment"])
        except Exception as e:  # noqa: BLE001
            status, reason = "failed", str(e)[:300]
            domain = res.get("domain") or ""
        with db.tx() as conn:
            conn.execute("UPDATE scrape_results SET status=?, reason=?, domain=?, row=?, lead_id=? WHERE id=?",
                         (status, reason, domain, json.dumps(row, ensure_ascii=False) if row else None, lead_id, res["id"]))
            conn.execute("UPDATE scrape_jobs SET done=done+1 WHERE id=?", (job_id,))

    with ThreadPoolExecutor(max_workers=int(opts.get("workers", 3))) as ex:
        list(ex.map(work, results))
    with db.tx() as conn:
        conn.execute("UPDATE scrape_jobs SET status=CASE WHEN status='cancelled' THEN 'cancelled' ELSE 'done' END, "
                     "finished_at=? WHERE id=?", (db.now_iso(), job_id))


def scraper_loop():
    with db.tx() as conn:  # jobs interrupted by a restart go back to pending
        conn.execute("UPDATE scrape_results SET status='pending' WHERE status='running'")
        conn.execute("UPDATE scrape_jobs SET status='pending' WHERE status IN ('running','discovering')")
    while True:
        time.sleep(3)
        with db.tx() as conn:
            job = conn.execute("SELECT id FROM scrape_jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if job:
            try:
                run_scrape_job(job["id"])
            except Exception as e:  # noqa: BLE001
                with db.tx() as conn:
                    conn.execute("UPDATE scrape_jobs SET status='failed', error=? WHERE id=?", (str(e), job["id"]))


# ---- profile-driven targeting (what to scrape is decided from your profile + past leads)

POSITIVE = ("Replied", "Call booked", "Proposal sent", "Won")


def lead_stats(conn):
    """Past leads grouped by segment: volume, outreach, replies, average AI Need Score."""
    rows = conn.execute(
        "SELECT CASE WHEN sheet_name='Scraped' THEN COALESCE(NULLIF(practice_area,''),'Scraped') "
        "            WHEN segment='dubai' THEN 'Dubai real estate brokerages' "
        "            WHEN segment='law' THEN 'Law firms: ' || COALESCE(NULLIF(practice_area,''),'general') ELSE sheet_name END AS seg, "
        "COALESCE(NULLIF(country,''), CASE WHEN segment='dubai' THEN 'UAE' ELSE '' END) AS country, "
        "COUNT(*) AS leads, ROUND(AVG(score)) AS avg_score, "
        "SUM(CASE WHEN status<>'Not contacted' THEN 1 ELSE 0 END) AS contacted, "
        f"SUM(CASE WHEN status IN {POSITIVE} THEN 1 ELSE 0 END) AS replied, "
        "SUM(CASE WHEN status IN ('Bounced','Not interested') THEN 1 ELSE 0 END) AS negative "
        "FROM leads GROUP BY seg, country ORDER BY replied DESC, leads DESC LIMIT 30").fetchall()
    return [dict(r) for r in rows]


def best_examples(conn, n=12):
    rows = conn.execute(
        "SELECT company, practice_area, country, city, score, status, substr(evidence, 1, 160) AS why FROM leads "
        f"ORDER BY CASE WHEN status IN {POSITIVE} THEN 0 ELSE 1 END, score DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


def read_resume(upload):
    """Return (text, pdf_bytes) from an uploaded resume (.pdf, .docx, .txt/.md)."""
    name = (upload.filename or "").lower()
    data = upload.read()
    if name.endswith(".pdf"):
        return "", data
    if name.endswith(".docx"):
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml), None
    return data.decode("utf-8", "replace"), None


@app.post("/scraper/profile")
def scraper_profile():
    s = db.get_settings()
    website = request.form.get("profile_website", "").strip()
    skills = request.form.get("profile_skills", "").strip()
    db.set_setting("profile_website", website)
    db.set_setting("profile_skills", skills)
    resume_path = os.path.join(db.DATA_DIR, "resume.pdf")
    resume_text, resume_pdf = s.get("profile_resume_text", ""), None
    up = request.files.get("resume")
    if up and up.filename:
        try:
            resume_text, resume_pdf = read_resume(up)
        except Exception as e:  # noqa: BLE001
            flash(f"Couldn't read that resume ({e}). Use PDF, DOCX or TXT.", "error")
            return redirect(url_for("scraper_page"))
        if resume_pdf:
            with open(resume_path, "wb") as f:
                f.write(resume_pdf)
            resume_text = ""
        elif os.path.exists(resume_path):
            os.remove(resume_path)
        db.set_setting("profile_resume_text", resume_text[:20000])
        db.set_setting("profile_resume_name", up.filename)
    elif os.path.exists(resume_path):
        with open(resume_path, "rb") as f:
            resume_pdf = f.read()
    if not ai.configured():
        flash("Saved. Add ANTHROPIC_API_KEY to .env so Claude can analyse your profile.", "error")
        return redirect(url_for("scraper_page"))
    site_text = ""
    if website:
        try:
            site_text = scraper.crawl(website, max_pages=6).get("page_text", "")
        except Exception:  # noqa: BLE001 - the website is optional context
            site_text = ""
    try:
        prof = ai.analyze_profile(site_text, resume_text, resume_pdf, skills, s.get("about_me", ""))
    except ai.AIError as e:
        flash(str(e), "error")
        return redirect(url_for("scraper_page"))
    db.set_setting("profile_json", json.dumps(prof, ensure_ascii=False))
    if request.form.get("update_about") and prof.get("summary"):
        db.set_setting("about_me", prof["summary"] + ("\nServices: " + "; ".join(prof["services"]) if prof.get("services") else ""))
    flash("Profile analysed." + ("" if site_text or not website else " (Your website couldn't be read, so it was skipped.)")
          + " Now get target suggestions.")
    return redirect(url_for("scraper_page") + "#targets")


@app.post("/scraper/targets")
def scraper_targets():
    s = db.get_settings()
    if not ai.configured():
        flash("Add ANTHROPIC_API_KEY to .env to get target suggestions.", "error")
        return redirect(url_for("scraper_page"))
    profile = json.loads(s.get("profile_json") or "{}") or {"about_me": s.get("about_me", ""), "skills": s.get("profile_skills", "")}
    with db.tx() as conn:
        stats, examples = lead_stats(conn), best_examples(conn)
    try:
        plan = ai.build_targeting(profile, stats, examples)
    except ai.AIError as e:
        flash(str(e), "error")
        return redirect(url_for("scraper_page"))
    plan["generated_at"] = db.now_iso()
    db.set_setting("targeting_plan", json.dumps(plan, ensure_ascii=False))
    flash(f"{len(plan['segments'])} target segments suggested. Tick the ones you want and click Find leads.")
    return redirect(url_for("scraper_page") + "#targets")


@app.route("/scraper", methods=["GET", "POST"])
def scraper_page():
    s = db.get_settings()
    plan = json.loads(s.get("targeting_plan") or "{}")
    if request.method == "POST":
        if not ai.configured():
            flash("Finding leads needs Claude: add ANTHROPIC_API_KEY to .env.", "error")
            return redirect(url_for("scraper_page"))
        opts = {k: bool(request.form.get(k)) for k in ("deep", "exclude_ai", "skip_known", "auto_add", "render_js")}
        opts.update(use_ai=True, pages=6, count=max(1, min(40, int(request.form.get("count") or 10))))
        segs = plan.get("segments", [])
        chosen = [segs[int(i)] for i in request.form.getlist("seg") if i.isdigit() and int(i) < len(segs)]
        custom = request.form.get("custom", "").strip()
        if custom:
            chosen.append({"name": custom[:80], "search_query": custom, "location": "", "industry_key": "other",
                           "why_fit": "", "signals": [], "pitch_angle": ""})
        if request.form.get("lookalike"):
            with db.tx() as conn:
                ex = best_examples(conn, 8)
            if ex:
                names = "; ".join(f"{e['company']} ({e['practice_area'] or ''} {e['city'] or e['country'] or ''})".strip() for e in ex)
                chosen.append({"name": "Lookalikes of my best leads",
                               "search_query": "businesses similar to these (same industry, size and region): " + names,
                               "location": "", "industry_key": "other",
                               "why_fit": "They match the leads that replied or scored highest.", "signals": [], "pitch_angle": ""})
        if not chosen:
            flash("Tick at least one target segment (or describe one).", "error")
            return redirect(url_for("scraper_page") + "#targets")
        prof = json.loads(s.get("profile_json") or "{}")
        first = None
        with db.tx() as conn:
            for seg in chosen:
                ctx = "\n".join(x for x in [
                    seg.get("why_fit", ""),
                    ("Signs they need help: " + ", ".join(seg.get("signals", []))) if seg.get("signals") else "",
                    ("Offer: " + seg["pitch_angle"]) if seg.get("pitch_angle") else "",
                    ("Seller: " + prof.get("headline", "")) if prof.get("headline") else "",
                ] if x)
                o = dict(opts, context=ctx)
                query = seg["search_query"] + (f" in {seg['location']}" if seg.get("location") and seg["location"].lower() not in seg["search_query"].lower() else "")
                cur = conn.execute("INSERT INTO scrape_jobs (name, segment, mode, query, options, status, total, created_at) "
                                   "VALUES (?,?,'discover',?,?,'pending',0,?)",
                                   (seg["name"][:120], seg.get("industry_key", "other"), query, json.dumps(o), db.now_iso()))
                first = first or cur.lastrowid
        flash(f"Started {len(chosen)} lead search{'es' if len(chosen) > 1 else ''}. Claude finds the businesses, then each website is scraped.")
        return redirect(url_for("scrape_job_page", job_id=first) if len(chosen) == 1 else url_for("scraper_page") + "#jobs")
    with db.tx() as conn:
        jobs = conn.execute(
            "SELECT j.*, (SELECT COUNT(*) FROM scrape_results r WHERE r.job_id=j.id AND r.status='ok') AS ok, "
            "(SELECT COUNT(*) FROM scrape_results r WHERE r.job_id=j.id AND r.lead_id IS NOT NULL) AS added "
            "FROM scrape_jobs j ORDER BY j.id DESC LIMIT 50").fetchall()
        stats = lead_stats(conn)
        has_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] > 0
    return render_template("scraper.html", jobs=jobs, s=s, plan=plan, stats=stats[:6], has_leads=has_leads,
                           profile=json.loads(s.get("profile_json") or "{}"),
                           has_resume=os.path.exists(os.path.join(db.DATA_DIR, "resume.pdf")) or bool(s.get("profile_resume_text")))


@app.route("/scraper/<int:job_id>")
def scrape_job_page(job_id):
    with db.tx() as conn:
        job = conn.execute("SELECT * FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            abort(404)
        res = []
        for r in conn.execute("SELECT * FROM scrape_results WHERE job_id=? ORDER BY "
                              "CASE status WHEN 'ok' THEN 0 WHEN 'running' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END, id",
                              (job_id,)).fetchall():
            d = dict(r)
            d["row"] = json.loads(d["row"]) if d["row"] else None
            res.append(d)
    res.sort(key=lambda d: (d["status"] != "ok", -(d["row"] or {}).get("score", 0)))
    return render_template("scrape_job.html", job=job, results=res, opts=json.loads(job["options"] or "{}"))


@app.route("/scraper/<int:job_id>/status")
def scrape_job_status(job_id):
    with db.tx() as conn:
        job = conn.execute("SELECT status, total, done FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
    return jsonify(dict(job) if job else {})


@app.post("/scraper/<int:job_id>/add")
def scrape_job_add(job_id):
    ids = request.form.getlist("rid")
    added = skipped = 0
    with db.tx() as conn:
        job = conn.execute("SELECT segment FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
        q = "SELECT * FROM scrape_results WHERE job_id=? AND status='ok' AND row IS NOT NULL"
        rows = conn.execute(q, (job_id,)).fetchall()
        for r in rows:
            if ids and str(r["id"]) not in ids:
                continue
            lid = add_scraped_lead(conn, json.loads(r["row"]), job["segment"])
            if lid:
                conn.execute("UPDATE scrape_results SET lead_id=? WHERE id=?", (lid, r["id"]))
                added += 1
            else:
                skipped += 1
    flash(f"Added {added} lead(s) to your pipeline." + (f" {skipped} skipped (no email found)." if skipped else ""))
    return redirect(url_for("scrape_job_page", job_id=job_id))


@app.post("/scraper/<int:job_id>/cancel")
def scrape_job_cancel(job_id):
    with db.tx() as conn:
        conn.execute("UPDATE scrape_jobs SET status='cancelled', finished_at=? WHERE id=? AND status IN ('pending','running','discovering')",
                     (db.now_iso(), job_id))
        conn.execute("UPDATE scrape_results SET status='skipped', reason='cancelled' WHERE job_id=? AND status='pending'", (job_id,))
    flash("Scrape stopped.")
    return redirect(url_for("scrape_job_page", job_id=job_id))


@app.post("/scraper/<int:job_id>/delete")
def scrape_job_delete(job_id):
    with db.tx() as conn:
        conn.execute("DELETE FROM scrape_jobs WHERE id=? AND status NOT IN ('running','discovering')", (job_id,))
    flash("Scrape job deleted (leads you added stay in your pipeline).")
    return redirect(url_for("scraper_page"))


@app.route("/scraper/<int:job_id>/export")
def scrape_job_export(job_id):
    with db.tx() as conn:
        job = conn.execute("SELECT name FROM scrape_jobs WHERE id=?", (job_id,)).fetchone()
        rows = [json.loads(r["row"]) for r in conn.execute(
            "SELECT row FROM scrape_results WHERE job_id=? AND status='ok' AND row IS NOT NULL", (job_id,))]
    dest = os.path.join(db.DATA_DIR, f"scrape_{job_id}.xlsx")
    scraper.export_xlsx(rows, dest)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", job["name"] or "scrape").strip("_")[:40]
    return send_file(dest, as_attachment=True, download_name=f"scraped_{safe}.xlsx")


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        for k in db.DEFAULT_SETTINGS:
            if k == "queue_paused":
                continue
            if k == "track_opens":
                db.set_setting(k, "1" if request.form.get(k) else "0")
            elif k in request.form:
                db.set_setting(k, request.form[k].strip())
        flash("Settings saved.")
        return redirect(url_for("settings_page"))
    c = mailer.cfg()
    with db.tx() as conn:
        n_drafts = conn.execute("SELECT COUNT(*) FROM emails WHERE state IN ('draft','failed')").fetchone()[0]
    return render_template("settings.html", s=db.get_settings(), from_email=c["from_email"], model=ai.MODEL,
                           smtp_host=c["smtp_host"], imap_host=c["imap_host"], n_drafts=n_drafts)


@app.post("/settings/rules")
def settings_rules():
    db.set_setting("email_addition", request.form.get("email_addition", "").strip())
    db.set_setting("email_rules", request.form.get("email_rules", "").strip())
    if request.form.get("action") != "apply":
        flash("Rules saved. The 'add to every email' text now shows in every draft; AI rules apply to new AI drafts.")
        return redirect(url_for("settings_page") + "#rules")
    if not request.form.get("email_rules", "").strip():
        flash("Write at least one AI rule first. The fixed text already appears in every email without AI.", "error")
        return redirect(url_for("settings_page") + "#rules")
    if not ai.configured():
        flash("Rewriting drafts needs Claude: add ANTHROPIC_API_KEY to .env.", "error")
        return redirect(url_for("settings_page") + "#rules")
    with db.tx() as conn:
        drafts = conn.execute("SELECT e.id, l.company FROM emails e JOIN leads l ON l.id=e.lead_id "
                              "WHERE e.state IN ('draft','failed') ORDER BY e.id").fetchall()
    for d in drafts:
        queue_ai({"kind": "revise", "email_id": d["id"], "label": d["company"]})
    flash(f"Rules saved. Rewriting {len(drafts)} draft{'s' if len(drafts) != 1 else ''} in the background; "
          "progress shows at the top of the page." if drafts else "Rules saved. There are no drafts to rewrite yet.")
    return redirect(url_for("leads_page", view="drafts") if drafts else url_for("settings_page") + "#rules")


@app.post("/settings/test")
def settings_test():
    to = request.form.get("to") or mailer.cfg()["from_email"]
    try:
        mailer.send_test(to, db.get_settings())
        copy = mailer.SENT_COPY.get("last")
        note = (f" A copy is in your '{copy[1]}' folder." if copy and copy[0] == "ok" and copy[1] else
                f" But the copy for your Sent folder failed: {copy[1]}" if copy and copy[0] == "error" else "")
        flash(f"Test email sent to {to}.{note}", "error" if copy and copy[0] == "error" else None)
    except Exception as e:  # noqa: BLE001
        flash(f"Test failed: {e}", "error")
    return redirect(url_for("settings_page"))


@app.route("/export")
def export():
    dest = os.path.join(db.DATA_DIR, "leads_export.xlsx")
    importer.export_workbook(dest)
    return send_file(dest, as_attachment=True,
                     download_name=f"leads_{datetime.now().strftime('%Y-%m-%d')}.xlsx")


@app.route("/api/jobs")
def api_jobs():
    return jsonify(JOBS)


# ------------------------------------------------------------------ public tracking endpoints

@app.route("/t/<token>.gif")
def track_open(token):
    with db.tx() as conn:
        em = conn.execute("SELECT id, lead_id, opened_at, sent_at FROM emails WHERE token=? AND state='sent'", (token,)).fetchone()
        # Ignore hits in the first 20s after sending (mail scanners pre-fetching images).
        if em and (datetime.now(timezone.utc) - parse_ts(em["sent_at"])).total_seconds() > 20:
            conn.execute("UPDATE emails SET open_count=open_count+1, opened_at=COALESCE(opened_at, ?) WHERE id=?",
                         (db.now_iso(), em["id"]))
            if not em["opened_at"]:
                db.add_event(conn, em["lead_id"], "open", "First open", em["id"])
    return Response(PIXEL, mimetype="image/gif", headers={"Cache-Control": "no-store, max-age=0"})


@app.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    with db.tx() as conn:
        em = conn.execute("SELECT lead_id FROM emails WHERE token=?", (token,)).fetchone()
        if not em:
            abort(404)
        if request.method == "POST":
            conn.execute("UPDATE leads SET unsubscribed=1 WHERE id=?", (em["lead_id"],))
            db.add_event(conn, em["lead_id"], "unsubscribe", "Clicked unsubscribe link")
            db.set_status(conn, em["lead_id"], "Not interested", "unsubscribed")
            return "<p style='font-family:sans-serif'>You're unsubscribed. You won't hear from me again.</p>"
    return ("<form method=post style='font-family:sans-serif'><p>Stop receiving emails from me?</p>"
            "<button>Unsubscribe</button></form>")


if __name__ == "__main__":
    start_workers()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))  # 5000 is taken by AirPlay Receiver on macOS
    print(f"\n  AI Outreach Agent running at http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
