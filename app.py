"""AI Outreach Agent: draft, send and track personalised cold emails from your lead sheet."""
import base64
import json
import os
import queue
import random
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file,
                   url_for)

import ai
import db
import importer
import mailer

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
        lead_id, step, instructions = AI_QUEUE.get()
        JOBS["running"] = True
        try:
            settings = db.get_settings()
            with db.tx() as conn:
                lead = get_lead(conn, lead_id)
                prev = sent_emails(conn, lead_id)
            out = ai.draft_email(lead, step, settings, instructions, prev)
            with db.tx() as conn:
                save_draft(conn, lead, step, out["subject"], out["body"], "ai")
        except Exception as e:  # noqa: BLE001
            JOBS["errors"].append(f"Lead {lead_id}: {e}")
        finally:
            JOBS["done"] += 1
            JOBS["running"] = not AI_QUEUE.empty()


def start_workers():
    for fn in (sender_loop, ai_loop):
        threading.Thread(target=fn, daemon=True).start()


# ------------------------------------------------------------------ pages

@app.context_processor
def inject_globals():
    return {"STATUSES": db.STATUSES, "STEP_NAMES": db.STEP_NAMES, "JOBS": JOBS,
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
    f = {k: request.args.get(k, "") for k in ("seg", "pri", "status", "q", "view")}
    where, args = ["1=1"], []
    if f["seg"]:
        where.append("l.segment=?"); args.append(f["seg"])
    if f["pri"]:
        where.append("l.priority LIKE ?"); args.append(f["pri"] + "%")
    if f["status"]:
        where.append("l.status=?"); args.append(f["status"])
    if f["q"]:
        where.append("(l.company LIKE ? OR l.contact_name LIKE ? OR l.email LIKE ? OR l.city LIKE ?)")
        args += [f"%{f['q']}%"] * 4
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
        stats = {
            "leads": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
            "drafts": conn.execute("SELECT COUNT(*) FROM emails WHERE state IN ('draft','failed')").fetchone()[0],
            "queued": conn.execute("SELECT COUNT(*) FROM emails WHERE state='queued'").fetchone()[0],
            "sent": conn.execute("SELECT COUNT(*) FROM emails WHERE state='sent'").fetchone()[0],
            "contacted": conn.execute("SELECT COUNT(DISTINCT lead_id) FROM emails WHERE state='sent'").fetchone()[0],
            "opened": conn.execute("SELECT COUNT(DISTINCT lead_id) FROM emails WHERE open_count>0").fetchone()[0],
            "replied": conn.execute("SELECT COUNT(*) FROM leads WHERE status IN ('Replied','Call booked','Proposal sent','Won')").fetchone()[0],
            "bounced": conn.execute("SELECT COUNT(*) FROM leads WHERE status='Bounced'").fetchone()[0],
            "today": sent_today(conn),
        }
        due = due_followups(conn, s)
    if f["view"] == "due":
        due_ids = {d["id"] for d in due}
        leads = [l for l in leads if l["id"] in due_ids]
    return render_template("dashboard.html", leads=leads, stats=stats, due=due, f=f, s=s,
                           paused=s.get("queue_paused") == "1", sender=SENDER_STATE)


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
    preview_footer = mailer.footer(s, lead)
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
        return redirect(request.referrer or url_for("dashboard"))
    done, skipped = 0, []
    if action in ("draft_sheet", "draft_ai"):
        if action == "draft_ai" and not ai.configured():
            flash("Add ANTHROPIC_API_KEY to .env to use AI drafting.", "error")
            return redirect(request.referrer or url_for("dashboard"))
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
                    if JOBS["done"] >= JOBS["total"]:
                        JOBS.update(total=0, done=0, errors=[])
                    JOBS["total"] += 1
                    AI_QUEUE.put((lid, step, request.form.get("instructions", "")))
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
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/check-inbox")
def check_inbox():
    try:
        c = mailer.check_inbox()
        flash(f"Scanned {c['scanned']} messages: {c['replies']} new replies, {c['auto']} auto-replies, "
              f"{c['bounces']} bounces, {c['unsubscribes']} opt-outs.")
    except Exception as e:  # noqa: BLE001
        flash(f"Inbox check failed: {e}", "error")
    return redirect(request.referrer or url_for("dashboard"))


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
            return redirect(url_for("dashboard"))
        except Exception as e:  # noqa: BLE001
            flash(f"Import failed: {e}", "error")
    return render_template("import.html", s=db.get_settings())


@app.route("/research", methods=["GET", "POST"])
def research_page():
    if request.method == "POST":
        q = request.form.get("query", "").strip()
        if not q:
            flash("Enter a website or company name.", "error")
        elif not ai.configured():
            flash("Add ANTHROPIC_API_KEY to .env to use research.", "error")
        else:
            try:
                r = ai.research_lead(q, db.get_settings())
                email_addr = r["email"] if "@" in r["email"] else ""
                score = max(0, min(100, int(r["ai_need_score"] or 0)))
                with db.tx() as conn:
                    cur = conn.execute(
                        "INSERT INTO leads (sheet_name, segment, company, website, contact_name, first_name, title, email, "
                        "email_type, other_emails, phone, linkedin, country, city, practice_area, score, priority, evidence, "
                        "solution, services, opener, check_note, sheet_subject, sheet_email1, raw, status, created_at, updated_at) "
                        "VALUES ('Researched','other',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Not contacted',?,?) "
                        "ON CONFLICT(sheet_name, company, email) DO UPDATE SET evidence=excluded.evidence, "
                        "solution=excluded.solution, opener=excluded.opener, updated_at=excluded.updated_at",
                        (r["company"] or q, r["website"], r["contact_name"], importer._first_name(r["contact_name"]),
                         r["title"], email_addr, r["email_type"], r["other_emails"], r["phone"], r["linkedin"],
                         r["country"], r["city"], r["industry"], score, importer._priority(score), r["evidence"],
                         r["solution"], r["services"], r["opener"],
                         (r["check_note"] + (f" Email source: {r['email_source']}" if r["email_source"] else "")).strip(),
                         f"Quick idea for {r['company']}", r["opener"],
                         json.dumps({"Research report": r["report"]}), db.now_iso(), db.now_iso()))
                    lid = cur.lastrowid or conn.execute("SELECT id FROM leads WHERE sheet_name='Researched' AND company=? AND email=?",
                                                        (r["company"] or q, email_addr)).fetchone()["id"]
                flash("Research done. Check the facts before you send." + ("" if email_addr else " No published email was found."))
                return redirect(url_for("lead_page", lead_id=lid))
            except ai.AIError as e:
                flash(str(e), "error")
    return render_template("research.html")


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
    return render_template("settings.html", s=db.get_settings(), from_email=c["from_email"], model=ai.MODEL,
                           smtp_host=c["smtp_host"], imap_host=c["imap_host"])


@app.post("/settings/test")
def settings_test():
    to = request.form.get("to") or mailer.cfg()["from_email"]
    try:
        mailer.send_test(to, db.get_settings())
        flash(f"Test email sent to {to}.")
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
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  AI Outreach Agent running at http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
