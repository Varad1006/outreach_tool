"""Import leads from the lead workbook (.xlsx file or a Google Sheets link)."""
import json
import os
import re
import shutil
import urllib.request

import openpyxl

import db

SOURCE_COPY = os.path.join(db.DATA_DIR, "source.xlsx")

# Sheet name -> segment key. Any other sheet that has a recognisable header row is imported as "other".
KNOWN_SHEETS = {"Dubai Real Estate": "dubai", "Law Firms": "law"}

# Column aliases: first match wins. Lets the importer cope with slightly different sheets.
COLS = {
    "company": ["Company", "Firm", "Company name", "Business"],
    "website": ["Website", "URL", "Site"],
    "contact_name": ["Decision-maker", "Contact", "Contact name", "Name"],
    "title": ["Title", "Role", "Job title"],
    "email": ["Send to", "Best email to use", "Email", "Email address"],
    "email_type": ["Email type"],
    "other_emails": ["Other emails"],
    "phone": ["Phone", "Phone number"],
    "linkedin": ["DM LinkedIn", "LinkedIn"],
    "country": ["Country"],
    "city": ["City / region", "City", "Office address", "Address"],
    "practice_area": ["Main practice area", "Industry"],
    "score": ["AI Need Score", "Score"],
    "evidence": ["Why they need AI (evidence)", "Why they need AI"],
    "solution": ["How: AI solution to pitch", "AI solution to pitch", "How AI can help"],
    "services": ["Your services to pitch", "Services to pitch"],
    "opener": ["Personalised opener (email / LinkedIn)", "Personalised opener", "Opener"],
    "check_note": ["Check before sending"],
    "sheet_subject": ["Subject"],
    "sheet_email1": ["Email 1 (ready to send)", "Email 1"],
    "sheet_fu1": ["Follow-up 1 (day 3-4)", "Follow-up 1"],
    "sheet_fu2": ["Follow-up 2 (day 8-10)", "Follow-up 2"],
    "status": ["Outreach status", "Status"],
    "notes": ["Notes"],
}


