"""SQLite storage for leads, emails, events and settings."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "outreach.db")
_lock = threading.RLock()

STATUSES = [
    "Not contacted", "Emailed", "Follow-up 1 sent", "Follow-up 2 sent", "Replied",
    "Call booked", "Proposal sent", "Won", "Not interested", "Bounced",
]
# Statuses where the sequence should stop (no more automated follow-ups).
STOP_STATUSES = {"Replied", "Call booked", "Proposal sent", "Won", "Not interested", "Bounced"}
STEP_STATUS = {0: "Emailed", 1: "Follow-up 1 sent", 2: "Follow-up 2 sent"}
STEP_NAMES = {0: "First email", 1: "Follow-up 1", 2: "Follow-up 2"}

DEFAULT_SETTINGS = {
    "sender_name": "Varad Adhyapak",
    "signature": "Varad Adhyapak · AI Automation Engineer · varad-tech.site",
    "about_me": (
        "I'm Varad Adhyapak, an AI automation engineer (varad-tech.site). I build production AI systems for "
        "small and mid-size businesses: 24/7 website & WhatsApp chat assistants, AI voice calling agents that "
        "call new leads within 60 seconds, AI agents for lead follow-up and CRM updates, paperwork & workflow "
        "automation, and 'ask-your-documents' assistants. I've shipped production chat + voice agents "
        "(English + regional language) for a public-services platform. Fixed-price projects, live in 2-3 weeks."
    ),
    "postal_address": "",
    "opt_out_line": "Not relevant? Just reply 'unsubscribe' and I won't email you again.",
    "daily_limit": "30",
    "gap_min_seconds": "90",
    "gap_max_seconds": "240",
    "followup1_days": "3",
    "followup2_days": "5",
    "track_opens": "0",
    "public_url": "",
    "queue_paused": "0",
}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_name TEXT NOT NULL,
    row_num INTEGER,
    segment TEXT,
    company TEXT,
    website TEXT,
    contact_name TEXT,
    first_name TEXT,
    title TEXT,
    email TEXT,
    email_type TEXT,
    other_emails TEXT,
    phone TEXT,
    linkedin TEXT,
    country TEXT,
    city TEXT,
    practice_area TEXT,
    score REAL,
    priority TEXT,
    evidence TEXT,
    solution TEXT,
    services TEXT,
    opener TEXT,
    check_note TEXT,
    sheet_subject TEXT,
    sheet_email1 TEXT,
    sheet_fu1 TEXT,
    sheet_fu2 TEXT,
    status TEXT DEFAULT 'Not contacted',
    notes TEXT DEFAULT '',
    unsubscribed INTEGER DEFAULT 0,
    raw TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(sheet_name, company, email)
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    step INTEGER NOT NULL DEFAULT 0,
    to_email TEXT,
    subject TEXT,
    body TEXT,
    state TEXT NOT NULL DEFAULT 'draft',   -- draft | queued | sending | sent | failed | cancelled
    source TEXT,                            -- sheet | ai | manual
    token TEXT UNIQUE,
    message_id TEXT,
    in_reply_to TEXT,
    queued_at TEXT,
    sent_at TEXT,
    opened_at TEXT,
    open_count INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_lead ON emails(lead_id);
CREATE INDEX IF NOT EXISTS idx_emails_state ON emails(state);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    email_id INTEGER,
    kind TEXT NOT NULL,     -- open | reply | auto_reply | bounce | unsubscribe | status | note | error
    detail TEXT,
    ext_id TEXT UNIQUE,     -- e.g. IMAP Message-ID, for de-duplication
    at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    segment TEXT DEFAULT 'other',
    mode TEXT,                 -- urls | discover
    query TEXT,
    options TEXT,              -- json
    status TEXT DEFAULT 'pending',   -- pending | discovering | running | done | failed | cancelled
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS scrape_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES scrape_jobs(id) ON DELETE CASCADE,
    url TEXT,
    domain TEXT,
    status TEXT DEFAULT 'pending',   -- pending | running | ok | failed | skipped
    reason TEXT,
    row TEXT,                  -- json lead row
    lead_id INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_job ON scrape_results(job_id);
"""


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_settings():
    with tx() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def set_setting(key, value):
    with tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def add_event(conn, lead_id, kind, detail="", email_id=None, ext_id=None):
    try:
        conn.execute("INSERT INTO events(lead_id, email_id, kind, detail, ext_id, at) VALUES (?,?,?,?,?,?)",
                     (lead_id, email_id, kind, detail, ext_id, now_iso()))
        return True
    except sqlite3.IntegrityError:  # duplicate ext_id
        return False


def set_status(conn, lead_id, status, reason=""):
    row = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row or row["status"] == status:
        return
    conn.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?", (status, now_iso(), lead_id))
    add_event(conn, lead_id, "status", f"{row['status']} → {status}" + (f" ({reason})" if reason else ""))
    if status in STOP_STATUSES:
        # Stop any queued follow-ups for this lead.
        conn.execute("UPDATE emails SET state='cancelled', updated_at=? WHERE lead_id=? AND state='queued'",
                     (now_iso(), lead_id))


def lead_dict(row):
    d = dict(row)
    try:
        d["raw"] = json.loads(d.get("raw") or "{}")
    except ValueError:
        d["raw"] = {}
    return d
