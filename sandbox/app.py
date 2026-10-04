"""Acme Corp sandbox: the "company computer" the worker operates on.

Runs as its own process (default port 8100) so the agent talks to it over real
HTTP through a real browser, the same way it would talk to any internal tool.
Everything lives under /acme so it can be reverse proxied behind the worker API.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import secrets
from datetime import date, datetime, timedelta
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db

P = "/acme"
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
ADMIN_TOKEN = os.environ.get("SANDBOX_ADMIN_TOKEN", "")
SESSION_LIMIT = 5  # requests before a forced expiry when the session_expiry chaos mode is on

app = FastAPI(title="Acme Corp sandbox", docs_url=None, redoc_url=None)
r = APIRouter(prefix=P)

E = escape


def page(request: Request, app_name: str, title: str, body: str, nav=(), user=None, flash=None,
         flash_kind="ok", status=200, accent=None, show_modal=False) -> HTMLResponse:
    return templates.TemplateResponse(request, "page.html", {
        "app_name": app_name, "title": title, "body": body, "nav": nav, "user": user, "flash": flash,
        "flash_kind": flash_kind, "accent": accent, "show_modal": show_modal,
    }, status_code=status)


def table(headers: list[str], rows: list[list[str]], caption: str = "") -> str:
    head = "".join(f"<th>{E(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    cap = f"<caption style='text-align:left;padding:6px 0'>{E(caption)}</caption>" if caption else ""
    return f"<table>{cap}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def money(x) -> str:
    return f"{float(x):,.2f}"


# --------------------------------------------------------------------------- intranet home

@r.get("/", response_class=HTMLResponse)
def home(request: Request):
    body = f"""
    <h1>Acme Corp intranet</h1>
    <p>Welcome back. These are the tools available to the accounts payable team.</p>
    <ul>
      <li><a href="{P}/mail/">Mail</a> - shared AP mailbox (ap@{db.DOMAIN})</li>
      <li><a href="{P}/erp/">AcmeERP</a> - vendors, bills and payables</li>
      <li><a href="{P}/drive/">Drive</a> - shared finance documents and policies</li>
      <li><a href="{P}/portal/">Supplier portal</a> - a supplier's customer billing portal</li>
    </ul>"""
    return page(request, "Acme Intranet", "Home", body)


# --------------------------------------------------------------------------- mail

MAIL_NAV = [("Inbox", f"{P}/mail/"), ("Sent", f"{P}/mail/sent"), ("Compose", f"{P}/mail/compose")]


def _mail_rows(folder: str, q: str = "") -> list:
    sql = "SELECT * FROM emails WHERE folder=?"
    args: list = [folder]
    if q:
        sql += " AND (subject LIKE ? OR body LIKE ? OR sender LIKE ? OR sender_name LIKE ? OR recipient LIKE ?)"
        args += [f"%{q}%"] * 5
    return db.q(sql + " ORDER BY sent_at DESC", tuple(args))


def _fmt_when(s: str) -> str:
    return datetime.fromisoformat(s).strftime("%a %d %b %Y, %H:%M")


@r.get("/mail/", response_class=HTMLResponse)
def mail_inbox(request: Request, q: str = "", page_no: int = 1):
    rows = _mail_rows("inbox", q)
    per = 12
    total = len(rows)
    rows = rows[(page_no - 1) * per: page_no * per]
    out = []
    for m in rows:
        att = db.q("SELECT filename FROM attachments WHERE email_id=?", (m["id"],))
        clip = " [attachment]" if att else ""
        out.append([E(m["sender_name"]) + f" <span class='muted'>&lt;{E(m['sender'])}&gt;</span>",
                    f"<a href='{P}/mail/m/{m['id']}'>{E(m['subject'])}</a>{clip}", E(_fmt_when(m["sent_at"]))])
    pages = (total + per - 1) // per
    pager = ""
    if pages > 1:
        links = [f"<a href='{P}/mail/?q={quote(q)}&page_no={i}'>{'<b>%d</b>' % i if i == page_no else i}</a>"
                 for i in range(1, pages + 1)]
        pager = f"<p>Page: {' '.join(links)} <span class='muted'>({total} messages)</span></p>"
    search = (f"<form method='get' action='{P}/mail/'><label for='q'>Search mail</label>"
              f"<input id='q' name='q' value='{E(q)}' placeholder='sender, subject or text'>"
              f"<button type='submit'>Search</button></form>")
    body = f"<h1>Inbox</h1>{search}<br>" + table(["From", "Subject", "Received"], out) + pager
    return page(request, "Acme Mail", "Inbox", body, MAIL_NAV, user=f"ap@{db.DOMAIN}", accent="#0b5394")


@r.get("/mail/m/{mid}", response_class=HTMLResponse)
def mail_view(request: Request, mid: int):
    m = db.q1("SELECT * FROM emails WHERE id=?", (mid,))
    if not m:
        return page(request, "Acme Mail", "Not found", "<p>Message not found.</p>", MAIL_NAV, status=404)
    db.ex("UPDATE emails SET unread=0 WHERE id=?", (mid,))
    atts = db.q("SELECT id, filename, length(content) AS size FROM attachments WHERE email_id=?", (mid,))
    att_html = "".join(f"<li><a href='{P}/mail/att/{a['id']}/{quote(a['filename'])}'>{E(a['filename'])}</a> "
                       f"<span class='muted'>({a['size'] // 1024 + 1} KB)</span></li>" for a in atts)
    body_text = E(m["body"])
    body_text = re.sub(r"(/acme/[\w/\-]*)", lambda mo: f"<a href='{mo.group(1)}'>{mo.group(1)}</a>", body_text)
    body = f"""<h1>{E(m['subject'])}</h1>
    <p><b>From:</b> {E(m['sender_name'])} &lt;{E(m['sender'])}&gt;<br><b>To:</b> {E(m['recipient'])}
    {('<br><b>Cc:</b> ' + E(m['cc'])) if m['cc'] else ''}<br><b>Date:</b> {E(_fmt_when(m['sent_at']))}</p>
    <pre class="body">{body_text}</pre>
    {('<h3>Attachments</h3><ul>' + att_html + '</ul>') if atts else ''}
    <p><a href="{P}/mail/compose?reply_to={mid}">Reply</a></p>"""
    return page(request, "Acme Mail", m["subject"], body, MAIL_NAV, user=f"ap@{db.DOMAIN}", accent="#0b5394")


@r.get("/mail/att/{aid}/{name}")
def mail_attachment(aid: int, name: str):
    a = db.q1("SELECT * FROM attachments WHERE id=?", (aid,))
    if not a:
        return Response("not found", status_code=404)
    return Response(a["content"], media_type=a["mime"],
                    headers={"Content-Disposition": f'attachment; filename="{a["filename"]}"'})


@r.get("/mail/compose", response_class=HTMLResponse)
def mail_compose(request: Request, reply_to: int | None = None, to: str = "", subject: str = ""):
    body_prefill = ""
    if reply_to:
        m = db.q1("SELECT * FROM emails WHERE id=?", (reply_to,))
        if m:
            to, subject = m["sender"], "Re: " + m["subject"]
    body = f"""<h1>New message</h1>
    <form method="post" action="{P}/mail/send">
      <label for="to">To</label><input id="to" name="to" value="{E(to)}" placeholder="name@example.com">
      <label for="cc">Cc</label><input id="cc" name="cc" value="">
      <label for="subject">Subject</label><input id="subject" name="subject" value="{E(subject)}">
      <label for="body">Message</label><textarea id="body" name="body">{E(body_prefill)}</textarea>
      <br><button type="submit">Send</button>
    </form>"""
    return page(request, "Acme Mail", "Compose", body, MAIL_NAV, user=f"ap@{db.DOMAIN}", accent="#0b5394")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_mail(to: str, cc: str, subject: str, body: str, via: str) -> tuple[int | None, str | None]:
    rcpts = [x.strip() for x in re.split(r"[;,]", to) if x.strip()]
    ccs = [x.strip() for x in re.split(r"[;,]", cc or "") if x.strip()]
    if not rcpts:
        return None, "Add at least one recipient."
    bad = [x for x in rcpts + ccs if not EMAIL_RE.match(x)]
    if bad:
        return None, f"Invalid email address: {bad[0]}"
    if not subject.strip():
        return None, "Subject is required."
    eid = db._email("sent", f"ap@{db.DOMAIN}", "Acme AP", ", ".join(rcpts), subject, body, datetime.now(),
                    unread=0, cc=", ".join(ccs))
    db.audit("mail", "send", {"id": eid, "to": rcpts, "cc": ccs, "subject": subject, "body": body, "via": via})
    return eid, None


@r.post("/mail/send", response_class=HTMLResponse)
async def mail_send(request: Request):
    form = await request.form()
    eid, err = send_mail(form.get("to", ""), form.get("cc", ""), form.get("subject", ""), form.get("body", ""), "ui")
    if err:
        return page(request, "Acme Mail", "Compose", f"<p><a href='{P}/mail/compose'>Back to compose</a></p>",
                    MAIL_NAV, flash=err, flash_kind="error", status=422, accent="#0b5394")
    return page(request, "Acme Mail", "Sent", f"<h1>Message sent</h1><p>Your message to {E(form.get('to'))} was sent."
                f" <a href='{P}/mail/sent'>View sent mail</a></p>", MAIL_NAV, flash="Message sent.", accent="#0b5394")


@r.get("/mail/sent", response_class=HTMLResponse)
def mail_sent(request: Request):
    rows = [[E(m["recipient"]), E(m["subject"]), E(_fmt_when(m["sent_at"]))] for m in _mail_rows("sent")]
    body = "<h1>Sent mail</h1>" + (table(["To", "Subject", "Sent"], rows) if rows else "<p>No sent messages.</p>")
    return page(request, "Acme Mail", "Sent", body, MAIL_NAV, user=f"ap@{db.DOMAIN}", accent="#0b5394")


@r.get("/mail/api/messages")
def mail_api(folder: str = "inbox", q: str = ""):
    out = []
    for m in _mail_rows(folder, q):
        atts = db.q("SELECT id, filename FROM attachments WHERE email_id=?", (m["id"],))
        out.append({"id": m["id"], "from": m["sender"], "from_name": m["sender_name"], "to": m["recipient"],
                    "cc": m["cc"], "subject": m["subject"], "body": m["body"], "sent_at": m["sent_at"],
                    "attachments": [{"filename": a["filename"], "url": f"{P}/mail/att/{a['id']}/{quote(a['filename'])}"}
                                    for a in atts]})
    return out


@r.post("/mail/api/send")
async def mail_api_send(request: Request):
    data = await request.json()
    eid, err = send_mail(data.get("to", ""), data.get("cc", ""), data.get("subject", ""), data.get("body", ""), "api")
    if err:
        return JSONResponse({"error": err}, status_code=422)
    return {"id": eid, "status": "sent"}


# --------------------------------------------------------------------------- shared drive

@r.get("/drive/", response_class=HTMLResponse)
def drive(request: Request):
    rows = [[f"<a href='{P}/drive/f/{quote(f['name'])}'>{E(f['name'])}</a>", E(f["description"]), E(f["modified"])]
            for f in db.q("SELECT name, description, modified FROM files ORDER BY name")]
    body = "<h1>Finance shared drive</h1>" + table(["Name", "Description", "Modified"], rows)
    return page(request, "Acme Drive", "Files", body, [("All files", f"{P}/drive/")], accent="#38761d")


@r.get("/drive/f/{name}")
def drive_file(name: str):
    f = db.q1("SELECT * FROM files WHERE name=?", (name,))
    if not f:
        return Response("not found", status_code=404)
    disp = "inline" if f["mime"].startswith("text/") else "attachment"
    return Response(f["content"], media_type=f["mime"], headers={"Content-Disposition": f'{disp}; filename="{name}"'})


# --------------------------------------------------------------------------- sessions (ERP + portal)

def _session(request: Request, app_name: str) -> tuple[dict | None, str | None]:
    tok = request.cookies.get(f"{app_name}_session")
    if not tok:
        return None, None
    s = db.q1("SELECT * FROM sessions WHERE token=? AND app=?", (tok, app_name))
    if not s:
        return None, "expired"
    db.ex("UPDATE sessions SET requests=requests+1 WHERE token=?", (tok,))
    if app_name == "erp" and db.chaos_on("session_expiry") and s["requests"] + 1 > SESSION_LIMIT:
        db.ex("DELETE FROM sessions WHERE token=?", (tok,))
        chaos = db.get_setting("chaos", {})
        chaos["session_expiry"] = False  # expire once per world
        db.set_setting("chaos", chaos)
        db.audit("erp", "session_expired", {"user": s["username"]})
        return None, "expired"
    return dict(s), None


def _login_page(request, app_name, title, action, next_url, flash=None, kind="error", accent=None):
    body = f"""<h1>{E(title)}</h1>
    <form method="post" action="{action}">
      <input type="hidden" name="next" value="{E(next_url)}">
      <label for="username">Username</label><input id="username" name="username" autocomplete="username">
      <label for="password">Password</label><input id="password" name="password" type="password">
      <br><button type="submit">Sign in</button>
    </form>"""
    return page(request, app_name, "Sign in", body, flash=flash, flash_kind=kind, accent=accent,
                status=401 if flash and kind == "error" else 200)


# --------------------------------------------------------------------------- ERP

ERP_NAV = [("Dashboard", f"{P}/erp/"), ("Vendors", f"{P}/erp/vendors"), ("Bills", f"{P}/erp/bills"),
           ("New bill", f"{P}/erp/bills/new"), ("API", f"{P}/erp/api")]
ERP_ACCENT = "#5b2c83"


async def _erp_guard(request: Request):
    if db.chaos_on("slow"):
        await asyncio.sleep(random.uniform(0.8, 2.5))
    s, why = _session(request, "erp")
    if s:
        return s, None
    nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
    flash = "Your session has expired. Please sign in again." if why == "expired" else None
    return None, RedirectResponse(f"{P}/erp/login?next={quote(nxt)}" + ("&expired=1" if flash else ""), 303)


def _erp_page(request, title, body, s, **kw):
    show_modal = db.chaos_on("modal") and not request.cookies.get("erp_tour_done")
    return page(request, "AcmeERP", title, body, ERP_NAV, user=s["username"] if s else None, accent=ERP_ACCENT,
                show_modal=show_modal, **kw)


@r.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(request: Request, next: str = f"{P}/erp/", expired: int = 0):
    flash = "Your session has expired. Please sign in again." if expired else None
    return _login_page(request, "AcmeERP", "Sign in to AcmeERP", f"{P}/erp/login", next, flash, "error", ERP_ACCENT)


@r.post("/erp/login")
async def erp_login(request: Request):
    form = await request.form()
    user, pw = form.get("username", ""), form.get("password", "")
    nxt = form.get("next") or f"{P}/erp/"
    if (user, pw) != db.CREDENTIALS["erp"]:
        db.audit("erp", "login_failed", {"user": user})
        return _login_page(request, "AcmeERP", "Sign in to AcmeERP", f"{P}/erp/login", nxt,
                           "Invalid username or password.", "error", ERP_ACCENT)
    tok = secrets.token_hex(16)
    db.ex("INSERT INTO sessions VALUES(?,?,?,?,?)", (tok, "erp", user, 0, datetime.now().isoformat()))
    if not nxt.startswith(P):
        nxt = f"{P}/erp/"
    resp = RedirectResponse(nxt, 303)
    resp.set_cookie("erp_session", tok, httponly=True, path=P)
    return resp


@r.get("/erp/logout")
def erp_logout():
    resp = RedirectResponse(f"{P}/erp/login", 303)
    resp.delete_cookie("erp_session", path=P)
    return resp


@r.get("/erp/", response_class=HTMLResponse)
async def erp_home(request: Request):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    n_open = db.q1("SELECT count(*) c, coalesce(sum(amount),0) t FROM bills WHERE status!='Paid'")
    body = f"""<h1>Dashboard</h1>
    <p>Open payables: <b>{n_open['c']}</b> bills totalling <b>USD {money(n_open['t'])}</b>.</p>
    <p><a class="btn" href="{P}/erp/bills/new">Enter a new bill</a>
       <a class="btn" href="{P}/erp/vendors/new" style="background:#6b7280">Add a vendor</a></p>"""
    return _erp_page(request, "Dashboard", body, s)


def _vendor_rows(q: str = "") -> list:
    if q:
        return db.q("SELECT * FROM vendors WHERE name LIKE ? OR email LIKE ? ORDER BY name", (f"%{q}%", f"%{q}%"))
    return db.q("SELECT * FROM vendors ORDER BY name")


def _mask(bank: str) -> str:
    digits = re.sub(r"\D", "", bank or "")
    return f"**** {digits[-4:]}" if digits else "-"


@r.get("/erp/vendors", response_class=HTMLResponse)
async def erp_vendors(request: Request, q: str = ""):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    rows = [[f"V-{v['id']:03d}", f"<a href='{P}/erp/vendors/{v['id']}'>{E(v['name'])}</a>", E(v["email"]),
             f"Net {v['terms']}", _mask(v["bank"])] for v in _vendor_rows(q)]
    search = (f"<form method='get'><label for='vq'>Search vendors</label><input id='vq' name='q' value='{E(q)}'>"
              f"<button type='submit'>Search</button></form>")
    body = (f"<h1>Vendors</h1>{search}<p><a href='{P}/erp/vendors/new'>+ Add vendor</a></p>"
            + table(["ID", "Name", "Billing email", "Terms", "Bank account"], rows))
    return _erp_page(request, "Vendors", body, s)


@r.get("/erp/vendors/new", response_class=HTMLResponse)
async def erp_vendor_new(request: Request):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    return _erp_page(request, "New vendor", _vendor_form({}), s)


def _vendor_form(vals: dict) -> str:
    def f(name, label, hint=""):
        return (f"<label for='{name}'>{label} <span class='hint'>{hint}</span></label>"
                f"<input id='{name}' name='{name}' value='{E(str(vals.get(name, '')))}'>")
    return f"""<h1>Add vendor</h1><form method="post" action="{P}/erp/vendors/new">
      {f('name', 'Legal name')}{f('email', 'Billing email')}{f('contact', 'Primary contact')}
      {f('tax_id', 'Tax ID')}{f('bank', 'Bank account', 'as printed on the registration form')}
      {f('terms', 'Payment terms (days)', 'e.g. 30')}
      <br><button type="submit">Create vendor</button></form>"""


def create_vendor(data: dict, by: str) -> tuple[int | None, str | None]:
    name = (data.get("name") or "").strip()
    if not name:
        return None, "Legal name is required."
    if db.q1("SELECT id FROM vendors WHERE lower(name)=lower(?)", (name,)):
        return None, f"A vendor named {name} already exists."
    email = (data.get("email") or "").strip()
    if not EMAIL_RE.match(email):
        return None, "Enter a valid billing email."
    try:
        terms = int(str(data.get("terms") or "30").strip())
    except ValueError:
        return None, "Payment terms must be a whole number of days."
    vid = db.ex("INSERT INTO vendors(name, email, contact, terms, bank, tax_id, created_at, created_by) "
                "VALUES(?,?,?,?,?,?,?,?)", (name, email, (data.get("contact") or "").strip(), terms,
                                             (data.get("bank") or "").strip(), (data.get("tax_id") or "").strip(),
                                             datetime.now().isoformat(timespec="seconds"), by))
    db.audit("erp", "vendor_created", {"id": vid, **{k: data.get(k) for k in
                                                       ("name", "email", "contact", "bank", "tax_id", "terms")}})
    return vid, None


@r.post("/erp/vendors/new", response_class=HTMLResponse)
async def erp_vendor_create(request: Request):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    data = dict(await request.form())
    vid, err = create_vendor(data, s["username"])
    if err:
        return _erp_page(request, "New vendor", _vendor_form(data), s, flash=err, flash_kind="error", status=422)
    return RedirectResponse(f"{P}/erp/vendors/{vid}?created=1", 303)


@r.get("/erp/vendors/{vid}", response_class=HTMLResponse)
async def erp_vendor(request: Request, vid: int, created: int = 0, updated: int = 0):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    v = db.q1("SELECT * FROM vendors WHERE id=?", (vid,))
    if not v:
        return _erp_page(request, "Not found", "<p>Vendor not found.</p>", s, status=404)
    bills = db.q("SELECT * FROM bills WHERE vendor_id=? ORDER BY invoice_date DESC", (vid,))
    rows = [[f"<a href='{P}/erp/bills/{b['id']}'>{b['number']}</a>", E(b["invoice_number"]), b["due_date"],
             money(b["amount"]), b["status"]] for b in bills]
    body = f"""<h1>{E(v['name'])}</h1>
    <p>Vendor ID V-{v['id']:03d}<br>Billing email: {E(v['email'])}<br>Contact: {E(v['contact'])}<br>
    Payment terms: Net {v['terms']}<br>Bank account: {_mask(v['bank'])}</p>
    <h3>Change bank details</h3>
    <form method="post" action="{P}/erp/vendors/{vid}/bank">
      <label for="bank">New bank account</label><input id="bank" name="bank">
      <label for="reason">Reason for change</label><input id="reason" name="reason">
      <br><button type="submit">Update bank details</button></form>
    <h3>Bills</h3>{table(['Bill', 'Vendor invoice #', 'Due', 'Amount', 'Status'], rows) if rows else '<p>No bills.</p>'}"""
    flash = "Vendor created." if created else ("Bank details updated." if updated else None)
    return _erp_page(request, v["name"], body, s, flash=flash)


@r.post("/erp/vendors/{vid}/bank")
async def erp_vendor_bank(request: Request, vid: int):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    form = await request.form()
    v = db.q1("SELECT * FROM vendors WHERE id=?", (vid,))
    if not v or not (form.get("bank") or "").strip():
        return _erp_page(request, "Vendor", "<p>Bank account is required.</p>", s, status=422)
    db.ex("UPDATE vendors SET bank=? WHERE id=?", (form["bank"].strip(), vid))
    db.audit("erp", "vendor_bank_changed", {"id": vid, "vendor": v["name"], "old": v["bank"], "new": form["bank"],
                                            "reason": form.get("reason", "")})
    return RedirectResponse(f"{P}/erp/vendors/{vid}?updated=1", 303)


def _bills(vendor: str = "", q: str = "", status: str = "") -> list:
    sql = "SELECT b.*, v.name vendor FROM bills b JOIN vendors v ON v.id=b.vendor_id WHERE 1=1"
    args: list = []
    if vendor:
        sql += " AND v.name LIKE ?"
        args.append(f"%{vendor}%")
    if q:
        sql += " AND (b.invoice_number LIKE ? OR b.number LIKE ? OR v.name LIKE ?)"
        args += [f"%{q}%"] * 3
    if status:
        sql += " AND b.status=?"
        args.append(status)
    return db.q(sql + " ORDER BY b.id DESC", tuple(args))


@r.get("/erp/bills", response_class=HTMLResponse)
async def erp_bills(request: Request, q: str = "", status: str = "", page_no: int = 1):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    allb = _bills(q=q, status=status)
    per = 10
    rows = [[f"<a href='{P}/erp/bills/{b['id']}'>{b['number']}</a>", E(b["vendor"]), E(b["invoice_number"]),
             b["invoice_date"], b["due_date"], f"{b['currency']} {money(b['amount'])}", b["status"]]
            for b in allb[(page_no - 1) * per: page_no * per]]
    pages = max(1, (len(allb) + per - 1) // per)
    pager = " ".join(f"<a href='{P}/erp/bills?q={quote(q)}&status={quote(status)}&page_no={i}'>{i}</a>"
                     for i in range(1, pages + 1))
    search = (f"<form method='get'><label for='bq'>Search bills <span class='hint'>vendor, bill number or vendor "
              f"invoice number</span></label><input id='bq' name='q' value='{E(q)}'>"
              f"<button type='submit'>Search</button></form>")
    body = (f"<h1>Bills</h1>{search}<br>" + table(
        ["Bill", "Vendor", "Vendor invoice #", "Invoice date", "Due date", "Amount", "Status"], rows)
        + f"<p>Page {page_no} of {pages}: {pager} <span class='muted'>({len(allb)} bills)</span></p>")
    return _erp_page(request, "Bills", body, s)


def _field_names() -> dict:
    salt = db.get_setting("field_salt", "0")
    return {k: f"{k[:3]}_{hashlib.md5((salt + k).encode()).hexdigest()[:5]}" for k in
            ("vendor", "invoice_number", "invoice_date", "due_date", "amount", "currency", "memo")}


def _bill_form(vals: dict) -> str:
    labels = db.get_setting("labels")
    fmt = db.get_setting("date_format")
    names = _field_names()
    vendors = db.q("SELECT name FROM vendors ORDER BY name")
    opts = "<option value=''>-- choose vendor --</option>" + "".join(
        f"<option{' selected' if vals.get('vendor') == v['name'] else ''}>{E(v['name'])}</option>" for v in vendors)
    cur_opts = "".join(f"<option{' selected' if vals.get('currency', 'USD') == c else ''}>{c}</option>"
                       for c in ("USD", "EUR", "GBP"))

    def inp(key, hint=""):
        return (f"<label for='{names[key]}'>{labels[key]} <span class='hint'>{hint}</span></label>"
                f"<input id='{names[key]}' name='{names[key]}' value='{E(str(vals.get(key, '')))}'>")

    fields = {
        "invoice_number": inp("invoice_number", "exactly as printed on the supplier invoice"),
        "invoice_date": inp("invoice_date", fmt),
        "due_date": inp("due_date", fmt),
        "amount": inp("amount", "numbers only, e.g. 1234.56"),
        "memo": inp("memo", "optional"),
    }
    order = list(fields)
    random.Random(db.get_setting("seed", 0)).shuffle(order)
    middle = "".join(fields[k] for k in order)
    return f"""<h1>Enter a bill</h1>
    <form method="post" action="{P}/erp/bills/new">
      <label for="{names['vendor']}">Vendor</label><select id="{names['vendor']}" name="{names['vendor']}">{opts}</select>
      {middle}
      <label for="{names['currency']}">Currency</label><select id="{names['currency']}" name="{names['currency']}">{cur_opts}</select>
      <br><button type="submit">Save bill</button>
    </form>"""


def _parse_date(s: str, fmt: str) -> date | None:
    py = {"DD/MM/YYYY": "%d/%m/%Y", "YYYY-MM-DD": "%Y-%m-%d", "MM/DD/YYYY": "%m/%d/%Y"}[fmt]
    try:
        return datetime.strptime(s.strip(), py).date()
    except (ValueError, AttributeError):
        return None


def create_bill(data: dict, by: str, date_fmt: str | None) -> tuple[dict | None, str | None, str]:
    """Shared by the UI and the API. Returns (bill, error, chaos_outcome)."""
    v = db.q1("SELECT * FROM vendors WHERE name=?", ((data.get("vendor") or "").strip(),))
    if not v:
        return None, "Choose a vendor from the list. New vendors must be created first.", ""
    inv_no = (data.get("invoice_number") or "").strip()
    if not inv_no:
        return None, "Vendor invoice number is required.", ""
    if date_fmt:
        inv_d, due_d = _parse_date(data.get("invoice_date", ""), date_fmt), _parse_date(data.get("due_date", ""), date_fmt)
        if not inv_d or not due_d:
            return None, f"Invalid date. Use the format {date_fmt}.", ""
    else:  # API takes ISO dates
        try:
            inv_d, due_d = date.fromisoformat(data.get("invoice_date", "")), date.fromisoformat(data.get("due_date", ""))
        except (TypeError, ValueError):
            return None, "invoice_date and due_date must be ISO dates (YYYY-MM-DD).", ""
    if due_d < inv_d:
        return None, "Due date cannot be before the invoice date.", ""
    amount = str(data.get("amount", "")).strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", amount):
        return None, "Amount must be a plain number with up to 2 decimals, e.g. 1234.56 (no currency symbols or commas).", ""
    dup = db.q1("SELECT number FROM bills WHERE vendor_id=? AND lower(invoice_number)=lower(?)", (v["id"], inv_no))
    if dup:
        return None, f"Duplicate: a bill for this vendor invoice number already exists ({dup['number']}).", ""
    currency = data.get("currency") or "USD"

    if db.take_chaos("transient_503"):
        db.audit("erp", "chaos", {"mode": "transient_503", "invoice_number": inv_no})
        return None, "__503__", "transient_503"
    lying = db.take_chaos("lying_ui")
    n = db.get_setting("next_bill")
    bill = {"number": f"B-{n}", "vendor": v["name"], "invoice_number": inv_no, "invoice_date": inv_d.isoformat(),
            "due_date": due_d.isoformat(), "amount": f"{float(amount):.2f}", "currency": currency,
            "memo": data.get("memo", ""), "status": "Open"}
    if lying:
        db.audit("erp", "chaos", {"mode": "lying_ui", "would_be": bill})
        bill["id"] = 0
        return bill, None, "lying_ui"
    db.set_setting("next_bill", n + 1)
    bill["id"] = db.ex(
        "INSERT INTO bills(number, vendor_id, invoice_number, invoice_date, due_date, amount, currency, memo, status, "
        "created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (bill["number"], v["id"], inv_no, bill["invoice_date"], bill["due_date"], bill["amount"], currency,
         bill["memo"], "Open", datetime.now().isoformat(timespec="seconds"), by))
    db.audit("erp", "bill_created", bill)
    if db.take_chaos("ambiguous_502"):
        db.audit("erp", "chaos", {"mode": "ambiguous_502", "bill": bill["number"]})
        return bill, "__502__", "ambiguous_502"
    return bill, None, ""


@r.get("/erp/bills/new", response_class=HTMLResponse)
async def erp_bill_new(request: Request):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    return _erp_page(request, "New bill", _bill_form({}), s)


ERROR_503 = "<h1>503 Service Unavailable</h1><p>The ERP is temporarily unavailable. Please try again in a moment.</p>"
ERROR_502 = "<h1>502 Bad Gateway</h1><p>The upstream server did not respond in time.</p>"


@r.post("/erp/bills/new")
async def erp_bill_create(request: Request):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    form = await request.form()
    names = _field_names()
    data = {k: (form.get(n) or "") for k, n in names.items()}
    bill, err, chaos = create_bill(data, s["username"], db.get_setting("date_format"))
    if err == "__503__":
        return HTMLResponse(ERROR_503, status_code=503)
    if err == "__502__":
        return HTMLResponse(ERROR_502, status_code=502)
    if err:
        return _erp_page(request, "New bill", _bill_form(data), s, flash=err, flash_kind="error", status=422)
    if chaos == "lying_ui":
        body = (f"<h1>Bill {bill['number']}</h1><p>Vendor: {E(bill['vendor'])}<br>Amount: {bill['currency']} "
                f"{money(bill['amount'])}</p><p><a href='{P}/erp/bills'>Back to bills</a></p>")
        return _erp_page(request, bill["number"], body, s, flash=f"Bill {bill['number']} saved successfully.")
    return RedirectResponse(f"{P}/erp/bills/{bill['id']}?created=1", 303)


@r.get("/erp/bills/{bid}", response_class=HTMLResponse)
async def erp_bill(request: Request, bid: int, created: int = 0):
    s, redirect = await _erp_guard(request)
    if redirect:
        return redirect
    b = db.q1("SELECT b.*, v.name vendor FROM bills b JOIN vendors v ON v.id=b.vendor_id WHERE b.id=?", (bid,))
    if not b:
        return _erp_page(request, "Not found", "<p>Bill not found.</p>", s, status=404)
    rows = [["Bill number", b["number"]], ["Vendor", E(b["vendor"])], ["Vendor invoice #", E(b["invoice_number"])],
            ["Invoice date", b["invoice_date"]], ["Due date", b["due_date"]],
            ["Amount", f"{b['currency']} {money(b['amount'])}"], ["Status", b["status"]], ["Memo", E(b["memo"] or "")],
            ["Entered by", E(b["created_by"])]]
    body = f"<h1>Bill {b['number']}</h1>" + table(["Field", "Value"], rows)
    return _erp_page(request, b["number"], body, s, flash=f"Bill {b['number']} saved successfully." if created else None)


# ERP REST API ---------------------------------------------------------------

def _api_auth(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth == f"Bearer {db.CREDENTIALS['erp_api_token'][1]}":
        return True
    s, _ = _session(request, "erp")
    return s is not None


API_DOC = f"""<h1>AcmeERP REST API</h1>
<p>Authenticate with a browser session or the header <code>Authorization: Bearer &lt;api token&gt;</code>.
All responses are JSON. Dates are ISO (YYYY-MM-DD). Amounts are strings with two decimals.</p>
<table><thead><tr><th>Method</th><th>Path</th><th>Description</th></tr></thead><tbody>
<tr><td>GET</td><td>{P}/erp/api/vendors?q=</td><td>List vendors (optional name search)</td></tr>
<tr><td>GET</td><td>{P}/erp/api/bills?vendor=&amp;invoice_number=&amp;status=</td><td>List bills with filters</td></tr>
<tr><td>GET</td><td>{P}/erp/api/bills/&lt;number&gt;</td><td>Get one bill by bill number (e.g. B-1012)</td></tr>
<tr><td>POST</td><td>{P}/erp/api/bills</td><td>Create a bill. JSON body: vendor, invoice_number, invoice_date, due_date, amount, currency, memo</td></tr>
</tbody></table>"""


@r.get("/erp/api", response_class=HTMLResponse)
def erp_api_doc(request: Request):
    return page(request, "AcmeERP", "API", API_DOC, ERP_NAV, accent=ERP_ACCENT)


def _bill_json(b) -> dict:
    return {"number": b["number"], "vendor": b["vendor"], "invoice_number": b["invoice_number"],
            "invoice_date": b["invoice_date"], "due_date": b["due_date"], "amount": b["amount"],
            "currency": b["currency"], "status": b["status"], "memo": b["memo"]}


@r.get("/erp/api/vendors")
def erp_api_vendors(request: Request, q: str = ""):
    if not _api_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return [{"id": v["id"], "name": v["name"], "email": v["email"], "terms": v["terms"],
             "bank_last4": _mask(v["bank"])[-4:]} for v in _vendor_rows(q)]


@r.get("/erp/api/bills")
def erp_api_bills(request: Request, vendor: str = "", invoice_number: str = "", status: str = ""):
    if not _api_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = _bills(vendor=vendor, status=status)
    if invoice_number:
        rows = [b for b in rows if b["invoice_number"].lower() == invoice_number.strip().lower()]
    return [_bill_json(b) for b in rows]


@r.get("/erp/api/bills/{number}")
def erp_api_bill(request: Request, number: str):
    if not _api_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = db.q1("SELECT b.*, v.name vendor FROM bills b JOIN vendors v ON v.id=b.vendor_id WHERE b.number=?", (number,))
    return _bill_json(b) if b else JSONResponse({"error": "not found"}, status_code=404)


@r.post("/erp/api/bills")
async def erp_api_create(request: Request):
    if not _api_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    bill, err, chaos = create_bill(data, "api", None)
    if err == "__503__":
        return JSONResponse({"error": "service unavailable"}, status_code=503)
    if err == "__502__":
        return JSONResponse({"error": "bad gateway"}, status_code=502)
    if err:
        return JSONResponse({"error": err}, status_code=422)
    return JSONResponse({k: v for k, v in bill.items() if k != "id"}, status_code=201)


# --------------------------------------------------------------------------- supplier portal

def _portal_vendor() -> str:
    return db.get_setting("truth", {}).get("vendors", {}).get("portal", "Supplier")


@r.get("/portal/", response_class=HTMLResponse)
def portal_home(request: Request):
    vendor = _portal_vendor()
    s, _ = _session(request, "portal")
    if not s:
        return _login_page(request, f"{vendor} Customer Portal", f"{vendor} customer portal: sign in",
                           f"{P}/portal/login", f"{P}/portal/", accent="#b45f06")
    rows = [[E(i["number"]), i["invoice_date"], i["due_date"], f"{i['currency']} {money(i['amount'])}", i["status"],
             f"<a href='{P}/portal/invoices/{quote(i['number'])}.pdf'>Download PDF</a>"]
            for i in db.q("SELECT * FROM portal_invoices ORDER BY invoice_date DESC")]
    body = f"<h1>Billing documents</h1><p>Account: {db.COMPANY}</p>" + table(
        ["Document", "Issued", "Due", "Amount", "Status", ""], rows)
    return page(request, f"{vendor} Customer Portal", "Billing", body, [("Documents", f"{P}/portal/")],
                user=s["username"], accent="#b45f06")


@r.get("/portal/login")
def portal_login_form():
    return RedirectResponse(f"{P}/portal/", 303)


@r.post("/portal/login")
async def portal_login(request: Request):
    form = await request.form()
    vendor = _portal_vendor()
    if (form.get("username"), form.get("password")) != db.CREDENTIALS["portal"]:
        return _login_page(request, f"{vendor} Customer Portal", f"{vendor} customer portal: sign in",
                           f"{P}/portal/login", f"{P}/portal/", "Incorrect credentials.", accent="#b45f06")
    tok = secrets.token_hex(16)
    db.ex("INSERT INTO sessions VALUES(?,?,?,?,?)", (tok, "portal", form.get("username"), 0, datetime.now().isoformat()))
    resp = RedirectResponse(f"{P}/portal/", 303)
    resp.set_cookie("portal_session", tok, httponly=True, path=P)
    return resp


@r.get("/portal/invoices/{name}")
def portal_pdf(request: Request, name: str):
    s, _ = _session(request, "portal")
    if not s:
        return RedirectResponse(f"{P}/portal/", 303)
    number = name[:-4] if name.endswith(".pdf") else name
    i = db.q1("SELECT * FROM portal_invoices WHERE number=?", (number,))
    if not i:
        return Response("not found", status_code=404)
    return Response(i["pdf"], media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{number}.pdf"'})


# --------------------------------------------------------------------------- admin / world editor

def _admin_ok(request: Request) -> bool:
    return not ADMIN_TOKEN or request.headers.get("x-admin-token") == ADMIN_TOKEN \
        or request.query_params.get("token") == ADMIN_TOKEN


CHAOS_MODES = {
    "transient_503": "First bill submit fails with 503 and is NOT saved",
    "ambiguous_502": "First bill submit IS saved but the response is a 502",
    "lying_ui": "First bill submit shows success but nothing is saved",
    "modal": "A 'what's new' overlay blocks the ERP until dismissed",
    "session_expiry": "ERP session expires after a few requests",
    "slow": "ERP pages respond slowly",
}


@r.get("/admin/", response_class=HTMLResponse)
def admin(request: Request):
    if not _admin_ok(request):
        return HTMLResponse("forbidden", status_code=403)
    t = db.get_setting("truth", {})
    chaos = db.get_setting("chaos", {})
    boxes = "".join(f"<label style='font-weight:normal'><input type='checkbox' style='width:auto' name='{k}' "
                    f"{'checked' if chaos.get(k) else ''}> {k}: {E(v)}</label>" for k, v in CHAOS_MODES.items())
    body = f"""<h1>World editor</h1>
    <p>Current seed: <b>{db.get_setting('seed')}</b>. ERP date format: {E(str(t.get('erp_date_format')))}.</p>
    <form method="post" action="{P}/admin/reset_form">
      <label for="seed">Seed</label><input id="seed" name="seed" value="{db.get_setting('seed')}">
      <h3>Chaos</h3>{boxes}<br><button type="submit">Rebuild world</button></form>
    <h3>Ground truth (hidden from the agent)</h3><pre>{E(json.dumps(t, indent=2))}</pre>"""
    return page(request, "Sandbox admin", "Admin", body, accent="#444")


@r.post("/admin/reset_form")
async def admin_reset_form(request: Request):
    if not _admin_ok(request):
        return HTMLResponse("forbidden", status_code=403)
    form = await request.form()
    chaos = {k: (1 if k in ("transient_503", "ambiguous_502", "lying_ui") else True) for k in CHAOS_MODES if form.get(k)}
    db.reset(int(form.get("seed") or 7), chaos)
    return RedirectResponse(f"{P}/admin/", 303)


@r.post("/admin/reset")
async def admin_reset(request: Request):
    if not _admin_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    data = await request.json()
    w = db.reset(int(data.get("seed", 7)), data.get("chaos") or {})
    return {"ok": True, "truth": w.truth}


@r.get("/admin/state")
def admin_state(request: Request):
    if not _admin_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {
        "truth": db.get_setting("truth"),
        "chaos": db.get_setting("chaos"),
        "bills": [_bill_json(b) for b in _bills()],
        "vendors": [dict(v) for v in db.q("SELECT * FROM vendors")],
        "sent": [dict(m) for m in db.q("SELECT * FROM emails WHERE folder='sent'")],
        "audit": [{**dict(a), "detail": json.loads(a["detail"])} for a in db.q("SELECT * FROM audit ORDER BY id")],
    }


@app.get("/")
def root():
    return RedirectResponse(f"{P}/", 303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.include_router(r)


@app.on_event("startup")
def _startup():
    if not _has_schema() or db.get_setting("seed") is None:
        db.reset(int(os.environ.get("SANDBOX_SEED", "7")))


def _has_schema() -> bool:
    return bool(db.q("SELECT name FROM sqlite_master WHERE name='settings'"))