def google_sheet_to_xlsx_url(url):
    """Turn any docs.google.com/spreadsheets link into its .xlsx export link."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError("That doesn't look like a Google Sheets link.")
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"


def download_google_sheet(url, dest):
    req = urllib.request.Request(google_sheet_to_xlsx_url(url), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not data.startswith(b"PK"):
        raise ValueError("Google didn't return a spreadsheet. Set the sheet's sharing to "
                         "'Anyone with the link can view', or download it as .xlsx and upload it instead.")
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return "" if s.lower() == "none" else s


def _priority(score):
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ""
    return "A: Hot" if s >= 70 else "B: Warm" if s >= 60 else "C: Nurture"


def _parse_templates(wb):
    """Read the 'Email Templates' sheet into {segment: {subject, email1, fu1, fu2}}."""
    out = {}
    if "Email Templates" not in wb.sheetnames:
        return out
    seg = None
    for a, b in wb["Email Templates"].iter_rows(min_col=1, max_col=2, values_only=True):
        a = _clean(a)
        if a.upper().startswith("DUBAI"):
            seg = "dubai"
            out[seg] = {}
        elif a.upper().startswith("LAW"):
            seg = "law"
            out[seg] = {}
        elif seg and b:
            key = ("subject" if a.lower().startswith("subject") else "email1" if a.lower().startswith("email 1")
                   else "fu1" if "follow-up 1" in a.lower() else "fu2" if "follow-up 2" in a.lower() else None)
            if key:
                out[seg][key] = str(b)
    return out


def _merge(template, rec):
    """Fill {Field} merge tags from a row dict (column names) plus a few friendly aliases."""
    def rep(m):
        key = m.group(1).strip()
        if key == "Personalised opener column":
            return rec.get("Personalised opener (email / LinkedIn)", "")
        if key == "First name":
            return rec.get("_first_name", "")
        if key.startswith("Your postal address"):
            return "{postal_address}"  # filled at send time from Settings
        return _clean(rec.get(key, m.group(0)))
    return re.sub(r"\{([^{}]+)\}", rep, template)


def _first_name(full):
    full = re.sub(r"^(dr|mr|mrs|ms|miss)\.?\s+", "", (full or "").strip(), flags=re.I)
    return full.split()[0] if full else ""


def import_workbook(path):
    """Import every lead sheet. Returns a summary dict."""
    wb = openpyxl.load_workbook(path, data_only=False)
    templates = _parse_templates(wb)
    summary = {"added": 0, "updated": 0, "skipped": 0, "sheets": []}

    for ws in wb.worksheets:
        if ws.title in ("README", "Summary", "Email Templates"):
            continue
        rows = ws.iter_rows(values_only=True)
        header = [(_clean(h)) for h in next(rows, [])]
        idx = {}
        for field, aliases in COLS.items():
            for a in aliases:
                if a in header:
                    idx[field] = header.index(a)
                    break
        if "company" not in idx or "email" not in idx:
            continue  # not a lead sheet
        segment = KNOWN_SHEETS.get(ws.title, "other")
        summary["sheets"].append(ws.title)
        tmpl = templates.get(segment, {})

        with db.tx() as c:
            for row_num, row in enumerate(rows, start=2):
                rec = {header[i]: row[i] for i in range(min(len(header), len(row))) if header[i]}
                f = {k: _clean(row[i]) if i < len(row) else "" for k, i in idx.items()}
                if not f["company"]:
                    continue
                if not f["email"] and "Best email to use" in rec:
                    f["email"] = _clean(rec["Best email to use"])
                if not f["email"]:
                    summary["skipped"] += 1
                    continue
                f["first_name"] = _first_name(f.get("contact_name"))
                rec["_first_name"] = f["first_name"]
                f["priority"] = _priority(f.get("score"))
                try:
                    f["score"] = float(f["score"]) if f.get("score") else None
                except ValueError:
                    f["score"] = None

                # Sheets without ready-made emails (e.g. Law Firms): build them from the Email Templates sheet.
                if not f.get("sheet_email1") and tmpl.get("email1") and f.get("opener"):
                    body = _merge(tmpl["email1"], rec)
                    body = re.sub(r"\s*\(see the 'How' column\)", "", body)  # note to self in the template, not for clients
                    if "15-minute call" in f["opener"]:
                        body = body.replace("\n\nOpen to a 15-minute call next week?", "")  # opener already asks
                    if "general inbox" in f.get("email_type", "").lower() or "other staff" in f.get("email_type", "").lower():
                        body = f"FAO {f.get('contact_name')}, {f.get('title')}\n\n" + body
                    f["sheet_email1"] = body
                    subj = tmpl.get("subject", "").split("·")[0].strip()
                    f["sheet_subject"] = _merge(subj, rec) if subj else ""
                    if tmpl.get("fu1"):
                        f["sheet_fu1"] = _merge(tmpl["fu1"], rec) + "\n\nVarad"
                    if tmpl.get("fu2"):
                        f["sheet_fu2"] = _merge(tmpl["fu2"], rec)

                raw = json.dumps({k: _clean(v) for k, v in rec.items() if not k.startswith("_")}, ensure_ascii=False)
                sheet_status = f.pop("status", "") or "Not contacted"
                sheet_notes = f.pop("notes", "")
                cols = ["segment", "row_num", "raw"] + [k for k in f.keys()]
                vals = [segment, row_num, raw] + list(f.values())

                existing = c.execute("SELECT id, status, notes FROM leads WHERE sheet_name=? AND company=? AND email=?",
                                     (ws.title, f["company"], f["email"])).fetchone()
                if existing:
                    sets = ", ".join(f"{k}=?" for k in cols)
                    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?", vals + [db.now_iso(), existing["id"]])
                    # Pull pipeline progress from the sheet only if the app hasn't tracked anything yet.
                    if existing["status"] == "Not contacted" and sheet_status in db.STATUSES and sheet_status != "Not contacted":
                        c.execute("UPDATE leads SET status=? WHERE id=?", (sheet_status, existing["id"]))
                    if not existing["notes"] and sheet_notes:
                        c.execute("UPDATE leads SET notes=? WHERE id=?", (sheet_notes, existing["id"]))
                    summary["updated"] += 1
                else:
                    status = sheet_status if sheet_status in db.STATUSES else "Not contacted"
                    c.execute(
                        f"INSERT INTO leads (sheet_name, status, notes, created_at, updated_at, {', '.join(cols)}) "
                        f"VALUES (?, ?, ?, ?, ?, {', '.join('?' for _ in cols)})",
                        [ws.title, status, sheet_notes, db.now_iso(), db.now_iso()] + vals)
                    summary["added"] += 1

    if os.path.abspath(path) != os.path.abspath(SOURCE_COPY):
        shutil.copyfile(path, SOURCE_COPY)  # kept so "Export" can write statuses back into your original workbook
    return summary


def export_workbook(dest):
    """Write current status / notes / activity back into a copy of the imported workbook."""
    wb = openpyxl.load_workbook(SOURCE_COPY) if os.path.exists(SOURCE_COPY) else openpyxl.Workbook()
    with db.tx() as c:
        leads = c.execute("SELECT * FROM leads").fetchall()
        activity = {r["lead_id"]: r for r in c.execute(
            "SELECT lead_id, MAX(sent_at) AS last_sent, COUNT(*) AS n, SUM(open_count) AS opens "
            "FROM emails WHERE state='sent' GROUP BY lead_id").fetchall()}

    extra = []
    for lead in leads:
        act = activity.get(lead["id"])
        note = lead["notes"] or ""
        if act:
            note = (note + " | " if note else "") + f"App: {act['n']} sent, last {act['last_sent'][:10]}, opens {act['opens'] or 0}"
        if lead["sheet_name"] in wb.sheetnames and lead["row_num"]:
            ws = wb[lead["sheet_name"]]
            header = [c.value for c in ws[1]]
            if "Outreach status" in header:
                ws.cell(row=lead["row_num"], column=header.index("Outreach status") + 1, value=lead["status"])
            if "Notes" in header:
                ws.cell(row=lead["row_num"], column=header.index("Notes") + 1, value=note or None)
        else:
            extra.append((lead, note))

    if extra:
        name = "Researched Leads"
        ws = wb[name] if name in wb.sheetnames else wb.create_sheet(name)
        ws.delete_rows(1, ws.max_row)
        ws.append(["Company", "Website", "Decision-maker", "Title", "Email", "Country", "AI Need Score",
                   "Why they need AI (evidence)", "How: AI solution to pitch", "Your services to pitch",
                   "Outreach status", "Notes"])
        for lead, note in extra:
            ws.append([lead["company"], lead["website"], lead["contact_name"], lead["title"], lead["email"],
                       lead["country"], lead["score"], lead["evidence"], lead["solution"], lead["services"],
                       lead["status"], note])
    wb.save(dest)
    return dest
